from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_DEFAULT_SECRETS: frozenset[str] = frozenset({
    "CHANGE_ME_INTERNAL_STUDENT_EDGE_KEY",
    "CHANGE_ME_AUDIT_HMAC_SECRET",
})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "llm-gateway-student"
    db_path: Path = Path("/var/lib/llm-gateway-student/students.db")

    upstream_base_url: str = "https://llm-internal.eva.univ-pau.fr"
    upstream_api_key: str = "CHANGE_ME_INTERNAL_STUDENT_EDGE_KEY"
    upstream_ca_path: Path | None = Path("/etc/llm-gateway-student/ca-uppa.pem")
    upstream_client_cert_path: Path | None = Path("/etc/llm-gateway-student/gw-student.crt")
    upstream_client_key_path: Path | None = Path("/etc/llm-gateway-student/gw-student.key")

    allowed_models: list[str] = Field(default_factory=lambda: ["llama-3.1-8b-instruct", "qwen-9b"])
    default_model_id: str = "llama-3.1-8b-instruct"

    default_rpm_limit: int = 10
    default_daily_token_limit: int = 100_000
    default_hourly_token_limit: int = 20_000   # 0 = désactivé
    default_concurrent_stream_limit: int = 1

    # Burst limiter : couche courte commune à tous les étudiants
    burst_limit: int = 3           # max requêtes dans la fenêtre burst
    burst_window_seconds: int = 10 # durée de la fenêtre burst (secondes)

    max_body_bytes: int = 64 * 1024
    max_prompt_chars: int = 32 * 1024
    max_message_chars: int = 8 * 1024
    max_messages: int = 32
    max_tools_bytes: int = 16 * 1024
    max_completion_tokens: int = 2048
    max_stop_sequences: int = 4        # nombre max de séquences stop (aligné OpenAI)
    max_stop_sequence_chars: int = 64  # longueur max d'une séquence stop

    # Estimation du volume complété en cas de coupure de stream (pas de chunk usage).
    # ~4 caractères par token pour l'anglais/français ; imputé au quota si l'usage exact manque.
    est_chars_per_token: int = 4

    audit_hmac_secret: str = "CHANGE_ME_AUDIT_HMAC_SECRET"
    audit_log_path: Path = Path("/var/log/llm-gateway-student/audit.jsonl")

    @field_validator("upstream_api_key", "audit_hmac_secret")
    @classmethod
    def validate_not_default_secret(cls, value: str) -> str:
        # Rejet par préfixe : bloque toute valeur placeholder (y compris les
        # exemples de deploy/env.example), pas seulement les défauts exacts.
        if value in _DEFAULT_SECRETS or value.startswith("CHANGE_ME"):
            raise ValueError("Secret non remplacé depuis la valeur par défaut — voir deploy/env.example")
        if len(value) < 32:
            raise ValueError("Secret trop court (minimum 32 caractères requis)")
        return value

    @field_validator("db_path", "upstream_ca_path", "upstream_client_cert_path", "upstream_client_key_path", mode="before")
    @classmethod
    def coerce_path(cls, value: object) -> Path | None:
        if value in (None, ""):
            return None
        return Path(str(value))

    @field_validator("allowed_models", mode="before")
    @classmethod
    def split_models(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value  # type: ignore[return-value]

    @field_validator("upstream_base_url")
    @classmethod
    def validate_upstream_base_url(cls, value: str) -> str:
        cleaned = value.rstrip("/")
        if not cleaned.startswith("https://"):
            raise ValueError("UPSTREAM_BASE_URL doit utiliser https://")
        return cleaned

    def upstream_verify(self) -> str | bool:
        return str(self.upstream_ca_path) if self.upstream_ca_path else True

    def upstream_cert(self) -> tuple[str, str] | None:
        if self.upstream_client_cert_path and self.upstream_client_key_path:
            return (str(self.upstream_client_cert_path), str(self.upstream_client_key_path))
        return None


settings = Settings()

