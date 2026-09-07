"""
Registre des modèles — chargé depuis un fichier YAML (models.yaml).

Principes de sécurité :
- yaml.safe_load() obligatoire (pas yaml.load — prévient l'injection YAML)
- model_id validé par regex stricte (pas de /, .., caractères spéciaux)
- path doit être absolu et avec extension .gguf
- Si allowed_model_dirs configuré : path doit être sous un répertoire autorisé
- Écriture atomique du YAML (tmp + rename) pour éviter la corruption

Structure du fichier YAML :
  models:
    - id: "llama-3.3-70b-instruct"
      path: "/models/Llama-3.3-70B-Instruct-Q4_K_M.gguf"
      description: "..."
      vram_gb: 42.0
      enabled: true
      capabilities: [text_generation, tool_calls, streaming]
      llama_params:
        n_gpu_layers: 999
        ctx_size: 32768
        ...
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

import yaml

log = logging.getLogger(__name__)

# Regex stricte pour les model_id : minuscules, chiffres, tirets, points, underscores
_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

# Un SHA-256 déclaré doit être exactement 64 caractères hexadécimaux.
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# Taille de bloc pour le hachage incrémental des GGUF (1 Mo).
_HASH_CHUNK_SIZE = 1024 * 1024

# ── Persistance : sauvegardes des mutations admin (COR-020) ──────────────────
#
# Convention de nommage de `database._backup_path()` : « <nom>.<raison>.<horodatage>.bak ».
# Le motif est DISTINCT de celui de `bootstrap.registry_writer` (« .pre-bootstrap. ») :
# un exploitant doit pouvoir dire qui a écrit, et la purge de l'un ne doit pas
# manger les copies de l'autre.
_ADMIN_BACKUP_INFIX = ".pre-admin."
_ADMIN_BACKUP_SUFFIX = ".bak"

# Nombre de sauvegardes admin conservées. BORNÉ, contrairement aux
# `*.pre-migration.*.bak` des migrations SQLite qu'OPS-002 constate non purgées :
# le dashboard peut déclencher une écriture à chaque clic.
ADMIN_BACKUP_RETENTION = 5

# En-tête du fichier créé de toutes pièces, quand `models.yaml` n'existe pas
# encore. Se termine par la clé `models:` pour que l'ajout textuel s'y accroche.
_NEW_REGISTRY_HEADER = (
    "# Registre des modèles — EVA Inference Gateway.\n"
    "#\n"
    "# Fichier créé par l'API admin. Les commentaires ajoutés à la main sont\n"
    "# préservés : les mutations retouchent le texte au lieu de le réécrire.\n"
    "\n"
    "models:\n"
)

# Ligne de la clé racine `models:`, avec sa forme vide et un commentaire de fin
# éventuel. Non indentée : c'est une clé de premier niveau.
_MODELS_KEY_RE = re.compile(r"^models:[ \t]*(\[[ \t]*\])?[ \t]*(?P<comment>#.*)?$")


class IntegrityError(Exception):
    """Levée quand la vérification d'intégrité (SHA-256) d'un GGUF échoue."""


class RegistryWriteRefused(ValueError):
    """
    L'écriture de `models.yaml` est refusée : rien n'a été écrit (COR-020).

    Hérite de `ValueError` **à dessein** : `admin.py` traduit déjà les `ValueError`
    des mutations de registre en HTTP 422 avec le message. Un refus est
    exploitable par un opérateur — « la mise en page de votre fichier empêche une
    modification sûre » — et doit lui parvenir, pas se perdre dans une 500 muette.
    """

# Types valides pour la quantisation du KV cache
_CACHE_TYPES = {"f16", "bf16", "q8_0", "q5_0", "q4_0"}

# Types de speculative decoding supportés. Pour l'instant uniquement MTP
# (Multi-Token Prediction) — tête intégrée au GGUF, pas de modèle draft séparé.
# Extensible plus tard (draft-simple, draft-eagle3, ngram-*…).
_SPEC_TYPES = {"mtp"}


@dataclass
class LlamaParams:
    """Paramètres de lancement llama-server, configurables par modèle."""
    n_gpu_layers: int = 999
    ctx_size: int = 32768
    parallel: int = 4
    batch_size: int = 4096
    ubatch_size: int = 512
    cache_type_k: str = "q8_0"
    cache_type_v: str = "q8_0"
    flash_attn: bool = True
    threads: int = 8
    threads_http: int = 4
    # Déporte les experts FFN des modèles MoE sur CPU (ex: MiniMax M2.7).
    # Les couches attention restent sur GPU. Sans ce flag, un MoE massif
    # ne tient pas en VRAM — llama-server échoue à charger.
    cpu_moe: bool = False

    def __post_init__(self) -> None:
        if self.ubatch_size > self.batch_size:
            raise ValueError(
                f"ubatch_size ({self.ubatch_size}) doit être ≤ batch_size ({self.batch_size})"
            )
        if self.cache_type_k not in _CACHE_TYPES:
            raise ValueError(f"cache_type_k invalide : {self.cache_type_k!r}. Valeurs : {_CACHE_TYPES}")
        if self.cache_type_v not in _CACHE_TYPES:
            raise ValueError(f"cache_type_v invalide : {self.cache_type_v!r}. Valeurs : {_CACHE_TYPES}")
        if self.n_gpu_layers < 0:
            raise ValueError(f"n_gpu_layers doit être ≥ 0, reçu : {self.n_gpu_layers}")
        if self.ctx_size < 512:
            raise ValueError(f"ctx_size doit être ≥ 512, reçu : {self.ctx_size}")
        if self.parallel < 1:
            raise ValueError(f"parallel doit être ≥ 1, reçu : {self.parallel}")


@dataclass
class SpeculativeParams:
    """
    Paramètres de speculative decoding MTP (Multi-Token Prediction).

    MTP utilise une tête de prédiction multi-tokens intégrée AU MÊME GGUF
    (DeepSeek-V3, GLM, etc.) : aucun modèle draft séparé, aucune VRAM
    additionnelle. La tête propose plusieurs tokens d'avance, vérifiés en un
    seul forward pass. Mappé sur les flags llama-server --spec-* .
    """
    type: str = "mtp"
    draft_max: int = 16       # --spec-draft-n-max : nb de tokens draftés par étape
    draft_min: int = 0        # --spec-draft-n-min : minimum de draft tokens
    draft_p_min: float = 0.0  # --spec-draft-p-min : proba min d'acceptation (greedy)

    def __post_init__(self) -> None:
        if self.type not in _SPEC_TYPES:
            raise ValueError(
                f"type de speculative invalide : {self.type!r}. Valeurs : {_SPEC_TYPES}"
            )
        if self.draft_max < 1:
            raise ValueError(f"draft_max doit être ≥ 1, reçu : {self.draft_max}")
        if self.draft_min < 0:
            raise ValueError(f"draft_min doit être ≥ 0, reçu : {self.draft_min}")
        if self.draft_min > self.draft_max:
            raise ValueError(
                f"draft_min ({self.draft_min}) doit être ≤ draft_max ({self.draft_max})"
            )
        if not (0.0 <= self.draft_p_min <= 1.0):
            raise ValueError(f"draft_p_min doit être dans [0, 1], reçu : {self.draft_p_min}")


@dataclass
class ModelDefinition:
    """
    Définition complète d'un modèle enregistré dans le registre.
    Immuable après création — toute modification passe par ModelRegistry.
    """
    id: str
    path: Path
    description: str
    vram_gb: float
    enabled: bool
    capabilities: list[str]
    llama_params: LlamaParams
    # Chemin vers le projector multimodal (requis pour la capability 'vision').
    # Sans ce fichier, llama-server retourne 500 sur toute requête avec image.
    mmproj_path: Path | None = None
    # Timeout de chargement spécifique au modèle (secondes).
    # Surcharge settings.model_load_timeout_seconds si défini.
    # Utile pour les modèles massifs (ex: MiniMax M2.7 — 248 GB, ~10 min).
    load_timeout_seconds: int | None = None
    # Speculative decoding MTP (tête intégrée au GGUF). None = désactivé.
    # N'ajoute pas de VRAM (pas de modèle draft séparé) — vram_gb inchangé.
    speculative: SpeculativeParams | None = None
    # Empreinte SHA-256 attendue du fichier GGUF (opt-in). None = pas de
    # vérification d'intégrité. Sert de garde-fou supply-chain contre un GGUF
    # substitué ou corrompu (overflows de parsing GGUF → RCE). Vérifié hors du
    # chemin de construction de commande (I/O coûteuse).
    sha256: str | None = None

    def verify_integrity(self) -> bool:
        """
        Vérifie que le SHA-256 du fichier GGUF correspond à `self.sha256`.

        No-op si `sha256` n'est pas déclaré (retourne True). Sinon calcule le
        hash par blocs de 1 Mo. Lève IntegrityError si le fichier est absent ou
        si l'empreinte ne correspond pas. Cette méthode synchrone est réservée
        aux outils hors event loop, notamment ``doctor`` ; le runtime utilise
        ``integrity.attest_gguf``. Une requête qui déclenche un chargement à
        froid peut attendre cette attestation, tandis qu'un modèle déjà READY
        ne la recalcule pas.
        """
        if self.sha256 is None:
            return True

        try:
            digest = hashlib.sha256()
            with self.path.open("rb") as f:
                for chunk in iter(lambda: f.read(_HASH_CHUNK_SIZE), b""):
                    digest.update(chunk)
        except FileNotFoundError as exc:
            raise IntegrityError(
                f"[{self.id}] Fichier GGUF introuvable pour vérification d'intégrité : {self.path}"
            ) from exc
        except OSError as exc:
            raise IntegrityError(
                f"[{self.id}] Lecture impossible pour vérification d'intégrité : {exc}"
            ) from exc

        actual = digest.hexdigest()
        if actual.lower() != self.sha256.lower():
            raise IntegrityError(
                f"[{self.id}] Empreinte SHA-256 non conforme pour {self.path} : "
                f"attendu {self.sha256.lower()}, obtenu {actual}. "
                f"Fichier GGUF potentiellement corrompu ou substitué."
            )
        return True

    def build_llama_cmd(
        self,
        binary: Path,
        host: str,
        port: int,
        log_path: Path,
    ) -> list[str]:
        """
        Construit la liste d'arguments pour lancer llama-server.

        La clé interne n'apparaît volontairement PAS ici : elle est transmise
        via la variable d'environnement LLAMA_API_KEY (cf. ServerManager).
        Les arguments de commande sont visibles via ps/procfs et dans les logs.
        """
        # Durcissement sécurité : `--context-shift` est délibérément ABSENT.
        # C'est le vecteur de la CVE `n_discard` (écriture hors-bornes non
        # authentifiée, GHSA-8947-pfff-2f3c). Ne PAS l'activer. Sans lui,
        # llama-server retourne une erreur propre au lieu de décaler le contexte
        # quand la fenêtre est pleine.
        p = self.llama_params
        cmd = [
            str(binary),
            "--model", str(self.path),
            "--host", host,
            "--port", str(port),
            "-ngl", str(p.n_gpu_layers),
            "-c", str(p.ctx_size),
            "--parallel", str(p.parallel),
            "-b", str(p.batch_size),
            "-ub", str(p.ubatch_size),
            "-ctk", p.cache_type_k,
            "-ctv", p.cache_type_v,
            "-t", str(p.threads),
            "--threads-http", str(p.threads_http),
            "--cont-batching",
            "--cache-prompt",
            "--metrics",
            "--log-file", str(log_path),
        ]
        if p.flash_attn:
            cmd += ["-fa", "on"]
        if p.cpu_moe:
            cmd += ["--cpu-moe"]
        if self.mmproj_path is not None and "vision" in self.capabilities:
            cmd += ["--mmproj", str(self.mmproj_path)]
        if self.speculative is not None:
            s = self.speculative
            # MTP : --spec-type draft-mtp active la tête intégrée au GGUF.
            cmd += ["--spec-type", f"draft-{s.type}", "--spec-draft-n-max", str(s.draft_max)]
            if s.draft_min:
                cmd += ["--spec-draft-n-min", str(s.draft_min)]
            if s.draft_p_min:
                cmd += ["--spec-draft-p-min", str(s.draft_p_min)]
        return cmd

    def to_dict(self) -> dict:
        """Sérialise vers le format YAML."""
        p = self.llama_params
        llama_dict: dict = {
            "n_gpu_layers": p.n_gpu_layers,
            "ctx_size": p.ctx_size,
            "parallel": p.parallel,
            "batch_size": p.batch_size,
            "ubatch_size": p.ubatch_size,
            "cache_type_k": p.cache_type_k,
            "cache_type_v": p.cache_type_v,
            "flash_attn": p.flash_attn,
            "threads": p.threads,
            "threads_http": p.threads_http,
        }
        if p.cpu_moe:
            llama_dict["cpu_moe"] = True

        d: dict = {
            "id": self.id,
            "path": str(self.path),
            "description": self.description,
            "vram_gb": self.vram_gb,
            "enabled": self.enabled,
            "capabilities": list(self.capabilities),
            "llama_params": llama_dict,
        }
        if self.mmproj_path is not None:
            d["mmproj_path"] = str(self.mmproj_path)
        if self.load_timeout_seconds is not None:
            d["load_timeout_seconds"] = self.load_timeout_seconds
        if self.sha256 is not None:
            d["sha256"] = self.sha256
        if self.speculative is not None:
            s = self.speculative
            d["speculative"] = {
                "type": s.type,
                "draft_max": s.draft_max,
                "draft_min": s.draft_min,
                "draft_p_min": s.draft_p_min,
            }
        return d


@dataclass(frozen=True)
class RegistrySnapshot:
    """Instantané validé issu d'une lecture unique de ``models.yaml``."""

    path: Path
    sha256: str
    models: tuple[ModelDefinition, ...]

    def get(self, model_id: str) -> ModelDefinition | None:
        return next((model for model in self.models if model.id == model_id), None)

    def by_id(self) -> Mapping[str, ModelDefinition]:
        return MappingProxyType({model.id: model for model in self.models})


