"""
AUT-013 — inspection du header GGUF : lecture bornée, agrégée, non autoritaire.

Ce que ce module fait
---------------------
Il lit l'en-tête d'un fichier GGUF — magie, version, compteurs, métadonnées
clé/valeur, table des tenseurs — et en tire les champs utiles à
une estimation mémoire : architecture,
nombre de blocs, dimension d'embedding, têtes KV (GQA/MQA), contexte maximal
déclaré, quantisation des tenseurs, métadonnées MoE.

Il en tire ensuite une **estimation** conservatrice de l'empreinte VRAM/RAM.

Ce que ce module n'est pas
--------------------------
Ce n'est pas une mesure. §9 fixe la hiérarchie et ce module n'occupe que le
premier étage :

    header GGUF + paramètres  →  ESTIMATION conservatrice
                              →  chargement réel
                              →  mesure des pics
                              →  valeur de capacité approuvée

Aucun champ produit ici ne doit être recopié tel quel dans `vram_gb` du registre
opérationnel. `FACTEURS_IGNORES` énumère ce que l'estimation ne peut pas voir ;
cette liste est publiée dans le rendu — pas seulement dans ce commentaire —
précisément pour qu'un opérateur ne puisse pas la manquer.

Pourquoi un parseur interne plutôt que le paquet `gguf`
-------------------------------------------------------
§9 demande d'évaluer le paquet officiel `gguf` (gguf-py de `llama.cpp`, MIT)
avant d'écrire un parseur. Évaluation faite, conclusion : **non retenu pour
cette vague**, pour trois raisons opérationnelles.

1. Il tire `numpy` (et, selon les versions, `tqdm`/`pyyaml`) dans le chemin d'un
   planificateur qui doit rester exécutable sur une machine vierge, avant même
   que le venv de la gateway n'existe. Le coût d'une dépendance ici n'est pas le
   téléchargement, c'est l'ordre d'amorçage.
2. `GGUFReader` mémorise l'ensemble des tenseurs et matérialise les tableaux de
   métadonnées, dont le vocabulaire du tokenizer. Nous ne voulons ni l'un ni
   l'autre : nous avons besoin d'une douzaine de scalaires et d'un volume
   agrégé, et nous voulons que la taille de ce que nous allouons ne dépende
   d'aucun champ de longueur lu dans le fichier.
3. Un GGUF est une entrée non fiable. Le périmètre à durcir, ici, tient en 200
   lignes bornées et testables avec des fichiers fabriqués ; adopter une
   bibliothèque tierce reviendrait à déplacer ce durcissement hors de portée de
   nos tests.

À réévaluer si l'on doit un jour écrire des GGUF, gérer les extensions récentes
du format ou lire des tenseurs — trois besoins que ce module n'a pas.

Robustesse hostile
------------------
Le fichier est une entrée, potentiellement tronquée ou fabriquée. Invariants
tenus par `_Reader` et vérifiés par les tests :

- lecture bornée à `MAX_HEADER_BYTES` ET à la taille réelle du fichier ;
- aucune allocation dimensionnée par un champ du fichier : les chaînes sont
  plafonnées, les tableaux sont **traversés sans être matérialisés** ;
- aucune boucle dont le compteur vient du fichier sans plafond : chaque
  itération consomme au moins un octet borné, et les compteurs sont plafonnés
  avant la première itération ;
- toute anomalie lève `GgufError`, jamais un `struct.error` ni un `MemoryError`.

Pas de projection vers `schema` : la section « Modèles retenus » appartient au
planificateur, qui décide quels modèles il retient. Ce module lui fournit
`GgufInspection.to_plan_data()` (JSON, sans secret) et ses `findings`.
"""
from __future__ import annotations

import math
import mmap
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import schema

SECTION_VERSION = 1

# Magie du format, en little-endian. Un GGUF big-endian existe (« GGUF » écrit à
# l'envers) mais aucun build llama.cpp que nous déployons ne le produit : on le
# refuse explicitement plutôt que de le lire de travers.
_MAGIC_LE = b"GGUF"
_MAGIC_BE = b"FUGG"

# Versions du conteneur que nous savons lire. La v1 codait les compteurs et les
# longueurs de chaîne sur 32 bits ; elle est éteinte depuis 2023 et nous la
# refusons plutôt que d'entretenir deux grammaires.
SUPPORTED_VERSIONS: frozenset[int] = frozenset({2, 3})

