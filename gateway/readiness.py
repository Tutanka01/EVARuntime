"""
Contrôles de readiness structurelle de la gateway (COR-005).

Modèle à trois niveaux
----------------------
Ce module implémente la readiness à trois niveaux du plan de consolidation :

| Niveau                | Signification                                                    |
|-----------------------|------------------------------------------------------------------|
| Liveness              | Le processus répond. C'est le rôle de `GET /health`.             |
| Structural readiness  | Configuration, DB, binaire, répertoires et au moins un modèle    |
|                       | activé sont valides. C'est ce que garantit `GET /ready`.         |
| Serving readiness     | Un modèle est chargé, ou un smoke load récent a prouvé qu'il     |
|                       | peut l'être. Exposé en information seulement — le feu vert de    |
|                       | production reste du ressort du smoke test (COR-006).             |

Avant COR-005, `/ready` se contentait de « un modèle est ready OU il reste de la
VRAM » : une gateway sans binaire `llama-server` et sans aucun GGUF présent
répondait 200. Comme `update.sh` décide du rollback uniquement sur `/ready`, une
version incapable de servir était acceptée puis déployée.

Coût et chemin chaud
--------------------
`/ready` est appelée par systemd, nginx et les scripts de déploiement. Les
contrôles ne font donc **que** des `stat`/`access` :

- aucune lecture du contenu d'un GGUF, aucun hash (un fichier vide suffit à
  passer le contrôle de présence — l'intégrité SHA-256 reste du ressort du
  démarrage, cf. `main._validate_inference_runtime`) ;
- aucun lancement de sous-processus (la sonde `llama-server --version` de
  `llama_version.py` reste réservée au démarrage et à `evaruntime doctor`) ;
- aucune ouverture de socket ni de connexion SQLite.

Les appels système sont malgré tout poussés hors de la boucle d'événements via
`asyncio.to_thread`, et mémorisés dans un cache à TTL court
(`READINESS_CACHE_TTL_SECONDS`) invalidé automatiquement dès que la
configuration ou la liste des modèles activés change. Un verrou garantit qu'un
pic d'appels concurrents ne déclenche qu'une seule sonde.

Réutilisation par la CLI
------------------------
`evaluate_readiness()` renvoie un résultat structuré (nom du contrôle, statut,
code stable, message actionnable). La route `/ready` n'en expose qu'une vue
publique sans chemin ni secret ; `evaruntime doctor` (AUT-012) peut consommer
directement les `CheckResult` pour son rapport humain/JSON.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets as _secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from config import Settings, secret_is_placeholder, secret_is_weak
from config import settings as _module_settings

log = logging.getLogger(__name__)

CheckStatus = Literal["pass", "fail", "warn", "skip"]
ReadinessLevel = Literal["none", "structural", "serving"]

# Nom SQLite spécial : base en mémoire, aucun fichier à valider.
_SQLITE_MEMORY = ":memory:"

# TTL de repli si l'objet de configuration fourni ne porte pas le réglage
# (par exemple un faux settings dans un test).
_DEFAULT_CACHE_TTL_SECONDS = 15.0


# ── Résultats structurés ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class CheckResult:
    """
    Résultat d'un contrôle unitaire.

    - `name`    : identifiant stable du contrôle (sûr à exposer publiquement) ;
    - `status`  : `pass` / `fail` / `warn` / `skip` ;
    - `code`    : code machine stable de la cause (sûr à exposer publiquement) ;
    - `message` : message actionnable pour un opérateur. Peut contenir des
                  chemins de fichiers et des détails d'infrastructure — il est
                  donc réservé aux appelants privilégiés (CLI locale, admin) ;
    - `critical`: un `fail` critique rend la gateway non-ready. Un contrôle non
                  critique (`warn`) est signalé sans jamais bloquer.
    """
    name: str
    status: CheckStatus
    code: str
    message: str
    critical: bool = True

    @property
    def is_blocking(self) -> bool:
        """True si ce contrôle interdit de déclarer la gateway prête."""
        return self.critical and self.status == "fail"

    def to_dict(self) -> dict:
        """Vue complète — contient `message`, donc réservée aux privilégiés."""
        return {
            "name": self.name,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "critical": self.critical,
        }


@dataclass(frozen=True)
class ReadinessReport:
    """Agrégat des contrôles + état de service observé, sans donnée sensible."""
    mode: str
    checks: tuple[CheckResult, ...]
    models_ready: tuple[str, ...]
    vram_available_gb: float
    nodes_online: int | None = None

    @property
    def structural_ok(self) -> bool:
        """Aucun contrôle critique en échec."""
        return not any(check.is_blocking for check in self.checks)

    @property
    def serving_ok(self) -> bool:
        """Un modèle est effectivement chargé et prêt à répondre."""
        return self.structural_ok and bool(self.models_ready)

    @property
    def level(self) -> ReadinessLevel:
        """Niveau atteint. La liveness est implicite : le process a répondu."""
        if not self.structural_ok:
            return "none"
        return "serving" if self.serving_ok else "structural"

    @property
    def reason(self) -> str | None:
        """Code du premier contrôle critique en échec, ou None si prêt."""
        for check in self.checks:
            if check.is_blocking:
                return check.code
        return None

    @property
    def failed(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status == "fail")

    @property
    def warnings(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status == "warn")

    def public_body(self) -> dict:
        """
        Corps de réponse pour un appelant NON privilégié.

        Ne contient ni chemin de fichier, ni URL de nœud, ni secret : seulement
        des identifiants de contrôle et des codes stables, tous déjà publics
        dans le code source.
        """
        body: dict = {
            "status": "ready" if self.structural_ok else "not_ready",
            "level": self.level,
            "levels": {
                "liveness": True,
                "structural": self.structural_ok,
                "serving": self.serving_ok,
            },
            "mode": self.mode,
            "models_ready": list(self.models_ready),
            "vram_available_gb": round(float(self.vram_available_gb), 2),
            "checks": {c.name: c.status for c in self.checks},
        }
        if self.nodes_online is not None:
            body["nodes_online"] = self.nodes_online
        reason = self.reason
        if reason is not None:
            body["reason"] = reason
        return body

    def detailed_body(self) -> dict:
        """
        Vue privilégiée : ajoute les messages actionnables (avec chemins).

        Réservée aux appelants présentant `ADMIN_SECRET` — même niveau de
        confiance que les routes `/admin/*`.
        """
        body = self.public_body()
        body["details"] = [c.to_dict() for c in self.checks]
        return body


# ── Cache des contrôles système ───────────────────────────────────────────────
# Un seul créneau mémorisé : la clé porte toute la configuration observée, donc
# un changement de settings ou de registre invalide le cache immédiatement.

_cache_lock = asyncio.Lock()
_cache: tuple[tuple, dict[str, CheckResult], float] | None = None


def clear_cache() -> None:
    """Invalide le cache des contrôles système (tests, CLI, après mutation)."""
    global _cache
    _cache = None


def _cache_ttl_seconds(config: Any) -> float:
    raw = getattr(config, "readiness_cache_ttl_seconds", _DEFAULT_CACHE_TTL_SECONDS)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return _DEFAULT_CACHE_TTL_SECONDS


def _cache_key(config: Any, enabled_models: Sequence[Any]) -> tuple:
    return (
        str(getattr(config, "cluster_mode", "local")),
        str(getattr(config, "models_config_path", "")),
        str(getattr(config, "llama_server_bin", "")),
        str(getattr(config, "db_path", "")),
        str(getattr(config, "log_dir", "")),
        str(getattr(config, "cluster_nodes_path", "")),
        tuple(
            (
                str(m.id),
                str(m.path),
                str(m.mmproj_path) if getattr(m, "mmproj_path", None) else "",
                "vision" in (getattr(m, "capabilities", None) or []),
            )
            for m in enabled_models
        ),
    )


# ── Contrôles système (exécutés dans un thread, uniquement des stat/access) ────

def _check_models_config(config: Any) -> CheckResult:
    path = Path(str(getattr(config, "models_config_path", "")))
    if not path.is_file():
        return CheckResult(
            "models_config", "fail", "models_config_missing",
            f"Registre des modèles introuvable : {path}. "
            "Créez le fichier ou corrigez MODELS_CONFIG_PATH.",
        )
    if not os.access(path, os.R_OK):
        return CheckResult(
            "models_config", "fail", "models_config_unreadable",
            f"Registre des modèles illisible : {path}. "
            "Vérifiez le propriétaire et les permissions (attendu : lisible par le service).",
        )
    return CheckResult(
        "models_config", "pass", "ok", "Registre des modèles présent et lisible."
    )


def _check_llama_binary(config: Any) -> CheckResult:
    if str(getattr(config, "cluster_mode", "local")) == "cluster":
        return CheckResult(
            "llama_server_binary", "skip", "delegated_to_nodes",
            "Mode cluster : le binaire llama-server vit sur les nœuds et est "
            "validé par leur préflight/node-agent, pas par l'orchestrateur.",
        )
    binary = Path(str(getattr(config, "llama_server_bin", "")))
    if not binary.is_file():
        return CheckResult(
            "llama_server_binary", "fail", "llama_server_missing",
            f"Binaire llama-server introuvable : {binary}. "
            "Compilez/installez llama.cpp ou corrigez LLAMA_SERVER_BIN.",
        )
    if not os.access(binary, os.X_OK):
        return CheckResult(
            "llama_server_binary", "fail", "llama_server_not_executable",
            f"Binaire llama-server non exécutable : {binary}. "
            "Corrigez avec : chmod +x sur le binaire.",
        )
    return CheckResult(
        "llama_server_binary", "pass", "ok", "Binaire llama-server présent et exécutable."
    )


def _check_model_files(config: Any, enabled_models: Sequence[Any]) -> CheckResult:
    """
    Présence des artefacts des modèles ACTIVÉS uniquement.

    Un modèle désactivé dont le GGUF a été retiré volontairement ne doit jamais
    rendre la gateway non-ready. Le contrôle se limite à des `stat` : aucune
    lecture, aucun hash — un fichier vide suffit à le satisfaire.
    """
    if str(getattr(config, "cluster_mode", "local")) == "cluster":
        return CheckResult(
            "model_files", "skip", "delegated_to_nodes",
            "Mode cluster : les GGUF vivent sur les nœuds et sont validés au "
            "chargement par le node-agent qui les possède.",
        )
    if not enabled_models:
        return CheckResult(
            "model_files", "skip", "no_enabled_model",
            "Aucun modèle activé à contrôler (cf. contrôle enabled_models).",
        )

    for model in enabled_models:
        path = Path(str(model.path))
        if not path.is_file():
            return CheckResult(
                "model_files", "fail", "model_file_missing",
                f"[{model.id}] Fichier GGUF introuvable : {path}. "
                "Téléchargez le modèle, corrigez son 'path' dans models.yaml, "
                "ou désactivez-le (enabled: false).",
            )
        if not os.access(path, os.R_OK):
            return CheckResult(
                "model_files", "fail", "model_file_unreadable",
                f"[{model.id}] Fichier GGUF illisible : {path}. "
                "Vérifiez les permissions du fichier et de ses répertoires parents.",
            )
        mmproj = getattr(model, "mmproj_path", None)
        if mmproj is not None and "vision" in (getattr(model, "capabilities", None) or []):
            mmproj_path = Path(str(mmproj))
            if not mmproj_path.is_file():
                return CheckResult(
                    "model_files", "fail", "mmproj_missing",
                    f"[{model.id}] Projecteur multimodal introuvable : {mmproj_path}. "
                    "Les requêtes avec image échoueraient en HTTP 500.",
                )
            if not os.access(mmproj_path, os.R_OK):
                return CheckResult(
                    "model_files", "fail", "mmproj_unreadable",
                    f"[{model.id}] Projecteur multimodal illisible : {mmproj_path}.",
                )

    return CheckResult(
        "model_files", "pass", "ok",
        f"Artefacts présents et lisibles pour les {len(enabled_models)} modèle(s) activé(s).",
    )


def _check_database(config: Any) -> CheckResult:
    raw = str(getattr(config, "db_path", ""))
    if raw == _SQLITE_MEMORY:
        return CheckResult(
            "database", "pass", "in_memory",
            "Base SQLite en mémoire : aucun fichier à valider.",
        )
    path = Path(raw)
    parent = path.parent
    if not parent.is_dir():
        return CheckResult(
            "database", "fail", "database_dir_missing",
            f"Répertoire de la base introuvable : {parent}. "
            "Créez-le et donnez-en la propriété au service.",
        )
    # SQLite en WAL crée -wal/-shm à côté du fichier : le répertoire doit être
    # inscriptible, pas seulement le fichier.
    if not os.access(parent, os.W_OK | os.X_OK):
        return CheckResult(
            "database", "fail", "database_dir_not_writable",
            f"Répertoire de la base non inscriptible : {parent}. "
            "SQLite en mode WAL doit pouvoir y créer les fichiers -wal et -shm.",
        )
    if path.exists() and not os.access(path, os.W_OK):
        return CheckResult(
            "database", "fail", "database_not_writable",
            f"Base de données non inscriptible : {path}. "
            "Vérifiez le propriétaire et le mode du fichier.",
        )
    return CheckResult("database", "pass", "ok", "Base de données inscriptible.")


def _check_log_dir(config: Any) -> CheckResult:
    """
    Répertoire de logs — NON critique.

    Les logs de la gateway partent sur journald ; seuls les logs par modèle de
    `llama-server` atterrissent ici. Un répertoire absent dégrade le diagnostic
    mais n'empêche pas de servir : on avertit sans bloquer, pour éviter qu'un
    contrôle trop zélé mette une gateway saine hors rotation.
    """
    path = Path(str(getattr(config, "log_dir", "")))
    if not path.is_dir():
        return CheckResult(
            "log_dir", "warn", "log_dir_missing",
            f"Répertoire de logs introuvable : {path}. "
            "Les logs par modèle de llama-server ne pourront pas être écrits.",
            critical=False,
        )
    if not os.access(path, os.W_OK | os.X_OK):
        return CheckResult(
            "log_dir", "warn", "log_dir_not_writable",
            f"Répertoire de logs non inscriptible : {path}.",
            critical=False,
        )
    return CheckResult(
        "log_dir", "pass", "ok", "Répertoire de logs inscriptible.", critical=False
    )


def _check_cluster_nodes_config(config: Any) -> CheckResult:
    if str(getattr(config, "cluster_mode", "local")) != "cluster":
        return CheckResult(
            "cluster_nodes_config", "skip", "local_mode",
            "Mode local : aucun inventaire de nœuds requis.",
        )
    path = Path(str(getattr(config, "cluster_nodes_path", "")))
    if not path.is_file():
        return CheckResult(
            "cluster_nodes_config", "fail", "cluster_nodes_config_missing",
            f"Inventaire des nœuds introuvable : {path}. "
            "Créez le fichier ou corrigez CLUSTER_NODES_PATH.",
        )
    if not os.access(path, os.R_OK):
        return CheckResult(
            "cluster_nodes_config", "fail", "cluster_nodes_config_unreadable",
            f"Inventaire des nœuds illisible : {path}.",
        )
    return CheckResult(
        "cluster_nodes_config", "pass", "ok", "Inventaire des nœuds présent et lisible."
    )


def _probe_filesystem(config: Any, enabled_models: Sequence[Any]) -> dict[str, CheckResult]:
    """
    Exécute tous les contrôles système d'un coup. Appelé dans un thread.

    Uniquement des `stat`/`access` : jamais de lecture, de hash, de socket ni de
    sous-processus.
    """
    results = [
        _check_models_config(config),
        _check_llama_binary(config),
        _check_model_files(config, enabled_models),
        _check_database(config),
        _check_log_dir(config),
        _check_cluster_nodes_config(config),
    ]
    return {check.name: check for check in results}


async def _filesystem_checks(
    config: Any,
    enabled_models: Sequence[Any],
    use_cache: bool,
) -> dict[str, CheckResult]:
    """Sonde système mémorisée, hors boucle d'événements, une seule à la fois."""
    global _cache

    key = _cache_key(config, enabled_models)
    ttl = _cache_ttl_seconds(config)

    def _fresh(snapshot) -> bool:
        return (
            use_cache
            and ttl > 0.0
            and snapshot is not None
            and snapshot[0] == key
            and (time.monotonic() - snapshot[2]) < ttl
        )

    # Chemin rapide : cache valide, aucun verrou pris.
    snapshot = _cache
    if _fresh(snapshot):
        return snapshot[1]

    async with _cache_lock:
        # Deuxième vérification : un pic d'appels concurrents ne déclenche
        # qu'une seule sonde système.
        snapshot = _cache
        if _fresh(snapshot):
            return snapshot[1]
        checks = await asyncio.to_thread(_probe_filesystem, config, enabled_models)
        _cache = (key, checks, time.monotonic())
        return checks


