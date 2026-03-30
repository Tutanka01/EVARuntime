"""
Schémas Pydantic pour les endpoints admin.
Les schémas OpenAI (chat/completions) sont proxiés tels quels vers llama-server,
on ne les revalide pas pour éviter de casser la compatibilité avec les futurs champs.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


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
        # Accepter datetime ou string ISO
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


# ── Statut système ────────────────────────────────────────────────────────────

class GatewayStatus(BaseModel):
    status: str
    model_state: str
    model_name: str
    model_path: str
    pid: Optional[int]
    uptime_seconds: Optional[float]
    idle_seconds: Optional[float]
    idle_timeout_seconds: int
    llama_params: dict