class ModelRegistry:
    """
    Registre des modèles disponibles sur la gateway.

    - Source de vérité : fichier YAML (models.yaml)
    - Chargé au démarrage, modifiable via l'API admin
    - Toute écriture est atomique (write tmp → rename)
    """

    def __init__(self, config_path: Path, allowed_model_dirs: list[str] | None = None) -> None:
        self._path = config_path
        # Résolus dès l'init : les chemins de modèles sont comparés après
        # resolve(), les répertoires autorisés doivent l'être aussi (sinon un
        # répertoire autorisé qui est un symlink rejetterait tous les modèles).
        self._allowed_dirs: list[Path] = [
            Path(d).resolve() for d in (allowed_model_dirs or [])
        ]
        self._models: dict[str, ModelDefinition] = {}
        self._load()

    # ── Chargement ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        snapshot = self.read_snapshot()
        self.publish_snapshot(snapshot)
        enabled_count = sum(1 for model in snapshot.models if model.enabled)
        log.info(
            "Registre chargé depuis %s — %d modèle(s), %d activé(s)",
            self._path, len(snapshot.models), enabled_count,
        )

    def read_snapshot(self) -> RegistrySnapshot:
        """Lit, hache et parse les mêmes octets, sans muter la mémoire."""
        if not self._path.exists():
            raise FileNotFoundError(
                f"Fichier de registre des modèles introuvable : {self._path}\n"
                f"Créez ce fichier ou définissez MODELS_CONFIG_PATH dans .env"
            )

        raw = self._path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Encodage UTF-8 invalide dans {self._path} : {exc}") from exc
        data = yaml.safe_load(text)  # safe_load — jamais yaml.load()

        if not isinstance(data, dict) or "models" not in data:
            raise ValueError(f"Format invalide dans {self._path} : clé 'models' manquante")
        if not isinstance(data["models"], list):
            raise ValueError(
                f"Format invalide dans {self._path} : 'models' doit être une liste"
            )

        models: dict[str, ModelDefinition] = {}
        for entry in data["models"]:
            if not isinstance(entry, dict):
                raise ValueError(
                    f"Format invalide dans {self._path} : chaque modèle doit être un objet"
                )
            model = self._parse_entry(entry)
            if model.id in models:
                raise ValueError(f"ID de modèle dupliqué dans {self._path} : '{model.id}'")
            models[model.id] = model

        return RegistrySnapshot(
            path=self._path.resolve(),
            sha256=hashlib.sha256(raw).hexdigest(),
            models=tuple(models.values()),
        )

    def snapshot_is_current(self, snapshot: RegistrySnapshot) -> bool:
        """Recoupe l'instantané contre les octets actuellement publiés."""
        if snapshot.path != self._path.resolve():
            return False
        try:
            current = hashlib.sha256(self._path.read_bytes()).hexdigest()
        except OSError:
            return False
        return current == snapshot.sha256

    def memory_models(self) -> Mapping[str, ModelDefinition]:
        """Vue en lecture seule de la génération actuellement servie."""
        return MappingProxyType(dict(self._models))

    def publish_snapshot(
        self,
        snapshot: RegistrySnapshot,
        *,
        overrides: Mapping[str, ModelDefinition] | None = None,
    ) -> None:
        """Publie un snapshot en mémoire seulement ; n'écrit jamais le YAML."""
        if snapshot.path != self._path.resolve():
            raise ValueError(
                f"snapshot issu de {snapshot.path}, registre courant {self._path.resolve()}"
            )
        models = {model.id: model for model in snapshot.models}
        for model_id, model in (overrides or {}).items():
            if model_id not in models:
                raise ValueError(f"override inconnu dans le snapshot : '{model_id}'")
            if model.id != model_id:
                raise ValueError(
                    f"override incohérent : clé '{model_id}', modèle '{model.id}'"
                )
            models[model_id] = model
        self._models = models

    def _parse_entry(self, entry: dict) -> ModelDefinition:
        """Parse et valide une entrée du YAML. Lève ValueError si invalide."""
        model_id = str(entry.get("id", ""))
        self._validate_model_id(model_id)

        raw_path = str(entry.get("path", ""))
        path = self._validate_model_path(raw_path)

        vram_gb = float(entry.get("vram_gb", 0))
        if vram_gb <= 0:
            raise ValueError(f"[{model_id}] vram_gb doit être > 0, reçu : {vram_gb}")

        llama_raw = entry.get("llama_params", {})
        llama_params = LlamaParams(**llama_raw)

        capabilities = list(entry.get("capabilities", ["text_generation"]))

        # mmproj_path — optionnel, mais obligatoire en pratique si 'vision' est déclaré.
        # Sans lui, llama-server retourne HTTP 500 sur toute requête avec image.
        raw_mmproj = entry.get("mmproj_path")
        mmproj_path: Path | None = None
        if raw_mmproj:
            mmproj_path = self._validate_model_path(str(raw_mmproj))

        if "vision" in capabilities and mmproj_path is None:
            log.warning(
                "[%s] La capability 'vision' est déclarée mais 'mmproj_path' est absent "
                "— les requêtes avec images retourneront HTTP 500. "
                "Ajoutez mmproj_path dans models.yaml.",
                model_id,
            )

        raw_timeout = entry.get("load_timeout_seconds")
        load_timeout_seconds: int | None = None
        if raw_timeout is not None:
            load_timeout_seconds = int(raw_timeout)
            if load_timeout_seconds < 30:
                raise ValueError(
                    f"[{model_id}] load_timeout_seconds doit être ≥ 30, reçu : {load_timeout_seconds}"
                )

        # sha256 — empreinte GGUF optionnelle (opt-in supply-chain). Si présente,
        # doit être 64 caractères hexadécimaux. Normalisée en minuscules.
        raw_sha256 = entry.get("sha256")
        sha256: str | None = None
        if raw_sha256 is not None:
            sha256 = str(raw_sha256).strip()
            if not _SHA256_RE.match(sha256):
                raise ValueError(
                    f"[{model_id}] sha256 invalide : {raw_sha256!r}. "
                    f"Attendu : 64 caractères hexadécimaux."
                )
            sha256 = sha256.lower()

        # speculative — bloc optionnel MTP. Absent = comportement inchangé.
        spec_raw = entry.get("speculative")
        speculative: SpeculativeParams | None = None
        if spec_raw:
            try:
                speculative = SpeculativeParams(**spec_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"[{model_id}] speculative invalide : {exc}") from exc

        enabled = entry.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError(
                f"[{model_id}] enabled doit être un booléen YAML réel "
                f"(true ou false non quoté), reçu : {enabled!r}"
            )

        return ModelDefinition(
            id=model_id,
            path=path,
            description=str(entry.get("description", "")),
            vram_gb=vram_gb,
            enabled=enabled,
            capabilities=capabilities,
            llama_params=llama_params,
            mmproj_path=mmproj_path,
            load_timeout_seconds=load_timeout_seconds,
            speculative=speculative,
            sha256=sha256,
        )

    def _validate_model_id(self, model_id: str) -> None:
        if not model_id:
            raise ValueError("L'ID de modèle ne peut pas être vide")
        if not _MODEL_ID_RE.match(model_id):
            raise ValueError(
                f"ID de modèle invalide : {model_id!r}. "
                f"Autorisé : lettres minuscules, chiffres, tirets, points, underscores. "
                f"Doit commencer par une lettre ou un chiffre. Max 63 caractères."
            )

    def _validate_model_path(self, raw_path: str) -> Path:
        if not raw_path:
            raise ValueError("Le chemin du modèle ne peut pas être vide")

        path = Path(raw_path)

        if not path.is_absolute():
            raise ValueError(
                f"Le chemin du modèle doit être absolu : {raw_path!r}"
            )
        if path.suffix.lower() != ".gguf":
            raise ValueError(
                f"Le chemin du modèle doit pointer vers un fichier .gguf : {raw_path!r}"
            )

        # Vérification des répertoires autorisés (si configuré)
        if self._allowed_dirs:
            try:
                resolved = path.resolve()
            except Exception:
                resolved = path
            if not any(
                resolved == allowed or resolved.is_relative_to(allowed)
                for allowed in self._allowed_dirs
            ):
                allowed_str = ", ".join(str(d) for d in self._allowed_dirs)
                raise ValueError(
                    f"Chemin refusé : {raw_path!r} n'est pas sous un répertoire autorisé. "
                    f"Répertoires autorisés : {allowed_str}"
                )

        return path

    # ── Lecture ────────────────────────────────────────────────────────────────

    def get(self, model_id: str) -> ModelDefinition | None:
        """Retourne un modèle par son ID, ou None s'il n'existe pas."""
        return self._models.get(model_id)

    def list_all(self) -> list[ModelDefinition]:
        """Liste tous les modèles enregistrés (activés et désactivés)."""
        return list(self._models.values())

    def list_enabled(self) -> list[ModelDefinition]:
        """Liste uniquement les modèles activés."""
        return [m for m in self._models.values() if m.enabled]

    def first_enabled_id(self) -> str | None:
        """Retourne l'ID du premier modèle activé (pour le modèle par défaut)."""
        for model in self._models.values():
            if model.enabled:
                return model.id
        return None

    # ── Écriture (API admin) ──────────────────────────────────────────────────

    def add(self, entry: dict) -> ModelDefinition:
        """
        Ajoute un modèle au registre et persiste le YAML.
        Lève ValueError si l'ID existe déjà ou si les données sont invalides.
        """
        model = self._parse_entry(entry)
        if model.id in self._models:
            raise ValueError(f"Un modèle avec l'ID '{model.id}' existe déjà dans le registre.")
        precedent = dict(self._models)
        self._models[model.id] = model
        self._save(precedent)
        log.info("Modèle enregistré : '%s' (vram=%.1f GB, enabled=%s)", model.id, model.vram_gb, model.enabled)
        return model

    def set_enabled(self, model_id: str, enabled: bool) -> ModelDefinition:
        """Active ou désactive un modèle dans le registre."""
        model = self._models.get(model_id)
        if not model:
            raise KeyError(f"Modèle inconnu : '{model_id}'")
        # Recréer avec le nouveau flag enabled — préserver tous les champs optionnels
        updated = ModelDefinition(
            id=model.id,
            path=model.path,
            description=model.description,
            vram_gb=model.vram_gb,
            enabled=enabled,
            capabilities=model.capabilities,
            llama_params=model.llama_params,
            mmproj_path=model.mmproj_path,
            load_timeout_seconds=model.load_timeout_seconds,
            speculative=model.speculative,
            sha256=model.sha256,
        )
        precedent = dict(self._models)
        self._models[model_id] = updated
        self._save(precedent)
        log.info("Modèle '%s' : enabled → %s", model_id, enabled)
        return updated

    def update(self, model_id: str, **kwargs) -> ModelDefinition:
        """
        Met à jour les champs d'un modèle (vram_gb, description, enabled, llama_params).
        Ne modifie pas l'ID ni le path (pour ça, supprimer et re-créer).

        llama_params — remplacement complet si fourni (dict ou objet avec .model_dump()).
        L'appelant est responsable de décharger le modèle si llama_params change,
        car le processus llama-server en cours utilise encore les anciens paramètres.
        """
        model = self._models.get(model_id)
        if not model:
            raise KeyError(f"Modèle inconnu : '{model_id}'")

        # Résoudre les nouveaux llama_params si fournis
        new_llama_params = model.llama_params
        if "llama_params" in kwargs and kwargs["llama_params"] is not None:
            lp_raw = kwargs["llama_params"]
            if hasattr(lp_raw, "model_dump"):
                lp_raw = lp_raw.model_dump()
            try:
                new_llama_params = LlamaParams(**lp_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"llama_params invalide : {exc}") from exc

        updated = ModelDefinition(
            id=model.id,
            path=model.path,
            description=kwargs.get("description", model.description),
            vram_gb=kwargs.get("vram_gb", model.vram_gb),
            enabled=kwargs.get("enabled", model.enabled),
            capabilities=model.capabilities,
            llama_params=new_llama_params,
            mmproj_path=model.mmproj_path,
            load_timeout_seconds=model.load_timeout_seconds,
            speculative=model.speculative,
            sha256=model.sha256,
        )
        if updated.vram_gb <= 0:
            raise ValueError(f"vram_gb doit être > 0, reçu : {updated.vram_gb}")

        precedent = dict(self._models)
        self._models[model_id] = updated
        self._save(precedent)
        return updated

    def remove(self, model_id: str) -> None:
        """
        Supprime un modèle du registre.
        L'appelant doit s'assurer que le modèle est déchargé avant d'appeler cette méthode.
        """
        if model_id not in self._models:
            raise KeyError(f"Modèle inconnu : '{model_id}'")
        precedent = dict(self._models)
        del self._models[model_id]
        self._save(precedent)
        log.info("Modèle '%s' supprimé du registre.", model_id)

    def reload(self) -> None:
        """Recharge le registre depuis le fichier YAML (utile après édition manuelle)."""
        self._load()

    # ── Persistance : retouche textuelle, jamais réécriture globale (COR-020) ──

    def _save(self, precedent: dict[str, ModelDefinition]) -> None:
        """
        Persiste la mutation en PRÉSERVANT le fichier de l'exploitant (COR-020).

        Avant ce correctif, `_save()` sérialisait la mémoire par `yaml.dump` et
        écrasait le fichier : les 55 lignes d'en-tête opérationnel du `models.yaml`
        livré — budget VRAM, table RAM hôte, procédure de réactivation de
        `minimax-m2.7` — et tous les commentaires d'entrée disparaissaient au
        premier clic du dashboard. Sans sauvegarde, sans `fsync`, et avec un
        basculement silencieux des permissions en 0600.

        Ce qui est fait à la place, dans l'ordre :

        1. le fichier est relu, texte ET structure. Illisible ou non conforme
           (« models » absent, liste attendue) ⇒ **refus**, rien n'est écrit ;
        2. l'écart entre le disque et la mémoire est réduit à des opérations
           TEXTUELLES minimales : suppression du bloc d'une entrée retirée,
           retouche des seules lignes de champ qui changent, ajout en fin de
           document pour une entrée nouvelle ;
        3. le texte candidat est reparsé et comparé au document attendu. S'il ne
           signifie pas exactement ce qui était prévu ⇒ **refus**. Jamais de repli
           sur une réécriture globale : c'est elle, le défaut ;
        4. sauvegarde horodatée et **bornée** (`ADMIN_BACKUP_RETENTION`) ;
        5. écriture atomique — temporaire dans le même répertoire, `fsync` du
           fichier, validation par `ModelRegistry` lui-même, `os.replace`, puis
           `fsync` du **répertoire parent** sans lequel le renommage n'est pas
           durable —, mode d'origine réappliqué, propriétaire et groupe rétablis.

        `precedent` est l'état mémoire d'avant la mutation : un refus le restaure,
        pour qu'une écriture refusée ne laisse jamais la mémoire en avance sur le
        disque. Il sert aussi de verrou optimiste : si le sens du fichier a changé
        depuis son chargement, la mutation refuse d'écraser l'édition concurrente.
        """
        try:
            self._write_preserving_layout(precedent)
        except Exception as exc:
            self._models = precedent
            log.error("Échec de la sauvegarde du registre %s : %s", self._path, exc)
            raise

    def _write_preserving_layout(self, precedent: dict[str, ModelDefinition]) -> None:
        """Corps de `_save()`, sans la restauration mémoire (cf. sa docstring)."""
        rw = _text_write_policy()
        horodatage = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        try:
            document = rw._read_document(self._path)
        except Exception as exc:
            raise RegistryWriteRefused(
                f"{self._path} n'a pas pu être relu avant écriture ({exc}). Rien n'a été "
                "écrit : la gateway ne remplace pas un registre qu'elle ne comprend pas."
            ) from exc

        texte, attendu = self._plan_registry_text(rw, document, horodatage, precedent)

        if document.exists and texte == document.text:
            # Mutation sans effet sur le disque (valeur réécrite à l'identique) :
            # ni sauvegarde, ni churn de fichier. Idempotence.
            return

        try:
            rw._assert_candidate_matches(texte, attendu, f"écriture de {self._path.name}")
        except Exception as exc:
            raise RegistryWriteRefused(str(exc)) from exc

        avant = os.stat(self._path) if self._path.exists() else None
        if avant is not None:
            _backup_registry(self._path, horodatage, ADMIN_BACKUP_RETENTION)

        rw._atomic_write(self._path, texte, self._allowed_dirs, horodatage)

        if avant is not None:
            _restore_ownership(self._path, avant)

    # ── Planification de l'écriture ───────────────────────────────────────────

    def _plan_registry_text(
        self,
        rw,
        document,
        horodatage: str,
        precedent: dict[str, ModelDefinition],
    ) -> tuple[str, dict]:
        """
        Rend `(texte candidat, document attendu)` pour l'état mémoire courant.

        Le « document attendu » n'est PAS `{"models": [m.to_dict() …]}` : ce serait
        redire la sortie de `yaml.dump` qu'on cherche justement à ne plus produire.
        C'est le document **brut du disque** auquel on applique le même écart que
        celui appliqué au texte. Les deux se recoupent ensuite, structure comprise.
        """
        brutes = [copy.deepcopy(entree) for entree in document.models]
        positions: dict[str, int] = {}
        for position, entree in enumerate(brutes):
            if not isinstance(entree, dict) or not entree.get("id"):
                raise RegistryWriteRefused(
                    f"{self._path} : une entrée sans clé « id » exploitable empêche toute "
                    "retouche sûre. Rien n'a été écrit."
                )
            identifiant = str(entree["id"])
            if identifiant in positions:
                raise RegistryWriteRefused(
                    f"{self._path} : « {identifiant} » figure plusieurs fois. Le fichier est "
                    "déjà incohérent, la gateway n'y touchera pas."
                )
            positions[identifiant] = position

        self._assert_disk_matches_precedent(brutes, positions, precedent)

        retires = [identifiant for identifiant in positions if identifiant not in self._models]
        ajoutes = [identifiant for identifiant in self._models if identifiant not in positions]
        communs = [identifiant for identifiant in positions if identifiant in self._models]

        if document.exists and document.text.strip():
            texte = document.text if document.text.endswith("\n") else document.text + "\n"
        else:
            texte = _NEW_REGISTRY_HEADER

        # 1. Suppressions. Elles décalent les lignes : chaque opération repart du
        #    texte courant plutôt que d'un index calculé une fois pour toutes.
        for identifiant in retires:
            texte = _delete_entry_block(rw, texte, identifiant)

        # 2. Mises à jour, champ par champ.
        for identifiant in communs:
            brute = brutes[positions[identifiant]]
            ecart = self._entry_delta(brute, self._models[identifiant])
            if ecart is None:
                continue
            scalaires, llama, llama_absent = ecart
            for champ, valeur in scalaires.items():
                texte = _set_entry_scalar(rw, texte, identifiant, champ, valeur)
                brute[champ] = valeur
            if llama:
                if llama_absent:
                    complet = self._models[identifiant].to_dict()["llama_params"]
                    texte = _insert_entry_mapping(rw, texte, identifiant, "llama_params", complet)
                    brute["llama_params"] = dict(complet)
                else:
                    for champ, valeur in llama.items():
                        texte = _set_entry_nested_scalar(
                            rw, texte, identifiant, "llama_params", champ, valeur
                        )
                    sous = dict(brute.get("llama_params") or {})
                    sous.update(llama)
                    brute["llama_params"] = sous

        restantes = [brutes[positions[i]] for i in positions if i not in retires]

        # 3. Ajouts, en fin de document. `models:` doit porter la bonne forme :
        #    « models: [] » quand la liste est vide, « models: » sinon.
        texte = _set_models_key(texte, vide=not (restantes or ajoutes))
        for identifiant in ajoutes:
            entree = self._models[identifiant].to_dict()
            texte = _append_entry_block(rw, texte, entree, horodatage)
            restantes.append(entree)

        attendu = copy.deepcopy(document.data) if document.exists else {}
        attendu["models"] = restantes
        return texte, attendu

    def _assert_disk_matches_precedent(
        self,
        brutes: list[dict],
        positions: dict[str, int],
        precedent: dict[str, ModelDefinition],
    ) -> None:
        """
        Refuse une mutation si le registre a changé depuis son chargement.

        Les commentaires peuvent évoluer : ils ne changent pas le document YAML
        normalisé et seront préservés par la retouche textuelle. Une différence
        de valeur, d'entrée ajoutée ou supprimée est en revanche une édition
        concurrente ; la gateway ne peut pas distinguer une intention opérateur
        d'un ancien snapshot mémoire, donc elle demande un `reload()` explicite.
        """
        ids_disque = set(positions)
        ids_precedents = set(precedent)
        if ids_disque != ids_precedents:
            ajoutes = sorted(ids_disque - ids_precedents)
            retires = sorted(ids_precedents - ids_disque)
            details = []
            if ajoutes:
                details.append(f"entrées ajoutées sur disque : {ajoutes}")
            if retires:
                details.append(f"entrées retirées du disque : {retires}")
            raise RegistryWriteRefused(
                f"{self._path} a changé depuis son chargement ({'; '.join(details)}). "
                "La mutation admin est refusée pour ne pas écraser une édition concurrente ; "
                "rechargez le registre puis rejouez-la. Rien n'a été écrit."
            )

        for identifiant, modele_precedent in precedent.items():
            brute = brutes[positions[identifiant]]
            try:
                actuel = self._parse_entry(brute).to_dict()
            except (ValueError, TypeError) as exc:
                raise RegistryWriteRefused(
                    f"{self._path} : l'entrée « {identifiant} » modifiée sur disque n'est "
                    f"plus lisible ({exc}). Rien n'a été écrit."
                ) from exc
            attendu = modele_precedent.to_dict()
            if actuel == attendu:
                continue
            champs = sorted(
                champ for champ in set(actuel) | set(attendu)
                if actuel.get(champ) != attendu.get(champ)
            )
            raise RegistryWriteRefused(
                f"{self._path} : l'entrée « {identifiant} » a changé depuis son chargement "
                f"(champs : {champs}). La mutation admin est refusée pour ne pas écraser "
                "une édition concurrente ; rechargez le registre puis rejouez-la. Rien n'a "
                "été écrit."
            )

    def _entry_delta(
        self, brute: dict, model: ModelDefinition
    ) -> tuple[dict[str, Any], dict[str, Any], bool] | None:
        """
        Écart entre l'entrée du disque et le modèle en mémoire, ou `None` si nul.

        La comparaison est **normalisée** : l'entrée du disque est reparsée puis
        resérialisée, si bien qu'un champ omis qui vaut son défaut ne compte pas
        pour une divergence. Sans cette normalisation, toute mutation aurait
        « touché » chaque entrée du fichier livré.

        Rend `(scalaires, llama_params, llama_params_absent_du_disque)`.

        **Refus** si un champ non scalaire diverge — `capabilities`, `speculative`,
        `path`, `id` : aucune mutation admin ne les change, donc une divergence
        vient d'une édition manuelle concurrente ou d'un fichier déjà désynchronisé.
        La retoucher à l'aveugle écraserait le réglage de l'exploitant.
        """
        cible = model.to_dict()
        try:
            actuel = self._parse_entry(brute).to_dict()
        except (ValueError, TypeError) as exc:
            raise RegistryWriteRefused(
                f"{self._path} : l'entrée « {brute.get('id')} » présente sur le disque n'est "
                f"plus lisible ({exc}). Rien n'a été écrit."
            ) from exc

        if actuel == cible:
            return None

        disparus = sorted(champ for champ in actuel if champ not in cible)
        if disparus:
            raise RegistryWriteRefused(
                f"« {model.id} » : les champs {disparus} devraient disparaître du registre. "
                "La gateway ne supprime pas de champ par retouche textuelle — modifiez "
                "l'entrée à la main. Rien n'a été écrit."
            )

        scalaires: dict[str, Any] = {}
        for champ, valeur in cible.items():
            if champ == "llama_params":
                continue
            if champ in actuel and actuel[champ] == valeur:
                continue
            if valeur is None or isinstance(valeur, (str, int, float, bool)):
                scalaires[champ] = valeur
            else:
                raise RegistryWriteRefused(
                    f"« {model.id} » : le champ « {champ} » diverge du disque et n'est pas un "
                    "scalaire. Aucune mutation admin ne le modifie ; la gateway refuse de "
                    "réécrire le bloc. Rien n'a été écrit."
                )

        llama_cible = cible["llama_params"]
        llama_actuel = actuel["llama_params"]
        llama: dict[str, Any] = {
            champ: valeur
            for champ, valeur in llama_cible.items()
            if champ not in llama_actuel or llama_actuel[champ] != valeur
        }
        for champ in llama_actuel:
            if champ not in llama_cible:
                # `to_dict()` omet `cpu_moe` quand il est faux. L'écrire
                # explicitement à `false` est plus sûr que d'effacer la ligne :
                # même sens, et le commentaire de fin de ligne survit.
                llama[champ] = getattr(model.llama_params, champ, False)

        sous_disque = brute.get("llama_params")
        absent = not isinstance(sous_disque, dict) or not sous_disque
        return scalaires, llama, absent


