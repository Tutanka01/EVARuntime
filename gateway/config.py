"""
Configuration — chargée depuis variables d'environnement ou fichier .env.
Toutes les valeurs sensibles (clés, chemins) vivent dans /etc/llm-gateway/env,
jamais dans le code source.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def secret_is_placeholder(secret: str) -> bool:
    """True si un secret est vide ou laissé à sa valeur d'exemple CHANGE_ME_*."""
    return not secret or secret.strip().upper().startswith("CHANGE_ME")


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

    # ── Queue d'admission VRAM ────────────────────────────────────────────────
    # Quand un modèle ne peut pas être chargé car la VRAM/les ports sont
    # temporairement occupés par des requêtes actives, attendre au lieu de
    # retourner immédiatement 503. La queue reste bornée pour éviter l'abus.
    capacity_queue_enabled: bool = True
    capacity_queue_timeout_seconds: int = 120
    capacity_queue_max_waiters: int = 100
    capacity_queue_retry_after_seconds: int = 10

    # ── Sécurité ───────────────────────────────────────────────────────────────
    # Clé interne entre la gateway et llama-server (jamais exposée aux users)
    internal_api_key: str = "CHANGE_ME_INTERNAL_KEY"
    # Secret pour les endpoints /admin (en plus du filtrage IP)
    admin_secret: str = "CHANGE_ME_ADMIN_SECRET"

    # ── Gateway réseau ─────────────────────────────────────────────────────────
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8000

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Origines autorisées, séparées par des virgules.
    # "*" (défaut) convient en dev ; en production, restreindre aux domaines
    # clients connus : CORS_ALLOW_ORIGINS=https://app.univ-pau.fr
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"])

    # ── Rate limiting par défaut ───────────────────────────────────────────────
    default_rpm_limit: int = 20
    # 0 = quota mensuel illimité
    default_monthly_token_limit: int = 0

    # ── GPU ────────────────────────────────────────────────────────────────────
    cuda_visible_devices: str = "0"

    # ── Cluster multi-nœuds (opt-in avancé) ───────────────────────────────────
    # local   : comportement historique — la gateway lance llama-server localement
    #           (mode par défaut, rétro-compatible avec tous les déploiements existants)
    # cluster : la gateway pilote N agents distants via HTTPS, lit cluster_nodes_path
    cluster_mode: Literal["local", "cluster"] = "local"

    # Fichier YAML décrivant les nœuds GPU pilotés en mode cluster
    cluster_nodes_path: Path = Path("/etc/llm-gateway/nodes.yaml")

    # Secret bearer partagé orchestrateur ↔ agents (même valeur sur tous les agents)
    # Utilisé uniquement quand cluster_mode=cluster
    agent_secret: str = "CHANGE_ME_AGENT_SECRET"

    # Plan de contrôle (load/unload/health) — timeout court
    cluster_request_timeout: float = 10.0
    # Heartbeat — intervalle entre deux GET /agent/health par nœud
    cluster_health_interval: int = 10
    # Échecs consécutifs avant de marquer un nœud offline
    cluster_health_failures_to_offline: int = 3

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

    @field_validator(
        "capacity_queue_timeout_seconds",
        "capacity_queue_max_waiters",
        "capacity_queue_retry_after_seconds",
    )
    @classmethod
    def validate_capacity_queue_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"Valeur capacity_queue doit être ≥ 1, reçu : {v}")
        return v

    @field_validator("cluster_nodes_path", mode="before")
    @classmethod
    def coerce_cluster_path(cls, v: object) -> Path:
        return Path(str(v))

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def split_cors_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @field_validator("cluster_health_interval", "cluster_health_failures_to_offline")
    @classmethod
    def validate_cluster_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"Valeur cluster doit être ≥ 1, reçu : {v}")
        return v

    def effective_vram_budget_gb(self) -> float:
        """Budget VRAM net disponible pour les modèles (après overhead et marge)."""
        return self.total_vram_gb - self.vram_overhead_gb - (self.total_vram_gb * self.vram_safety_margin)

    def admin_secret_is_placeholder(self) -> bool:
        return secret_is_placeholder(self.admin_secret)

    def internal_api_key_is_placeholder(self) -> bool:
        return secret_is_placeholder(self.internal_api_key)

    def agent_secret_is_placeholder(self) -> bool:
        return secret_is_placeholder(self.agent_secret)


# Instance globale — importée partout dans l'application
settings = Settings()
