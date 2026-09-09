"""
Configuration — chargée depuis variables d'environnement ou fichier .env.
Toutes les valeurs sensibles (clés, chemins) vivent dans /etc/llm-gateway/env,
jamais dans le code source.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def secret_is_placeholder(secret: str) -> bool:
    """True si un secret est vide ou laissé à sa valeur d'exemple CHANGE_ME_*."""
    return not secret or secret.strip().upper().startswith("CHANGE_ME")


# Longueur minimale d'un secret d'exploitation non-placeholder. Alignement avec
# node_agent.validate_runtime_security() et le garde cluster AGENT_SECRET de
# model_manager._build_manager().
SECRET_MIN_LENGTH = 32

# Bornes imposées par les interfaces utilisées au runtime. Le plafond SQLite
# évite qu'un quota par défaut ne soit accepté au démarrage puis refusé lors de
# la première insertion d'un utilisateur.
MAX_PORT = 65535
MAX_DEFAULT_RPM = 1000
MAX_SQLITE_INTEGER = 2**63 - 1


def secret_is_weak(secret: str) -> bool:
    """
    True si un secret est vide, placeholder ou trop court pour l'exploitation.

    Un secret trivial (« password », « 12345678 ») passe le filtre placeholder :
    c'est la faille corrigée côté ADMIN_SECRET (audit 2026-08-28, ISSUE 2).
    """
    return secret_is_placeholder(secret) or len(secret) < SECRET_MIN_LENGTH


