"""
Configuration — chargée depuis variables d'environnement ou fichier .env.
Toutes les valeurs sensibles (clés, chemins) vivent dans /etc/llm-gateway/env,
jamais dans le code source.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Chemins ────────────────────────────────────────────────────────────────
    model_path: Path = Path("/models/Llama-3.3-70B-Instruct-Q4_K_M.gguf")
    llama_server_bin: Path = Path("/usr/local/bin/llama-server")
    db_path: Path = Path("/var/lib/llm-gateway/gateway.db")
    log_dir: Path = Path("/var/log/llm-gateway")

    # ── Modèle ─────────────────────────────────────────────────────────────────
    # Nom exposé dans /v1/models (peut être différent du nom de fichier)
    model_public_name: str = "llama-3.3-70b-instruct"

    # ── llama-server réseau ────────────────────────────────────────────────────
    llama_server_host: str = "127.0.0.1"
    llama_server_port: int = 8081

    # ── llama-server paramètres inférence (optimisés L40S 48GB) ───────────────
    # -ngl : nombre de couches GPU. 999 = "tout offloader" (plafonné automatiquement)
    llama_n_gpu_layers: int = 999
    # Taille totale du KV cache = parallel × tokens_par_slot
    # Pour 70B Q4_K_M : 4 slots × 8K = 32K → ~40.5GB VRAM total
    llama_ctx_size: int = 32768
    # Slots d'inférence concurrents (utilisateurs simultanés)
    llama_parallel: int = 4
    # Batch size prefill (logical) et micro-batch (physical)
    llama_batch_size: int = 4096
    llama_ubatch_size: int = 512
    # Flash Attention (Ada Lovelace / compute cap 8.9 = supporté)
    llama_flash_attn: bool = True
    # Quantisation du KV cache : q8_0 = -50% VRAM, qualité quasi-identique
    llama_cache_type_k: Literal["f16", "bf16", "q8_0", "q5_0", "q4_0"] = "q8_0"
    llama_cache_type_v: Literal["f16", "bf16", "q8_0", "q5_0", "q4_0"] = "q8_0"
    # Threads CPU (génération et HTTP)
    llama_threads: int = 8
    llama_threads_http: int = 4
    # GPU visible (index CUDA)
    cuda_visible_devices: str = "0"

    # ── Lifecycle modèle ───────────────────────────────────────────────────────
    # Délai d'inactivité avant déchargement du modèle (secondes)
    idle_timeout_seconds: int = 300
    # Timeout max pour le chargement du modèle (secondes)
    model_load_timeout_seconds: int = 180
    # Intervalle de vérification idle (secondes)
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

    @field_validator("model_path", "llama_server_bin", mode="before")
    @classmethod
    def coerce_path(cls, v: object) -> Path:
        return Path(str(v))

    @model_validator(mode="after")
    def validate_ubatch_lte_batch(self) -> "Settings":
        if self.llama_ubatch_size > self.llama_batch_size:
            raise ValueError(
                f"llama_ubatch_size ({self.llama_ubatch_size}) "
                f"must be ≤ llama_batch_size ({self.llama_batch_size})"
            )
        return self

    def llama_server_url(self) -> str:
        return f"http://{self.llama_server_host}:{self.llama_server_port}"

    def build_llama_cmd(self) -> list[str]:
        """Construit la liste d'arguments pour lancer llama-server."""
        cmd = [
            str(self.llama_server_bin),
            "--model", str(self.model_path),
            "--host", self.llama_server_host,
            "--port", str(self.llama_server_port),
            "-ngl", str(self.llama_n_gpu_layers),
            "-c", str(self.llama_ctx_size),
            "--parallel", str(self.llama_parallel),
            "-b", str(self.llama_batch_size),
            "-ub", str(self.llama_ubatch_size),
            "-ctk", self.llama_cache_type_k,
            "-ctv", self.llama_cache_type_v,
            "-t", str(self.llama_threads),
            "--threads-http", str(self.llama_threads_http),
            "--cont-batching",
            "--cache-prompt",
            "--metrics",
            "--api-key", self.internal_api_key,
            "--log-file", str(self.log_dir / "llama-server.log"),
        ]
        if self.llama_flash_attn:
            cmd += ["-fa", "on"]
        return cmd


# Instance globale — importée partout dans l'application
settings = Settings()