# ── Politique d'écriture partagée ────────────────────────────────────────────

def _text_write_policy():
    """
    Rend le module qui porte la politique d'écriture textuelle de `models.yaml`.

    Il n'existe volontairement **qu'une** politique dans le dépôt, celle
    d'AUT-007 : ajout textuel, retouche de ligne, reparse comparatif, écriture
    atomique validée par le registre lui-même. La dupliquer ici aurait produit
    deux politiques divergentes sur le même fichier — exactement la classe de
    défaut que COR-020 corrige.

    Import tardif, comme `cli.py` le fait déjà pour ce paquet : `registry_writer`
    importe `model_registry` au chargement, un import au niveau module serait
    circulaire.

    Indisponible ⇒ **refus d'écrire**. Pas de repli sur un `yaml.dump` global :
    ce repli est le défaut.
    """
    try:
        from bootstrap import registry_writer
    except ImportError as exc:  # pragma: no cover - le paquet est livré avec la gateway
        raise RegistryWriteRefused(
            "la politique d'écriture de models.yaml (bootstrap.registry_writer) est "
            f"introuvable : {exc}. Le registre n'a pas été modifié — une réécriture "
            "globale détruirait les commentaires d'exploitation du fichier."
        ) from exc
    return registry_writer


# ── Retouches textuelles ─────────────────────────────────────────────────────

