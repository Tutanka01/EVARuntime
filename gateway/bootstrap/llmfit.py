"""
AUT-004 — adaptateur LLMfit : CLI épinglée, sortie JSON validée à la frontière.

Ce que ce module fait, et pourquoi si peu
-----------------------------------------
LLMfit (MIT, https://github.com/AlexsJones/llmfit) détecte le matériel et
recommande une quantification. EVARuntime l'adopte
**par sa CLI versionnée et sa sortie JSON**, sans réimplémenter son moteur ni le
forker. Ce module est donc un adaptateur, pas une intégration : il localise un
binaire déjà installé, vérifie son empreinte, l'exécute avec un délai maximal,
**valide sa sortie comme une entrée non fiable**, et projette le résultat vers
`schema.PlanSection`.

Il n'installe rien, ne télécharge rien, ne touche pas le réseau et n'exécute
aucun script d'installation. Un `curl | sh` dans un chemin de production est
exactement ce que §7 demande d'éviter : le binaire est présent ou il ne l'est
pas.

LLMfit est un conseiller, jamais une autorité
---------------------------------------------
Ses estimations ignorent, entre autres, les paramètres EVARuntime, le coût réel
de `ctx_size × parallel`, les caches K/V, `cpu_moe`, les projecteurs
multimodaux, la fragmentation VRAM, les autres modèles déjà chargés et les
limites systemd de l'hôte (liste complète : `LIMITATIONS`). La règle d'activation
reste `ACTIVATION_RULE` : recommandation + catalogue approuvé + estimation
conservatrice + chargement réel de calibration.

Cette subordination est **structurelle**, pas déclarative :

- ce module ne construit ni n'importe `schema.PlanStep`, et ne nomme aucune des
  constantes `ACTION_*` : il lui est donc impossible d'émettre l'action
  `enable_model`, seule voie par laquelle un plan active quoi que ce soit ;
- les identifiants qui sortent de LLMfit sont publiés sous la clé `candidate`,
  jamais `model` ou `model_id` : rien de ce qu'écrit ce module n'a la forme
  d'une entrée de registre, et aucun consommateur ne peut le confondre avec un
  modèle approuvé ;
- chaque entrée porte `catalog_approved: null` — « non statué ici ». C'est
  AUT-005 (catalogue) qui tranche, et il ne lit pas cette section pour le faire.

`gateway/tests/test_bootstrap_llmfit.py` vérifie ces trois points.

Statuts émis
------------
| Situation                                   | Statut | Code de constat            |
|---------------------------------------------|--------|----------------------------|
| binaire absent (cas normal en CI et en dev)  | `skip` | `llmfit_absent`            |
| adaptateur désactivé                         | `skip` | `llmfit_disabled`          |
| aucun épinglage fourni — non exécuté         | `skip` | `llmfit_pin_absent`        |
| SHA-256 différent de l'épinglage — non exécuté | `fail` | `llmfit_sha256_mismatch` |
| profil manuel déclaré mais illisible         | `fail` | `manual_profile_unreadable`|
| version différente de l'épinglage            | `warn` | `llmfit_version_mismatch`  |
| délai dépassé / code de retour non nul       | `warn` | `llmfit_timeout`, …        |
| sortie JSON invalide                         | `warn` | `llmfit_schema_invalid`    |
| recommandation exploitable                   | `ok`   | —                          |

Deux asymétries volontaires. D'abord : **absence n'est pas échec**. Un conseiller
optionnel qui manque ne doit jamais empêcher un plan d'exister ; c'est le cas par
défaut sur une machine de développement et en CI. Ensuite : **ce que l'opérateur
a explicitement déclaré doit tenir**. Une empreinte épinglée qui ne correspond
plus, ou un profil manuel désigné mais illisible, sont des `fail` — non parce que
le conseil manque, mais parce que la machine n'est pas dans l'état déclaré, et
qu'ignorer silencieusement une déclaration de l'opérateur est la façon dont on
applique un plan bâti sur autre chose que ce qu'il croit avoir configuré.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import schema

SECTION_NAME = schema.SECTION_RECOMMENDATION
SECTION_VERSION = 1

# Nom cherché dans le PATH quand aucun chemin explicite n'est configuré.
DEFAULT_BINARY_NAME = "llmfit"

# Sous-commande figée. `argv` est **fermé** : aucun paramètre appelant n'y entre,
# donc aucun jeton ni chemin arbitraire ne peut y transiter. §7 exige un token
# hors `argv` ; le plus sûr est de n'en accepter aucun.
LLMFIT_ARGV: tuple[str, ...] = ("recommend", "--json")

# Délai par défaut. Une détection matérielle locale se compte en secondes ; 20 s
# laisse de la marge à un premier appel qui énumère plusieurs GPU, sans qu'un
# LLMfit bloqué ne fige le planificateur.
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TIMEOUT_SECONDS = 600.0

# Bornes de défiance. Toute donnée franchissant la frontière est bornée : une
# sortie d'outil tiers est une entrée non fiable, une taille non bornée est une
# consommation mémoire non bornée.
MAX_OUTPUT_BYTES = 1_048_576          # 1 Mio de JSON — au-delà, ce n'est pas un conseil
MAX_BINARY_BYTES = 536_870_912        # 512 Mio — au-delà, on ne hache pas
MAX_RECOMMENDATIONS = 32
MAX_STRING_LENGTH = 256
MAX_MEMORY_MB = 8_388_608             # 8 Tio exprimés en Mio
MAX_GPU_LAYERS = 1_024
MAX_CONTEXT_LENGTH = 8_388_608

# Verdicts de tenue en mémoire acceptés. Fermer l'ensemble évite qu'une valeur
# inconnue d'une version future soit lue comme « ça rentre ».
FIT_VERDICTS: frozenset[str] = frozenset({"fits", "tight", "cpu_offload", "no_fit", "unknown"})

# Jeu de caractères d'une étiquette de quantification (`Q4_K_M`, `IQ3_XXS`…).
_QUANT_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")

# Caractères de contrôle : une sortie d'outil finit dans un terminal d'opérateur
# et dans un ticket. Une séquence ANSI y est une injection, pas une donnée.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Variables d'environnement retirées de l'environnement du sous-processus. LLMfit
# n'a aucun besoin des secrets de la gateway ; ne pas les lui transmettre supprime
# la question de savoir ce qu'il en ferait.
_SECRET_ENV_RE = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|PRIVATE_?KEY|_KEY$)",
    re.IGNORECASE,
)

# Ce que LLMfit ne sait pas — §7, verbatim. Publié dans la section pour que la
# limite voyage avec le conseil, et non dans un commentaire que personne ne lit.
LIMITATIONS: tuple[str, ...] = (
    "tous les paramètres EVARuntime",
    "le coût exact de ctx_size × parallel",
    "les caches K/V sélectionnés",
    "le comportement exact de cpu_moe",
    "l'empreinte des projecteurs multimodaux",
    "la fragmentation VRAM",
    "les autres modèles chargés simultanément",
    "les contraintes systemd de l'hôte",
)

ACTIVATION_RULE = (
    "recommandation LLMfit + modèle approuvé par le catalogue EVARuntime "
    "+ estimation conservatrice + chargement réel de calibration = modèle activable"
)


class LLMfitError(Exception):
    """Défaut d'adaptation LLMfit. Jamais laissée remonter par `run_llmfit()`."""