# ── Plafonds. Aucun n'est négociable depuis le fichier. ───────────────────────
#
# Un header réaliste pèse quelques mégaoctets (le vocabulaire d'un tokenizer de
# 150 000 entrées en représente l'essentiel). 64 Mio laissent une marge large
# tout en gardant la fenêtre de lecture bornée quelle que soit la taille du GGUF.
MAX_HEADER_BYTES = 64 * 1024 * 1024
MAX_KV_COUNT = 16_384
MAX_TENSOR_COUNT = 200_000
MAX_STRING_BYTES = 64 * 1024
MAX_ARRAY_LEN = 4_000_000
MAX_TENSOR_DIMS = 8

# Types de valeur GGUF (gguf_metadata_value_type).
_T_UINT8, _T_INT8 = 0, 1
_T_UINT16, _T_INT16 = 2, 3
_T_UINT32, _T_INT32 = 4, 5
_T_FLOAT32 = 6
_T_BOOL = 7
_T_STRING = 8
_T_ARRAY = 9
_T_UINT64, _T_INT64 = 10, 11
_T_FLOAT64 = 12

# Format struct et taille, pour les types scalaires de largeur fixe.
_SCALAR_FORMATS: dict[int, tuple[str, int]] = {
    _T_UINT8: ("<B", 1),
    _T_INT8: ("<b", 1),
    _T_UINT16: ("<H", 2),
    _T_INT16: ("<h", 2),
    _T_UINT32: ("<I", 4),
    _T_INT32: ("<i", 4),
    _T_FLOAT32: ("<f", 4),
    _T_BOOL: ("<B", 1),
    _T_UINT64: ("<Q", 8),
    _T_INT64: ("<q", 8),
    _T_FLOAT64: ("<d", 8),
}

# Types ggml : nom, éléments par bloc, octets par bloc. Sert uniquement à
# qualifier le mélange de quantisation et à agréger un volume ; un type inconnu
# n'est pas une erreur, il est compté à part et rend l'agrégat partiel.
_GGML_TYPES: dict[int, tuple[str, int, int]] = {
    0: ("F32", 1, 4),
    1: ("F16", 1, 2),
    2: ("Q4_0", 32, 18),
    3: ("Q4_1", 32, 20),
    6: ("Q5_0", 32, 22),
    7: ("Q5_1", 32, 24),
    8: ("Q8_0", 32, 34),
    9: ("Q8_1", 32, 36),
    10: ("Q2_K", 256, 84),
    11: ("Q3_K", 256, 110),
    12: ("Q4_K", 256, 144),
    13: ("Q5_K", 256, 176),
    14: ("Q6_K", 256, 210),
    15: ("Q8_K", 256, 292),
    16: ("IQ2_XXS", 256, 66),
    17: ("IQ2_XS", 256, 74),
    18: ("IQ3_XXS", 256, 98),
    19: ("IQ1_S", 256, 50),
    20: ("IQ4_NL", 32, 18),
    21: ("IQ3_S", 256, 110),
    22: ("IQ2_S", 256, 82),
    23: ("IQ4_XS", 256, 136),
    24: ("I8", 1, 1),
    25: ("I16", 1, 2),
    26: ("I32", 1, 4),
    27: ("I64", 1, 8),
    28: ("F64", 1, 8),
    29: ("IQ1_M", 256, 56),
    30: ("BF16", 1, 2),
    34: ("TQ1_0", 256, 54),
    35: ("TQ2_0", 256, 66),
}

# Octets par élément du cache KV, par type de cache llama-server. Valeurs
# arrondies vers le haut : sous-estimer un cache KV, c'est promettre une VRAM
# qu'on n'a pas.
_KV_BYTES_PER_ELEMENT: dict[str, float] = {
    "f32": 4.0,
    "f16": 2.0,
    "bf16": 2.0,
    "q8_0": 34 / 32,
    "q5_1": 24 / 32,
    "q5_0": 22 / 32,
    "q4_1": 20 / 32,
    "q4_0": 18 / 32,
}

# Marge fixe attribuée au runtime et au driver (contexte CUDA, buffers de calcul,
# graphes). Ordre de grandeur observé sur llama.cpp/CUDA ; volontairement large,
# c'est le poste que le header ne renseigne pas du tout.
DEFAULT_RUNTIME_OVERHEAD_BYTES = 768 * 1024 * 1024

# Marge de sécurité multiplicative appliquée au total.
DEFAULT_SAFETY_MARGIN = 0.20

# Ce que l'estimation ne voit pas — §9. Publié dans `to_plan_data()` et dans
# `render_human()` : une estimation dont on cache les angles morts finit par
# être lue comme une mesure.
FACTEURS_IGNORES: tuple[str, ...] = (
    "placement réel des couches et des experts sur GPU/CPU",
    "buffers de calcul et graphes CUDA du build utilisé",
    "implémentation de Flash Attention (activée ou non)",
    "batch, ubatch et parallélisme effectivement servis",
    "fragmentation et allocations du driver",
    "version et options de compilation de llama.cpp",
    "modèles chargés simultanément sur le même GPU",
)