def _lines(texte: str) -> list[str]:
    return texte.splitlines()


def _join(lignes: list[str]) -> str:
    return ("\n".join(lignes) + "\n") if lignes else ""


def _entry_bounds(lignes: list[str], model_id: str) -> tuple[int, int, int]:
    """
    Bornes textuelles de l'entrée `model_id` : (début, fin exclue, indentation des champs).

    Localisateur de référence, pour le chemin admin comme pour le bootstrap :
    `registry_writer._entry_block_bounds` délègue ici depuis COR-028. Il ancrait
    auparavant sa recherche sur une ligne « - id: <model_id> », donc sur des
    entrées dont `id` est la PREMIÈRE clé. C'est vrai du `models.yaml` livré et
    de ce que le bootstrap écrit, mais pas d'un fichier produit par
    `yaml.safe_dump`, qui trie les clés par ordre alphabétique et place
    `capabilities` en tête — c'est-à-dire pas d'un fichier déjà passé par une
    mutation admin d'avant COR-020. L'étape `enable_model` du parcours
    d'amorçage refusait donc sur un registre pourtant valide.

    On délimite donc les éléments de la séquence, puis on cherche la ligne `id:`
    à l'indentation des champs À L'INTÉRIEUR de chaque élément. Zéro ou plusieurs
    correspondances ⇒ **refus** : la gateway ne retouche pas un fichier dont elle
    n'identifie pas l'entrée avec certitude.
    """
    debuts: list[tuple[int, int]] = []  # (ligne, indentation des champs)
    indent_liste: int | None = None
    for index, ligne in enumerate(lignes):
        match = re.match(r"^(\s*)-(\s+)(?=\S)", ligne)
        if not match:
            continue
        tiret_indent = len(match.group(1))
        if indent_liste is None:
            indent_liste = tiret_indent
        if tiret_indent != indent_liste:
            continue
        debuts.append((index, tiret_indent + 1 + len(match.group(2))))

    trouves: list[tuple[int, int, int]] = []
    for rang, (debut, champ_indent) in enumerate(debuts):
        fin = debuts[rang + 1][0] if rang + 1 < len(debuts) else len(lignes)
        # Une ligne moins indentée et signifiante ferme la séquence avant
        # l'élément suivant (une autre clé racine, par exemple).
        for curseur in range(debut + 1, fin):
            depouillee = lignes[curseur].strip()
            if not depouillee or depouillee.startswith("#"):
                continue
            if len(lignes[curseur]) - len(lignes[curseur].lstrip()) < champ_indent:
                fin = curseur
                break
        motif = re.compile(
            r"^(\s*)id:\s*[\"']?" + re.escape(model_id) + r"[\"']?\s*(#.*)?$"
        )
        for curseur in range(debut, fin):
            candidate = lignes[curseur]
            if curseur == debut:
                candidate = " " * champ_indent + candidate[champ_indent:]
            match_id = motif.match(candidate)
            if match_id and len(match_id.group(1)) == champ_indent:
                trouves.append((debut, fin, champ_indent))
                break

    if len(trouves) != 1:
        raise RegistryWriteRefused(
            f"« {model_id} » : {len(trouves)} entrée(s) identifiée(s) dans le texte du "
            "registre, une seule est attendue. La gateway ne retouche pas un fichier dont "
            "elle n'identifie pas l'entrée avec certitude ; rien n'a été écrit."
        )
    return trouves[0]