# ── Contrôles en mémoire (coût nul) ───────────────────────────────────────────

def _check_enabled_models(enabled_models: Sequence[Any]) -> CheckResult:
    if not enabled_models:
        return CheckResult(
            "enabled_models", "fail", "no_enabled_model",
            "Aucun modèle activé dans le registre : la gateway ne peut servir "
            "aucune requête. Activez au moins un modèle (enabled: true).",
        )
    return CheckResult(
        "enabled_models", "pass", "ok",
        f"{len(enabled_models)} modèle(s) activé(s) dans le registre.",
    )


def _check_secrets(config: Any) -> CheckResult:
    """
    Secrets laissés à leur valeur d'exemple — signalé, JAMAIS bloquant.

    Un `ADMIN_SECRET` faible (placeholder ou trop court, même critère que
    `auth.require_admin`) désactive déjà les routes `/admin` (fail-closed côté
    route) et un `INTERNAL_API_KEY` placeholder rend la clé
    gateway ↔ llama-server prévisible : deux défauts réels, mais qui n'empêchent
    pas de servir. Les rendre bloquants transformerait `/ready` en boucle de
    rollback sur les déploiements existants — on avertit donc sans bloquer.
    """
    problems: list[str] = []
    if secret_is_weak(str(getattr(config, "admin_secret", ""))):
        problems.append("ADMIN_SECRET")
    if str(getattr(config, "cluster_mode", "local")) == "local":
        if secret_is_placeholder(str(getattr(config, "internal_api_key", ""))):
            problems.append("INTERNAL_API_KEY")
    elif secret_is_placeholder(str(getattr(config, "agent_secret", ""))):
        problems.append("AGENT_SECRET")

    if problems:
        return CheckResult(
            "secrets", "warn", "secret_placeholder",
            "Secret(s) non configuré(s), laissé(s) à une valeur d'exemple ou "
            f"trop court(s) : {', '.join(problems)}. Générez un secret fort dans "
            "l'EnvironmentFile.",
            critical=False,
        )
    return CheckResult(
        "secrets", "pass", "ok", "Aucun secret laissé à sa valeur d'exemple.",
        critical=False,
    )