class GgufError(Exception):
    """En-tête GGUF illisible, tronqué, incohérent ou hors des plafonds admis."""


# ── Lecture bornée ────────────────────────────────────────────────────────────

class _Reader:
    """
    Curseur en lecture seule sur une fenêtre bornée.

    Toute primitive vérifie qu'elle reste dans `limit` AVANT de consommer, et
    `limit` vaut `min(taille du fichier, MAX_HEADER_BYTES)`. C'est le seul
    endroit du module qui touche aux octets : borner ici suffit à borner tout.
    """

    def __init__(self, buffer: Any, limit: int) -> None:
        self._buf = buffer
        self._limit = limit
        self.pos = 0

    @property
    def remaining(self) -> int:
        return self._limit - self.pos

    def take(self, size: int) -> bytes:
        if size < 0:
            raise GgufError(f"taille de lecture négative ({size}) — en-tête incohérent")
        if size > self.remaining:
            raise GgufError(
                f"lecture de {size} octets à l'offset {self.pos} au-delà de la fenêtre "
                f"({self._limit} octets) — fichier tronqué ou en-tête mensonger"
            )
        chunk = bytes(self._buf[self.pos:self.pos + size])
        self.pos += size
        return chunk

    def skip(self, size: int) -> None:
        """Avance sans matérialiser. Même contrôle de bornes que `take`."""
        if size < 0:
            raise GgufError(f"saut négatif ({size}) — en-tête incohérent")
        if size > self.remaining:
            raise GgufError(
                f"saut de {size} octets à l'offset {self.pos} au-delà de la fenêtre "
                f"({self._limit} octets) — fichier tronqué ou en-tête mensonger"
            )
        self.pos += size

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def string(self) -> str:
        """
        Chaîne GGUF : longueur sur 64 bits puis octets UTF-8.

        La longueur est confrontée à deux bornes indépendantes — le plafond fixe
        et la fenêtre restante — avant toute allocation.
        """
        length = self.u64()
        if length > MAX_STRING_BYTES:
            raise GgufError(
                f"chaîne de {length} octets à l'offset {self.pos} : au-delà du plafond "
                f"de {MAX_STRING_BYTES} — en-tête refusé sans allocation"
            )
        return self.take(length).decode("utf-8", errors="replace")

    def skip_string(self) -> None:
        length = self.u64()
        if length > MAX_STRING_BYTES:
            raise GgufError(
                f"chaîne de {length} octets à l'offset {self.pos} : au-delà du plafond "
                f"de {MAX_STRING_BYTES} — en-tête refusé sans allocation"
            )
        self.skip(length)


@dataclass(frozen=True)
class ArraySummary:
    """
    Un tableau de métadonnées, résumé au lieu d'être matérialisé.

    Le vocabulaire d'un tokenizer représente à lui seul l'essentiel d'un header
    et n'informe aucune décision de capacité ; seule sa longueur nous sert
    (taille de vocabulaire). On la conserve, on jette le contenu.
    """
    element_type: int
    length: int

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "array", "element_type": self.element_type, "length": self.length}


@dataclass(frozen=True)
class TensorInventory:
    """
    Inventaire des tenseurs, agrégé et non nominatif.

    Pourquoi agrégé : un GGUF dense de 8 milliards de paramètres déclare près de
    300 tenseurs, un MoE plusieurs milliers. La liste nominative alourdirait le
    plan que l'opérateur relit avant d'appliquer, sans rien lui apprendre : la
    décision de capacité se prend sur un volume total et un mélange de
    quantisation, pas sur le nom d'une matrice. `gguf` officiel ou `llama-server`
    restent disponibles quand le détail nominatif est réellement nécessaire.
    """
    count: int
    total_elements: int
    bytes_by_type: dict[str, int] = field(default_factory=dict)
    elements_by_type: dict[str, int] = field(default_factory=dict)
    unknown_type_count: int = 0

    @property
    def complete(self) -> bool:
        """Faux si un type ggml inconnu empêche d'agréger tout le volume."""
        return self.unknown_type_count == 0

    @property
    def total_bytes(self) -> int | None:
        return sum(self.bytes_by_type.values()) if self.complete else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "total_elements": self.total_elements,
            "complete": self.complete,
            "total_bytes": self.total_bytes,
            "unknown_type_count": self.unknown_type_count,
            "bytes_by_type": dict(sorted(self.bytes_by_type.items())),
            "elements_by_type": dict(sorted(self.elements_by_type.items())),
        }