def _set_models_key(texte: str, *, vide: bool) -> str:
    """
    Met la clé racine `models:` à la forme qu'exige la liste résultante.

    Supprimer la dernière entrée laisserait `models:` seul, que YAML lit comme
    `None` et que `read_snapshot()` refuse : le fichier deviendrait illisible par
    la gateway qui vient de l'écrire. Symétriquement, ajouter sous un
    `models: []` produirait un document invalide. Le commentaire de fin de ligne
    éventuel est conservé.
    """
    lignes = _lines(texte)
    for index, ligne in enumerate(lignes):
        match = _MODELS_KEY_RE.match(ligne)
        if not match:
            continue
        commentaire = match.group("comment")
        suffixe = f"  {commentaire.strip()}" if commentaire else ""
        lignes[index] = ("models: []" if vide else "models:") + suffixe
        return _join(lignes)
    raise RegistryWriteRefused(
        "la clé racine « models: » est introuvable en début de ligne dans le registre. "
        "La gateway ne retouche pas un fichier dont elle n'identifie pas la structure ; "
        "rien n'a été écrit."
    )


def _delete_entry_block(rw, texte: str, model_id: str) -> str:
    """
    Retire les lignes de l'entrée `model_id`, et elles seules.

    Deux ajustements sur les bornes de `registry_writer._entry_block_bounds`,
    qui sont taillées pour la RETOUCHE et non pour la suppression :

    - vers le haut, les commentaires collés juste au-dessus du tiret documentent
      cette entrée : les laisser en place les orphelinerait sur la suivante ;
    - vers le bas, la borne haute court jusqu'à l'élément suivant et englobe donc
      la ligne vide de séparation et les commentaires de l'entrée SUIVANTE. On la
      ramène sur la dernière ligne de champ.

    Ce qui est **perdu** : les commentaires internes au bloc supprimé. Ils
    décrivent l'entrée qui disparaît, et la sauvegarde horodatée les conserve.
    Ce qui est **gardé** : un commentaire placé après le dernier champ reste dans
    le fichier plutôt que d'être supprimé — un commentaire orphelin se relit, un
    commentaire effacé ne se retrouve pas.
    """
    lignes = _lines(texte)
    debut, fin, _ = _entry_bounds(lignes, model_id)

    tiret_indent = len(lignes[debut]) - len(lignes[debut].lstrip())
    while debut > 0:
        precedente = lignes[debut - 1]
        depouillee = precedente.strip()
        if not depouillee.startswith("#"):
            break
        if len(precedente) - len(precedente.lstrip()) != tiret_indent:
            break
        debut -= 1

    while fin > debut:
        derniere = lignes[fin - 1].strip()
        if derniere and not derniere.startswith("#"):
            break
        fin -= 1

    del lignes[debut:fin]
    return _join(lignes)


