"""
Schémas Pydantic pour les endpoints admin.
Les schémas OpenAI (chat/completions) sont proxiés tels quels vers llama-server,
on ne les revalide pas pour éviter de casser la compatibilité avec les futurs champs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from model_registry import MODEL_CAPABILITIES


MAX_SQLITE_INTEGER = 2**63 - 1


# ── Utilisateurs ──────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    username: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    email: Optional[str] = None
    rpm_limit: Optional[int] = Field(None, ge=1, le=1000, strict=True)
    monthly_token_limit: Optional[int] = Field(
        None, ge=0, le=MAX_SQLITE_INTEGER, strict=True
    )
    notes: Optional[str] = Field(None, max_length=500)


class UserUpdate(BaseModel):
    email: Optional[str] = None
    is_active: Optional[bool] = None
    rpm_limit: Optional[int] = Field(None, ge=1, le=1000, strict=True)
    monthly_token_limit: Optional[int] = Field(
        None, ge=0, le=MAX_SQLITE_INTEGER, strict=True
    )
    notes: Optional[str] = Field(None, max_length=500)


class UserResponse(BaseModel):
    id: int
    username: str
    email: Optional[str]
    created_at: str
    is_active: bool
    rpm_limit: int
    monthly_token_limit: int
    notes: Optional[str]
    # Non NULL = compte anonymisé (RGPD, DEC-001) : permet de le distinguer d'un
    # compte simplement désactivé. `username` est alors un pseudonyme, `email` et
    # `notes` sont définitivement effacés.
    anonymized_at: Optional[str] = None


class UserAnonymizeResponse(BaseModel):
    """
    Réponse de `DELETE /admin/users/{username}`.

    La route porte le verbe `DELETE` pour compatibilité, mais son effet est une
    anonymisation irréversible : ce corps de réponse énumère explicitement ce qui
    est effacé et ce qui est conservé, pour qu'aucun opérateur ne puisse croire à
    une suppression de ligne.
    """
    status: Literal["anonymized", "already_anonymized"]
    message: str
    user_id: int
    anonymized_username: str = Field(
        ..., description="Pseudonyme stable qui remplace le nom d'utilisateur effacé."
    )
    anonymized_at: str
    keys_revoked: int = Field(..., description="Clés encore actives révoquées par cet appel.")
    keys_total: int = Field(..., description="Clés attachées au compte, toutes révoquées.")
    erased_fields: list[str]
    retained: list[str]


# ── Clés API ──────────────────────────────────────────────────────────────────

class KeyCreate(BaseModel):
    name: Optional[str] = Field(None, max_length=64)
    expires_at: Optional[str] = None  # ISO 8601

    @field_validator("expires_at", mode="before")
    @classmethod
    def validate_expiry(cls, v: object) -> str | None:
        if v is None or v == "":
            return None
        if isinstance(v, datetime):
            dt = v
        else:
            try:
                dt = datetime.fromisoformat(str(v))
            except ValueError:
                raise ValueError(
                    "expires_at doit être une date ISO 8601 valide "
                    "(ex: 2026-12-31 ou 2026-12-31T23:59:59+00:00)"
                )
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()


class KeyCreateResponse(BaseModel):
    """
    Retournée une seule fois à la création.
    api_key est la clé brute — après ça, elle est perdue côté serveur.
    """
    api_key: str = Field(..., description="Clé API brute — à sauvegarder maintenant, non récupérable ensuite.")
    key_prefix: str
    name: Optional[str]
    created_at: str
    expires_at: Optional[str]


class KeyResponse(BaseModel):
    """Sans la clé brute — pour lister les clés."""
    id: int
    key_prefix: str
    name: Optional[str]
    created_at: str
    last_used: Optional[str]
    is_active: bool
    expires_at: Optional[str]


# ── Usage / reporting ─────────────────────────────────────────────────────────

class UsageEntry(BaseModel):
    id: int
    timestamp: str
    username: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    duration_ms: Optional[int]
    status_code: Optional[int]
    request_id: Optional[str]


class UsageSummaryEntry(BaseModel):
    username: str
    request_count: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    avg_duration_ms: Optional[float]
    first_request: Optional[str]
    last_request: Optional[str]


# ── Registre des modèles ──────────────────────────────────────────────────────

class LlamaParamsSchema(BaseModel):
    """Paramètres llama-server configurables par modèle."""
    model_config = ConfigDict(strict=True, extra="forbid")

    n_gpu_layers: int = Field(999, ge=0)
    ctx_size: int = Field(32768, ge=512)
    parallel: int = Field(4, ge=1)
    batch_size: int = Field(4096, ge=1)
    ubatch_size: int = Field(512, ge=1)
    cache_type_k: Literal["f16", "bf16", "q8_0", "q5_0", "q4_0"] = "q8_0"
    cache_type_v: Literal["f16", "bf16", "q8_0", "q5_0", "q4_0"] = "q8_0"
    flash_attn: bool = True
    threads: int = Field(8, ge=1)
    threads_http: int = Field(4, ge=1)
    cpu_moe: bool = False  # Déporte les experts FFN des modèles MoE sur CPU

    @field_validator("ubatch_size")
    @classmethod
    def ubatch_lte_batch(cls, v: int, info) -> int:
        batch = (info.data or {}).get("batch_size", v)
        if v > batch:
            raise ValueError(f"ubatch_size ({v}) doit être ≤ batch_size ({batch})")
        return v


class SpeculativeParamsSchema(BaseModel):
    """Bloc MTP optionnel, avec les mêmes bornes que le registre YAML."""
    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["mtp"] = "mtp"
    draft_max: int = Field(16, ge=1)
    draft_min: int = Field(0, ge=0)
    draft_p_min: float = Field(0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def draft_min_lte_max(self):
        if self.draft_min > self.draft_max:
            raise ValueError(
                f"draft_min ({self.draft_min}) doit être ≤ draft_max ({self.draft_max})"
            )
        return self


_SHA256_SCHEMA_RE = re.compile(r"^[0-9a-f]{64}$")


def _normalize_sha256(value: object) -> str | None:
    """Aligne les champs d'empreinte admin sur le parseur du registre."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("l'empreinte SHA-256 doit être une chaîne")
    normalized = value.strip().lower()
    if not _SHA256_SCHEMA_RE.fullmatch(normalized):
        raise ValueError("l'empreinte SHA-256 doit contenir 64 caractères hexadécimaux")
    return normalized