class LLMfitSchemaError(LLMfitError):
    """
    Sortie JSON refusée : champ manquant, type inattendu, valeur hors bornes.

    Le message désigne le chemin fautif (`recommendations[2].estimated_vram_mb`)
    et ce qui était attendu, pour qu'un opérateur sache quoi regarder sans lire
    ce module.
    """


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LLMfitPin:
    """
    Épinglage du binaire : version attendue **et** empreinte attendue.

    Les deux ensemble, jamais l'un sans l'autre. Une version seule se déclare
    (`--version` est une chaîne que le binaire choisit) ; une empreinte seule ne
    dit pas ce qu'on croyait installer. `sha256` est en hexadécimal minuscule sur
    64 caractères ; toute autre forme est un épinglage mal saisi, donc refusé à
    la construction plutôt qu'au moment où il aurait dû protéger.
    """
    version: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.version or not self.version.strip():
            raise LLMfitError("LLMfitPin.version doit être une chaîne non vide")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise LLMfitError(
                "LLMfitPin.sha256 doit être un SHA-256 en hexadécimal minuscule "
                f"(64 caractères), reçu {len(self.sha256)} caractère(s)"
            )


@dataclass(frozen=True)
class LLMfitConfig:
    """
    Réglages de l'adaptateur. Tous facultatifs : l'absence totale de LLMfit est
    une configuration valide et le comportement par défaut sur une machine nue.

    `manual_profile_path` est le fallback de §7 : un profil écrit à la main
    remplace intégralement LLMfit et **passe par la même validation**. Une entrée
    d'opérateur n'est pas plus fiable qu'une sortie d'outil ; elle est seulement
    plus facile à corriger.
    """
    enabled: bool = True
    binary_path: Path | None = None
    pin: LLMfitPin | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    manual_profile_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.timeout_seconds, (int, float)) or isinstance(self.timeout_seconds, bool):
            raise LLMfitError("timeout_seconds doit être un nombre")
        if not (0 < float(self.timeout_seconds) <= MAX_TIMEOUT_SECONDS):
            raise LLMfitError(
                f"timeout_seconds doit être dans ]0, {MAX_TIMEOUT_SECONDS}], "
                f"reçu {self.timeout_seconds!r}"
            )