def _set_entry_scalar(rw, texte: str, model_id: str, champ: str, valeur: Any) -> str:
    """Remplace (ou insère) un champ scalaire de premier niveau dans l'entrée."""
    lignes = _lines(texte)
    debut, fin, champ_indent = _entry_bounds(lignes, model_id)
    lignes = rw._set_scalar_field(
        lignes, debut, fin, champ_indent, champ, rw._render_scalar(valeur)
    )
    return _join(lignes)


def _sub_block_bounds(
    lignes: list[str], debut: int, fin: int, champ_indent: int, champ: str
) -> tuple[int, int, int] | None:
    """Bornes du sous-bloc `champ:` d'une entrée, ou `None` s'il est absent/vide."""
    motif = re.compile(r"^(\s*)" + re.escape(champ) + r":\s*(#.*)?$")
    for index in range(debut, fin):
        match = motif.match(lignes[index])
        if not match or len(match.group(1)) != champ_indent:
            continue
        sous_debut = index + 1
        sous_indent: int | None = None
        curseur = sous_debut
        while curseur < fin:
            ligne = lignes[curseur]
            if not ligne.strip():
                curseur += 1
                continue
            indent = len(ligne) - len(ligne.lstrip())
            if indent <= champ_indent:
                break
            if sous_indent is None:
                sous_indent = indent
            curseur += 1
        if sous_indent is None:
            return None
        return sous_debut, curseur, sous_indent
    return None