def split_list_setting(value: object, name: str) -> object:
    """
    Normalise un réglage de liste reçu depuis l'environnement.

    Accepte une valeur vide (→ liste vide), une liste CSV — la syntaxe que
    documentent `.env.example` et `docs/deployment.md` — ou un tableau JSON.
    Une valeur déjà structurée (liste Python, cas des tests et des appels
    directs) traverse sans modification.

    Même sémantique que `AgentSettings.allowed_model_dirs_list()` du node-agent,
    qui a rencontré le même piège avant nous.
    """
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return []
    # Un objet JSON serait sinon traité comme un unique élément CSV : l'allowlist
    # contiendrait une entrée qui ne correspond à rien, donc un contrôle de
    # sécurité inerte sans le moindre message. On refuse explicitement.
    if raw.startswith("{"):
        raise ValueError(f"{name} : attendu une liste CSV ou un tableau JSON, pas un objet")
    if raw.startswith("["):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} : tableau JSON invalide") from exc
        if not isinstance(decoded, list) or not all(isinstance(v, str) for v in decoded):
            raise ValueError(f"{name} : le JSON doit être une liste de chaînes")
        return [item.strip() for item in decoded if item.strip()]
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Chemins ────────────────────────────────────────────────────────────────
    models_config_path: Path = Path("/var/lib/llm-gateway/models.yaml")
    llama_server_bin: Path = Path("/opt/llama.cpp/current/llama-server")
    db_path: Path = Path("/var/lib/llm-gateway/gateway.db")
    log_dir: Path = Path("/var/log/llm-gateway")

    # ── llama-server réseau ────────────────────────────────────────────────────
    # Hôte partagé par tous les sous-processus llama-server
    llama_server_host: str = "127.0.0.1"

    # ── Épinglage de version llama-server (mitigation supply-chain) ────────────
    # Build minimal accepté du binaire llama-server. 0 = pas d'enforcement (défaut).
    # Recommandé : fixer au premier build patché contre GHSA-8947-pfff-2f3c
    # (écriture OOB via n_discard/context-shift) et les overflows de parsing GGUF.
    # Si > 0 et que le binaire lu est plus ancien OU illisible, le démarrage est
    # REFUSÉ : un plancher qu'on ne peut pas attester reste fail-closed.
    llama_server_min_build: int = 0

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
    #
    # L'annotation `str | list[str]` est volontaire, ce n'est pas un raccourci.
    # pydantic-settings décode un champ *complexe* — donc `list[str]` — comme du
    # JSON directement dans la source d'environnement, AVANT tout validateur.
    # Une valeur CSV, et même une valeur vide, faisaient donc échouer le
    # démarrage sur `SettingsError` : le fichier livré par `.env.example` rendait
    # le service mort. Élargir l'annotation rend le champ non-complexe pour la
    # source ; le validateur `mode="before"` ci-dessous produit toujours une
    # `list[str]`. Le node-agent contourne le même piège autrement
    # (`allowed_model_dirs: str` + accesseur), cf. `node_agent/config.py`.
    allowed_model_dirs: str | list[str] = Field(default_factory=list)

    # ── Lifecycle modèle ───────────────────────────────────────────────────────
    idle_timeout_seconds: int = 300
    model_load_timeout_seconds: int = 180
    idle_check_interval_seconds: int = 30

    # ── Robustesse cycle de vie (shutdown / réconciliation / orphelins) ───────
    # Drain des requêtes actives au SIGTERM : durée max d'attente que les modèles
    # pinnés se libèrent avant de forcer le déchargement. 0 = pas d'attente.
    shutdown_drain_timeout_seconds: float = 25.0
    # Intervalle de poll pendant le drain (court pour réactivité).
    # Utilisé aussi par le drain des opérations admin de déchargement.
    shutdown_drain_poll_seconds: float = 0.2
    # Drain des requêtes actives sur une opération ADMIN de déchargement
    # (POST /admin/models/{id}/unload, DELETE /admin/models/{id},
    #  PATCH enabled:false ou llama_params, POST /admin/unload).
    # Volontairement beaucoup plus court que le drain de shutdown : une route
    # admin ne doit jamais bloquer longtemps. Si des requêtes sont encore actives
    # à l'expiration, l'opération est refusée en 409 (jamais un stream tué en
    # silence) — sauf force=true explicite. 0 = refus immédiat si occupé.
    admin_unload_drain_timeout_seconds: float = 5.0
    # Flush des lignes d'usage fire-and-forget au shutdown : deadline (s)
    # d'attente des tâches d'arrière-plan encore en vol (background.drain_pending),
    # après le déchargement des modèles et avant la fermeture du client HTTP.
    # Sans ce drain, les derniers log_usage juste avant le SIGTERM étaient
    # perdus. 0 = pas d'attente.
    shutdown_background_flush_seconds: float = 5.0

    # Réconciliation VRAM avec nvidia-smi (détection de dérive, NON FATAL).
    # 0 = désactivé. Intervalle entre deux sondes nvidia-smi.
    vram_reconcile_interval_seconds: float = 60.0
    # Timeout de la sonde nvidia-smi (court).
    vram_reconcile_probe_timeout_seconds: float = 5.0
    # Seuil de dérive : VRAM réelle > somme des vram_gb déclarés × (1 + seuil)
    # → warning. 0.15 = +15%.
    vram_reconcile_drift_threshold: float = 0.15

    # Orphelins llama-server au démarrage : détection best-effort des ports du
    # pool déjà occupés. Par défaut, on LOG seulement (aucun kill).
    # Mettre à True pour tenter un kill best-effort (nécessite psutil).
    kill_orphan_llama_on_startup: bool = False

    # ── Queue d'admission VRAM ────────────────────────────────────────────────
    # Quand un modèle ne peut pas être chargé car la VRAM/les ports sont
    # temporairement occupés par des requêtes actives, attendre au lieu de
    # retourner immédiatement 503. La queue reste bornée pour éviter l'abus.
    capacity_queue_enabled: bool = True
    capacity_queue_timeout_seconds: int = 120
    capacity_queue_max_waiters: int = 100
    capacity_queue_retry_after_seconds: int = 10

    # ── Pool de connexions HTTP vers llama-server (chemin chaud d'inférence) ───
    # Un unique httpx.AsyncClient partagé par processus proxifie toutes les
    # requêtes vers les sous-processus llama-server locaux. Le keep-alive évite
    # un handshake TCP par requête. Dimensionnement par défaut : marge large
    # au-dessus de max_loaded_models pour absorber le parallélisme par modèle.
    # 0 = illimité (déconseillé en prod).
    httpx_max_connections: int = 200
    httpx_max_keepalive: int = 100
    httpx_keepalive_expiry: float = 30.0

    # ── Readiness structurelle (/ready) ───────────────────────────────────────
    # Durée de mémorisation des contrôles système de /ready (existence du binaire
    # llama-server, présence des GGUF activés, inscriptibilité de la DB…).
    # Ces contrôles ne font que des stat/access, mais /ready est sondée souvent
    # par systemd, nginx et update.sh : le cache borne le coût sur un stockage
    # lent (NFS). Le cache est de toute façon invalidé dès que la configuration
    # ou la liste des modèles activés change. 0 = pas de cache (toujours frais).
    readiness_cache_ttl_seconds: float = 15.0

    # ── Sécurité ───────────────────────────────────────────────────────────────
    # Clé interne entre la gateway et llama-server (jamais exposée aux users)
    internal_api_key: str = "CHANGE_ME_INTERNAL_KEY"
    # Secret pour les endpoints /admin (en plus du filtrage IP)
    admin_secret: str = "CHANGE_ME_ADMIN_SECRET"

    # ── Rétention RGPD ─────────────────────────────────────────────────────────
    # Entrées usage_log supprimées au-delà de N jours : au démarrage, puis
    # toutes les 24 h (suppression seule, sans VACUUM). 0 = rétention
    # désactivée. L'espace disque n'est rendu que par la purge manuelle
    # `llmgw purge-usage` (VACUUM, hors ligne).
    usage_retention_days: int = 365
    # Sauvegardes `*.pre-migration.*.bak` conservées (les plus récentes).
    # Minimum 1 : la procédure de rollback documentée repose sur la plus
    # récente. Elles contiennent une copie complète de la base — la borne est
    # ce qui empêche une copie de survivre indéfiniment à `anonymize_user`.
    migration_backups_to_keep: int = 2

    # ── Gateway réseau ─────────────────────────────────────────────────────────
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 8000

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Origines autorisées, séparées par des virgules.
    # "*" (défaut) convient en dev ; en production, restreindre aux domaines
    # clients connus : CORS_ALLOW_ORIGINS=https://app.univ-pau.fr
    # Même contrainte d'annotation que `allowed_model_dirs` ci-dessus : sans
    # elle, le validateur `split_cors_origins` ne s'exécutait jamais, l'échec
    # ayant lieu dans la source d'environnement.
    cors_allow_origins: str | list[str] = Field(default_factory=lambda: ["*"])

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
    # Timeout dédié au chargement de modèle — un gros GGUF prend souvent
    # plusieurs minutes côté agent, bien au-delà du timeout court du plan de
    # contrôle. Distinct de cluster_request_timeout pour ne pas casser le mode
    # cluster sur les gros modèles.
    cluster_load_timeout: float = 300.0
    # Heartbeat — intervalle entre deux GET /agent/health par nœud
    cluster_health_interval: int = 10
    # Échecs consécutifs avant de marquer un nœud offline
    cluster_health_failures_to_offline: int = 3

    @field_validator(
        "llama_server_min_build",
        "base_llama_port",
        "max_loaded_models",
        "total_vram_gb",
        "vram_overhead_gb",
        "vram_safety_margin",
        "idle_timeout_seconds",
        "model_load_timeout_seconds",
        "idle_check_interval_seconds",
        "shutdown_drain_timeout_seconds",
        "shutdown_drain_poll_seconds",
        "admin_unload_drain_timeout_seconds",
        "shutdown_background_flush_seconds",
        "vram_reconcile_interval_seconds",
        "vram_reconcile_probe_timeout_seconds",
        "vram_reconcile_drift_threshold",
        "capacity_queue_timeout_seconds",
        "capacity_queue_max_waiters",
        "capacity_queue_retry_after_seconds",
        "httpx_max_connections",
        "httpx_max_keepalive",
        "httpx_keepalive_expiry",
        "readiness_cache_ttl_seconds",
        "usage_retention_days",
        "migration_backups_to_keep",
        "gateway_port",
        "default_rpm_limit",
        "default_monthly_token_limit",
        "cluster_request_timeout",
        "cluster_load_timeout",
        "cluster_health_interval",
        "cluster_health_failures_to_offline",
        mode="before",
    )
    @classmethod
    def reject_boolean_numeric_settings(cls, v: object) -> object:
        """Refuse `True`/`False` silently coerced to 1/0 par Pydantic."""
        if isinstance(v, bool):
            raise ValueError("une valeur booléenne n'est pas un nombre de configuration")
        return v

    @field_validator("models_config_path", "llama_server_bin", mode="before")
    @classmethod
    def coerce_path(cls, v: object) -> Path:
        return Path(str(v))

    @field_validator("vram_safety_margin")
    @classmethod
    def validate_safety_margin(cls, v: float) -> float:
        if not math.isfinite(v) or not 0.0 <= v < 1.0:
            raise ValueError(f"vram_safety_margin doit être dans [0, 1), reçu : {v}")
        return v

    @field_validator("total_vram_gb")
    @classmethod
    def validate_total_vram(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"total_vram_gb doit être un nombre fini > 0, reçu : {v}")
        return v

    @field_validator("vram_overhead_gb")
    @classmethod
    def validate_vram_overhead(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"vram_overhead_gb doit être un nombre fini ≥ 0, reçu : {v}")
        return v

    @field_validator("llama_server_min_build")
    @classmethod
    def validate_min_build(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"llama_server_min_build doit être ≥ 0, reçu : {v}")
        return v

    @field_validator("base_llama_port", "gateway_port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        if not 1 <= v <= MAX_PORT:
            raise ValueError(f"port doit être compris entre 1 et {MAX_PORT}, reçu : {v}")
        return v

    @field_validator("max_loaded_models")
    @classmethod
    def validate_max_models(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"max_loaded_models doit être ≥ 1, reçu : {v}")
        return v

    @field_validator(
        "idle_timeout_seconds",
        "model_load_timeout_seconds",
        "idle_check_interval_seconds",
    )
    @classmethod
    def validate_lifecycle_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"Timeout lifecycle doit être ≥ 1, reçu : {v}")
        return v

    @field_validator("usage_retention_days")
    @classmethod
    def validate_usage_retention(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"usage_retention_days doit être ≥ 0 (0 = désactivé), reçu : {v}")
        return v

    @field_validator("migration_backups_to_keep")
    @classmethod
    def validate_migration_backups_to_keep(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"migration_backups_to_keep doit être ≥ 1, reçu : {v}")
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

    @field_validator("default_rpm_limit")
    @classmethod
    def validate_default_rpm(cls, v: int) -> int:
        if not 1 <= v <= MAX_DEFAULT_RPM:
            raise ValueError(
                f"default_rpm_limit doit être compris entre 1 et {MAX_DEFAULT_RPM}, reçu : {v}"
            )
        return v

    @field_validator("default_monthly_token_limit")
    @classmethod
    def validate_default_monthly_tokens(cls, v: int) -> int:
        if not 0 <= v <= MAX_SQLITE_INTEGER:
            raise ValueError(
                "default_monthly_token_limit doit être compris entre 0 et "
                f"{MAX_SQLITE_INTEGER}, reçu : {v}"
            )
        return v

    @field_validator("cluster_nodes_path", mode="before")
    @classmethod
    def coerce_cluster_path(cls, v: object) -> Path:
        return Path(str(v))

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def split_cors_origins(cls, v: object) -> object:
        return split_list_setting(v, "CORS_ALLOW_ORIGINS")

    @field_validator("allowed_model_dirs", mode="before")
    @classmethod
    def split_allowed_model_dirs(cls, v: object) -> object:
        return split_list_setting(v, "ALLOWED_MODEL_DIRS")

    @field_validator("cluster_health_interval", "cluster_health_failures_to_offline")
    @classmethod
    def validate_cluster_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"Valeur cluster doit être ≥ 1, reçu : {v}")
        return v

    @field_validator("cluster_request_timeout", "cluster_load_timeout")
    @classmethod
    def validate_cluster_timeout_positive(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"Timeout cluster doit être > 0, reçu : {v}")
        return v

    @field_validator("httpx_max_connections", "httpx_max_keepalive")
    @classmethod
    def validate_httpx_pool_non_negative(cls, v: int) -> int:
        # 0 = illimité (sémantique httpx.Limits : None). Négatif interdit.
        if v < 0:
            raise ValueError(f"Valeur httpx pool doit être ≥ 0, reçu : {v}")
        return v

    @field_validator("httpx_keepalive_expiry")
    @classmethod
    def validate_httpx_keepalive_expiry(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"httpx_keepalive_expiry doit être ≥ 0, reçu : {v}")
        return v

    @field_validator("shutdown_drain_poll_seconds")
    @classmethod
    def validate_shutdown_poll_positive(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError(f"shutdown_drain_poll_seconds doit être > 0, reçu : {v}")
        return v

    @field_validator("vram_reconcile_probe_timeout_seconds")
    @classmethod
    def validate_vram_probe_timeout_positive(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError(
                f"vram_reconcile_probe_timeout_seconds doit être > 0, reçu : {v}"
            )
        return v

    @field_validator(
        "shutdown_drain_timeout_seconds",
        "admin_unload_drain_timeout_seconds",
        "shutdown_background_flush_seconds",
        "vram_reconcile_interval_seconds",
        "vram_reconcile_drift_threshold",
        "readiness_cache_ttl_seconds",
    )
    @classmethod
    def validate_robustness_non_negative(cls, v: float) -> float:
        # 0 est autorisé (désactivation). Négatif interdit.
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"Valeur robustesse doit être ≥ 0, reçu : {v}")
        return v

    @model_validator(mode="after")
    def validate_cross_field_invariants(self) -> "Settings":
        """Refuse les combinaisons qui casseraient le runtime après le boot."""
        errors: list[str] = []
        last_llama_port = self.base_llama_port + self.max_loaded_models - 1

        if last_llama_port > MAX_PORT:
            errors.append(
                "la plage BASE_LLAMA_PORT + MAX_LOADED_MODELS dépasse 65535"
            )
        if self.gateway_port in range(self.base_llama_port, last_llama_port + 1):
            errors.append(
                "GATEWAY_PORT ne doit pas chevaucher la plage de ports llama-server"
            )

        budget = self.effective_vram_budget_gb()
        if self.vram_overhead_gb >= self.total_vram_gb:
            errors.append("VRAM_OVERHEAD_GB doit être strictement inférieur à TOTAL_VRAM_GB")
        if not math.isfinite(budget) or budget <= 0:
            errors.append("le budget VRAM net doit être strictement positif")

        if self.idle_check_interval_seconds > self.idle_timeout_seconds:
            errors.append(
                "IDLE_CHECK_INTERVAL_SECONDS doit être inférieur ou égal à "
                "IDLE_TIMEOUT_SECONDS"
            )
        if self.cluster_mode == "cluster":
            if self.cluster_load_timeout < self.cluster_request_timeout:
                errors.append(
                    "CLUSTER_LOAD_TIMEOUT doit être supérieur ou égal à "
                    "CLUSTER_REQUEST_TIMEOUT"
                )
            if self.cluster_load_timeout < self.model_load_timeout_seconds:
                errors.append(
                    "CLUSTER_LOAD_TIMEOUT doit couvrir MODEL_LOAD_TIMEOUT_SECONDS"
                )

        if self.httpx_max_connections > 0 and (
            self.httpx_max_keepalive > self.httpx_max_connections
        ):
            errors.append(
                "HTTPX_MAX_KEEPALIVE ne doit pas dépasser "
                "HTTPX_MAX_CONNECTIONS quand ce dernier est limité"
            )

        for name, timeout in (
            ("SHUTDOWN_DRAIN_TIMEOUT_SECONDS", self.shutdown_drain_timeout_seconds),
            ("ADMIN_UNLOAD_DRAIN_TIMEOUT_SECONDS", self.admin_unload_drain_timeout_seconds),
        ):
            if timeout > 0 and self.shutdown_drain_poll_seconds > timeout:
                errors.append(
                    "SHUTDOWN_DRAIN_POLL_SECONDS doit être inférieur ou égal à "
                    f"{name}"
                )

        if errors:
            raise ValueError("Configuration incohérente : " + "; ".join(errors))
        return self

    def effective_vram_budget_gb(self) -> float:
        """Budget VRAM net disponible pour les modèles (après overhead et marge)."""
        return self.total_vram_gb - self.vram_overhead_gb - (self.total_vram_gb * self.vram_safety_margin)

    def admin_secret_is_placeholder(self) -> bool:
        return secret_is_placeholder(self.admin_secret)

    def admin_secret_is_weak(self) -> bool:
        """Placeholder OU trop court → routes /admin fail-closed (SEC)."""
        return secret_is_weak(self.admin_secret)

    def internal_api_key_is_placeholder(self) -> bool:
        return secret_is_placeholder(self.internal_api_key)

    def agent_secret_is_placeholder(self) -> bool:
        return secret_is_placeholder(self.agent_secret)


# Instance globale — importée partout dans l'application
settings = Settings()