def _check_vram_budget_fit(config: Any, enabled_models: Sequence[Any]) -> CheckResult:
    """
    Au moins un modèle activé doit tenir dans le budget VRAM net (mode local).

    Arithmétique pure, coût nul. Volontairement permissif : le contrôle échoue
    seulement si AUCUN modèle activé ne peut jamais être chargé.
    """
    if str(getattr(config, "cluster_mode", "local")) == "cluster":
        return CheckResult(
            "vram_budget_fit", "skip", "delegated_to_nodes",
            "Mode cluster : les budgets VRAM sont lus dynamiquement sur les nœuds.",
        )
    if not enabled_models:
        return CheckResult(
            "vram_budget_fit", "skip", "no_enabled_model",
            "Aucun modèle activé à confronter au budget VRAM.",
        )
    try:
        budget = float(config.effective_vram_budget_gb())
    except Exception:
        return CheckResult(
            "vram_budget_fit", "skip", "budget_unavailable",
            "Budget VRAM net indisponible — contrôle ignoré.",
        )
    if budget <= 0.0:
        return CheckResult(
            "vram_budget_fit", "fail", "vram_budget_exhausted",
            f"Budget VRAM net nul ou négatif ({budget:.1f} GB) : relevez "
            "TOTAL_VRAM_GB ou abaissez VRAM_OVERHEAD_GB / VRAM_SAFETY_MARGIN.",
        )
    smallest = min(float(m.vram_gb) for m in enabled_models)
    if smallest > budget:
        return CheckResult(
            "vram_budget_fit", "fail", "no_model_fits_vram_budget",
            f"Aucun modèle activé ne tient dans le budget VRAM net "
            f"({budget:.1f} GB ; plus petit modèle activé : {smallest:.1f} GB). "
            "Corrigez vram_gb, activez un modèle plus petit ou relevez TOTAL_VRAM_GB.",
        )
    return CheckResult(
        "vram_budget_fit", "pass", "ok",
        f"Au moins un modèle activé tient dans le budget VRAM net ({budget:.1f} GB).",
    )