# ── Modèle de la sortie validée ───────────────────────────────────────────────

@dataclass(frozen=True)
class LLMfitCandidate:
    """
    Une recommandation, après validation et bornage.

    `candidate` n'est pas un identifiant de registre : c'est une chaîne qu'un
    outil tiers a proposée. Le nom du champ le dit, pour que personne ne
    l'utilise comme clé de `models.yaml` par distraction.
    """
    candidate: str
    quantization: str | None = None
    estimated_vram_mb: float | None = None
    estimated_ram_mb: float | None = None
    gpu_layers: int | None = None
    context_length: int | None = None
    fit: str | None = None
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate,
            "quantization": self.quantization,
            "estimated_vram_mb": self.estimated_vram_mb,
            "estimated_ram_mb": self.estimated_ram_mb,
            "gpu_layers": self.gpu_layers,
            "context_length": self.context_length,
            "fit": self.fit,
            "score": self.score,
            # Non statué ici, et volontairement : le catalogue (AUT-005) est seul
            # à pouvoir approuver, et il ne lit pas cette section pour le faire.
            "catalog_approved": None,
        }


@dataclass(frozen=True)
class LLMfitRecommendation:
    """Sortie LLMfit validée. Aucun champ n'a échappé au bornage."""
    llmfit_version: str | None
    candidates: tuple[LLMfitCandidate, ...]
    ignored_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "llmfit_version": self.llmfit_version,
            "candidates": [c.to_dict() for c in self.candidates],
            "ignored_fields": list(self.ignored_fields),
        }


@dataclass(frozen=True)
class LLMfitResult:
    """Ce que l'adaptateur a constaté. `source` dit d'où vient la recommandation."""
    status: schema.SectionStatus
    summary: str
    source: str  # "llmfit" | "manual" | "none"
    findings: tuple[schema.Finding, ...] = ()
    recommendation: LLMfitRecommendation | None = None
    binary_path: str | None = None
    binary_sha256: str | None = None
    pinned_version: str | None = None
    pin_verified: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    duration_ms: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ── Exécution du sous-processus ───────────────────────────────────────────────

@dataclass(frozen=True)
class _Completed:
    """Résultat d'exécution réduit à ce dont l'adaptateur a besoin."""
    returncode: int
    stdout: bytes
    stderr: bytes


# Signature d'un exécuteur injectable : les tests fournissent le leur, ce qui
# évite de dépendre d'un binaire réel pour tester délai, code de retour et sortie.
Runner = Callable[[Sequence[str], float], _Completed]


def child_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """
    Environnement du sous-processus, purgé de toute variable au nom sensible.

    LLMfit inspecte le matériel local ; il n'a aucun usage de `ADMIN_SECRET`, de
    `HF_TOKEN` ou d'une clé d'API. Les retirer supprime la question, plutôt que
    de faire confiance à un outil tiers pour ne pas les journaliser.
    """
    source = dict(os.environ if base is None else base)
    return {key: value for key, value in source.items() if not _SECRET_ENV_RE.search(key)}


def _default_runner(argv: Sequence[str], timeout: float) -> _Completed:
    """Exécute LLMfit sans shell, sans entrée standard, avec délai maximal."""
    completed = subprocess.run(  # noqa: S603 — argv fermé, aucun shell
        list(argv),
        capture_output=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=child_environment(),
        check=False,
    )
    return _Completed(
        returncode=completed.returncode,
        stdout=completed.stdout or b"",
        stderr=completed.stderr or b"",
    )