def _set_entry_nested_scalar(
    rw, texte: str, model_id: str, bloc: str, champ: str, valeur: Any
) -> str:
    """Remplace (ou insère) un scalaire DANS le sous-bloc `bloc:` de l'entrée."""
    lignes = _lines(texte)
    debut, fin, champ_indent = _entry_bounds(lignes, model_id)
    bornes = _sub_block_bounds(lignes, debut, fin, champ_indent, bloc)
    if bornes is None:
        raise RegistryWriteRefused(
            f"« {model_id} » : le bloc « {bloc} » est introuvable dans le texte du registre "
            f"alors que le champ « {champ} » doit y changer. Rien n'a été écrit."
        )
    sous_debut, sous_fin, sous_indent = bornes
    lignes = rw._set_scalar_field(
        lignes, sous_debut, sous_fin, sous_indent, champ, rw._render_scalar(valeur)
    )
    return _join(lignes)


def _insert_entry_mapping(
    rw, texte: str, model_id: str, champ: str, mapping: Mapping[str, Any]
) -> str:
    """
    Insère un sous-bloc `champ:` complet dans une entrée qui n'en a pas.

    Cas réel : une entrée écrite à la main qui s'appuie sur les défauts de
    `LlamaParams`, dont l'exploitant change `ctx_size` via le dashboard. Il n'y a
    alors aucune ligne à retoucher — il faut créer le bloc, avec ses paramètres
    EFFECTIFS. Les autres lignes de l'entrée ne sont pas touchées.
    """
    lignes = _lines(texte)
    debut, fin, champ_indent = _entry_bounds(lignes, model_id)

    rendu = yaml.safe_dump(
        dict(mapping), allow_unicode=True, default_flow_style=False, sort_keys=False, width=100
    )
    bloc = [f"{' ' * champ_indent}{champ}:"]
    bloc += [
        f"{' ' * (champ_indent + 2)}{ligne}" if ligne else ""
        for ligne in rendu.rstrip("\n").split("\n")
    ]

    # Une ligne `champ:` vide peut exister sans sous-bloc : on écrit sous elle.
    motif = re.compile(r"^(\s*)" + re.escape(champ) + r":\s*(#.*)?$")
    for index in range(debut, fin):
        match = motif.match(lignes[index])
        if match and len(match.group(1)) == champ_indent:
            lignes[index + 1:index + 1] = bloc[1:]
            return _join(lignes)

    lignes[fin:fin] = bloc
    return _join(lignes)