def _check_cluster_nodes_online(config: Any, nodes_online: int | None) -> CheckResult:
    """
    Readiness structurelle de l'orchestrateur en mode cluster.

    En cluster, le binaire et les GGUF ne vivent pas sur l'orchestrateur :
    l'équivalent structurel de « binaire + fichiers présents » est « l'inventaire
    des nœuds est configuré ET au moins un nœud répond au heartbeat en annonçant
    son inventaire ». Sans nœud joignable, aucun placement n'est possible : la
    gateway est structurellement incapable de servir.
    """
    if str(getattr(config, "cluster_mode", "local")) != "cluster":
        return CheckResult(
            "cluster_nodes_online", "skip", "local_mode",
            "Mode local : aucun nœud distant impliqué.",
        )
    if nodes_online is None:
        return CheckResult(
            "cluster_nodes_online", "skip", "nodes_unknown",
            "Le manager n'expose pas nodes_online — contrôle ignoré.",
        )
    if nodes_online <= 0:
        return CheckResult(
            "cluster_nodes_online", "fail", "all_nodes_offline",
            "Aucun node-agent en ligne : aucun placement de modèle possible. "
            "Vérifiez les services node-agent et le heartbeat du plan de contrôle.",
        )
    return CheckResult(
        "cluster_nodes_online", "pass", "ok", f"{nodes_online} nœud(s) en ligne."
    )


