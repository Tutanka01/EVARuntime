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

import ipaddress
import json
import math
import re
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


MAX_PORT = 65535


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
    agent_port: int = Field(default=9443, ge=1, le=65535)

    # TLS — en prod, pointer vers le certificat et la clé privée
    agent_tls_cert: Path | None = Path("/etc/llm-gateway-agent/tls/agent.crt")
    agent_tls_key: Path | None = Path("/etc/llm-gateway-agent/tls/agent.key")

    # ── Sécurité ───────────────────────────────────────────────────────────────
    agent_secret: str = "CHANGE_ME_AGENT_SECRET"

    # ── llama-server ──────────────────────────────────────────────────────────
    llama_server_bin: Path = Path("/opt/llama.cpp/current/llama-server")
    # Bind data-plane : doit être joignable depuis l'orchestrateur. Les sondes
    # locales utilisent loopback pour ne pas dépendre du routage vers 0.0.0.0.
    llama_server_host: str = "0.0.0.0"
    llama_server_health_host: str = "127.0.0.1"
    base_llama_port: int = Field(default=8081, ge=1, le=65535)
    max_loaded_models: int = 5

    # ── Épinglage de version llama-server (mitigation supply-chain) ────────────
    # Build minimal accepté du binaire llama-server. 0 = pas d'enforcement (défaut).
    # Recommandé : fixer au premier build patché contre GHSA-8947-pfff-2f3c
    # (écriture OOB via n_discard/context-shift) et les overflows de parsing GGUF.
    # Si > 0 et que le binaire lu est plus ancien ou illisible, le démarrage de
    # l'agent est REFUSÉ (fail-closed).
    llama_server_min_build: int = Field(default=0, ge=0)

    # ── Mémoire GPU (unifiée sur GB10) ────────────────────────────────────────
    # Sur GB10 128 GB physiques : nvidia-smi rapporte ~122 GiB → 120 GB net
    total_vram_gb: float = Field(default=120.0, gt=0)
    vram_overhead_gb: float = Field(default=4.0, ge=0)  # CUDA + OS + frameworks
    vram_safety_margin: float = 0.03

    # ── Lifecycle modèle ───────────────────────────────────────────────────────
    idle_timeout_seconds: int = Field(default=300, gt=0)
    model_load_timeout_seconds: int = Field(default=180, gt=0)
    idle_check_interval_seconds: int = Field(default=30, gt=0)

    # ── Sécurité des chemins .gguf ────────────────────────────────────────────
    # L'union conserve la syntaxe CSV de l'EnvironmentFile : une annotation
    # `list[str]` seule ferait décoder la source comme JSON avant le validator.
    allowed_model_dirs: str | list[str] = Field(default_factory=list)

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

    @field_validator(
        "agent_port",
        "base_llama_port",
        "max_loaded_models",
        "llama_server_min_build",
        "total_vram_gb",
        "vram_overhead_gb",
        "vram_safety_margin",
        "idle_timeout_seconds",
        "model_load_timeout_seconds",
        "idle_check_interval_seconds",
        mode="before",
    )
    @classmethod
    def _reject_boolean_numeric_settings(cls, v: object) -> object:
        """Refuse `True`/`False` silently coerced to 1/0 par Pydantic."""
        if isinstance(v, bool):
            raise ValueError("une valeur booléenne n'est pas un nombre de configuration")
        return v

    @field_validator("vram_safety_margin")
    @classmethod
    def _validate_margin(cls, v: float) -> float:
        if not math.isfinite(v) or not 0.0 <= v < 1.0:
            raise ValueError(f"vram_safety_margin doit être dans [0, 1), reçu : {v}")
        return v

    @field_validator("total_vram_gb")
    @classmethod
    def _validate_total_vram(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"total_vram_gb doit être un nombre fini > 0, reçu : {v}")
        return v

    @field_validator("vram_overhead_gb")
    @classmethod
    def _validate_vram_overhead(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"vram_overhead_gb doit être un nombre fini ≥ 0, reçu : {v}")
        return v

    @field_validator("max_loaded_models")
    @classmethod
    def _validate_max_models(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"max_loaded_models doit être ≥ 1, reçu : {v}")
        return v

    @field_validator("node_id")
    @classmethod
    def _validate_node_id(cls, v: str) -> str:
        v = v.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", v):
            raise ValueError(
                "node_id doit contenir 1 à 63 caractères minuscules, chiffres, '.', '_' ou '-'"
            )
        return v

    @field_validator("agent_host", "llama_server_host", "llama_server_health_host")
    @classmethod
    def _validate_bind_host(cls, v: str) -> str:
        value = v.strip()
        if not value or any(ch.isspace() for ch in value) or "://" in value or "/" in value:
            raise ValueError(f"hôte invalide : {v!r}")
        # Accepte les IP (IPv4/IPv6) et les noms DNS simples sans effectuer de
        # résolution réseau au démarrage.
        try:
            ipaddress.ip_address(value)
        except ValueError:
            if not re.fullmatch(
                r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
                value,
            ):
                raise ValueError(f"hôte invalide : {v!r}")
        return value

    @field_validator("allowed_model_dirs", mode="before")
    @classmethod
    def _split_allowed_model_dirs(cls, v: object) -> object:
        """Normalise la valeur d'environnement CSV ou tableau JSON."""
        if not isinstance(v, str):
            return v
        raw = v.strip()
        if not raw:
            return []
        if raw.startswith("{"):
            raise ValueError(
                "ALLOWED_MODEL_DIRS : attendu une liste CSV ou un tableau JSON, pas un objet"
            )
        if raw.startswith("["):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("ALLOWED_MODEL_DIRS JSON invalide") from exc
            if not isinstance(decoded, list) or not all(isinstance(v, str) for v in decoded):
                raise ValueError("ALLOWED_MODEL_DIRS JSON doit être une liste de chaînes")
            return [item.strip() for item in decoded if item.strip()]
        return [item.strip() for item in raw.split(",") if item.strip()]

    def allowed_model_dirs_list(self) -> list[str]:
        """Normalise ALLOWED_MODEL_DIRS depuis valeur simple, CSV ou JSON."""
        values = self.allowed_model_dirs
        if isinstance(values, str):
            values = self._split_allowed_model_dirs(values)
        if not isinstance(values, list):
            raise ValueError("ALLOWED_MODEL_DIRS doit être une liste de chaînes")

        normalized: list[str] = []
        for raw in values:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                raise ValueError(f"allowed_model_dirs doit contenir des chemins absolus : {raw!r}")
            normalized.append(str(path.resolve(strict=False)))
        return normalized

    @model_validator(mode="after")
    def _validate_capacity_and_ports(self) -> "AgentSettings":
        # Force la validation du format même si aucun modèle n'est encore chargé.
        self.allowed_model_dirs_list()
        errors: list[str] = []
        if self.vram_overhead_gb >= self.total_vram_gb:
            errors.append("VRAM_OVERHEAD_GB doit être strictement inférieur à TOTAL_VRAM_GB")
        budget = self.effective_vram_budget_gb()
        if not math.isfinite(budget) or budget <= 0:
            errors.append("le budget VRAM net doit être strictement positif")
        last_llama_port = self.base_llama_port + self.max_loaded_models - 1
        if last_llama_port > MAX_PORT:
            errors.append("la plage BASE_LLAMA_PORT + MAX_LOADED_MODELS dépasse 65535")
        if self.base_llama_port <= self.agent_port <= last_llama_port:
            errors.append("AGENT_PORT ne doit pas chevaucher la plage de ports llama-server")
        if self.idle_check_interval_seconds > self.idle_timeout_seconds:
            errors.append(
                "IDLE_CHECK_INTERVAL_SECONDS doit être inférieur ou égal à "
                "IDLE_TIMEOUT_SECONDS"
            )
        if (self.agent_tls_cert is None) != (self.agent_tls_key is None):
            errors.append("AGENT_TLS_CERT et AGENT_TLS_KEY doivent être définis ensemble")
        if errors:
            raise ValueError("Configuration incohérente : " + "; ".join(errors))
        return self

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

    def internal_api_key_is_placeholder(self) -> bool:
        """True si la clé protégeant le data-plane n'est pas configurée."""
        return not self.internal_api_key or self.internal_api_key.strip().upper().startswith("CHANGE_ME")

    def validate_runtime_security(self) -> None:
        """Refuse un agent réseau qui démarrerait avec une sécurité incomplète."""
        errors: list[str] = []
        if self.agent_secret_is_placeholder() or len(self.agent_secret) < 32:
            errors.append("AGENT_SECRET doit être un secret non-placeholder d'au moins 32 caractères")
        if self.internal_api_key_is_placeholder() or len(self.internal_api_key) < 32:
            errors.append("INTERNAL_API_KEY doit être une clé non-placeholder d'au moins 32 caractères")
        if self.agent_tls_cert is None or self.agent_tls_key is None:
            errors.append("AGENT_TLS_CERT et AGENT_TLS_KEY sont obligatoires pour l'agent réseau")
        try:
            data_plane_ip = ipaddress.ip_address(self.llama_server_host)
        except ValueError:
            data_plane_ip = None
        if self.llama_server_host.lower() == "localhost" or (
            data_plane_ip is not None and data_plane_ip.is_loopback
        ):
            errors.append(
                "LLAMA_SERVER_HOST doit être joignable depuis l'orchestrateur "
                "(utilisez 0.0.0.0 ou l'IP privée du nœud, pas loopback)"
            )
        if errors:
            raise RuntimeError("Configuration node-agent non sûre : " + "; ".join(errors))


_settings: AgentSettings | None = None


def get_settings() -> AgentSettings:
    """Instancie la configuration runtime à la première demande seulement."""
    global _settings
    if _settings is None:
        _settings = AgentSettings()
    return _settings


def __getattr__(name: str):
    # `from config import settings` reste compatible pour main.py et
    # server_manager.py, tandis que preflight peut importer AgentSettings sans
    # évaluer l'environnement ambiant avant de lire l'EnvironmentFile ciblé.
    if name == "settings":
        return get_settings()
    raise AttributeError(name)