@dataclass(frozen=True)
class GgufHeader:
    """Les champs de §9, extraits et normalisés. Aucun champ libre du fichier."""
    path: str
    file_size: int
    version: int
    tensor_count: int
    kv_count: int
    architecture: str | None = None
    model_name: str | None = None
    block_count: int | None = None
    embedding_length: int | None = None
    head_count: int | None = None
    head_count_kv: int | None = None
    key_length: int | None = None
    value_length: int | None = None
    context_length: int | None = None
    rope_freq_base: float | None = None
    expert_count: int | None = None
    expert_used_count: int | None = None
    vocab_size: int | None = None
    file_type: int | None = None
    quantization_version: int | None = None
    tensors: TensorInventory | None = None

    @property
    def is_moe(self) -> bool:
        return bool(self.expert_count and self.expert_count > 1)

    @property
    def head_dim(self) -> int | None:
        """
        Dimension d'une tête d'attention.

        `{arch}.attention.key_length` fait autorité quand il est présent : les
        architectures récentes découplent la dimension de tête du rapport
        embedding/têtes, et le déduire donnerait alors une valeur fausse.
        """
        if self.key_length:
            return self.key_length
        if self.embedding_length and self.head_count:
            return self.embedding_length // self.head_count
        return None

    @property
    def kv_head_count(self) -> int | None:
        """Têtes KV effectives — `head_count` en MHA, `head_count_kv` en GQA/MQA."""
        return self.head_count_kv or self.head_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "file_size": self.file_size,
            "gguf_version": self.version,
            "tensor_count": self.tensor_count,
            "metadata_kv_count": self.kv_count,
            "architecture": self.architecture,
            "model_name": self.model_name,
            "block_count": self.block_count,
            "embedding_length": self.embedding_length,
            "head_count": self.head_count,
            "head_count_kv": self.head_count_kv,
            "head_dim": self.head_dim,
            "context_length": self.context_length,
            "rope_freq_base": self.rope_freq_base,
            "expert_count": self.expert_count,
            "expert_used_count": self.expert_used_count,
            "is_moe": self.is_moe,
            "vocab_size": self.vocab_size,
            "file_type": self.file_type,
            "quantization_version": self.quantization_version,
            "tensors": self.tensors.to_dict() if self.tensors else None,
        }


# ── Parsing ───────────────────────────────────────────────────────────────────

# Clés scalaires retenues, indexées par suffixe : la partie avant le premier
# point est l'architecture déclarée et varie (`llama.`, `qwen2.`, `gemma3.`…).
# Whitelist volontaire : rien de ce que le fichier nomme librement n'entre dans
# la sortie, ce qui rend une fuite par nom de clé structurellement impossible.
_ARCH_KEY_SUFFIXES: tuple[str, ...] = (
    "block_count",
    "embedding_length",
    "context_length",
    "attention.head_count",
    "attention.head_count_kv",
    "attention.key_length",
    "attention.value_length",
    "rope.freq_base",
    "expert_count",
    "expert_used_count",
)

_GENERAL_KEYS: frozenset[str] = frozenset({
    "general.architecture",
    "general.name",
    "general.file_type",
    "general.quantization_version",
})


def inspect_gguf(path: str | os.PathLike[str]) -> GgufHeader:
    """
    Lit l'en-tête d'un GGUF. Ne lit jamais les poids, n'écrit jamais.

    Lève `GgufError` sur tout fichier absent, illisible, tronqué, de version non
    supportée ou dont un compteur dépasse les plafonds du module.
    """
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise GgufError(f"GGUF illisible ({file_path}) : {exc}") from exc

    if size < 24:
        raise GgufError(
            f"GGUF trop court ({size} octets) : l'en-tête minimal en fait 24 — {file_path}"
        )

    window = min(size, MAX_HEADER_BYTES)
    try:
        with open(file_path, "rb") as handle:
            # mmap en lecture seule : la fenêtre est bornée par le curseur, pas
            # par une copie en mémoire. Un GGUF de 30 Gio ne coûte donc rien ici.
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as view:
                return _parse_header(_Reader(view, window), str(file_path), size)
    except GgufError:
        raise
    except (OSError, ValueError) as exc:
        raise GgufError(f"GGUF illisible ({file_path}) : {exc}") from exc