def file_sha256(path: Path) -> str:
    """
    Empreinte d'un fichier, lue par blocs et bornée par `MAX_BINARY_BYTES`.

    Hacher sans borne un chemin fourni par la configuration reviendrait à laisser
    une erreur de saisie (`/dev/zero`, un point de montage) faire tourner le
    planificateur indéfiniment.
    """
    size = path.stat().st_size
    if size > MAX_BINARY_BYTES:
        raise LLMfitError(
            f"{path} pèse {size} octets, au-delà de la borne de {MAX_BINARY_BYTES} — "
            "vérifiez le chemin épinglé, ce n'est probablement pas le binaire LLMfit"
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ── Validation de la sortie JSON ──────────────────────────────────────────────

def parse_llmfit_json(raw: str | bytes) -> LLMfitRecommendation:
    """
    Valide et normalise la sortie de `llmfit recommend --json`.

    Écrit à la main, sans `jsonschema` : le contrat tient en une page et ses
    messages doivent nommer le champ fautif, pas empiler une trace de
    bibliothèque. Tout ce qui n'est pas explicitement accepté est refusé ; tout
    ce qui est accepté est borné.

    Lève `LLMfitSchemaError` sur JSON invalide, sortie vide ou tronquée, champ
    manquant, type inattendu, valeur hors bornes ou liste trop longue.

    ATTENTION — la forme d'entrée acceptée ici est une **hypothèse**. La sortie
    exacte de `llmfit recommend --json` n'est pas documentée publiquement (le
    dépôt annonce « top picks as JSON » sans en fixer les champs). Les clés
    consommées sont dérivées de la description de §7. Elles doivent être
    confrontées à une capture réelle avant mise en production ; le seul effet
    d'une hypothèse fausse est un `llmfit_schema_invalid`, jamais une donnée
    fausse propagée dans le plan.
    """
    text = _decode_output(raw)
    document = _load_json(text)

    if not isinstance(document, dict):
        raise LLMfitSchemaError(
            f"la racine doit être un objet JSON, reçu {_type_name(document)} — "
            "attendu un objet contenant « recommendations »"
        )

    version = _optional_label(document, ("llmfit_version", "version"), "llmfit_version")

    entries = document.get("recommendations")
    if entries is None:
        raise LLMfitSchemaError(
            "champ « recommendations » manquant à la racine — "
            f"clés reçues : {sorted(str(k) for k in document)[:10]}"
        )
    if not isinstance(entries, list):
        raise LLMfitSchemaError(
            f"recommendations doit être une liste, reçu {_type_name(entries)}"
        )
    if len(entries) > MAX_RECOMMENDATIONS:
        raise LLMfitSchemaError(
            f"recommendations compte {len(entries)} entrées, borne à {MAX_RECOMMENDATIONS} — "
            "sortie refusée plutôt que tronquée en silence"
        )

    candidates = tuple(
        _parse_candidate(entry, f"recommendations[{index}]")
        for index, entry in enumerate(entries)
    )

    known = {"llmfit_version", "version", "recommendations"}
    ignored = tuple(sorted(str(k) for k in document if str(k) not in known))
    return LLMfitRecommendation(
        llmfit_version=version, candidates=candidates, ignored_fields=ignored
    )


def _decode_output(raw: str | bytes) -> str:
    if isinstance(raw, bytes):
        if len(raw) > MAX_OUTPUT_BYTES:
            raise LLMfitSchemaError(
                f"sortie de {len(raw)} octets, au-delà de la borne de {MAX_OUTPUT_BYTES} — "
                "refusée sans être analysée"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LLMfitSchemaError(
                f"sortie non décodable en UTF-8 à l'octet {exc.start} — "
                "le binaire épinglé n'est probablement pas celui attendu"
            ) from exc
    elif isinstance(raw, str):
        text = raw
        if len(text.encode("utf-8", "replace")) > MAX_OUTPUT_BYTES:
            raise LLMfitSchemaError(
                f"sortie au-delà de la borne de {MAX_OUTPUT_BYTES} octets — "
                "refusée sans être analysée"
            )
    else:
        raise LLMfitSchemaError(
            f"parse_llmfit_json attend str ou bytes, reçu {_type_name(raw)}"
        )

    if not text.strip():
        raise LLMfitSchemaError(
            "sortie vide — LLMfit n'a rien écrit sur la sortie standard "
            "(vérifiez le code de retour et la sortie d'erreur)"
        )
    return text


def _load_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        hint = ""
        if exc.pos >= len(text.rstrip()) - 1:
            hint = " — sortie probablement tronquée ou incomplète"
        raise LLMfitSchemaError(
            f"JSON invalide ligne {exc.lineno}, colonne {exc.colno} : {exc.msg}{hint}"
        ) from exc


def _parse_candidate(entry: Any, path: str) -> LLMfitCandidate:
    if not isinstance(entry, dict):
        raise LLMfitSchemaError(f"{path} doit être un objet, reçu {_type_name(entry)}")

    name = entry.get("model", entry.get("candidate"))
    if not isinstance(name, str) or not name.strip():
        raise LLMfitSchemaError(
            f"{path}.model doit être une chaîne non vide, reçu {_type_name(name)}"
        )
    candidate = _clean_label(name, f"{path}.model")

    quantization = entry.get("quantization")
    if quantization is not None:
        if not isinstance(quantization, str) or not _QUANT_RE.fullmatch(quantization):
            raise LLMfitSchemaError(
                f"{path}.quantization doit être une étiquette courte "
                f"[A-Za-z0-9_.-] de 1 à 32 caractères, reçu {quantization!r}"
            )

    fit = entry.get("fit")
    if fit is not None and (not isinstance(fit, str) or fit not in FIT_VERDICTS):
        raise LLMfitSchemaError(
            f"{path}.fit inconnu : {fit!r} — attendu parmi {sorted(FIT_VERDICTS)}"
        )

    return LLMfitCandidate(
        candidate=candidate,
        quantization=quantization,
        estimated_vram_mb=_number(entry.get("estimated_vram_mb"), f"{path}.estimated_vram_mb", 0.0, MAX_MEMORY_MB),
        estimated_ram_mb=_number(entry.get("estimated_ram_mb"), f"{path}.estimated_ram_mb", 0.0, MAX_MEMORY_MB),
        gpu_layers=_integer(entry.get("gpu_layers"), f"{path}.gpu_layers", 0, MAX_GPU_LAYERS),
        context_length=_integer(entry.get("context_length"), f"{path}.context_length", 1, MAX_CONTEXT_LENGTH),
        fit=fit,
        score=_number(entry.get("score"), f"{path}.score", 0.0, 1.0),
    )


def _optional_label(document: dict[str, Any], keys: Sequence[str], label: str) -> str | None:
    for key in keys:
        if key not in document:
            continue
        value = document[key]
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise LLMfitSchemaError(
                f"{label} doit être une chaîne non vide, reçu {_type_name(value)}"
            )
        return _clean_label(value, label)
    return None


def _clean_label(value: str, path: str) -> str:
    if len(value) > MAX_STRING_LENGTH:
        raise LLMfitSchemaError(
            f"{path} fait {len(value)} caractères, borne à {MAX_STRING_LENGTH}"
        )
    if _CONTROL_RE.search(value):
        raise LLMfitSchemaError(
            f"{path} contient un caractère de contrôle — refusé : cette chaîne "
            "est destinée à un terminal d'opérateur et à un ticket"
        )
    return value.strip()


def _number(value: Any, path: str, low: float, high: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LLMfitSchemaError(f"{path} doit être un nombre, reçu {_type_name(value)}")
    number = float(value)
    if not math.isfinite(number):
        raise LLMfitSchemaError(f"{path} doit être fini, reçu {value!r}")
    if not (low <= number <= high):
        raise LLMfitSchemaError(f"{path} doit être dans [{low}, {high}], reçu {number!r}")
    return number


def _integer(value: Any, path: str, low: int, high: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise LLMfitSchemaError(f"{path} doit être un entier, reçu {_type_name(value)}")
    if not (low <= value <= high):
        raise LLMfitSchemaError(f"{path} doit être dans [{low}, {high}], reçu {value!r}")
    return value


def _type_name(value: Any) -> str:
    return "null" if value is None else type(value).__name__


# ── Fallback manuel ───────────────────────────────────────────────────────────

def load_manual_profile(path: Path) -> LLMfitRecommendation:
    """
    Charge un profil de recommandation écrit à la main, par la **même** validation.

    §7 autorise à désactiver LLMfit et à fournir un profil manuel. Le relâcher
    au motif qu'un humain l'a écrit serait exactement l'inverse de ce que la
    validation à la frontière protège : un fichier édité à 3 h du matin sur un
    hôte en incident n'est pas une entrée de confiance.
    """
    if not path.is_file():
        raise LLMfitError(f"profil manuel introuvable : {path}")
    size = path.stat().st_size
    if size > MAX_OUTPUT_BYTES:
        raise LLMfitError(
            f"profil manuel {path} : {size} octets, au-delà de la borne de {MAX_OUTPUT_BYTES}"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LLMfitError(f"profil manuel {path} illisible : {exc}") from exc
    return parse_llmfit_json(raw)


# ── Collecte ──────────────────────────────────────────────────────────────────

def run_llmfit(
    config: LLMfitConfig | None = None,
    *,
    runner: Runner | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> LLMfitResult:
    """
    Localise, vérifie et exécute LLMfit — ou explique pourquoi il ne l'a pas fait.

    Ne lève jamais : toute condition d'erreur devient un statut et un constat.
    Un planificateur qui remonte une exception depuis un conseiller optionnel
    prive l'opérateur du plan entier pour un composant facultatif.

    `runner` et `which` sont injectables pour les tests : c'est ce qui permet de
    couvrir dépassement de délai, code de retour non nul et sortie malformée sans
    disposer d'un LLMfit réel.
    """
    config = config or LLMfitConfig()
    run = runner or _default_runner

    if config.manual_profile_path is not None:
        return _from_manual_profile(config)

    if not config.enabled:
        return LLMfitResult(
            status="skip",
            summary="conseil consultatif — LLMfit désactivé par configuration",
            source="none",
            timeout_seconds=float(config.timeout_seconds),
            findings=(schema.Finding(
                code="llmfit_disabled",
                level="info",
                message="LLMfit est désactivé. Le plan se construit sans recommandation ; "
                        "fournissez un profil manuel pour en obtenir une.",
            ),),
        )

    binary = _locate_binary(config, which)
    if binary is None:
        target = str(config.binary_path) if config.binary_path else DEFAULT_BINARY_NAME
        return LLMfitResult(
            status="skip",
            summary="conseil consultatif — LLMfit absent, aucune recommandation",
            source="none",
            timeout_seconds=float(config.timeout_seconds),
            findings=(schema.Finding(
                code="llmfit_absent",
                level="info",
                message=f"Binaire LLMfit introuvable ou non exécutable ({target}). "
                        "Ce n'est pas une erreur : LLMfit est un conseiller optionnel. "
                        "Installez-le et épinglez sa version et son SHA-256, ou fournissez "
                        "un profil manuel.",
            ),),
        )

    if config.pin is None:
        return LLMfitResult(
            status="skip",
            summary="conseil consultatif — LLMfit présent mais non épinglé, non exécuté",
            source="none",
            binary_path=str(binary),
            timeout_seconds=float(config.timeout_seconds),
            findings=(schema.Finding(
                code="llmfit_pin_absent",
                level="warn",
                message=f"{binary} n'est pas épinglé (version + SHA-256 attendus) et n'a "
                        "donc pas été exécuté. Figez-les dans le manifeste de bootstrap : "
                        "un binaire tiers non vérifié ne s'exécute pas dans un chemin de "
                        "production.",
            ),),
        )

    try:
        digest = file_sha256(binary)
    except (LLMfitError, OSError) as exc:
        return LLMfitResult(
            status="fail",
            summary="conseil consultatif — empreinte du binaire LLMfit illisible",
            source="none",
            binary_path=str(binary),
            pinned_version=config.pin.version,
            timeout_seconds=float(config.timeout_seconds),
            findings=(schema.Finding(
                code="llmfit_digest_unreadable",
                level="fail",
                message=f"Impossible de calculer l'empreinte de {binary} : {exc}",
            ),),
        )

    if digest != config.pin.sha256:
        return LLMfitResult(
            status="fail",
            summary="conseil consultatif — SHA-256 de LLMfit différent de l'épinglage, non exécuté",
            source="none",
            binary_path=str(binary),
            binary_sha256=digest,
            pinned_version=config.pin.version,
            timeout_seconds=float(config.timeout_seconds),
            findings=(schema.Finding(
                code="llmfit_sha256_mismatch",
                level="fail",
                message=f"{binary} a l'empreinte {digest}, l'épinglage attend "
                        f"{config.pin.sha256}. Le binaire n'a PAS été exécuté. Soit il a été "
                        "mis à jour sans mettre à jour le manifeste, soit il a été remplacé : "
                        "tranchez avant de continuer.",
            ),),
        )

    argv = (str(binary), *LLMFIT_ARGV)
    started = time.monotonic()
    try:
        completed = run(argv, float(config.timeout_seconds))
    except subprocess.TimeoutExpired:
        return _degraded(
            config, binary, digest,
            code="llmfit_timeout",
            message=f"LLMfit n'a pas répondu en {config.timeout_seconds} s et a été interrompu. "
                    "Le plan continue sans recommandation.",
            duration_ms=_elapsed_ms(started),
        )
    except OSError as exc:
        return _degraded(
            config, binary, digest,
            code="llmfit_exec_failed",
            message=f"Exécution de {binary} impossible : {exc}",
            duration_ms=_elapsed_ms(started),
        )

    duration_ms = _elapsed_ms(started)

    if completed.returncode != 0:
        detail = _first_line(completed.stderr) or _first_line(completed.stdout) or "(aucun message)"
        return _degraded(
            config, binary, digest,
            code="llmfit_exit_nonzero",
            message=f"LLMfit a terminé en code {completed.returncode} : {detail}",
            duration_ms=duration_ms,
        )

    try:
        recommendation = parse_llmfit_json(completed.stdout)
    except LLMfitSchemaError as exc:
        return _degraded(
            config, binary, digest,
            code="llmfit_schema_invalid",
            message=f"Sortie de « {' '.join(LLMFIT_ARGV)} » refusée : {exc}",
            duration_ms=duration_ms,
        )

    findings = list(_version_findings(config.pin, recommendation.llmfit_version))
    if not recommendation.candidates:
        findings.append(schema.Finding(
            code="llmfit_no_recommendation",
            level="warn",
            message="LLMfit n'a proposé aucun candidat pour ce matériel. Le plan "
                    "reste applicable ; la sélection revient au catalogue.",
        ))

    status: schema.SectionStatus = "warn" if any(f.level == "warn" for f in findings) else "ok"
    return LLMfitResult(
        status=status,
        summary=_summary(recommendation),
        source="llmfit",
        findings=tuple(findings),
        recommendation=recommendation,
        binary_path=str(binary),
        binary_sha256=digest,
        pinned_version=config.pin.version,
        pin_verified=True,
        timeout_seconds=float(config.timeout_seconds),
        duration_ms=duration_ms,
    )


def _from_manual_profile(config: LLMfitConfig) -> LLMfitResult:
    path = config.manual_profile_path
    if path is None:  # garanti par l'appelant — mais `python -O` retirerait un assert
        raise LLMfitError(
            "profil manuel demandé sans `manual_profile_path` : rien à lire, et rien "
            "à recommander"
        )
    try:
        recommendation = load_manual_profile(path)
    except (LLMfitError, OSError) as exc:
        return LLMfitResult(
            status="fail",
            summary="conseil consultatif — profil manuel déclaré mais inexploitable",
            source="manual",
            timeout_seconds=float(config.timeout_seconds),
            findings=(schema.Finding(
                code="manual_profile_unreadable",
                level="fail",
                message=f"Le profil manuel {path} a été déclaré mais n'est pas exploitable : "
                        f"{exc}. Il n'est pas ignoré en silence : corrigez-le ou retirez-le "
                        "de la configuration.",
            ),),
        )

    findings: list[schema.Finding] = [schema.Finding(
        code="manual_profile_used",
        level="info",
        message=f"Recommandation fournie manuellement ({path}), LLMfit n'a pas été exécuté. "
                "Ce profil a subi exactement la même validation qu'une sortie LLMfit.",
    )]
    if not recommendation.candidates:
        findings.append(schema.Finding(
            code="llmfit_no_recommendation",
            level="warn",
            message="Le profil manuel ne contient aucun candidat.",
        ))

    status: schema.SectionStatus = "warn" if any(f.level == "warn" for f in findings) else "ok"
    return LLMfitResult(
        status=status,
        summary=_summary(recommendation, source="profil manuel"),
        source="manual",
        findings=tuple(findings),
        recommendation=recommendation,
        timeout_seconds=float(config.timeout_seconds),
        extra={"manual_profile_path": str(path)},
    )


def _locate_binary(config: LLMfitConfig, which: Callable[[str], str | None]) -> Path | None:
    if config.binary_path is not None:
        path = Path(config.binary_path)
        return path if _is_executable_file(path) else None
    found = which(DEFAULT_BINARY_NAME)
    if not found:
        return None
    path = Path(found)
    return path if _is_executable_file(path) else None


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _version_findings(pin: LLMfitPin, reported: str | None) -> list[schema.Finding]:
    if reported is None:
        return [schema.Finding(
            code="llmfit_version_unknown",
            level="warn",
            message=f"La sortie ne déclare aucune version ; l'épinglage attend {pin.version}. "
                    "L'empreinte correspond, mais la concordance de version n'a pas pu être "
                    "vérifiée dans la sortie elle-même.",
        )]
    if reported != pin.version:
        return [schema.Finding(
            code="llmfit_version_mismatch",
            level="warn",
            message=f"LLMfit annonce la version {reported}, l'épinglage attend {pin.version}. "
                    "La recommandation est conservée mais son comportement peut différer de "
                    "celui qualifié : réalignez le manifeste.",
        )]
    return []


def _summary(recommendation: LLMfitRecommendation, *, source: str = "LLMfit") -> str:
    count = len(recommendation.candidates)
    if count == 0:
        return f"conseil consultatif — {source} : aucun candidat proposé"
    first = recommendation.candidates[0]
    quant = f" en {first.quantization}" if first.quantization else ""
    return (
        f"conseil consultatif — {source} propose {count} candidat(s), "
        f"en tête « {first.candidate} »{quant} ; n'active aucun modèle par lui-même"
    )


def _degraded(
    config: LLMfitConfig,
    binary: Path,
    digest: str,
    *,
    code: str,
    message: str,
    duration_ms: int | None,
) -> LLMfitResult:
    """Échec d'exécution ou de validation : dégradé, jamais bloquant."""
    if config.pin is None:  # garanti par l'appelant — mais `python -O` retirerait un assert
        raise LLMfitError(
            "dégradation LLMfit sans épinglage : `pin` est absent alors que le binaire "
            "a été exécuté — l'état est incohérent, il n'est pas rapporté comme normal"
        )
    return LLMfitResult(
        status="warn",
        summary="conseil consultatif — LLMfit inexploitable, plan poursuivi sans recommandation",
        source="none",
        findings=(schema.Finding(code=code, level="warn", message=message),),
        binary_path=str(binary),
        binary_sha256=digest,
        pinned_version=config.pin.version,
        pin_verified=True,
        timeout_seconds=float(config.timeout_seconds),
        duration_ms=duration_ms,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _first_line(data: bytes) -> str:
    text = data.decode("utf-8", "replace").strip()
    if not text:
        return ""
    line = text.splitlines()[0]
    line = _CONTROL_RE.sub("", line)
    return line[:MAX_STRING_LENGTH]


# ── Projection vers le plan ───────────────────────────────────────────────────

def to_plan_section(result: LLMfitResult) -> schema.PlanSection:
    """
    Projette le constat vers le contrat de `schema`. Jamais vers une étape.

    Cette fonction est le seul point de sortie du module vers le plan, et elle ne
    peut retourner qu'une `PlanSection`. Une section décrit ; seule une
    `PlanStep` agit. Ce module ne construit aucune `PlanStep` et ne nomme aucune
    constante `ACTION_*` : une recommandation ne peut donc pas, par ce chemin,
    activer un modèle.

    `data` est sérialisable JSON et sans secret : aucune valeur d'environnement
    n'y entre, et l'environnement du sous-processus est lui-même purgé.
    """
    data: dict[str, Any] = {
        "source": result.source,
        "advisory_only": True,
        "activation_rule": ACTIVATION_RULE,
        "limitations": list(LIMITATIONS),
        "binary_path": result.binary_path,
        "binary_sha256": result.binary_sha256,
        "pinned_version": result.pinned_version,
        "pin_verified": result.pin_verified,
        "timeout_seconds": result.timeout_seconds,
        "duration_ms": result.duration_ms,
        "llmfit_version": None,
        "candidates": [],
        "ignored_output_fields": [],
    }
    if result.recommendation is not None:
        data["llmfit_version"] = result.recommendation.llmfit_version
        data["candidates"] = [c.to_dict() for c in result.recommendation.candidates]
        data["ignored_output_fields"] = list(result.recommendation.ignored_fields)
    data.update(result.extra)

    return schema.PlanSection(
        name=SECTION_NAME,
        version=SECTION_VERSION,
        status=result.status,
        summary=result.summary,
        data=data,
        findings=result.findings,
    )


def render_advisory_notice() -> str:
    """
    Texte de mise en garde, prêt à être imprimé par la CLI sous la section.

    `schema.render_human()` n'imprime ni `data` ni les constats de niveau `info` ;
    la liste des limites de §7 serait donc invisible en rendu humain si elle n'y
    vivait que dans `data`. Ce helper existe pour que la limite reste lisible là
    où l'opérateur décide, et non seulement dans le JSON. Le résumé de la section
    porte de son côté la mention « conseil consultatif », qui, elle, est toujours
    imprimée.
    """
    lines = [
        "LLMfit est un conseiller, pas une autorité. Ses estimations ignorent :",
    ]
    lines.extend(f"  · {item}" for item in LIMITATIONS)
    lines.extend(["", f"Règle d'activation : {ACTIVATION_RULE}."])
    return "\n".join(lines)