def _append_entry_block(rw, texte: str, entree: Mapping[str, Any], horodatage: str) -> str:
    """
    Ajoute une entrée en fin de document, sans toucher un octet de l'existant.

    L'indentation reprend celle de la liste du fichier, quelle qu'elle soit : un
    registre écrit à 0 colonne par `yaml.safe_dump` et le fichier livré, indenté
    de 2, restent tous deux cohérents.
    """
    indent = rw._list_indent(texte)
    prefixe = " " * indent
    rendu = yaml.safe_dump(
        [dict(entree)], allow_unicode=True, default_flow_style=False, sort_keys=False, width=100
    )
    bloc = [f"{prefixe}# Entrée ajoutée par l'API admin le {horodatage}."]
    bloc += [prefixe + ligne if ligne else "" for ligne in rendu.rstrip("\n").split("\n")]

    separateur = "" if texte.endswith("\n\n") or not texte.strip() else "\n"
    return texte + separateur + "\n".join(bloc) + "\n"


# ── Sauvegarde bornée et propriété du fichier ────────────────────────────────

def _admin_backup_path(cible: Path, horodatage: str) -> Path:
    return cible.with_name(f"{cible.name}{_ADMIN_BACKUP_INFIX}{horodatage}{_ADMIN_BACKUP_SUFFIX}")


def _backup_registry(cible: Path, horodatage: str, retention: int) -> Path:
    """
    Copie horodatée du registre AVANT toute écriture, puis purge bornée.

    `O_EXCL` : une sauvegarde existante n'est jamais recouverte — deux mutations
    dans la même seconde prennent des suffixes distincts. Le mode d'origine est
    repris : une sauvegarde ne doit pas être plus largement lisible que l'original.
    """
    contenu = cible.read_bytes()
    mode = os.stat(cible).st_mode & 0o777

    chemin = _admin_backup_path(cible, horodatage)
    for essai in range(1, 100):
        try:
            descripteur = os.open(chemin, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            chemin = _admin_backup_path(cible, f"{horodatage}-{essai}")
    else:
        raise RegistryWriteRefused(
            f"impossible de créer une sauvegarde libre pour {cible} — 100 tentatives. "
            "Rien n'a été écrit."
        )
    with os.fdopen(descripteur, "wb") as fichier:
        fichier.write(contenu)
        fichier.flush()
        os.fsync(fichier.fileno())
    os.chmod(chemin, mode)

    _purge_admin_backups(cible, retention)
    return chemin


def _purge_admin_backups(cible: Path, retention: int) -> tuple[str, ...]:
    """
    Ne conserve que les `retention` sauvegardes admin les plus récentes.

    Motif strict, et propre à `.pre-admin.` : les `*.pre-bootstrap.*.bak`
    d'AUT-007 ne sont pas touchées, et les `*.pre-migration.*.bak` que le dépôt
    laisse s'accumuler (OPS-002) non plus. Le dashboard peut déclencher une
    écriture à chaque clic : ne pas borner ici recréerait ce défaut en pire.
    """
    motif = re.compile(
        r"^" + re.escape(cible.name + _ADMIN_BACKUP_INFIX)
        + r"\d{8}T\d{6}Z(-\d+)?" + re.escape(_ADMIN_BACKUP_SUFFIX) + r"$"
    )
    existantes = sorted(
        (p for p in cible.parent.iterdir() if p.is_file() and motif.match(p.name)),
        key=lambda p: p.name,
    )
    supprimees: list[str] = []
    for vieille in existantes[:-retention] if retention < len(existantes) else []:
        try:
            vieille.unlink()
            supprimees.append(vieille.name)
        except OSError:
            # Une sauvegarde non supprimable occupe de la place, elle ne casse rien.
            continue
    return tuple(supprimees)


def _restore_ownership(chemin: Path, avant: os.stat_result) -> None:
    """
    Rétablit propriétaire et groupe après `os.replace`.

    Le fichier publié est l'inode du temporaire : il porte l'uid du processus et
    le gid du répertoire (ou du processus). Sur un hôte où `models.yaml` est
    `root:eva-gateway` en 0640, une écriture par le service dédié le ferait
    basculer en `eva-gateway:eva-gateway` — le mode 0640, réappliqué par
    l'écriture atomique, ne protégerait alors plus rien.

    Best-effort : un service non privilégié ne peut pas toujours redonner un uid.
    L'échec est journalisé, jamais silencieux, et n'annule pas une écriture déjà
    publiée et valide.
    """
    apres = os.stat(chemin)
    uid = avant.st_uid if apres.st_uid != avant.st_uid else -1
    gid = avant.st_gid if apres.st_gid != avant.st_gid else -1
    if uid == -1 and gid == -1:
        return
    try:
        os.chown(chemin, uid, gid)
    except OSError as exc:
        log.warning(
            "Registre %s : propriété d'origine non rétablie (%s). Vérifiez uid/gid "
            "et les permissions du fichier.", chemin, exc,
        )
