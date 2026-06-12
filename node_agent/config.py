"""
Configuration de l'agent nœud — chargée depuis variables d'environnement ou .env.

L'agent est un processus autonome sur chaque DGX Spark. Il reçoit ses paramètres
via l'environnement (systemd EnvironmentFile) et n'a PAS besoin d'accéder au .env
de l'orchestrateur — les deux processus sont indépendants.

Paramètres clés :
  AGENT_SECRET   : bearer secret partagé avec l'orchestrateur (identique des deux côtés)
  TOTAL_VRAM_GB  : capacité mémoire unifiée du nœud (GB) — pour GB10, ~120
  NODE_ID        : identifiant unique de ce nœud (doit correspondre à nodes.yaml)
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Identité du nœud ──────────────────────────────────────────────────────
    node_id: str = Field(
        default="node-a",
        description="Identifiant de ce nœud — doit correspondre à nodes.yaml",
    )

    # ── Réseau de l'agent ─────────────────────────────────────────────────────
    agent_host: str = "0.0.0.0"
    agent_port: int = 9443

    # TLS — en prod, pointer vers le certificat et la clé privée
    agent_tls_cert: Path | None = None
    agent_tls_key: Path | None = None

    # ── Sécurité ───────────────────────────────────────────────────────────────
    agent_secret: str = "CHANGE_ME_AGENT_SECRET"

    # ── llama-server ──────────────────────────────────────────────────────────
    llama_server_bin: Path = Path("/usr/local/bin/llama-server")
    llama_server_host: str = "127.0.0.1"
    base_llama_port: int = 8081
    max_loaded_models: int = 5

    # ── Mémoire GPU (unifiée sur GB10) ────────────────────────────────────────
    # Sur GB10 128 GB physiques : nvidia-smi rapporte ~122 GiB → 120 GB net
    total_vram_gb: float = 120.0
    vram_overhead_gb: float = 4.0   # CUDA 13 + Grace OS + frameworks
    vram_safety_margin: float = 0.03

    # ── Lifecycle modèle ───────────────────────────────────────────────────────
    idle_timeout_seconds: int = 300
    model_load_timeout_seconds: int = 180
    idle_check_interval_seconds: int = 30

    # ── Sécurité des chemins .gguf ────────────────────────────────────────────
    allowed_model_dirs: list[str] = Field(default_factory=list)

    # ── Clé interne gateway ↔ llama-server ───────────────────────────────────
    # Cette clé est différente de agent_secret : elle protège le canal
    # orchestrateur → llama-server (pas orchestrateur → agent).
    # L'orchestrateur la reçoit dans LoadResponse et la présente à llama-server.
    internal_api_key: str = "CHANGE_ME_INTERNAL_KEY"

    # ── GPU ────────────────────────────────────────────────────────────────────
    cuda_visible_devices: str = "0"

    # ── Logging ────────────────────────────────────────────────────────────────
    log_dir: Path = Path("/var/log/llm-gateway-agent")
    db_path: Path = Path(":memory:")  # pas de SQLite persistant côté agent

    @field_validator("vram_safety_margin")
    @classmethod
    def _validate_margin(cls, v: float) -> float:
        if not 0.0 <= v < 1.0:
            raise ValueError(f"vram_safety_margin doit être dans [0, 1), reçu : {v}")
        return v

    @field_validator("max_loaded_models")
    @classmethod
    def _validate_max_models(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"max_loaded_models doit être ≥ 1, reçu : {v}")
        return v

    def effective_vram_budget_gb(self) -> float:
        """Budget VRAM net disponible pour les modèles (après overhead et marge)."""
        return (
            self.total_vram_gb
            - self.vram_overhead_gb
            - self.total_vram_gb * self.vram_safety_margin
        )

    def agent_secret_is_placeholder(self) -> bool:
        """True si AGENT_SECRET est vide ou laissé à sa valeur d'exemple."""
        return not self.agent_secret or self.agent_secret.strip().upper().startswith("CHANGE_ME")


settings = AgentSettings()