def _check_serving_capacity(
    models_ready: Sequence[str],
    vram_available_gb: float,
) -> CheckResult:
    """
    Contrat historique de `/ready`, conservé : un modèle est déjà prêt, ou il
    reste de la capacité pour en charger un. Saturation totale sans modèle prêt
    → non-ready (retrait de rotation), comme avant COR-005.
    """
    if models_ready:
        return CheckResult(
            "serving_capacity", "pass", "model_ready",
            f"{len(models_ready)} modèle(s) chargé(s) et prêt(s).",
        )
    if vram_available_gb > 0.0:
        return CheckResult(
            "serving_capacity", "pass", "capacity_available",
            "Aucun modèle chargé, mais il reste de la capacité pour en charger un.",
        )
    return CheckResult(
        "serving_capacity", "fail", "no_model_ready_and_no_capacity",
        "Aucun modèle prêt et aucune capacité disponible : la gateway est "
        "saturée. Retirez-la de la rotation le temps que la charge retombe.",
    )


# ── Évaluation complète ───────────────────────────────────────────────────────

# Ordre canonique : le `reason` exposé est le code du PREMIER contrôle critique
# en échec, du plus structurel au plus conjoncturel.
_CHECK_ORDER = (
    "models_config",
    "enabled_models",
    "secrets",
    "llama_server_binary",
    "model_files",
    "database",
    "log_dir",
    "vram_budget_fit",
    "cluster_nodes_config",
    "cluster_nodes_online",
    "serving_capacity",
)


