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

import hashlib
import re
import tempfile
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

log = logging.getLogger(__name__)

# Regex stricte pour les model_id : minuscules, chiffres, tirets, points, underscores
_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

# Un SHA-256 déclaré doit être exactement 64 caractères hexadécimaux.
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# Taille de bloc pour le hachage incrémental des GGUF (1 Mo).
_HASH_CHUNK_SIZE = 1024 * 1024


class IntegrityError(Exception):
    """Levée quand la vérification d'intégrité (SHA-256) d'un GGUF échoue."""

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
        si l'empreinte ne correspond pas. Coûteux sur un gros GGUF — à n'appeler
        qu'au chargement, jamais dans le chemin de requête.
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
        if not self._path.exists():
            raise FileNotFoundError(
                f"Fichier de registre des modèles introuvable : {self._path}\n"
                f"Créez ce fichier ou définissez MODELS_CONFIG_PATH dans .env"
            )

        with self._path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)  # safe_load — jamais yaml.load()

        if not isinstance(data, dict) or "models" not in data:
            raise ValueError(f"Format invalide dans {self._path} : clé 'models' manquante")

        models: dict[str, ModelDefinition] = {}
        for entry in data["models"]:
            model = self._parse_entry(entry)
            if model.id in models:
                raise ValueError(f"ID de modèle dupliqué dans {self._path} : '{model.id}'")
            models[model.id] = model

        self._models = models
        enabled_count = sum(1 for m in models.values() if m.enabled)
        log.info(
            "Registre chargé depuis %s — %d modèle(s), %d activé(s)",
            self._path, len(models), enabled_count,
        )

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

        return ModelDefinition(
            id=model_id,
            path=path,
            description=str(entry.get("description", "")),
            vram_gb=vram_gb,
            enabled=bool(entry.get("enabled", True)),
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
        self._models[model.id] = model
        self._save()
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
        self._models[model_id] = updated
        self._save()
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

        self._models[model_id] = updated
        self._save()
        return updated

    def remove(self, model_id: str) -> None:
        """
        Supprime un modèle du registre.
        L'appelant doit s'assurer que le modèle est déchargé avant d'appeler cette méthode.
        """
        if model_id not in self._models:
            raise KeyError(f"Modèle inconnu : '{model_id}'")
        del self._models[model_id]
        self._save()
        log.info("Modèle '%s' supprimé du registre.", model_id)

    def reload(self) -> None:
        """Recharge le registre depuis le fichier YAML (utile après édition manuelle)."""
        self._load()

    # ── Persistance atomique ──────────────────────────────────────────────────

    def _save(self) -> None:
        """
        Écrit le registre dans le fichier YAML de manière atomique :
        écriture dans un fichier temporaire → rename.
        Évite la corruption en cas de crash pendant l'écriture.
        """
        data = {"models": [m.to_dict() for m in self._models.values()]}
        parent = self._path.parent

        # Écriture atomique via fichier temporaire dans le même répertoire
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=parent,
                suffix=".tmp",
                delete=False,
            ) as tmp:
                yaml.dump(data, tmp, allow_unicode=True, default_flow_style=False, sort_keys=False)
                tmp_path = Path(tmp.name)

            tmp_path.replace(self._path)
        except Exception as exc:
            log.error("Échec de la sauvegarde du registre : %s", exc)
            raise