def _parse_header(reader: _Reader, path: str, size: int) -> GgufHeader:
    magic = reader.take(4)
    if magic == _MAGIC_BE:
        raise GgufError(f"GGUF big-endian non supporté — {path}")
    if magic != _MAGIC_LE:
        raise GgufError(f"magie GGUF absente (lu {magic!r}) — ce fichier n'est pas un GGUF : {path}")

    version = reader.u32()
    if version not in SUPPORTED_VERSIONS:
        raise GgufError(
            f"version de conteneur GGUF {version} non supportée "
            f"(attendu {sorted(SUPPORTED_VERSIONS)}) — {path}"
        )

    tensor_count = reader.u64()
    kv_count = reader.u64()
    # Plafonds appliqués AVANT toute boucle : un compteur aberrant est refusé,
    # il ne devient jamais un nombre d'itérations.
    if tensor_count > MAX_TENSOR_COUNT:
        raise GgufError(
            f"tensor_count aberrant ({tensor_count} > {MAX_TENSOR_COUNT}) — en-tête refusé : {path}"
        )
    if kv_count > MAX_KV_COUNT:
        raise GgufError(
            f"metadata_kv_count aberrant ({kv_count} > {MAX_KV_COUNT}) — en-tête refusé : {path}"
        )

    scalars, arrays, architecture = _read_metadata(reader, kv_count)
    tensors = _read_tensor_infos(reader, tensor_count)

    def arch_field(name: str) -> Any:
        return scalars.get(f"{architecture}.{name}") if architecture else None

    vocab = arrays.get("tokenizer.ggml.tokens")

    return GgufHeader(
        path=path,
        file_size=size,
        version=version,
        tensor_count=tensor_count,
        kv_count=kv_count,
        architecture=architecture,
        model_name=_as_str(scalars.get("general.name")),
        block_count=_as_int(arch_field("block_count")),
        embedding_length=_as_int(arch_field("embedding_length")),
        head_count=_as_int(arch_field("attention.head_count")),
        head_count_kv=_as_int(arch_field("attention.head_count_kv")),
        key_length=_as_int(arch_field("attention.key_length")),
        value_length=_as_int(arch_field("attention.value_length")),
        context_length=_as_int(arch_field("context_length")),
        rope_freq_base=_as_float(arch_field("rope.freq_base")),
        expert_count=_as_int(arch_field("expert_count")),
        expert_used_count=_as_int(arch_field("expert_used_count")),
        vocab_size=vocab.length if vocab else None,
        file_type=_as_int(scalars.get("general.file_type")),
        quantization_version=_as_int(scalars.get("general.quantization_version")),
        tensors=tensors,
    )


def _read_metadata(
    reader: _Reader, kv_count: int
) -> tuple[dict[str, Any], dict[str, ArraySummary], str | None]:
    """
    Parcourt les paires clé/valeur. Ne retient que la whitelist et les longueurs.

    Les valeurs hors whitelist sont sautées sans être décodées : le coût mémoire
    du parcours ne dépend donc pas du contenu du fichier.
    """
    scalars: dict[str, Any] = {}
    arrays: dict[str, ArraySummary] = {}
    architecture: str | None = None

    for _ in range(kv_count):
        key = reader.string()
        value_type = reader.u32()

        if value_type == _T_ARRAY:
            summary = _read_array_summary(reader)
            if key == "tokenizer.ggml.tokens":
                arrays[key] = summary
            continue

        keep = key in _GENERAL_KEYS or any(
            key.endswith("." + suffix) for suffix in _ARCH_KEY_SUFFIXES
        )
        if not keep:
            _skip_value(reader, value_type)
            continue

        scalars[key] = _read_value(reader, value_type)
        if key == "general.architecture":
            architecture = _as_str(scalars[key])

    return scalars, arrays, architecture


def _read_array_summary(reader: _Reader) -> ArraySummary:
    """
    Lit l'en-tête d'un tableau puis le franchit sans le matérialiser.

    Terminaison garantie : pour un type de largeur fixe le saut est direct ;
    pour un tableau de chaînes chaque itération consomme au moins 8 octets de
    la fenêtre, donc la boucle ne peut pas dépasser `fenêtre / 8` tours avant
    que `skip_string` ne bute sur la borne et lève.
    """
    element_type = reader.u32()
    length = reader.u64()
    if length > MAX_ARRAY_LEN:
        raise GgufError(
            f"tableau de {length} éléments à l'offset {reader.pos} : au-delà du plafond "
            f"de {MAX_ARRAY_LEN} — en-tête refusé sans allocation"
        )

    if element_type in _SCALAR_FORMATS:
        _, width = _SCALAR_FORMATS[element_type]
        reader.skip(length * width)
    elif element_type == _T_STRING:
        for _ in range(length):
            reader.skip_string()
    else:
        # Tableau de tableaux, ou type inconnu : le format ne permet pas de le
        # franchir de façon sûre. On refuse plutôt que d'avancer à l'aveugle.
        raise GgufError(
            f"type d'élément de tableau non franchissable ({element_type}) "
            f"à l'offset {reader.pos} — en-tête refusé"
        )
    return ArraySummary(element_type=element_type, length=length)