class ModelEntryCreate(BaseModel):
    """Corps de requête pour POST /admin/models."""
    model_config = ConfigDict(strict=True, extra="forbid")

    id: str = Field(
        ...,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$",
        description="Identifiant unique du modèle (minuscules, chiffres, tirets, points, underscores)",
    )
    path: str = Field(..., description="Chemin absolu vers le fichier .gguf sur le serveur")
    description: str = Field("", max_length=200)
    vram_gb: float = Field(..., gt=0.0, description="VRAM estimée en GB (poids + KV cache à charge nominale)")
    enabled: bool = True
    capabilities: list[str] = Field(
        default_factory=lambda: ["text_generation"], min_length=1
    )
    llama_params: LlamaParamsSchema = Field(default_factory=LlamaParamsSchema)
    mmproj_path: Optional[str] = None
    mmproj_sha256: Optional[str] = None
    load_timeout_seconds: Optional[int] = Field(None, ge=30)
    speculative: Optional[SpeculativeParamsSchema] = None
    sha256: Optional[str] = None

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        if any(not capability for capability in value):
            raise ValueError("capabilities doit contenir uniquement des chaînes non vides")
        unknown = sorted(set(value) - MODEL_CAPABILITIES)
        if unknown:
            raise ValueError(
                f"capabilities inconnues : {unknown}. "
                f"Valeurs admises : {sorted(MODEL_CAPABILITIES)}"
            )
        return value

    @field_validator("sha256", "mmproj_sha256", mode="before")
    @classmethod
    def validate_sha256(cls, value: object) -> str | None:
        return _normalize_sha256(value)

    @model_validator(mode="after")
    def validate_vision_artifacts(self):
        if "vision" in self.capabilities:
            if self.mmproj_path is None:
                raise ValueError(
                    "la capability 'vision' exige un projecteur multimodal (mmproj_path)"
                )
            if self.mmproj_sha256 is None:
                raise ValueError(
                    "la capability 'vision' exige un projecteur multimodal (mmproj_sha256)"
                )
        elif self.mmproj_sha256 is not None and self.mmproj_path is None:
            raise ValueError("mmproj_sha256 ne peut pas être utilisé sans mmproj_path")
        return self


class ModelEntryUpdate(BaseModel):
    """
    Corps de requête pour PATCH /admin/models/{model_id}.

    llama_params — remplacement complet du bloc (pas de merge partiel).
    Si fourni, le modèle est déchargé et rechargé à la prochaine requête
    pour prendre en compte les nouveaux paramètres de lancement.
    """
    model_config = ConfigDict(strict=True, extra="forbid")

    enabled: Optional[bool] = None
    vram_gb: Optional[float] = Field(None, gt=0.0)
    description: Optional[str] = Field(None, max_length=200)
    llama_params: Optional[LlamaParamsSchema] = None