def _extract_serving_state(status: dict) -> tuple[tuple[str, ...], float, int | None]:
    """Lit l'état de service depuis `manager.status()` — purement en mémoire."""
    models = status.get("models") or []
    models_ready = tuple(
        str(m.get("id")) for m in models if m.get("state") == "ready"
    )
    budget = status.get("vram_budget") or {}
    try:
        available_gb = float(budget.get("available_gb") or 0.0)
    except (TypeError, ValueError):
        available_gb = 0.0
    # En cluster, status() expose nodes_online ; en local la clé est absente.
    raw_nodes = budget.get("nodes_online")
    nodes_online: int | None
    try:
        nodes_online = int(raw_nodes) if raw_nodes is not None else None
    except (TypeError, ValueError):
        nodes_online = None
    return models_ready, available_gb, nodes_online


async def evaluate_readiness(
    manager: Any | None = None,
    *,
    config: Settings | None = None,
    use_cache: bool = True,
) -> ReadinessReport:
    """
    Évalue la readiness structurelle (et observe la serving readiness).

    - `manager` : `LocalModelManager` ou `ClusterManager` (interface partagée :
      `.registry` et `.status()`). Par défaut, le singleton `model_manager`.
    - `config`  : objet de configuration. Par défaut, `config.settings`.
    - `use_cache` : `False` force une sonde système fraîche (utile pour
      `evaruntime doctor`, qui doit refléter l'instant présent).

    Ne lève jamais : toute anomalie devient un `CheckResult` en échec.
    """
    cfg: Any = config if config is not None else _module_settings
    if manager is None:
        from model_manager import model_manager as _default_manager
        manager = _default_manager

    mode = str(getattr(cfg, "cluster_mode", "local"))

    # État de service (en mémoire). Un échec ici est fail-closed sans fuite.
    try:
        status = manager.status()
        models_ready, available_gb, nodes_online = _extract_serving_state(status)
    except Exception as exc:  # défensif : status() ne devrait jamais lever
        log.warning("Readiness : manager.status() a échoué (%s).", exc)
        return ReadinessReport(
            mode=mode,
            checks=(
                CheckResult(
                    "manager_status", "fail", "status_unavailable",
                    "État du gestionnaire de modèles indisponible : "
                    f"{type(exc).__name__}.",
                ),
            ),
            models_ready=(),
            vram_available_gb=0.0,
        )

    try:
        enabled_models = list(manager.registry.list_enabled())
    except Exception as exc:
        log.warning("Readiness : registre inaccessible (%s).", exc)
        return ReadinessReport(
            mode=mode,
            checks=(
                CheckResult(
                    "models_config", "fail", "registry_unavailable",
                    f"Registre des modèles inaccessible : {type(exc).__name__}.",
                ),
            ),
            models_ready=models_ready,
            vram_available_gb=available_gb,
            nodes_online=nodes_online,
        )

    fs_checks = await _filesystem_checks(cfg, enabled_models, use_cache)

    by_name: dict[str, CheckResult] = dict(fs_checks)
    by_name["enabled_models"] = _check_enabled_models(enabled_models)
    by_name["secrets"] = _check_secrets(cfg)
    by_name["vram_budget_fit"] = _check_vram_budget_fit(cfg, enabled_models)
    by_name["cluster_nodes_online"] = _check_cluster_nodes_online(cfg, nodes_online)
    by_name["serving_capacity"] = _check_serving_capacity(models_ready, available_gb)

    checks = tuple(by_name[name] for name in _CHECK_ORDER if name in by_name)

    return ReadinessReport(
        mode=mode,
        checks=checks,
        models_ready=models_ready,
        vram_available_gb=available_gb,
        nodes_online=nodes_online,
    )


# ── Détection d'appelant privilégié ───────────────────────────────────────────

def caller_is_privileged(
    authorization: str | None,
    config: Settings | None = None,
) -> bool:
    """
    True si l'en-tête `Authorization` porte le bon `ADMIN_SECRET`.

    Utilisé par `/ready` pour décider d'ajouter (ou non) les messages détaillés,
    qui contiennent des chemins de fichiers. Comparaison constant-time.

    Fail-closed, avec EXACTEMENT le critère de `auth.require_admin` : un secret
    vide, placeholder ou plus court que `SECRET_MIN_LENGTH` ne privilégie
    personne. Un critère plus laxe ici rouvrirait la vue détaillée avec un
    secret que `/admin/*` refuse (« password ») — sur une route publique dont
    nginx ne journalise pas les accès.
    """
    cfg: Any = config if config is not None else _module_settings
    admin_secret = str(getattr(cfg, "admin_secret", ""))
    if secret_is_weak(admin_secret):
        return False
    if not authorization:
        return False
    scheme, _, token = authorization.partition(" ")
    if scheme.strip().lower() != "bearer" or not token.strip():
        return False
    return _secrets.compare_digest(token.strip().encode(), admin_secret.encode())