def _read_value(reader: _Reader, value_type: int) -> Any:
    if value_type == _T_STRING:
        return reader.string()
    fmt = _SCALAR_FORMATS.get(value_type)
    if fmt is None:
        raise GgufError(f"type de métadonnée inconnu ({value_type}) à l'offset {reader.pos}")
    raw = struct.unpack(fmt[0], reader.take(fmt[1]))[0]
    return bool(raw) if value_type == _T_BOOL else raw


def _skip_value(reader: _Reader, value_type: int) -> None:
    if value_type == _T_STRING:
        reader.skip_string()
        return
    fmt = _SCALAR_FORMATS.get(value_type)
    if fmt is None:
        raise GgufError(f"type de métadonnée inconnu ({value_type}) à l'offset {reader.pos}")
    reader.skip(fmt[1])


def _read_tensor_infos(reader: _Reader, tensor_count: int) -> TensorInventory:
    """Agrège la table des tenseurs : volumes par type, jamais la liste nominative."""
    total_elements = 0
    bytes_by_type: dict[str, int] = {}
    elements_by_type: dict[str, int] = {}
    unknown = 0

    for _ in range(tensor_count):
        reader.skip_string()  # nom du tenseur — non conservé, cf. TensorInventory
        n_dims = reader.u32()
        if n_dims > MAX_TENSOR_DIMS:
            raise GgufError(
                f"tenseur à {n_dims} dimensions à l'offset {reader.pos} "
                f"(plafond {MAX_TENSOR_DIMS}) — en-tête refusé"
            )
        elements = 1
        for _ in range(n_dims):
            dim = reader.u64()
            if dim == 0:
                elements = 0
            elif elements:
                elements *= dim
        ggml_type = reader.u32()
        reader.u64()  # offset dans le bloc de données — inutile ici

        total_elements += elements
        spec = _GGML_TYPES.get(ggml_type)
        if spec is None:
            unknown += 1
            continue
        name, block_elems, block_bytes = spec
        elements_by_type[name] = elements_by_type.get(name, 0) + elements
        # Division entière arrondie au bloc supérieur : un tenseur occupe des
        # blocs entiers, jamais une fraction.
        blocks = (elements + block_elems - 1) // block_elems
        bytes_by_type[name] = bytes_by_type.get(name, 0) + blocks * block_bytes

    return TensorInventory(
        count=tensor_count,
        total_elements=total_elements,
        bytes_by_type=bytes_by_type,
        elements_by_type=elements_by_type,
        unknown_type_count=unknown,
    )


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


# ── Estimation conservatrice ──────────────────────────────────────────────────

@dataclass(frozen=True)
class EstimationInputs:
    """Paramètres de service. Ce sont eux, pas le header, qui dimensionnent le KV."""
    ctx_size: int = 4096
    parallel: int = 1
    cache_type_k: str = "f16"
    cache_type_v: str = "f16"
    runtime_overhead_bytes: int = DEFAULT_RUNTIME_OVERHEAD_BYTES
    safety_margin: float = DEFAULT_SAFETY_MARGIN

    def __post_init__(self) -> None:
        if self.ctx_size < 1:
            raise ValueError(f"ctx_size doit être ≥ 1, reçu {self.ctx_size}")
        if self.parallel < 1:
            raise ValueError(f"parallel doit être ≥ 1, reçu {self.parallel}")
        if self.safety_margin < 0:
            raise ValueError(f"safety_margin doit être ≥ 0, reçu {self.safety_margin}")
        for label, value in (("cache_type_k", self.cache_type_k), ("cache_type_v", self.cache_type_v)):
            if value not in _KV_BYTES_PER_ELEMENT:
                raise ValueError(
                    f"{label} inconnu : {value!r} (connus : {sorted(_KV_BYTES_PER_ELEMENT)})"
                )


@dataclass(frozen=True)
class FootprintEstimate:
    """
    Estimation — pas une mesure. Le vocabulaire est délibéré jusque dans le nom.

    `total_bytes` vaut `None` quand le header ne permet pas de dimensionner le
    cache KV. On préfère l'absence de chiffre à un chiffre incomplet : une
    estimation amputée du KV serait une sous-estimation, c'est-à-dire exactement
    l'erreur qui fait échouer un chargement en production.
    """
    weights_bytes: int
    kv_cache_bytes: int | None
    runtime_overhead_bytes: int
    safety_margin: float
    ctx_size: int
    parallel: int
    notes: tuple[str, ...] = ()

    @property
    def total_bytes(self) -> int | None:
        if self.kv_cache_bytes is None:
            return None
        base = self.weights_bytes + self.kv_cache_bytes + self.runtime_overhead_bytes
        return int(base * (1.0 + self.safety_margin))

    @property
    def total_gb(self) -> float | None:
        total = self.total_bytes
        return round(total / (1024 ** 3), 2) if total is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "estimation",
            "avertissement": (
                "Estimation conservatrice issue du header GGUF et des paramètres de "
                "service. Ce n'est PAS une mesure : seule une calibration par "
                "chargement réel (§9) produit une valeur de capacité approuvée."
            ),
            "weights_bytes": self.weights_bytes,
            "kv_cache_bytes": self.kv_cache_bytes,
            "runtime_overhead_bytes": self.runtime_overhead_bytes,
            "safety_margin": self.safety_margin,
            "ctx_size": self.ctx_size,
            "parallel": self.parallel,
            "estimated_total_bytes": self.total_bytes,
            "estimated_total_gb": self.total_gb,
            "facteurs_ignores": list(FACTEURS_IGNORES),
            "notes": list(self.notes),
        }


