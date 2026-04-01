"""
Configuration — chargée depuis variables d'environnement ou fichier .env.
Toutes les valeurs sensibles (clés, chemins) vivent dans /etc/llm-gateway/env,
jamais dans le code source.
"""
from __future__ import annotations

from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Chemins ────────────────────────────────────────────────────────────────
    models_config_path: Path = Path("/var/lib/llm-gateway/models.yaml")
    llama_server_bin: Path = Path("/usr/local/bin/llama-server")
    db_path: Path = Path("/var/lib/llm-gateway/gateway.db")
    log_dir: Path = Path("/var/log/llm-gateway")

    # ── llama-server réseau ────────────────────────────────────────────────────
    # Hôte partagé par tous les sous-processus llama-server
    llama_server_host: str = "127.0.0.1"

    # ── Pool de ports multi-modèles ────────────────────────────────────────────
    # Chaque llama-server chargé consomme un port du pool
    # Pool : base_llama_port … base_llama_port + max_loaded_models - 1
    base_llama_port: int = 8081
    max_loaded_models: int = 5

    # ── Budget VRAM (L40S 48 GB par défaut) ───────────────────────────────────
    # Ajuster selon le GPU : nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits
    total_vram_gb: float = 48.0
    # Réservé pour le contexte CUDA, le framework, et les allocateurs
    vram_overhead_gb: float = 2.0
    # Marge de sécurité supplémentaire (fraction de total_vram_gb)
    vram_safety_margin: float = 0.05

    # ── Modèle par défaut ─────────────────────────────────────────────────────
    # Utilisé quand le client ne précise pas de champ "model" dans sa requête.
    # Laisser vide ("") pour utiliser automatiquement le premier modèle activé du registre.
    default_model_id: str = ""

    # ── Répertoires autorisés pour les fichiers .gguf ─────────────────────────
    # Liste séparée par des virgules. Vide = pas de restriction (tous répertoires autorisés).
    # Exemple : ALLOWED_MODEL_DIRS=/models,/data/models
    allowed_model_dirs: list[str] = Field(default_factory=list)

    # ── Lifecycle modèle ───────────────────────────────────────────────────────
    idle_timeout_seconds: int = 300
    model_load_timeout_seconds: int = 180
    idle_check_interval_seconds: int = 30

    # ── Sécurité ───────────────────────────────────────────────────────────────
    # Clé interne entre la gateway et llama-server (jamais exposée aux users)
    internal_api_key: str = "CHANGE_ME_INTERNAL_KEY"
    # Secret pour les endpoints /admin (en plus du filtrage IP)
    admin_secret: str = "CHANGE_ME_ADMIN_SECRET"

    # ── Gateway réseau ─────────────────────────────────────────────────────────
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8000

    # ── Rate limiting par défaut ───────────────────────────────────────────────
    default_rpm_limit: int = 20
    # 0 = quota mensuel illimité
    default_monthly_token_limit: int = 0

    # ── GPU ────────────────────────────────────────────────────────────────────
    cuda_visible_devices: str = "0"

    @field_validator("models_config_path", "llama_server_bin", mode="before")
    @classmethod
    def coerce_path(cls, v: object) -> Path:
        return Path(str(v))

    @field_validator("vram_safety_margin")
    @classmethod
    def validate_safety_margin(cls, v: float) -> float:
        if not 0.0 <= v < 1.0:
            raise ValueError(f"vram_safety_margin doit être dans [0, 1), reçu : {v}")
        return v

    @field_validator("max_loaded_models")
    @classmethod
    def validate_max_models(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"max_loaded_models doit être ≥ 1, reçu : {v}")
        return v

    def effective_vram_budget_gb(self) -> float:
        """Budget VRAM net disponible pour les modèles (après overhead et marge)."""
        return self.total_vram_gb - self.vram_overhead_gb - (self.total_vram_gb * self.vram_safety_margin)


# Instance globale — importée partout dans l'application
settings = Settings()
