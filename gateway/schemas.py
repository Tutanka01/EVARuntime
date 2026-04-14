"""
Schémas Pydantic pour les endpoints admin.
Les schémas OpenAI (chat/completions) sont proxiés tels quels vers llama-server,
on ne les revalide pas pour éviter de casser la compatibilité avec les futurs champs.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ── Utilisateurs ──────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    username: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    email: Optional[str] = None
    rpm_limit: Optional[int] = Field(None, ge=1, le=1000)
    monthly_token_limit: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = Field(None, max_length=500)


class UserUpdate(BaseModel):
    email: Optional[str] = None
    is_active: Optional[bool] = None
    rpm_limit: Optional[int] = Field(None, ge=1, le=1000)
    monthly_token_limit: Optional[int] = Field(None, ge=0)
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


# ── Clés API ──────────────────────────────────────────────────────────────────

class KeyCreate(BaseModel):
    name: Optional[str] = Field(None, max_length=64)
    expires_at: Optional[str] = None  # ISO 8601

    @field_validator("expires_at", mode="before")
    @classmethod
    def validate_expiry(cls, v: object) -> str | None:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.isoformat()
        return str(v)


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


class ModelEntryCreate(BaseModel):
    """Corps de requête pour POST /admin/models."""
    id: str = Field(
        ...,
        pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$",
        description="Identifiant unique du modèle (minuscules, chiffres, tirets, points, underscores)",
    )
    path: str = Field(..., description="Chemin absolu vers le fichier .gguf sur le serveur")
    description: str = Field("", max_length=200)
    vram_gb: float = Field(..., gt=0.0, description="VRAM estimée en GB (poids + KV cache à charge nominale)")
    enabled: bool = True
    capabilities: list[str] = Field(default_factory=lambda: ["text_generation"])
    llama_params: LlamaParamsSchema = Field(default_factory=LlamaParamsSchema)


class ModelEntryUpdate(BaseModel):
    """
    Corps de requête pour PATCH /admin/models/{model_id}.

    llama_params — remplacement complet du bloc (pas de merge partiel).
    Si fourni, le modèle est déchargé et rechargé à la prochaine requête
    pour prendre en compte les nouveaux paramètres de lancement.
    """
    enabled: Optional[bool] = None
    vram_gb: Optional[float] = Field(None, gt=0.0)
    description: Optional[str] = Field(None, max_length=200)
    llama_params: Optional[LlamaParamsSchema] = None


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
    llama_params: Optional[dict]


class VramBudgetResponse(BaseModel):
    """Budget VRAM global de la gateway."""
    total_gb: float
    overhead_gb: float
    safety_margin: float
    used_gb: float
    available_gb: float
    budget_net_gb: float


class GatewayStatus(BaseModel):
    """Statut complet de la gateway — retourné par GET /admin/status."""
    status: str
    vram_budget: VramBudgetResponse
    models: list[ModelStatusResponse]