def estimate_footprint(
    header: GgufHeader, inputs: EstimationInputs | None = None
) -> FootprintEstimate:
    """
    Combine header et paramètres en une estimation conservatrice.

    Poste des poids : la **taille du fichier**, pas la somme des tenseurs. Le
    fichier majore les tenseurs (il contient en plus l'en-tête et l'alignement)
    et il est mesuré, pas déduit d'une table de types que le format peut faire
    évoluer sous nos pieds. La somme par type reste publiée à titre indicatif
    dans `TensorInventory`.
    """
    params = inputs or EstimationInputs()
    notes: list[str] = []

    kv_bytes: int | None = None
    heads = header.kv_head_count
    head_dim = header.head_dim
    layers = header.block_count

    if heads and head_dim and layers:
        per_element = (
            _KV_BYTES_PER_ELEMENT[params.cache_type_k]
            + _KV_BYTES_PER_ELEMENT[params.cache_type_v]
        )
        slots = params.ctx_size * params.parallel
        # ceil : le cache est alloué en entier, on n'arrondit jamais vers le bas.
        kv_bytes = math.ceil(slots * layers * heads * head_dim * per_element)
    else:
        notes.append(
            "cache KV non estimable : le header ne déclare pas à la fois block_count, "
            "les têtes KV et la dimension de tête — aucun total n'est produit."
        )

    if header.context_length and params.ctx_size > header.context_length:
        notes.append(
            f"ctx_size demandé ({params.ctx_size}) supérieur au contexte déclaré par le "
            f"modèle ({header.context_length}) — incohérence à trancher avant activation."
        )

    if header.is_moe:
        notes.append(
            f"modèle MoE ({header.expert_count} experts, {header.expert_used_count} actifs) : "
            "les experts déportés sur CPU (cpu_moe) déplacent la charge vers la RAM hôte, "
            "ce que cette estimation ne modélise pas."
        )

    if header.tensors and not header.tensors.complete:
        notes.append(
            f"{header.tensors.unknown_type_count} tenseur(s) d'un type ggml inconnu de ce "
            "module — le volume agrégé par type est partiel (le total repose sur la taille "
            "du fichier, qui reste correcte)."
        )

    return FootprintEstimate(
        weights_bytes=header.file_size,
        kv_cache_bytes=kv_bytes,
        runtime_overhead_bytes=params.runtime_overhead_bytes,
        safety_margin=params.safety_margin,
        ctx_size=params.ctx_size,
        parallel=params.parallel,
        notes=tuple(notes),
    )


# ── Agrégation pour le plan ───────────────────────────────────────────────────

@dataclass(frozen=True)
class GgufInspection:
    """
    Résultat d'une inspection d'un ou plusieurs GGUF locaux.

    `status` suit la grille de `schema` : `skip` quand aucun fichier n'est
    présent — le cas normal en CI et sur une machine vierge, qui n'est PAS un
    échec —, `warn` quand une inspection a échoué mais qu'une autre a réussi,
    `fail` quand toutes ont échoué, `ok` sinon.
    """
    status: schema.SectionStatus
    summary: str
    entries: tuple[dict[str, Any], ...] = ()
    findings: tuple[schema.Finding, ...] = ()

    def to_plan_data(self) -> dict[str, Any]:
        """Projection JSON, sans secret, prête à être intégrée par le planificateur."""
        return {
            "inspection_version": SECTION_VERSION,
            "status": self.status,
            "kind": "estimation",
            "facteurs_ignores": list(FACTEURS_IGNORES),
            "hierarchie": (
                "header GGUF + paramètres → estimation conservatrice → chargement réel "
                "→ mesure des pics → valeur de capacité approuvée"
            ),
            "files": list(self.entries),
        }