class BootstrapModelSync(BaseModel):
    """Transition live étroite du bootstrap, sans aucune écriture YAML."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["activate", "rollback", "confirm"]
    digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    vram_gb: Optional[float] = Field(None, gt=0.0, strict=True)
    lease_seconds: Optional[int] = Field(None, ge=30, le=3600, strict=True)

    @model_validator(mode="after")
    def validate_action_fields(self):
        if self.action == "activate" and self.vram_gb is None:
            raise ValueError("vram_gb est obligatoire pour action=activate")
        if self.action == "activate" and self.lease_seconds is None:
            raise ValueError("lease_seconds est obligatoire pour action=activate")
        if self.action != "activate" and self.vram_gb is not None:
            raise ValueError("vram_gb n'est admis que pour action=activate")
        if self.action != "activate" and self.lease_seconds is not None:
            raise ValueError("lease_seconds n'est admis que pour action=activate")
        return self


# ── Statut système multi-modèles ──────────────────────────────────────────────

class ModelStatusResponse(BaseModel):
    """État live d'un modèle (chargé ou non)."""
    id: str
    description: str
    enabled: bool
    vram_gb: float
    capabilities: list[str]
    state: str  # "unloaded" | "loading" | "ready" | "unloading"
    path: str
    pid: Optional[int]
    port: Optional[int]
    uptime_seconds: Optional[float]
    idle_seconds: Optional[float]
    # Durée du dernier chargement réussi du modèle, en secondes. Mesurée côté
    # gateway en local ; côté orchestrateur (RPC réseau inclus) en cluster.
    # None si le modèle n'a jamais atteint « ready » — ou en cluster si le nœud
    # l'avait déjà chargé au moment du placement (durée d'origine inconnue).
    last_load_seconds: Optional[float] = None
    llama_params: Optional[dict]
    # Cluster uniquement : nœud d'hébergement et charge live. `llama_url` reste
    # volontairement hors de ce schéma (détail d'infra interne) — il n'est
    # exposé que par GET /admin/cluster.
    node: Optional[str] = None
    active_requests: Optional[int] = None


class GpuVramSampleResponse(BaseModel):
    """Mesure d'un GPU local, conservant son identité UUID."""

    index: Optional[int] = None
    uuid: str
    name: str = ""
    memory_used_mb: Optional[float] = None
    memory_total_mb: Optional[float] = None
    memory_used_bytes: Optional[int] = None
    memory_total_bytes: Optional[int] = None
    driver_version: str = ""
    compute_capability: str = ""
    mig_mode_current: str = ""
    visible: bool


class GpuVramMeasurementResponse(BaseModel):
    """Résultat de la sonde GPU, y compris l'état ``unavailable``."""

    status: Literal["measured", "unavailable"]
    reason: str
    measured_at: Optional[str] = None
    cuda_visible_devices: Optional[str] = None
    visible_uuids: list[str] = Field(default_factory=list)
    devices: list[GpuVramSampleResponse] = Field(default_factory=list)
    visible_used_mb: Optional[float] = None
    visible_total_mb: Optional[float] = None
    detail: Optional[str] = None


class VramBudgetResponse(BaseModel):
    """
    Budget VRAM global de la gateway — commun aux modes local et cluster.

    Seuls `total_gb`, `used_gb` et `available_gb` ont un sens dans les deux
    modes et restent donc obligatoires.

    - `overhead_gb` / `budget_net_gb` : toujours fournis (config de l'hôte en
      local, agrégation des nœuds ONLINE en cluster). Optionnels au niveau du
      schéma pour qu'un manager dégradé produise un statut partiel plutôt
      qu'une ResponseValidationError (→ 500) sur une route d'observabilité.
    - `safety_margin` : ratio de configuration mono-hôte. Absent en cluster, où
      la réserve des nœuds est déjà agrégée en GB dans `overhead_gb`.
    - `nodes` / `nodes_online` : cluster uniquement.
    - `gpu_measurement` : état et inventaire par UUID de la dernière sonde
      locale. Le statut `unavailable` est conservé explicitement ; les agrégats
      ne comptent que les UUID exposés par `CUDA_VISIBLE_DEVICES`.
    - `gpu_used_mb_measured` / `vram_drift_mb` : compatibilité locale, seulement
      si une sonde nvidia-smi a réussi.
    """
    total_gb: float
    overhead_gb: Optional[float] = None
    safety_margin: Optional[float] = None
    used_gb: float
    available_gb: float
    budget_net_gb: Optional[float] = None
    # Cluster uniquement — sans ces champs, /admin/status masquerait la
    # topologie (ils étaient silencieusement supprimés par le response model).
    nodes: Optional[int] = None
    nodes_online: Optional[int] = None
    # Local uniquement — réconciliation nvidia-smi consommée par le dashboard.
    gpu_used_mb_measured: Optional[float] = None
    vram_drift_mb: Optional[float] = None
    gpu_measurement: Optional[GpuVramMeasurementResponse] = None


class CapacityQueueStatusResponse(BaseModel):
    """État de la queue d'admission VRAM locale."""
    enabled: bool
    waiters: int
    max_waiters: int
    timeout_seconds: int


class GatewayStatus(BaseModel):
    """Statut complet de la gateway — retourné par GET /admin/status."""
    status: str
    vram_budget: VramBudgetResponse
    models: list[ModelStatusResponse]
    capacity_queue: Optional[CapacityQueueStatusResponse] = None