def inspect_paths(
    paths: Sequence[str | os.PathLike[str]],
    inputs: EstimationInputs | None = None,
) -> GgufInspection:
    """
    Inspecte les GGUF présents localement. N'échoue jamais sur une absence.

    Un chemin inexistant est ignoré silencieusement : au moment où le plan est
    calculé, le modèle n'est justement pas encore téléchargé. Un chemin
    **présent mais illisible**, lui, est un constat `warn` — c'est une anomalie.
    """
    entries: list[dict[str, Any]] = []
    findings: list[schema.Finding] = []
    present = 0
    failed = 0

    for raw in paths:
        candidate = Path(raw)
        if not candidate.is_file():
            continue
        present += 1
        try:
            header = inspect_gguf(candidate)
        except GgufError as exc:
            failed += 1
            findings.append(schema.Finding(
                code="gguf_illisible",
                level="warn",
                message=f"En-tête GGUF illisible ({candidate.name}) : {exc}",
            ))
            entries.append({"path": str(candidate), "readable": False, "error": str(exc)})
            continue
        estimate = estimate_footprint(header, inputs)
        entries.append({
            "path": header.path,
            "readable": True,
            "header": header.to_dict(),
            "estimate": estimate.to_dict(),
        })

    if present == 0:
        return GgufInspection(
            status="skip",
            summary="Aucun GGUF présent localement — inspection non applicable ici.",
            findings=(schema.Finding(
                code="gguf_absent",
                level="info",
                message=(
                    "Aucun des GGUF attendus n'est présent sur cet hôte : l'inspection est "
                    "sautée. Elle sera exploitable après téléchargement (AUT-006)."
                ),
            ),),
        )

    if failed == present:
        status: schema.SectionStatus = "fail"
        summary = f"{present} GGUF présent(s), aucun en-tête lisible."
    elif failed:
        status = "warn"
        summary = f"{present - failed}/{present} en-têtes GGUF lus, {failed} illisible(s)."
    else:
        status = "ok"
        summary = f"{present} en-tête(s) GGUF lu(s) — estimations conservatrices produites."

    return GgufInspection(
        status=status, summary=summary, entries=tuple(entries), findings=tuple(findings)
    )


def render_human(inspection: GgufInspection) -> str:
    """
    Rend l'inspection en français, avec ses angles morts.

    `FACTEURS_IGNORES` est imprimé à chaque fois, y compris quand tout va bien :
    c'est la seule façon d'empêcher qu'une estimation soit relue plus tard comme
    une mesure.
    """
    lines: list[str] = [
        "INSPECTION DES EN-TÊTES GGUF (AUT-013)",
        f"  Statut : {inspection.status} — {inspection.summary}",
        "",
        "Les chiffres ci-dessous sont des ESTIMATIONS calculées depuis l'en-tête et",
        "les paramètres de service. Ce ne sont pas des mesures.",
        "",
    ]

    for entry in inspection.entries:
        name = Path(str(entry.get("path", "?"))).name
        if not entry.get("readable"):
            lines.append(f"  [XX] {name} — en-tête illisible : {entry.get('error')}")
            continue
        head = entry.get("header", {})
        est = entry.get("estimate", {})
        lines.append(f"  [ok] {name}")
        lines.append(
            f"       architecture={head.get('architecture')} blocs={head.get('block_count')} "
            f"embedding={head.get('embedding_length')} têtes_kv={head.get('head_count_kv')} "
            f"ctx_max={head.get('context_length')}"
        )
        tensors = head.get("tensors") or {}
        mix = ", ".join(f"{k}:{v}" for k, v in (tensors.get("elements_by_type") or {}).items())
        lines.append(
            f"       tenseurs={tensors.get('count')} éléments={tensors.get('total_elements')} "
            f"[{mix or 'n/a'}]"
        )
        total = est.get("estimated_total_gb")
        lines.append(
            f"       estimation VRAM ≈ {total if total is not None else 'indéterminée'} Gio "
            f"(ctx={est.get('ctx_size')}, parallel={est.get('parallel')}, "
            f"marge={est.get('safety_margin')})"
        )
        for note in est.get("notes", []):
            lines.append(f"       · {note}")

    lines.extend(["", "CE QUE CETTE ESTIMATION NE VOIT PAS"])
    for factor in FACTEURS_IGNORES:
        lines.append(f"  · {factor}")
    lines.extend([
        "",
        "Hiérarchie (§9) : header + paramètres → estimation conservatrice → chargement",
        "réel → mesure des pics → valeur de capacité approuvée.",
    ])
    return "\n".join(lines)


def findings_of(inspections: Iterable[GgufInspection]) -> tuple[schema.Finding, ...]:
    """Concatène les constats de plusieurs inspections, dédoublonnés par code."""
    return schema.merge_findings(*(i.findings for i in inspections))
