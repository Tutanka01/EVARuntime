"""
`evaruntime doctor` — diagnostic préflight de la gateway principale (AUT-012).

Pourquoi ce module
------------------
Le node-agent possède depuis longtemps un préflight opératoire
(`node_agent/preflight.py`) : permissions des fichiers sensibles, TLS, binaire
exécutable, répertoires lisibles. La gateway principale, plus critique, n'en
avait pas. Ce module comble ce manque, avec quatre cas d'usage :

- manuellement, avant le premier `systemctl start` ;
- depuis `install.sh`, avant activation du service ;
- depuis `update.sh`, avant et après bascule de version ;
- par l'opérateur lors d'un incident.

Ce que doctor N'EST PAS
-----------------------
Ce n'est ni une sonde de liveness ni un smoke test. Il ne parle à aucun service
en cours d'exécution, n'ouvre aucune connexion HTTP vers la gateway et ne charge
aucun modèle. Il inspecte l'HÔTE et la CONFIGURATION. L'état de service vivant
reste du ressort de `GET /ready` (COR-005) et du smoke test de génération
(COR-006) ; les contrôles de readiness qui exigent un process vivant sont donc
rapportés en `skip` avec le code `service_not_running`.

Composition avec `readiness.py`
-------------------------------
Les contrôles structurels sont DÉJÀ implémentés et testés dans `readiness.py`
(COR-005) : registre présent, modèles activés, secrets non placeholder, binaire
présent, GGUF présents, base inscriptible, répertoire de logs, budget VRAM,
inventaire de nœuds. doctor les consomme via
`evaluate_readiness(..., use_cache=False)` — il ne les réimplémente pas — puis
ajoute les contrôles trop coûteux ou trop intrusifs pour une sonde HTTP appelée
en boucle par systemd et nginx :

| Contrôle ajouté           | Coût                                        |
|---------------------------|---------------------------------------------|
| `config_env_file`         | stat + lecture du fichier d'environnement   |
| `database_permissions`    | stat du fichier, du WAL et des parents      |
| `disk_space`              | statvfs                                     |
| `models_registry`         | parsing complet de models.yaml              |
| `llama_server_version`    | sous-processus `llama-server --version`     |
| `gpu_inventory`           | sous-processus `nvidia-smi`                 |
| `vram_detected`           | arithmétique sur la sonde GPU               |
| `model_artifacts`         | stat des GGUF (hash SEULEMENT sur demande)  |
| `port_pool`               | connexions TCP locales très courtes         |
| `nginx_timeouts`          | lecture + parsing du fichier nginx          |
| `tls_certificate`         | lecture et décodage des certificats fournis |
| `systemd_limits`          | lecture des unités systemd                  |
| `cluster_agent_secret`    | arithmétique sur la configuration           |
| `cluster_nodes_inventory` | parsing de nodes.yaml                       |

Non-divulgation
---------------
Aucun secret, token ou valeur sensible ne doit apparaître dans le rapport, en
sortie humaine comme en JSON. Trois mesures cumulatives :

1. les contrôles ne rapportent JAMAIS la valeur d'un secret, seulement son nom ;
2. tout message produit passe par `redact()` avant d'entrer dans le rapport, avec
   la liste des valeurs sensibles lues dans le fichier d'environnement (clés
   contenant SECRET/TOKEN/KEY/PASSWORD) et dans la configuration chargée. C'est
   pour cela que le fichier d'environnement est lu AVANT le chargement de la
   configuration : même un message d'erreur pydantic citant une valeur invalide
   est ainsi rédigé ;
3. aucun secret ne transite par `argv` (visible dans `ps`) : les seuls
   sous-processus lancés sont `llama-server --version` et `nvidia-smi --query-*`.

Les CHEMINS de fichiers, eux, sont conservés dans les messages : sans eux un
diagnostic n'est pas actionnable. doctor est une commande locale, exécutée par un
opérateur qui a déjà accès à ces fichiers — contrairement au corps public de
`/ready`, qui n'expose que des codes (cf. `readiness.ReadinessReport`).

Exit codes — cf. `docs/admin.md`
--------------------------------
| Code | Signification                                                       |
|------|---------------------------------------------------------------------|
| 0    | Tous les contrôles passent (aucun `fail`, aucun `warn`).            |
| 1    | Au moins un contrôle critique en échec — ne pas démarrer/basculer.  |
| 2    | RÉSERVÉ : erreur d'usage de la CLI (convention Typer/Click).        |
| 3    | Avertissements seulement — démarrage possible, dette à traiter.     |
| 4    | Erreur d'exécution de doctor lui-même (bug, environnement cassé).   |

`--strict` remonte les avertissements au rang d'échec bloquant (exit 1).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import ssl
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from pydantic_settings import DotEnvSettingsSource

import readiness
from config import SECRET_MIN_LENGTH, Settings, secret_is_placeholder
from config import settings as _ambient_settings
from llama_version import probe_llama_version
from model_registry import IntegrityError, ModelDefinition, ModelRegistry
from readiness import CheckResult

# ── Codes de sortie ───────────────────────────────────────────────────────────
# 2 est délibérément laissé libre : Typer/Click l'utilise pour les erreurs
# d'usage (option inconnue, argument manquant). Un doctor qui rendrait 2 pour
# « avertissements » serait indistinguable d'une faute de frappe dans
# install.sh — d'où 3 pour les avertissements.
EXIT_OK = 0
EXIT_BLOCKING = 1
EXIT_USAGE = 2
EXIT_WARNINGS = 3
EXIT_ERROR = 4

# Version du schéma JSON. À incrémenter si la forme du document change de façon
# non rétro-compatible : les scripts de déploiement s'appuient dessus.
SCHEMA_VERSION = 1

# Emplacements par défaut, tels que posés par `gateway/deploy/install.sh`. Tous
# surchargeables par option, et l'EnvironmentFile est d'abord RELU depuis l'unité
# systemd quand elle est disponible : doctor ne suppose un chemin que lorsqu'il
# ne peut pas le lire.
DEFAULT_ENV_FILE = Path("/etc/llm-gateway/env")
DEFAULT_NGINX_CONF = Path("/etc/nginx/sites-available/llm-gateway")
DEFAULT_SYSTEMD_UNIT = Path("/etc/systemd/system/llm-gateway.service")

# Noms de variables considérées comme sensibles dans le fichier d'environnement.
# Sert uniquement à alimenter la rédaction : la valeur n'est jamais affichée.
_SECRET_NAME_RE = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|_KEY$)", re.IGNORECASE
)

# En dessous de cette longueur, une valeur n'est pas rédigée : masquer « 0 » ou
# « true » rendrait les messages illisibles sans rien protéger.
_MIN_REDACTED_LENGTH = 6
_REDACTED = "***"

# `ServerManager.ensure_loaded` attend `load_timeout_seconds + 10`. Tout
# reverse-proxy devant la gateway doit couvrir cette attente, sinon le client
# reçoit 504 alors que le chargement réussit côté serveur (défaut EVA-004).
_LOAD_TIMEOUT_GRACE_SECONDS = 10

# RAM hôte consommée hors poids de modèle (processus gateway, RSS des
# llama-server, buffers). Même ordre de grandeur que la baseline de
# `docs/deployment.md` §15 et de `tests/test_deploy_memory_profiles.py`.
_BASELINE_HOST_RAM_GB = 2.0

# Tâches attendues par llama-server (threads de calcul, threads HTTP, threads
# internes CUDA). Permet de dériver le TasksMax minimal de MAX_LOADED_MODELS au
# lieu de coder une valeur en dur — COR-007 peut faire évoluer les unités.
_TASKS_PER_LLAMA_SERVER = 64

# Seuils d'espace disque sur le volume de la base et des logs.
_DISK_FAIL_GB = 0.5
_DISK_WARN_GB = 5.0

# Fenêtre d'alerte avant expiration d'un certificat TLS.
_TLS_EXPIRY_WARN_DAYS = 30

# Un GGUF plus petit que cela est un téléchargement interrompu, pas un modèle.
_MIN_PLAUSIBLE_GGUF_BYTES = 1024 * 1024

# Renonciation GPU explicite (OPS-012). `gateway/deploy/install.sh --allow-no-gpu`
# écrit cette clé dans le fichier d'environnement; c'est la MÊME constante que
# `GPU_WAIVER_ENV_KEY` dans `deploy/gpu-preflight-lib.sh`.
#
# La clé est lue dans le fichier d'environnement BRUT, pas dans `Settings` : ce
# n'est pas un réglage de la gateway (aucun code de service ne la consulte), mais
# la trace d'une décision d'installation. `Settings` la laisse passer sans bruit
# grâce à `extra="ignore"`.
GPU_WAIVER_ENV_KEY = "ALLOW_NO_GPU"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def gpu_waiver_declared(env_values: dict[str, str]) -> bool:
    """True si l'opérateur a assumé l'absence de GPU à l'installation."""
    return env_values.get(GPU_WAIVER_ENV_KEY, "").strip().lower() in _TRUTHY


# ── Options et rapport ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DoctorOptions:
    """Entrées de `run_doctor`. Aucune ne contient de secret."""
    env_file: Path | None = None
    nginx_conf: Path | None = None
    systemd_unit: Path | None = None
    verify_hashes: bool = False


@dataclass(frozen=True)
class DoctorReport:
    """Rapport complet. `checks` est ordonné, du structurel au conjoncturel."""
    mode: str
    config_source: str
    checks: tuple[CheckResult, ...]
    generated_at: str = ""

    @property
    def failed(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status == "fail")

    @property
    def blocking(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.is_blocking)

    @property
    def warnings(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.status == "warn")

    @property
    def reason(self) -> str | None:
        """Code du premier contrôle bloquant, ou None si rien ne bloque."""
        for check in self.checks:
            if check.is_blocking:
                return check.code
        return None

    def counts(self) -> dict[str, int]:
        counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        counts["blocking"] = len(self.blocking)
        return counts

    def status(self, *, strict: bool = False) -> str:
        if self.blocking or (strict and self.warnings):
            return "fail"
        # Un `fail` NON critique n'empêche pas de servir : il compte comme
        # avertissement, mais il reste visible et n'est jamais silencieux.
        if self.warnings or self.failed:
            return "warn"
        return "ok"

    def exit_code(self, *, strict: bool = False) -> int:
        state = self.status(strict=strict)
        if state == "fail":
            return EXIT_BLOCKING
        if state == "warn":
            return EXIT_WARNINGS
        return EXIT_OK

    def to_dict(self, *, strict: bool = False) -> dict:
        body: dict = {
            "tool": "evaruntime-doctor",
            "schema_version": SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "mode": self.mode,
            "config_source": self.config_source,
            "strict": strict,
            "status": self.status(strict=strict),
            "exit_code": self.exit_code(strict=strict),
            "summary": self.counts(),
            "checks": [c.to_dict() for c in self.checks],
        }
        reason = self.reason
        if reason is not None:
            body["reason"] = reason
        return body


# ── Agrégation de contrôles multi-parties ─────────────────────────────────────

_SEVERITY = {"pass": 0, "skip": 1, "warn": 2, "fail": 3}


@dataclass(frozen=True)
class _Finding:
    """Constat élémentaire, agrégé ensuite en un `CheckResult` unique."""
    status: str
    code: str
    message: str
    critical: bool = True


def _combine(
    name: str,
    findings: Sequence[_Finding],
    ok_message: str,
    *,
    ok_code: str = "ok",
    critical: bool = True,
) -> CheckResult:
    """
    Réduit une liste de constats en un `CheckResult` unique.

    Le statut retenu est le plus sévère ; la criticité est celle des constats
    retenus (un `fail` non critique reste non bloquant) ; le message concatène
    tous les constats de ce niveau pour rester actionnable.
    """
    if not findings:
        return CheckResult(name, "pass", ok_code, ok_message, critical=critical)
    worst = max(_SEVERITY[f.status] for f in findings)
    selected = [f for f in findings if _SEVERITY[f.status] == worst]
    return CheckResult(
        name,
        selected[0].status,  # type: ignore[arg-type]
        selected[0].code,
        " ".join(f.message for f in selected),
        critical=any(f.critical for f in selected),
    )


# ── Rédaction des valeurs sensibles ───────────────────────────────────────────

def parse_env_file(path: Path) -> dict[str, str]:
    """
    Lit un EnvironmentFile systemd en paires clé/valeur, best-effort.

    Sert à deux choses seulement : alimenter la liste de rédaction et
    diagnostiquer la syntaxe. Ne lève jamais — un fichier illisible renvoie un
    dictionnaire vide (et le contrôle `config_env_file` le signale par ailleurs).
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def collect_secret_values(
    config: Any | None,
    env_values: dict[str, str],
) -> tuple[str, ...]:
    """
    Valeurs à masquer dans tout message du rapport.

    Source 1 : les variables du fichier d'environnement dont le NOM ressemble à
    un secret. Cela couvre HF_TOKEN, HUGGING_FACE_HUB_TOKEN, un mot de passe de
    proxy ou toute variable ajoutée plus tard, sans que doctor ait besoin de la
    connaître.
    Source 2 : les secrets de la configuration chargée, au cas où ils viendraient
    de l'environnement ambiant plutôt que du fichier.
    """
    candidates: list[str] = [
        value for name, value in env_values.items()
        if _SECRET_NAME_RE.search(name) and value
    ]
    for attr in ("admin_secret", "internal_api_key", "agent_secret"):
        value = str(getattr(config, attr, "") or "")
        if value:
            candidates.append(value)
    # Les plus longues d'abord : un secret qui contient un autre secret en
    # préfixe doit être masqué en entier.
    unique = {c for c in candidates if len(c) >= _MIN_REDACTED_LENGTH}
    return tuple(sorted(unique, key=len, reverse=True))


def redact(text: str, secrets: Iterable[str]) -> str:
    """Remplace toute occurrence d'une valeur sensible par `***`."""
    for value in secrets:
        if value and value in text:
            text = text.replace(value, _REDACTED)
    return text


def _redact_check(check: CheckResult, secrets: Sequence[str]) -> CheckResult:
    if not secrets:
        return check
    message = redact(check.message, secrets)
    if message == check.message:
        return check
    return CheckResult(check.name, check.status, check.code, message, check.critical)


# ── Chargement de la configuration ciblée ─────────────────────────────────────

def load_settings_from_env_file(path: Path) -> Settings:
    """
    Charge EXACTEMENT le fichier d'environnement visé, sans override ambiant.

    Même technique que `node_agent/preflight.py` : fournir explicitement tous les
    defaults en kwargs leur donne priorité sur `os.environ` dans `BaseSettings`.
    Sans cela, doctor validerait l'environnement du shell de l'opérateur (ou de
    `install.sh`) au lieu de celui que systemd donnera au service — le défaut le
    plus difficile à voir dans un préflight.
    """
    source = DotEnvSettingsSource(Settings, env_file=path, env_file_encoding="utf-8")
    raw = source()
    values: dict[str, Any] = {
        name: field_info.get_default(call_default_factory=True)
        for name, field_info in Settings.model_fields.items()
        if not field_info.is_required()
    }
    values.update(raw)
    return Settings(_env_file=None, **values)


def _complex_settings_fields() -> tuple[str, ...]:
    """
    Champs de `Settings` que pydantic-settings parserait exclusivement en JSON.

    Dérivé du modèle et non codé en dur. Depuis COR-014, les champs de liste
    réellement exposés à l'opérateur (`ALLOWED_MODEL_DIRS`, `CORS_ALLOW_ORIGINS`)
    sont annotés `str | list[str]` et acceptent le format CSV documenté : cette
    fonction ne renvoie donc plus rien aujourd'hui. Elle reste comme garde-fou —
    si un futur champ est déclaré comme conteneur nu, le format « a,b » ferait de
    nouveau échouer le DÉMARRAGE du service, et doctor doit savoir le nommer.
    """
    names: list[str] = []
    for name, field_info in Settings.model_fields.items():
        annotation = field_info.annotation
        origin = getattr(annotation, "__origin__", annotation)
        if origin in (list, set, tuple, dict):
            names.append(name)
    return tuple(names)


def _config_load_hint(exc: Exception) -> str:
    """Message actionnable pour un échec de chargement de la configuration."""
    text = str(exc)
    for name in _complex_settings_fields():
        if name in text:
            return (
                f"La variable {name.upper()} est typée comme une liste : "
                f"pydantic-settings la parse en JSON, et un format « a,b » fait "
                f"ÉCHOUER le démarrage du service. Écrivez "
                f"{name.upper()}=[\"/valeur1\",\"/valeur2\"], ou retirez la ligne "
                f"pour conserver le défaut."
            )
    return (
        "Corrigez la variable citée dans le fichier d'environnement puis relancez "
        "doctor : le service refuserait de démarrer avec cette valeur. Les "
        "variables de type liste acceptent le format « a,b » ou un tableau JSON "
        "[\"a\",\"b\"] ; une valeur vide conserve le défaut."
    )


# ── Manager hors-ligne consommé par readiness ─────────────────────────────────

class _EmptyRegistry:
    """Registre vide, utilisé quand models.yaml n'a pas pu être chargé."""

    def list_all(self) -> list[ModelDefinition]:
        return []

    def list_enabled(self) -> list[ModelDefinition]:
        return []


class _OfflineManager:
    """
    Adaptateur minimal exposant l'interface consommée par `evaluate_readiness`
    (`.registry` et `.status()`), SANS aucun effet de bord.

    Importer `model_manager` construirait le singleton : lecture du registre,
    allocation du pool de ports, et refus explicite de démarrer en mode cluster
    si `AGENT_SECRET` est faible. Or doctor doit précisément fonctionner AVANT le
    premier démarrage et DIAGNOSTIQUER ces cas plutôt que mourir avec eux.
    """

    def __init__(self, registry: Any, config: Any) -> None:
        self.registry = registry
        self._config = config

    def status(self) -> dict:
        try:
            available = max(0.0, float(self._config.effective_vram_budget_gb()))
        except Exception:
            available = 0.0
        return {"models": [], "vram_budget": {"available_gb": available}}


def _override(
    checks: dict[str, CheckResult],
    name: str,
    code: str,
    message: str,
    *,
    status: str = "skip",
) -> None:
    """
    Remplace un contrôle de readiness par un constat non bloquant documenté.

    Deux situations seulement : le verdict n'a pas de sens hors process vivant
    (`serving_capacity`, `cluster_nodes_online`), ou il serait un doublon
    trompeur d'un contrôle doctor déjà en échec (`enabled_models` quand le
    registre est illisible). On ne masque jamais un problème : le message
    renvoie explicitement vers le contrôle qui porte le verdict.
    """
    if name in checks:
        checks[name] = CheckResult(
            name, status, code, message, critical=False  # type: ignore[arg-type]
        )


# ── Utilitaires système ───────────────────────────────────────────────────────

def _is_root() -> bool:
    try:
        return os.geteuid() == 0
    except AttributeError:  # plateformes sans identifiants POSIX
        return False


def _file_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def world_reachable(path: Path) -> bool:
    """
    True si un utilisateur quelconque peut ATTEINDRE `path`.

    Un fichier en mode 0644 dans un répertoire 0750 n'est pas lisible par tous :
    juger le mode du fichier seul produirait un faux positif sur une
    installation correcte (`install.sh` met `chmod 750` sur /var/lib/llm-gateway
    et laisse la base au umask du service). On remonte donc la chaîne des
    répertoires parents et on exige `o+x` partout.
    """
    for parent in path.parents:
        mode = _file_mode(parent)
        if mode is None:
            # Répertoire inaccessible à doctor : on ne peut pas conclure. On
            # reste prudent (« atteignable ») pour ne jamais taire un risque.
            return True
        if not mode & stat.S_IXOTH:
            return False
    return True


def _host_ram_gb() -> float | None:
    """RAM physique de l'hôte en Go, ou None si non déterminable."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None
    if not pages or not page_size:
        return None
    return (pages * page_size) / 1024**3


def _probe_host(host: str) -> str:
    """Hôte joignable localement pour une sonde de port (wildcard → loopback)."""
    if host in ("0.0.0.0", ""):
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def port_is_occupied(port: int, host: str, timeout: float = 0.2) -> bool:
    """
    Sonde best-effort d'un port TCP local.

    Même sémantique que `model_manager._port_is_occupied` (dupliquée
    volontairement : importer `model_manager` construirait le singleton et ses
    effets de bord, ce que doctor doit éviter). Attrape TOUT → False.
    """
    try:
        with socket.create_connection((_probe_host(host), port), timeout=timeout):
            return True
    except Exception:
        return False


# ── Contrôle : fichier d'environnement (secrets) ───────────────────────────────

def check_env_file(
    path: Path | None,
    *,
    explicit: bool,
    service_user: str | None = None,
) -> CheckResult:
    """
    Permissions du fichier qui porte les secrets du service.

    Un EnvironmentFile lisible par tous expose `ADMIN_SECRET` et
    `INTERNAL_API_KEY` : c'est un défaut critique, traité comme dans
    `node_agent/preflight.validate_sensitive_file` (attendu 0600 ou 0640, jamais
    group-writable ni world-readable).

    Contrôle complémentaire, cause de panne au démarrage fréquente : si l'unité
    systemd déclare `User=`, le fichier doit être LISIBLE par cet utilisateur.
    Un 0600 root:root avec `User=llmservice` fait échouer le démarrage avec un
    message systemd peu explicite.
    """
    if path is None:
        return CheckResult(
            "config_env_file", "skip", "env_file_not_found",
            "Aucun fichier d'environnement trouvé : la configuration ambiante a "
            f"été utilisée. Passez --env-file si le vôtre n'est pas à "
            f"{DEFAULT_ENV_FILE}.",
            critical=False,
        )
    if not path.is_file():
        return CheckResult(
            "config_env_file", "fail", "env_file_missing",
            f"Fichier d'environnement introuvable : {path}. "
            "Créez-le (cf. gateway/.env.example) ou corrigez --env-file.",
            critical=explicit,
        )

    findings: list[_Finding] = []
    mode = _file_mode(path)
    if mode is None:
        findings.append(_Finding(
            "warn", "env_file_mode_unknown",
            f"Permissions de {path} illisibles par doctor.", critical=False,
        ))
    else:
        if mode & 0o027:
            findings.append(_Finding(
                "fail", "env_file_permissions_too_open",
                f"Permissions trop ouvertes sur le fichier de secrets {path} "
                f"(mode {mode:04o}) : attendu 0600 ou 0640, jamais "
                f"group-writable ni world-readable. Corrigez avec : "
                f"chmod 640 {path}.",
            ))
        if not os.access(path, os.R_OK):
            findings.append(_Finding(
                "fail", "env_file_unreadable",
                f"Fichier d'environnement illisible par l'utilisateur courant : "
                f"{path}. Relancez doctor via sudo, ou corrigez le propriétaire.",
                critical=False,
            ))
        elif service_user:
            findings.extend(_service_can_read(path, mode, service_user))

    return _combine(
        "config_env_file", findings,
        f"Fichier de secrets {path} correctement protégé (mode {mode:04o})."
        if mode is not None else f"Fichier de secrets {path} présent.",
    )


def _service_can_read(path: Path, mode: int, service_user: str) -> list[_Finding]:
    """
    L'utilisateur du service peut-il lire le fichier de secrets ?

    Dégrade proprement (aucun constat) si l'utilisateur n'existe pas encore sur
    l'hôte — c'est le cas normal avant le premier `install.sh`.
    """
    try:
        import grp
        import pwd

        entry = pwd.getpwnam(service_user)
        info = path.stat()

        if mode & stat.S_IROTH:
            return []
        if info.st_uid == entry.pw_uid and mode & stat.S_IRUSR:
            return []
        in_group = info.st_gid == entry.pw_gid or service_user in (
            grp.getgrgid(info.st_gid).gr_mem
        )
        if in_group and mode & stat.S_IRGRP:
            return []
    except Exception:
        # Utilisateur ou groupe inconnu de l'hôte : c'est le cas normal avant le
        # premier install.sh. On ne conclut pas.
        return []
    return [_Finding(
        "fail", "env_file_unreadable_by_service",
        f"Le fichier de secrets {path} (mode {mode:04o}) n'est pas lisible par "
        f"l'utilisateur du service « {service_user} » déclaré dans l'unité "
        f"systemd : le démarrage échouera. Corrigez avec : "
        f"chown root:{service_user} {path} && chmod 640 {path}.",
    )]


# ── Contrôle : permissions de la base ─────────────────────────────────────────

def check_database_permissions(config: Any) -> CheckResult:
    """
    La base contient les hashes de clés API, les emails et le journal d'usage.

    L'inscriptibilité est déjà couverte par le contrôle `database` de
    `readiness.py` ; ici on juge l'EXPOSITION. Un mode 0644 n'est pas un défaut
    en soi si le répertoire parent interdit la traversée (cas d'une installation
    correcte) : on ne signale que ce qui est réellement atteignable.
    """
    raw = str(getattr(config, "db_path", ""))
    if raw == ":memory:":
        return CheckResult(
            "database_permissions", "skip", "in_memory",
            "Base SQLite en mémoire : aucun fichier à protéger.", critical=False,
        )

    db_path = Path(raw)
    findings: list[_Finding] = []
    # SQLite en WAL crée -wal et -shm à côté du fichier : ils portent les mêmes
    # données et méritent le même examen.
    targets = [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
    checked = 0
    for target in targets:
        mode = _file_mode(target)
        if mode is None:
            continue
        checked += 1
        if mode & stat.S_IROTH and world_reachable(target):
            findings.append(_Finding(
                "fail", "database_world_readable",
                f"{target} est lisible par tous (mode {mode:04o}) et son "
                f"répertoire parent n'empêche pas la traversée : hashes de clés "
                f"API, emails et journal d'usage exposés. Corrigez avec : "
                f"chmod 640 {target} et chmod 750 {target.parent}.",
            ))
        elif mode & stat.S_IWGRP:
            findings.append(_Finding(
                "warn", "database_group_writable",
                f"{target} est modifiable par son groupe (mode {mode:04o}) : "
                f"attendu 0600 ou 0640.", critical=False,
            ))

    parent_mode = _file_mode(db_path.parent)
    if parent_mode is not None and parent_mode & (stat.S_IROTH | stat.S_IWOTH):
        findings.append(_Finding(
            "warn", "database_dir_world_accessible",
            f"Répertoire de la base {db_path.parent} accessible aux autres "
            f"utilisateurs (mode {parent_mode:04o}) : attendu 0750.",
            critical=False,
        ))

    if checked == 0 and not findings:
        return CheckResult(
            "database_permissions", "skip", "database_absent",
            f"Base non encore créée ({db_path}) : elle sera initialisée au "
            f"premier démarrage. Vérifiez le mode après création.",
            critical=False,
        )
    return _combine(
        "database_permissions", findings,
        f"Base et fichiers WAL correctement protégés ({checked} fichier(s) "
        f"contrôlé(s)).",
    )


# ── Contrôle : espace disque ──────────────────────────────────────────────────

def check_disk_space(config: Any) -> CheckResult:
    """
    Espace libre sur les volumes de la base et des logs.

    Un volume plein rend SQLite en écriture inopérant (WAL) et fait perdre les
    logs par modèle. Coût : un `statvfs` par volume distinct.
    """
    raw_db = str(getattr(config, "db_path", ""))
    targets: dict[str, Path] = {}
    if raw_db and raw_db != ":memory:":
        targets["base"] = Path(raw_db).parent
    log_dir = str(getattr(config, "log_dir", ""))
    if log_dir:
        targets["logs"] = Path(log_dir)

    findings: list[_Finding] = []
    reported: list[str] = []
    seen: set[int] = set()
    for label, directory in targets.items():
        try:
            device = directory.stat().st_dev
            usage = shutil.disk_usage(directory)
        except OSError:
            continue  # répertoire absent : déjà signalé par readiness
        free_gb = usage.free / 1024**3
        if device in seen:
            continue  # même volume que le précédent : un seul constat suffit
        seen.add(device)
        reported.append(f"{label} ({directory}) : {free_gb:.1f} Go libres")
        if free_gb < _DISK_FAIL_GB:
            findings.append(_Finding(
                "fail", "disk_space_exhausted",
                f"Volume de {label} presque plein : {free_gb:.2f} Go libres sur "
                f"{directory}. SQLite en WAL et les logs vont échouer. Libérez "
                f"de l'espace ou déplacez le volume avant de démarrer.",
            ))
        elif free_gb < _DISK_WARN_GB:
            findings.append(_Finding(
                "warn", "disk_space_low",
                f"Volume de {label} bas : {free_gb:.1f} Go libres sur "
                f"{directory} (seuil d'alerte {_DISK_WARN_GB:.0f} Go).",
                critical=False,
            ))

    if not reported:
        return CheckResult(
            "disk_space", "skip", "no_volume_to_check",
            "Aucun volume à contrôler (base en mémoire, répertoires absents).",
            critical=False,
        )
    return _combine(
        "disk_space", findings,
        "Espace disque suffisant — " + " ; ".join(reported) + ".",
    )


# ── Contrôle : registre des modèles ───────────────────────────────────────────

def check_models_registry(config: Any) -> tuple[CheckResult, Any]:
    """
    Charge réellement models.yaml et renvoie (résultat, registre).

    `readiness.py` se contente d'un `stat` sur le fichier — c'est le bon coût
    pour `/ready`. doctor le PARSE : un `vram_gb` manquant, un chemin relatif, un
    GGUF hors de `ALLOWED_MODEL_DIRS` ou un `cache_type_k` invalide empêchent le
    démarrage, et c'est exactement ce qu'un préflight doit dire avant.
    """
    path = Path(str(getattr(config, "models_config_path", "")))
    allowed = getattr(config, "allowed_model_dirs", None) or None
    try:
        registry = ModelRegistry(config_path=path, allowed_model_dirs=allowed)
    except Exception as exc:
        return (
            CheckResult(
                "models_registry", "fail", "models_registry_invalid",
                f"Registre des modèles invalide ({path}) : {exc} "
                "La gateway refuserait de démarrer.",
            ),
            _EmptyRegistry(),
        )

    total = len(registry.list_all())
    enabled = len(registry.list_enabled())
    return (
        CheckResult(
            "models_registry", "pass", "ok",
            f"Registre chargé et validé : {total} modèle(s) déclaré(s), "
            f"{enabled} activé(s).",
        ),
        registry,
    )


# ── Contrôle : version du binaire llama-server ─────────────────────────────────

async def check_llama_server_version(config: Any) -> CheckResult:
    """
    Exécute `llama-server --version` et confronte le build à la politique.

    `/ready` ne peut pas se le permettre (sous-processus sur un chemin sondé en
    boucle). `main._validate_inference_runtime`, le préflight node-agent et ce
    contrôle appliquent la même politique FAIL-CLOSED exigée par §6 : si un
    build minimal est demandé (`LLAMA_SERVER_MIN_BUILD > 0`) mais que la version
    est illisible, la validation échoue. Doctor rend en plus ce verdict dans un
    rapport opératoire stable.

    Aucun secret ne transite par `argv` : seul `--version` est passé.
    """
    if str(getattr(config, "cluster_mode", "local")) == "cluster":
        return CheckResult(
            "llama_server_version", "skip", "delegated_to_nodes",
            "Mode cluster : le binaire llama-server vit sur les nœuds et sa "
            "version est validée par leur préflight/node-agent.",
            critical=False,
        )

    binary = Path(str(getattr(config, "llama_server_bin", "")))
    min_build = int(getattr(config, "llama_server_min_build", 0) or 0)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return CheckResult(
            "llama_server_version", "skip", "binary_unavailable",
            f"Binaire llama-server absent ou non exécutable ({binary}) : sonde "
            "de version impossible. Voir le contrôle llama_server_binary.",
            critical=False,
        )

    version = await probe_llama_version(binary)

    if version.build is None:
        if min_build > 0:
            return CheckResult(
                "llama_server_version", "fail", "llama_server_version_unreadable",
                f"LLAMA_SERVER_MIN_BUILD={min_build} est exigé mais la version de "
                f"{binary} est illisible ({version.raw[:120]}). Politique "
                "fail-closed : impossible de prouver que le binaire est patché "
                "(cf. GHSA-8947-pfff-2f3c). Réinstallez un llama-server dont "
                "`--version` répond, ou retirez l'enforcement en connaissance de "
                "cause.",
            )
        return CheckResult(
            "llama_server_version", "warn", "llama_server_version_unreadable",
            f"Version de {binary} illisible ({version.raw[:120]}) et aucun build "
            "minimal exigé : le garde-fou supply-chain est inerte. Fixez "
            "LLAMA_SERVER_MIN_BUILD sur un build patché.",
            critical=False,
        )

    if min_build > 0 and version.build < min_build:
        return CheckResult(
            "llama_server_version", "fail", "llama_server_build_too_old",
            f"llama-server build {version.build} < minimum requis {min_build} "
            f"(LLAMA_SERVER_MIN_BUILD) : binaire potentiellement vulnérable "
            f"(GHSA-8947-pfff-2f3c, overflows de parsing GGUF). Mettez llama.cpp "
            f"à jour ; la gateway refuserait de démarrer en l'état.",
        )

    if min_build == 0:
        return CheckResult(
            "llama_server_version", "warn", "min_build_not_enforced",
            f"llama-server build {version.build} détecté, mais "
            "LLAMA_SERVER_MIN_BUILD=0 : aucun plancher de version n'est imposé. "
            f"Fixez LLAMA_SERVER_MIN_BUILD={version.build} (ou le premier build "
            "patché connu) pour activer le garde-fou supply-chain.",
            critical=False,
        )

    return CheckResult(
        "llama_server_version", "pass", "ok",
        f"llama-server build {version.build} ≥ minimum exigé {min_build}.",
    )


# ── Contrôle : inventaire GPU ─────────────────────────────────────────────────

@dataclass(frozen=True)
class GpuInfo:
    """Un GPU tel que rapporté par nvidia-smi. L'UUID n'est jamais affiché."""
    index: int | None
    uuid: str
    name: str
    memory_total_mib: float | None
    driver_version: str
    compute_capability: str


# Requête exactement telle que spécifiée par le plan de consolidation §5.
_NVIDIA_SMI_QUERY = (
    "--query-gpu=index,uuid,name,memory.total,driver_version,compute_cap"
)


def parse_nvidia_smi_csv(text: str) -> list[GpuInfo]:
    """Parse la sortie `--format=csv,noheader,nounits`. Ne lève jamais."""
    gpus: list[GpuInfo] = []
    for line in text.splitlines():
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 6 or not fields[2]:
            continue
        try:
            index: int | None = int(fields[0])
        except ValueError:
            index = None
        memory_raw = fields[3].strip().lower()
        if memory_raw in {"[n/a]", "n/a"}:
            # Les GB10/PGX exposent une mémoire unifiée, mais nvidia-smi peut
            # répondre [N/A] pour memory.total. Le GPU reste visible dans doctor
            # et la capacité est alors explicitement confiée à TOTAL_VRAM_GB.
            memory: float | None = None
        else:
            try:
                memory = float(fields[3])
            except ValueError:
                continue
        gpus.append(GpuInfo(
            index=index,
            uuid=fields[1],
            name=fields[2],
            memory_total_mib=memory,
            driver_version=fields[4],
            compute_capability=fields[5],
        ))
    return gpus


async def probe_nvidia_smi(timeout: float = 5.0) -> tuple[list[GpuInfo] | None, str]:
    """
    Interroge nvidia-smi. Retourne (GPU, raison) — `None` si la sonde a échoué.

    Aucun secret n'est passé en argument. Attrape TOUT : nvidia-smi absent est un
    cas légitime (mode cluster, hôte de développement sans GPU).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            _NVIDIA_SMI_QUERY,
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return None, "nvidia-smi introuvable dans le PATH"
    except Exception as exc:
        return None, f"nvidia-smi non exécutable ({type(exc).__name__})"

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None, "timeout de la sonde nvidia-smi"
    except Exception as exc:
        return None, f"erreur de sonde nvidia-smi ({type(exc).__name__})"

    text = (stdout or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        return None, f"nvidia-smi a échoué (code {proc.returncode})"
    return parse_nvidia_smi_csv(text), text.strip()[:200]


def check_gpu_inventory(
    config: Any,
    gpus: list[GpuInfo] | None,
    reason: str,
    *,
    waived: bool = False,
) -> CheckResult:
    """
    GPU, driver et compute capability détectés.

    nvidia-smi absent n'est PAS traité comme une erreur bloquante :
    - en mode cluster, les GPU vivent sur les nœuds → `skip` ;
    - en mode local, c'est un défaut réel (`install.sh` exige nvidia-smi) mais un
      hôte CPU-only ou un conteneur de développement doit rester diagnosticable
      → `warn` non bloquant. Le contrôle qui bloque vraiment est `vram_detected`
      quand la VRAM configurée dépasse la VRAM réelle.

    Renonciation explicite (OPS-012)
    --------------------------------
    « Pas de GPU, assumé » et « GPU attendu mais absent » ne sont pas le même
    diagnostic et ne rendent pas le même verdict :

    | Situation                              | Statut | Code                     |
    |----------------------------------------|--------|--------------------------|
    | GPU absent, `ALLOW_NO_GPU` non déclaré  | `warn` | `nvidia_smi_unavailable` |
    | GPU absent, `ALLOW_NO_GPU=true`         | `skip` | `gpu_absence_declared`   |
    | GPU présent, `ALLOW_NO_GPU=true`        | `warn` | `gpu_waiver_stale`       |

    Le `skip` est délibéré : un hôte installé exprès sans GPU ne doit pas rendre
    doctor jaune à chaque exécution — un avertissement permanent qu'on apprend à
    ignorer ne protège plus de rien, c'est exactement ce qui a discrédité le
    préflight. Il reste VISIBLE dans le rapport, avec un code distinct : le
    diagnostic dit « décision », pas « panne ». À l'inverse, une renonciation
    devenue fausse (GPU présent) est signalée, pour qu'elle ne survive pas à
    l'hôte qui l'a justifiée.
    """
    if str(getattr(config, "cluster_mode", "local")) == "cluster":
        return CheckResult(
            "gpu_inventory", "skip", "delegated_to_nodes",
            "Mode cluster : l'orchestrateur n'a pas de GPU local, l'inventaire "
            "matériel est celui des nœuds (heartbeat node-agent).",
            critical=False,
        )
    if gpus is None or not gpus:
        detail = (
            f"Sonde GPU impossible : {reason}."
            if gpus is None else
            "nvidia-smi répond mais ne rapporte aucun GPU."
        )
        if waived:
            return CheckResult(
                "gpu_inventory", "skip", "gpu_absence_declared",
                f"{detail} Absence de GPU ASSUMÉE à l'installation "
                f"({GPU_WAIVER_ENV_KEY}=true, posé par « install.sh "
                f"--allow-no-gpu ») : ce n'est pas une panne. Aucun modèle n'est "
                f"offloadé, la génération se fait sur CPU. Retirez "
                f"{GPU_WAIVER_ENV_KEY} du fichier d'environnement dès qu'un GPU "
                f"est en place, pour que ce contrôle redevienne diagnostique.",
                critical=False,
            )
        if gpus is None:
            return CheckResult(
                "gpu_inventory", "warn", "nvidia_smi_unavailable",
                f"{detail} En mode local, install.sh exige nvidia-smi — vérifiez "
                "le pilote NVIDIA et le PATH du service. Sur un hôte sans GPU, "
                "aucun modèle ne pourra être offloadé ; si c'est délibéré, "
                "réinstallez avec « install.sh --mode local --allow-no-gpu » "
                f"pour que ce choix soit tracé ({GPU_WAIVER_ENV_KEY}) au lieu "
                "d'être confondu avec une panne de pilote.",
                critical=False,
            )
        return CheckResult(
            "gpu_inventory", "warn", "no_gpu_detected",
            f"{detail} Pilote chargé sans carte visible (conteneur sans --gpus, "
            "ou GPU en erreur).",
            critical=False,
        )

    def describe(gpu: GpuInfo) -> str:
        memory = (
            f"{gpu.memory_total_mib / 1024:.1f} Go"
            if gpu.memory_total_mib is not None
            else "mémoire non rapportée"
        )
        return (
            f"GPU {gpu.index}: {gpu.name}, {memory}, "
            f"driver {gpu.driver_version}, compute {gpu.compute_capability}"
        )

    described = "; ".join(describe(gpu) for gpu in gpus)
    if waived:
        return CheckResult(
            "gpu_inventory", "warn", "gpu_waiver_stale",
            f"{len(gpus)} GPU détecté(s) — {described}. Or "
            f"{GPU_WAIVER_ENV_KEY}=true déclare cet hôte sans GPU : la "
            "renonciation a survécu au matériel qui la justifiait. Retirez-la du "
            "fichier d'environnement, sinon une future panne de pilote sera "
            "diagnostiquée comme une décision.",
            critical=False,
        )
    return CheckResult(
        "gpu_inventory", "pass", "ok",
        f"{len(gpus)} GPU détecté(s) — {described}.",
    )


# ── Contrôle : VRAM détectée vs configurée ────────────────────────────────────

def visible_devices(config: Any, gpus: Sequence[GpuInfo]) -> tuple[list[GpuInfo], list[str]]:
    """
    Restreint l'inventaire aux devices réellement exposés au service.

    `CUDA_VISIBLE_DEVICES` est propagé aux sous-processus llama-server par
    `ServerManager` : c'est donc CETTE liste, et non l'ensemble des GPU de
    l'hôte, qui détermine la VRAM disponible. Accepte les index (`0,1`) comme les
    UUID (`GPU-…`). Retourne (devices visibles, jetons non résolus).
    """
    raw = str(getattr(config, "cuda_visible_devices", "") or "")
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        return [], []

    visible: list[GpuInfo] = []
    unknown: list[str] = []
    for token in tokens:
        match = None
        if token.isdigit():
            match = next((g for g in gpus if g.index == int(token)), None)
        else:
            match = next((g for g in gpus if g.uuid == token), None)
        if match is None:
            unknown.append(token)
        elif match not in visible:
            visible.append(match)
    return visible, unknown


def check_vram_detected(
    config: Any,
    gpus: list[GpuInfo] | None,
) -> CheckResult:
    """
    Le budget VRAM doit tenir dans la VRAM réellement exposée.

    Le critère BLOQUANT est le budget NET (`effective_vram_budget_gb()`, soit
    `TOTAL_VRAM_GB` moins l'overhead et la marge) : c'est lui que le contrôle
    d'admission distribue. S'il dépasse la VRAM physique visible, `llama-server`
    meurt en cours de chargement sans qu'aucune éviction n'y change quoi que ce
    soit.

    Un `TOTAL_VRAM_GB` nominal légèrement supérieur à la VRAM utilisable (48
    « Go » commerciaux contre 46068 MiB réellement exposés sur une L40S, par
    exemple) est un défaut d'hygiène qui ronge la marge de sécurité, mais que
    cette marge absorbe : il est signalé sans bloquer. Sous-estimer gaspille de
    la capacité — signalé également sans bloquer.
    """
    if str(getattr(config, "cluster_mode", "local")) == "cluster":
        return CheckResult(
            "vram_detected", "skip", "delegated_to_nodes",
            "Mode cluster : la VRAM est celle des nœuds, lue dynamiquement au "
            "heartbeat.", critical=False,
        )
    if not gpus:
        return CheckResult(
            "vram_detected", "skip", "gpu_inventory_unavailable",
            "Aucun inventaire GPU exploitable : impossible de confronter "
            "TOTAL_VRAM_GB au matériel (voir gpu_inventory).",
            critical=False,
        )

    configured = float(getattr(config, "total_vram_gb", 0.0) or 0.0)
    try:
        net_budget = float(config.effective_vram_budget_gb())
    except Exception:
        net_budget = configured
    raw = str(getattr(config, "cuda_visible_devices", "") or "")
    visible, unknown = visible_devices(config, gpus)

    if not raw.strip():
        return CheckResult(
            "vram_detected", "fail", "no_visible_gpu",
            f"CUDA_VISIBLE_DEVICES est vide alors que {len(gpus)} GPU sont "
            "présents : aucun device ne sera exposé aux llama-server et tout "
            "chargement échouera. Renseignez CUDA_VISIBLE_DEVICES (ex. « 0 »).",
        )
    if unknown:
        return CheckResult(
            "vram_detected", "fail", "cuda_visible_devices_invalid",
            f"CUDA_VISIBLE_DEVICES={raw} désigne des devices inexistants "
            f"({', '.join(unknown)}) ; GPU réellement présents : "
            f"{', '.join(str(g.index) for g in gpus)}. CUDA n'exposera que les "
            "devices situés AVANT le premier jeton invalide.",
        )

    if any(g.memory_total_mib is None for g in visible):
        return CheckResult(
            "vram_detected", "skip", "vram_not_reported",
            f"nvidia-smi expose {len(visible)}/{len(gpus)} GPU(s), mais ne "
            "rapporte pas memory.total pour ce matériel. Le budget effectif "
            f"reste celui de la configuration (TOTAL_VRAM_GB={configured:.1f} Go, "
            f"budget net {net_budget:.1f} Go) ; "
            "vérifiez TOTAL_VRAM_GB avec la fiche du matériel et nvidia-smi.",
            critical=False,
        )

    detected = sum(g.memory_total_mib for g in visible) / 1024.0
    described = (
        f"{len(visible)}/{len(gpus)} GPU exposé(s) par CUDA_VISIBLE_DEVICES={raw} "
        f"→ {detected:.1f} Go détectés, TOTAL_VRAM_GB={configured:.1f}, "
        f"budget net {net_budget:.1f} Go"
    )
    if net_budget > detected:
        return CheckResult(
            "vram_detected", "fail", "vram_budget_exceeds_hardware",
            f"{described}. Le contrôle d'admission peut distribuer plus de VRAM "
            f"qu'il n'en existe : les chargements échoueront en cours de route, "
            f"sans qu'aucune éviction n'y remédie. Abaissez TOTAL_VRAM_GB à "
            f"{detected:.1f} au plus (ou relevez VRAM_OVERHEAD_GB / "
            f"VRAM_SAFETY_MARGIN).",
        )
    if configured > detected:
        return CheckResult(
            "vram_detected", "warn", "total_vram_gb_overstated",
            f"{described}. TOTAL_VRAM_GB annonce plus de VRAM que le matériel "
            f"n'en expose (capacité commerciale contre MiB réellement "
            f"disponibles) : l'écart est aujourd'hui absorbé par l'overhead et "
            f"la marge de sécurité, mais il les ronge. Alignez TOTAL_VRAM_GB sur "
            f"{detected:.1f}.",
            critical=False,
        )
    if configured < detected * 0.8:
        return CheckResult(
            "vram_detected", "warn", "total_vram_gb_understated",
            f"{described}. Plus de 20 % de la VRAM exposée reste inutilisée par "
            "le budget d'admission — volontaire ? Sinon relevez TOTAL_VRAM_GB.",
            critical=False,
        )
    return CheckResult("vram_detected", "pass", "ok", f"{described}.")


# ── Contrôle : artefacts des modèles ──────────────────────────────────────────

def check_model_artifacts(
    config: Any,
    models: Sequence[ModelDefinition],
    *,
    verify_hashes: bool = False,
) -> tuple[CheckResult, dict[str, int]]:
    """
    Présence, lisibilité et TAILLE des GGUF/mmproj. Renvoie aussi les tailles.

    IMPORTANT — coût : aucune empreinte SHA-256 n'est calculée par défaut. Un
    catalogue de production représente plusieurs centaines de gigaoctets ; hacher
    à chaque diagnostic saturerait le stockage pendant des dizaines de minutes.
    On se limite à des `stat` (donc un fichier factice suffit), et l'intégrité
    n'est vérifiée que si `--verify-hashes` est demandé explicitement.

    Valeur ajoutée par rapport au contrôle `model_files` de `readiness.py` : la
    taille. Un GGUF de 0 octet passe le `stat` de `/ready` (par conception) mais
    c'est un téléchargement interrompu, et `llama-server` mourra au chargement.
    """
    sizes: dict[str, int] = {}
    if str(getattr(config, "cluster_mode", "local")) == "cluster":
        return (
            CheckResult(
                "model_artifacts", "skip", "delegated_to_nodes",
                "Mode cluster : les GGUF vivent sur les nœuds et sont validés au "
                "chargement par le node-agent qui les possède.",
                critical=False,
            ),
            sizes,
        )
    if not models:
        return (
            CheckResult(
                "model_artifacts", "skip", "no_enabled_model",
                "Aucun modèle activé à contrôler.", critical=False,
            ),
            sizes,
        )

    findings: list[_Finding] = []
    total_bytes = 0
    measured = 0
    declared_hashes = 0
    for model in models:
        for label, path in _model_files(model):
            try:
                size = path.stat().st_size
            except OSError:
                # Absence et illisibilité sont déjà couvertes par `model_files`.
                continue
            if label == "gguf":
                sizes[model.id] = size
            measured += 1
            total_bytes += size
            if size == 0:
                findings.append(_Finding(
                    "fail", "model_file_empty",
                    f"[{model.id}] {label} de 0 octet : {path}. Téléchargement "
                    "interrompu — llama-server échouera au chargement. "
                    "Retéléchargez le fichier ou désactivez le modèle.",
                ))
            elif size < _MIN_PLAUSIBLE_GGUF_BYTES:
                findings.append(_Finding(
                    "warn", "model_file_suspiciously_small",
                    f"[{model.id}] {label} de {size} octets seulement : {path}. "
                    "Taille invraisemblable pour un artefact de modèle.",
                    critical=False,
                ))
        if model.sha256 is not None:
            declared_hashes += 1
            if verify_hashes:
                findings.extend(_verify_model_hash(model))

    if measured == 0:
        return (
            CheckResult(
                "model_artifacts", "skip", "artifacts_absent",
                "Aucun artefact de modèle mesurable sur cet hôte : voir le "
                "contrôle model_files.", critical=False,
            ),
            sizes,
        )

    if declared_hashes and not verify_hashes:
        findings.append(_Finding(
            "warn", "integrity_not_verified",
            f"{declared_hashes} modèle(s) déclarent un sha256 mais aucune "
            "empreinte n'a été calculée (coûteux sur plusieurs centaines de Go). "
            "Relancez avec --verify-hashes pour le contrôle d'intégrité complet.",
            critical=False,
        ))

    return (
        _combine(
            "model_artifacts", findings,
            f"{measured} artefact(s) mesuré(s) pour {len(models)} modèle(s) "
            f"activé(s), {total_bytes / 1024**3:.1f} Go au total, tailles "
            f"plausibles"
            + (" (empreintes SHA-256 vérifiées)." if verify_hashes else "."),
        ),
        sizes,
    )


def _model_files(model: ModelDefinition) -> list[tuple[str, Path]]:
    files = [("gguf", Path(str(model.path)))]
    mmproj = getattr(model, "mmproj_path", None)
    if mmproj is not None and "vision" in (getattr(model, "capabilities", None) or []):
        files.append(("mmproj", Path(str(mmproj))))
    return files


def _verify_model_hash(model: ModelDefinition) -> list[_Finding]:
    """Vérification d'intégrité opt-in, déléguée au registre (`verify_integrity`)."""
    try:
        model.verify_integrity()
    except IntegrityError as exc:
        return [_Finding("fail", "model_integrity_mismatch", f"{exc}")]
    except Exception as exc:
        return [_Finding(
            "warn", "model_integrity_unverifiable",
            f"[{model.id}] Vérification d'intégrité impossible : "
            f"{type(exc).__name__}.", critical=False,
        )]
    return []


# ── Contrôle : pool de ports ──────────────────────────────────────────────────

def check_port_pool(config: Any) -> CheckResult:
    """
    Disponibilité des ports que le gestionnaire de modèles peut attribuer.

    Le pool est `base_llama_port … base_llama_port + max_loaded_models - 1` (cf.
    `LocalModelManager.__init__`). Deux constats de nature différente :

    - collision avec le port de la gateway → défaut de configuration certain,
      donc bloquant ;
    - port déjà occupé → AVERTISSEMENT seulement, et c'est délibéré : doctor est
      appelé par `update.sh` après bascule, où un port du pool peut être occupé
      par un llama-server légitimement chargé. Impossible de distinguer ce cas
      d'un orphelin de crash sans inspecter les processus — `model_manager`
      lui-même se contente de journaliser (`detect_orphan_ports`).
    """
    if str(getattr(config, "cluster_mode", "local")) == "cluster":
        return CheckResult(
            "port_pool", "skip", "delegated_to_nodes",
            "Mode cluster : les ports de données sont ceux des nœuds "
            "(base_llama_port du node-agent), pas de l'orchestrateur.",
            critical=False,
        )

    base = int(getattr(config, "base_llama_port", 0) or 0)
    count = int(getattr(config, "max_loaded_models", 0) or 0)
    host = str(getattr(config, "llama_server_host", "127.0.0.1"))
    gateway_port = int(getattr(config, "gateway_port", 0) or 0)
    pool = list(range(base, base + count))
    if not pool:
        return CheckResult(
            "port_pool", "skip", "empty_pool",
            "Pool de ports vide (MAX_LOADED_MODELS=0) : configuration à revoir.",
            critical=False,
        )

    findings: list[_Finding] = []
    if gateway_port in pool:
        findings.append(_Finding(
            "fail", "port_pool_conflicts_with_gateway",
            f"GATEWAY_PORT={gateway_port} appartient au pool llama-server "
            f"{pool[0]}–{pool[-1]} : le premier chargement de modèle entrera en "
            f"collision avec la gateway. Déplacez BASE_LLAMA_PORT ou "
            f"GATEWAY_PORT.",
        ))

    occupied = [p for p in pool if port_is_occupied(p, host)]
    if occupied:
        findings.append(_Finding(
            "warn", "port_pool_partially_occupied",
            f"Port(s) du pool déjà occupé(s) sur {host} : "
            f"{', '.join(str(p) for p in occupied)}. Normal si la gateway tourne "
            f"avec des modèles chargés ; sinon ce sont des llama-server "
            f"orphelins tenant de la VRAM (diagnostic : pgrep -af llama-server).",
            critical=False,
        ))

    return _combine(
        "port_pool", findings,
        f"Pool de ports {pool[0]}–{pool[-1]} entièrement libre sur {host} "
        f"({count} modèle(s) simultané(s)).",
    )


# ── Parsing nginx ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NginxLocation:
    """Un bloc `location` : modificateur, motif, directives (dernière gagne)."""
    modifier: str
    pattern: str
    directives: dict[str, str]


@dataclass(frozen=True)
class NginxServer:
    """Un bloc `server` et ses `location`."""
    directives: dict[str, str]
    locations: tuple[NginxLocation, ...]

    @property
    def server_names(self) -> tuple[str, ...]:
        return tuple(self.directives.get("server_name", "").split())

    def effective(self, location: NginxLocation | None, key: str) -> str | None:
        """Valeur d'une directive, avec héritage server → location."""
        if location is not None and key in location.directives:
            return location.directives[key]
        return self.directives.get(key)


def _tokenize_nginx(text: str) -> list[str]:
    text = re.sub(r"#[^\n]*", "", text)
    return [m.group(0) for m in re.finditer(r"[{};]|[^\s{};]+", text)]


def _parse_nginx_block(tokens: list[str], i: int) -> tuple[list, list, int]:
    """Retourne (directives, sous-blocs, index) pour un niveau de bloc."""
    directives: list[list[str]] = []
    blocks: list[tuple[list[str], list, list]] = []
    words: list[str] = []
    while i < len(tokens):
        token = tokens[i]
        if token == ";":
            if words:
                directives.append(words)
            words = []
            i += 1
        elif token == "{":
            sub_directives, sub_blocks, i = _parse_nginx_block(tokens, i + 1)
            blocks.append((words, sub_directives, sub_blocks))
            words = []
        elif token == "}":
            return directives, blocks, i + 1
        else:
            words.append(token)
            i += 1
    return directives, blocks, i


def _flatten_directives(directives: list[list[str]]) -> dict[str, str]:
    """Dernière assignation gagnante, comme nginx pour les directives simples."""
    flat: dict[str, str] = {}
    for words in directives:
        if words:
            flat[words[0]] = " ".join(words[1:])
    return flat


def parse_nginx_servers(text: str) -> list[NginxServer]:
    """
    Extrait les blocs `server` et leurs `location`. Parsing volontairement
    minimal : commentaires, accolades et points-virgules suffisent pour lire des
    timeouts et des chemins de certificats. Ne lève jamais sur un fichier
    inattendu — retourne simplement une liste vide.
    """
    _, blocks, _ = _parse_nginx_block(_tokenize_nginx(text), 0)
    servers: list[NginxServer] = []
    for header, directives, sub_blocks in _walk_nginx(blocks):
        if not header or header[0] != "server":
            continue
        locations: list[NginxLocation] = []
        for sub_header, sub_directives, _unused in sub_blocks:
            if not sub_header or sub_header[0] != "location":
                continue
            args = sub_header[1:]
            if len(args) >= 2 and args[0] in ("=", "~", "~*", "^~"):
                modifier, pattern = args[0], args[1]
            elif args:
                modifier, pattern = "", args[0]
            else:
                continue
            locations.append(NginxLocation(
                modifier, pattern, _flatten_directives(sub_directives)
            ))
        servers.append(NginxServer(
            _flatten_directives(directives), tuple(locations)
        ))
    return servers


def _walk_nginx(blocks: list) -> list:
    """Aplati les blocs imbriqués (`http { server { … } }` comme `server { … }`)."""
    found = []
    for header, directives, sub_blocks in blocks:
        found.append((header, directives, sub_blocks))
        found.extend(_walk_nginx(sub_blocks))
    return found


def match_location(
    locations: Sequence[NginxLocation], path: str
) -> NginxLocation | None:
    """
    Reproduit l'essentiel de l'algorithme de sélection nginx : exact `=`, puis
    plus long préfixe (`^~` court-circuite les regex), puis première regex.
    """
    for location in locations:
        if location.modifier == "=" and location.pattern == path:
            return location
    best: NginxLocation | None = None
    for location in locations:
        if location.modifier in ("", "^~") and path.startswith(location.pattern):
            if best is None or len(location.pattern) > len(best.pattern):
                best = location
    if best is not None and best.modifier == "^~":
        return best
    for location in locations:
        if location.modifier in ("~", "~*"):
            flags = re.IGNORECASE if location.modifier == "~*" else 0
            try:
                if re.search(location.pattern, path, flags):
                    return location
            except re.error:
                continue
    return best


_NGINX_TIME_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_nginx_time(value: str) -> float | None:
    """Convertit une durée nginx (`30s`, `10m`, `600`) en secondes."""
    value = value.strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)?", value)
    if not match:
        return None
    amount = float(match.group(1))
    return amount * _NGINX_TIME_UNITS.get(match.group(2) or "s", 1.0)


def required_load_timeout_seconds(
    config: Any, models: Sequence[ModelDefinition]
) -> int:
    """
    Attente maximale qu'un client peut subir sur un chargement de modèle.

    Exactement la formule de `ServerManager.ensure_loaded` :
    `(load_timeout_seconds du modèle ou MODEL_LOAD_TIMEOUT_SECONDS) + 10`, prise
    sur le pire modèle ACTIVÉ. Un modèle désactivé ne peut pas être chargé, il ne
    dimensionne donc pas les timeouts du reverse-proxy.
    """
    default = int(getattr(config, "model_load_timeout_seconds", 0) or 0)
    per_model = [
        int(getattr(m, "load_timeout_seconds", None) or default) for m in models
    ]
    return (max(per_model) if per_model else default) + _LOAD_TIMEOUT_GRACE_SECONDS


# ── Contrôle : timeouts nginx ─────────────────────────────────────────────────

def check_nginx_timeouts(
    servers: Sequence[NginxServer],
    config: Any,
    models: Sequence[ModelDefinition],
) -> CheckResult:
    """
    Cohérence des timeouts nginx avec les timeouts de chargement de modèle.

    `POST /admin/models/{id}/load` attend `load_timeout_seconds + 10` du pire
    modèle activé — 190 s avec les seuls défauts, 310 s avec le registre livré
    (`gemma-4-26b-a4b` : 300 s), 610 s si `minimax-m2.7` est réactivé. Tout
    reverse-proxy plus court renvoie 504 alors que le chargement RÉUSSIT côté
    serveur : c'était le défaut EVA-004 / COR-009, `location /admin/` étant livré
    à `proxy_read_timeout 30s` et le bouton « Load » du dashboard échouant.

    `deploy/nginx.conf` est corrigé (900 s sur `/admin/` et sur les blocs
    d'inférence) ; ce contrôle reste le garde-fou pour les deux cas qu'un
    correctif ne couvre pas : une conf nginx modifiée à la main sur l'hôte, et
    l'ajout au registre d'un modèle plus lent que la marge retenue.

    L'exigence est TOUJOURS recalculée depuis le registre — aucune valeur
    attendue n'est codée en dur ici. Constat volontairement NON bloquant : le
    service sert normalement le trafic d'inférence, et un `install.sh` refusant
    d'installer pour cette raison serait plus nuisible que le défaut lui-même.
    """
    # Seuls les blocs qui PROXIFIENT vers la gateway sont concernés : un server
    # de redirection HTTP → HTTPS n'attend aucun upstream et n'a pas de timeout à
    # aligner.
    proxying = [
        s for s in servers
        if any("proxy_pass" in loc.directives for loc in s.locations)
        or "proxy_pass" in s.directives
    ]
    if not proxying:
        return CheckResult(
            "nginx_timeouts", "skip", "nginx_no_proxy_block",
            "Aucun bloc server proxifiant vers la gateway dans la configuration "
            "nginx fournie : rien à aligner.",
            critical=False,
        )

    required = required_load_timeout_seconds(config, models)
    example_id = models[0].id if models else "example-model"
    probes = {
        "chargement admin": (f"/admin/models/{example_id}/load",
                             "nginx_admin_timeout_too_short"),
        "inférence": ("/v1/chat/completions",
                      "nginx_inference_timeout_too_short"),
    }

    findings: list[_Finding] = []
    described: list[str] = []
    for server in proxying:
        for label, (path, code) in probes.items():
            location = match_location(server.locations, path)
            # `proxy_pass` n'est pas héritable en nginx : sans elle, la requête
            # n'atteint jamais la gateway (404, redirection…) et aucun timeout
            # d'upstream n'est à aligner.
            if location is None or "proxy_pass" not in location.directives:
                continue
            raw = server.effective(location, "proxy_read_timeout")
            if raw is None:
                # Défaut nginx : 60 s. C'est déjà trop court pour un chargement.
                actual = 60.0
                origin = "défaut nginx (60s, aucune directive proxy_read_timeout)"
            else:
                parsed = parse_nginx_time(raw)
                if parsed is None:
                    findings.append(_Finding(
                        "warn", "nginx_timeout_unparsable",
                        f"proxy_read_timeout illisible pour {label} : {raw!r}.",
                        critical=False,
                    ))
                    continue
                actual = parsed
                origin = f"proxy_read_timeout {raw}"
            where = location.pattern if location is not None else "server"
            described.append(f"{label} ({where}) : {actual:.0f}s")
            if actual < required:
                findings.append(_Finding(
                    "warn", code,
                    f"Timeout nginx trop court pour {label} : {origin} sur "
                    f"« {where} », alors que la gateway peut légitimement attendre "
                    f"{required}s (MODEL_LOAD_TIMEOUT_SECONDS + "
                    f"{_LOAD_TIMEOUT_GRACE_SECONDS}s, pire modèle activé). Le "
                    f"client recevra 504 alors que le chargement réussit côté "
                    f"serveur. Portez proxy_read_timeout et proxy_send_timeout "
                    f"au-delà de {required}s sur ce bloc (item COR-009/EVA-004).",
                    critical=False,
                ))

    return _combine(
        "nginx_timeouts", findings,
        f"Timeouts nginx compatibles avec une attente de chargement de "
        f"{required}s — " + " ; ".join(described) + ".",
    )


# ── Contrôle : certificat TLS fourni ──────────────────────────────────────────

def check_tls_certificate(servers: Sequence[NginxServer]) -> CheckResult:
    """
    Certificats FOURNIS : existence, lisibilité, expiration, domaine.

    doctor n'émet JAMAIS de certificat et n'en demande pas : la spécification §4
    place TLS explicitement hors automatisation (PKI interne de l'établissement).
    On vérifie seulement que ce que l'opérateur a déposé est utilisable.
    """
    server = next(
        (s for s in servers if s.directives.get("ssl_certificate")), None
    )
    if server is None:
        return CheckResult(
            "tls_certificate", "skip", "tls_not_configured",
            "Aucun ssl_certificate dans la configuration nginx fournie : "
            "terminaison TLS ailleurs, ou à configurer manuellement.",
            critical=False,
        )

    cert_path = Path(server.directives["ssl_certificate"])
    key_raw = server.directives.get("ssl_certificate_key")
    findings: list[_Finding] = []

    if not cert_path.is_file():
        findings.append(_Finding(
            "fail", "tls_cert_missing",
            f"Certificat TLS introuvable : {cert_path}. nginx refusera de "
            "démarrer. Déposez le certificat fourni par votre PKI (doctor n'en "
            "émet pas).",
        ))
    elif not os.access(cert_path, os.R_OK):
        findings.append(_Finding(
            "fail" if _is_root() else "warn",
            "tls_cert_unreadable",
            f"Certificat TLS illisible : {cert_path}"
            + ("." if _is_root() else
               " — doctor ne tourne pas en root ; nginx démarre en root et peut "
               "y arriver. Relancez avec sudo pour conclure."),
            critical=_is_root(),
        ))
    else:
        findings.extend(_inspect_certificate(cert_path, server.server_names))

    if key_raw is None:
        findings.append(_Finding(
            "warn", "tls_key_not_declared",
            "ssl_certificate est déclaré sans ssl_certificate_key.",
            critical=False,
        ))
    else:
        key_path = Path(key_raw)
        if not key_path.is_file():
            findings.append(_Finding(
                "fail", "tls_key_missing",
                f"Clé privée TLS introuvable : {key_path}. nginx refusera de "
                "démarrer.",
            ))
        else:
            mode = _file_mode(key_path)
            if mode is not None and mode & 0o077:
                findings.append(_Finding(
                    "fail", "tls_key_permissions_too_open",
                    f"Permissions trop ouvertes sur la clé privée TLS "
                    f"{key_path} (mode {mode:04o}) : attendu 0600, lisible par "
                    f"root seulement. Corrigez avec : chmod 600 {key_path}.",
                ))

    return _combine(
        "tls_certificate", findings,
        f"Certificat TLS fourni valide et clé protégée ({cert_path}).",
    )


def _inspect_certificate(
    path: Path, server_names: Sequence[str]
) -> list[_Finding]:
    """
    Expiration et correspondance de domaine, sans dépendance supplémentaire.

    `ssl._ssl._test_decode_cert` est le seul décodeur X.509 de la bibliothèque
    standard. Il est utilisé ici plutôt qu'`openssl` en sous-processus ou qu'un
    ajout de `cryptography` (cf. AGENTS.md : éviter les nouvelles dépendances).
    Toute défaillance dégrade en avertissement, jamais en traceback.
    """
    try:
        decoded = ssl._ssl._test_decode_cert(str(path))  # type: ignore[attr-defined]
    except Exception as exc:
        return [_Finding(
            "warn", "tls_cert_unparsable",
            f"Certificat {path} non décodable par doctor "
            f"({type(exc).__name__}) : vérifiez-le avec "
            f"openssl x509 -noout -dates -subject -in {path}.",
            critical=False,
        )]

    findings: list[_Finding] = []
    not_after = decoded.get("notAfter")
    if not_after:
        try:
            expiry = datetime.fromtimestamp(
                ssl.cert_time_to_seconds(not_after), tz=timezone.utc
            )
        except Exception:
            expiry = None
        if expiry is not None:
            now = datetime.now(timezone.utc)
            if expiry <= now:
                findings.append(_Finding(
                    "fail", "tls_cert_expired",
                    f"Certificat TLS EXPIRÉ depuis le {expiry.date().isoformat()} "
                    f"({path}) : tous les clients refuseront la connexion. "
                    "Renouvelez-le auprès de votre PKI.",
                ))
            elif expiry - now < timedelta(days=_TLS_EXPIRY_WARN_DAYS):
                remaining = (expiry - now).days
                findings.append(_Finding(
                    "warn", "tls_cert_expiring_soon",
                    f"Certificat TLS ({path}) expire dans {remaining} jour(s), le "
                    f"{expiry.date().isoformat()} : lancez le renouvellement.",
                    critical=False,
                ))

    names = _certificate_names(decoded)
    concrete = [n for n in server_names if n not in ("_", "") and "*" not in n
                and not n.startswith("~")]
    if names and concrete and not any(
        any(_hostname_matches(pattern, host) for pattern in names)
        for host in concrete
    ):
        findings.append(_Finding(
            "warn", "tls_cert_hostname_mismatch",
            f"Le certificat {path} ne couvre aucun server_name déclaré "
            f"({', '.join(concrete)}) ; noms du certificat : "
            f"{', '.join(sorted(names))}. Les clients rejetteront la connexion. "
            "Alignez server_name sur le certificat, ou demandez un certificat "
            "couvrant ce domaine.",
            critical=False,
        ))
    return findings


def _certificate_names(decoded: dict) -> set[str]:
    """Noms DNS d'un certificat : SAN d'abord, CN en complément."""
    names = {
        value for kind, value in decoded.get("subjectAltName", ())
        if kind.lower() == "dns"
    }
    for rdn in decoded.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                names.add(value)
    return names


def _hostname_matches(pattern: str, host: str) -> bool:
    """Comparaison de nom d'hôte, avec joker de premier label uniquement."""
    pattern = pattern.lower().rstrip(".")
    host = host.lower().rstrip(".")
    if pattern == host:
        return True
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix) and host.count(".") == pattern.count(".")
    return False


# ── Parsing systemd ──────────────────────────────────────────────────────────

def parse_systemd_unit(path: Path) -> dict[str, list[str]]:
    """
    Parse minimaliste d'une unité systemd : clé → valeurs déclarées, dans l'ordre.

    Gère les commentaires (`#`, `;`) et les continuations (`\\`). Même logique que
    `tests/test_deploy_memory_profiles.py::parse_unit` (COR-007) ; la duplication
    est assumée : doctor ne peut pas importer un module de tests, et l'unité de
    production n'est pas forcément celle du dépôt.
    """
    directives: dict[str, list[str]] = {}
    pending = ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return directives
    for raw in text.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#") or line.startswith(";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].rstrip() + " "
            continue
        line = pending + line
        pending = ""
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        directives.setdefault(key.strip(), []).append(value.strip())
    return directives


def unit_value(directives: dict[str, list[str]], key: str) -> str | None:
    values = directives.get(key)
    return values[-1] if values else None


_MEMORY_SUFFIXES = {"K": 1 / 1024**2, "M": 1 / 1024, "G": 1.0, "T": 1024.0}


def memory_allowance_gb(value: str, host_ram_gb: float | None) -> float | None:
    """
    Convertit `MemoryHigh=`/`MemoryMax=` en gigaoctets autorisés.

    `infinity` → None (aucune limite) ; `80%` → fraction de la RAM physique de
    l'hôte (None si celle-ci est inconnue) ; `64G` → valeur absolue.
    """
    value = value.strip()
    if value == "infinity":
        return None
    if value.endswith("%"):
        if host_ram_gb is None:
            return None
        try:
            return host_ram_gb * float(value[:-1]) / 100.0
        except ValueError:
            return None
    suffix = value[-1].upper()
    try:
        if suffix in _MEMORY_SUFFIXES:
            return float(value[:-1]) * _MEMORY_SUFFIXES[suffix]
        return float(value) / 1024**3
    except ValueError:
        return None


# ── Contrôle : limites systemd ────────────────────────────────────────────────

def check_systemd_limits(
    path: Path,
    config: Any,
    models: Sequence[ModelDefinition],
    gguf_sizes: dict[str, int],
) -> CheckResult:
    """
    Les limites de l'unité systemd sont-elles compatibles avec le profil activé ?

    Contrôle volontairement DÉRIVÉ, jamais codé en dur : COR-007 fait évoluer en
    parallèle les unités et `models.yaml`, et ce contrôle doit rester valide quelle
    que soit la politique retenue. On lit donc l'unité, la configuration et la
    taille réelle des GGUF, puis on compare :

    - la politique mémoire est-elle déclarée et cohérente (`MemoryHigh` ≤
      `MemoryMax`) ;
    - `TasksMax` couvre-t-il `MAX_LOADED_MODELS` × ~64 tâches par llama-server ;
    - un modèle activé en `cpu_moe` garde ses experts FFN RÉSIDENTS en RAM hôte
      (lus depuis le mmap à chaque token) : sa taille de GGUF doit tenir sous
      l'allocation mémoire du cgroup, sinon thrashing NVMe garanti et TTFT
      effondré SANS erreur visible ;
    - les répertoires de modèles apparaissent-ils dans `ReadWritePaths`, comme le
      veut la convention documentée de l'unité.
    """
    if not path.is_file():
        return CheckResult(
            "systemd_limits", "skip", "systemd_unit_not_found",
            f"Unité systemd introuvable : {path}. Passez --systemd-unit, ou "
            "ignorez si le service n'est pas encore installé.",
            critical=False,
        )
    directives = parse_systemd_unit(path)
    if not directives:
        return CheckResult(
            "systemd_limits", "skip", "systemd_unit_unreadable",
            f"Unité systemd illisible ou vide : {path}.", critical=False,
        )

    local_mode = str(getattr(config, "cluster_mode", "local")) == "local"
    host_ram = _host_ram_gb()
    findings: list[_Finding] = []

    missing = [
        key for key in ("MemoryHigh", "MemoryMax", "TasksMax")
        if unit_value(directives, key) is None
    ]
    if missing:
        findings.append(_Finding(
            "warn", "systemd_memory_policy_missing",
            f"Directive(s) absente(s) de {path.name} : {', '.join(missing)}. Les "
            "llama-server vivent dans ce cgroup en mode local : la politique "
            "mémoire doit être explicite.", critical=False,
        ))

    high_raw = unit_value(directives, "MemoryHigh")
    max_raw = unit_value(directives, "MemoryMax")
    high = memory_allowance_gb(high_raw, host_ram) if high_raw else None
    hard = memory_allowance_gb(max_raw, host_ram) if max_raw else None
    if high is not None and hard is not None and high > hard:
        findings.append(_Finding(
            "warn", "systemd_memory_high_above_max",
            f"MemoryHigh ({high:.0f} Go) dépasse MemoryMax ({hard:.0f} Go) dans "
            f"{path.name} : la pression douce ne précède plus la limite dure.",
            critical=False,
        ))

    tasks_raw = unit_value(directives, "TasksMax")
    needed_tasks = int(getattr(config, "max_loaded_models", 1) or 1) * _TASKS_PER_LLAMA_SERVER
    if local_mode and tasks_raw and tasks_raw != "infinity":
        try:
            if int(tasks_raw) < needed_tasks:
                findings.append(_Finding(
                    "warn", "systemd_tasks_max_too_low",
                    f"TasksMax={tasks_raw} dans {path.name} alors que "
                    f"MAX_LOADED_MODELS={getattr(config, 'max_loaded_models', '?')} "
                    f"× ~{_TASKS_PER_LLAMA_SERVER} tâches par llama-server exige "
                    f"au moins {needed_tasks}.", critical=False,
                ))
        except ValueError:
            pass

    if local_mode and high is not None:
        for model in models:
            if not getattr(model.llama_params, "cpu_moe", False):
                continue
            size = gguf_sizes.get(model.id)
            if not size:
                continue
            resident = size / 1024**3 + _BASELINE_HOST_RAM_GB
            if resident > high:
                findings.append(_Finding(
                    "fail", "systemd_memory_below_model_profile",
                    f"[{model.id}] modèle MoE activé avec cpu_moe: true — ses "
                    f"experts FFN restent résidents en RAM hôte "
                    f"(~{size / 1024**3:.0f} Go de GGUF + "
                    f"{_BASELINE_HOST_RAM_GB:.0f} Go de baseline), or "
                    f"MemoryHigh={high_raw} n'autorise que {high:.0f} Go sur cet "
                    f"hôte. Résultat : recyclage en boucle des pages mmap, "
                    f"thrashing NVMe et TTFT effondré sans erreur visible. "
                    f"Désactivez le modèle dans models.yaml ou déployez-le sur un "
                    f"hôte plus grand (cf. docs/deployment.md §15).",
                ))

    if local_mode and models:
        declared: set[str] = set()
        for entry in directives.get("ReadWritePaths", []):
            for token in entry.split():
                declared.add(token.lstrip("-+!:").rstrip("/"))
        undeclared = sorted({
            str(Path(str(m.path)).parent) for m in models
            if not any(str(m.path).startswith(d + "/") for d in declared if d)
        })
        if undeclared:
            findings.append(_Finding(
                "warn", "systemd_model_dir_not_declared",
                f"Répertoire(s) de modèles absent(s) de ReadWritePaths de "
                f"{path.name} : {', '.join(undeclared)}. Convention documentée de "
                f"l'unité (cf. docs/deployment.md §15) — ajoutez-les, préfixés "
                f"« - » s'ils peuvent ne pas exister.", critical=False,
            ))

    summary = f"Limites de {path.name} cohérentes avec la configuration"
    if high is not None:
        summary += f" (MemoryHigh={high_raw} → {high:.0f} Go"
        summary += f", RAM hôte {host_ram:.0f} Go)" if host_ram else ")"
    return _combine("systemd_limits", findings, summary + ".")


# ── Contrôles secrets d'exploitation ──────────────────────────────────────────

def check_admin_secret(config: Any) -> CheckResult:
    """
    ADMIN_SECRET faible = routes /admin protégées par un secret prévisible.

    La gateway est fail-closed (503 sur /admin/*) pour un secret placeholder
    OU plus court que SECRET_MIN_LENGTH — même politique que
    `auth.require_admin`. Doctor la rend visible et BLOQUANTE avant le
    démarrage (exit 1), sans jamais afficher la valeur : seule sa longueur
    est jugée. (Audit 2026-08-28, ISSUE 2 : « password » passait avant.)
    """
    secret = str(getattr(config, "admin_secret", "") or "")
    if secret_is_placeholder(secret):
        return CheckResult(
            "admin_secret", "fail", "admin_secret_placeholder",
            "ADMIN_SECRET est vide ou laissé à sa valeur d'exemple : les routes "
            "/admin sont DÉSACTIVÉES (fail-closed) et doctor bloque. Générez-en "
            "un : python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"",
        )
    if len(secret) < SECRET_MIN_LENGTH:
        return CheckResult(
            "admin_secret", "fail", "admin_secret_too_short",
            f"ADMIN_SECRET fait {len(secret)} caractères ; {SECRET_MIN_LENGTH} au "
            "minimum sont exigés. Les routes /admin sont DÉSACTIVÉES (fail-closed) "
            "et doctor bloque. Générez-en un : python3 -c \"import secrets; "
            "print(secrets.token_urlsafe(32))\"",
        )
    return CheckResult(
        "admin_secret", "pass", "ok",
        "ADMIN_SECRET présent et de longueur suffisante.",
    )


# ── Contrôles cluster ─────────────────────────────────────────────────────────

def check_cluster_agent_secret(config: Any) -> CheckResult:
    """
    En mode cluster, `AGENT_SECRET` faible = refus de démarrage, pas un warning.

    `model_manager._build_manager` lève une `RuntimeError` si le secret partagé
    est vide, placeholder ou plus court que 32 caractères. `readiness.py` ne
    peut que l'avertir (il tourne dans un process déjà démarré) ; doctor, lui,
    doit le rendre bloquant avant le premier démarrage. La valeur du secret
    n'apparaît jamais — seule sa longueur est jugée.
    """
    if str(getattr(config, "cluster_mode", "local")) != "cluster":
        return CheckResult(
            "cluster_agent_secret", "skip", "local_mode",
            "Mode local : aucun secret partagé orchestrateur ↔ nœuds.",
            critical=False,
        )
    secret = str(getattr(config, "agent_secret", "") or "")
    if secret_is_placeholder(secret):
        return CheckResult(
            "cluster_agent_secret", "fail", "agent_secret_placeholder",
            "CLUSTER_MODE=cluster mais AGENT_SECRET est vide ou laissé à sa "
            "valeur d'exemple : la gateway REFUSERA de démarrer. Générez-le avec "
            "python3 -c \"import secrets; print(secrets.token_urlsafe(32))\" et "
            "déployez la même valeur sur tous les node-agents.",
        )
    if len(secret) < 32:
        return CheckResult(
            "cluster_agent_secret", "fail", "agent_secret_too_short",
            f"AGENT_SECRET fait {len(secret)} caractères ; 32 au minimum sont "
            "exigés au démarrage en mode cluster. La gateway refuserait de "
            "démarrer.",
        )
    return CheckResult(
        "cluster_agent_secret", "pass", "ok",
        "Secret partagé orchestrateur ↔ nœuds présent et de longueur suffisante.",
    )


def check_cluster_nodes_inventory(config: Any) -> CheckResult:
    """
    `nodes.yaml` se charge-t-il réellement ?

    `readiness.py` vérifie que le fichier existe et est lisible ; doctor le
    PARSE avec le même code que la gateway. Un inventaire syntaxiquement
    invalide empêche le démarrage. Le rapport ne cite ni URL ni secret de nœud :
    seulement le nombre de nœuds et leurs identifiants.
    """
    if str(getattr(config, "cluster_mode", "local")) != "cluster":
        return CheckResult(
            "cluster_nodes_inventory", "skip", "local_mode",
            "Mode local : aucun inventaire de nœuds requis.", critical=False,
        )
    path = Path(str(getattr(config, "cluster_nodes_path", "")))
    if not path.is_file():
        return CheckResult(
            "cluster_nodes_inventory", "skip", "nodes_config_missing",
            f"Inventaire des nœuds absent ({path}) : voir le contrôle "
            "cluster_nodes_config.", critical=False,
        )
    try:
        from cluster.nodes_config import load_nodes_config

        cluster_config = load_nodes_config(path)
    except Exception as exc:
        return CheckResult(
            "cluster_nodes_inventory", "fail", "nodes_config_invalid",
            f"Inventaire des nœuds invalide ({path}) : {exc} La gateway "
            "refuserait de démarrer en mode cluster.",
        )

    nodes = tuple(getattr(cluster_config, "nodes", ()) or ())
    if not nodes:
        return CheckResult(
            "cluster_nodes_inventory", "fail", "no_node_declared",
            f"Aucun nœud déclaré dans {path} : aucun placement de modèle ne "
            "serait possible.",
        )

    node_ids = ", ".join(sorted(str(getattr(n, "id", "?")) for n in nodes))
    if getattr(cluster_config, "tls_verify", True) is False:
        return CheckResult(
            "cluster_nodes_inventory", "warn", "cluster_tls_verify_disabled",
            f"{len(nodes)} nœud(s) déclaré(s) ({node_ids}) mais "
            "cluster.tls_verify=false : les certificats des node-agents ne sont "
            "pas vérifiés, alors que le data-plane transporte les prompts en "
            "clair côté application. Acceptable seulement sur un LAN strictement "
            "isolé et attesté (cf. SEC-006).",
            critical=False,
        )
    return CheckResult(
        "cluster_nodes_inventory", "pass", "ok",
        f"{len(nodes)} nœud(s) déclaré(s) et validé(s) : {node_ids}.",
    )


# ── Orchestration ─────────────────────────────────────────────────────────────

# Ordre canonique du rapport : du plus structurel au plus conjoncturel. Le
# `reason` exposé est le code du PREMIER contrôle bloquant dans cet ordre.
CHECK_ORDER = (
    # Configuration et registre
    "config_env_file",
    "config_load",
    "models_config",
    "models_registry",
    "enabled_models",
    "secrets",
    "admin_secret",
    # État local persistant
    "database",
    "database_permissions",
    "log_dir",
    "disk_space",
    # Runtime d'inférence
    "llama_server_binary",
    "llama_server_version",
    "gpu_inventory",
    "vram_detected",
    "vram_budget_fit",
    "model_files",
    "model_artifacts",
    "port_pool",
    # Front et intégration système
    "nginx_timeouts",
    "tls_certificate",
    "systemd_limits",
    # Cluster
    "cluster_nodes_config",
    "cluster_agent_secret",
    "cluster_nodes_inventory",
    # Contrôles exigeant un process vivant (toujours ignorés par doctor)
    "cluster_nodes_online",
    "serving_capacity",
)

# Contrôles de readiness qui n'ont pas de sens hors process vivant.
_LIVE_ONLY_CHECKS = {
    "serving_capacity": (
        "Capacité de service non évaluable hors process vivant : doctor "
        "n'interroge aucun service. Utilisez GET /ready (COR-005) ou le smoke "
        "test (COR-006)."
    ),
    "cluster_nodes_online": (
        "Heartbeat des nœuds non évaluable hors process vivant : doctor ne "
        "contacte aucun node-agent. Utilisez GET /ready ou GET /admin/status."
    ),
}


def resolve_paths(options: DoctorOptions) -> tuple[Path, Path | None, bool, str | None]:
    """
    Résout l'unité systemd, le fichier d'environnement et l'utilisateur du service.

    L'EnvironmentFile est LU depuis l'unité systemd quand elle est disponible
    plutôt que supposé : c'est la seule source qui dit ce que systemd donnera
    réellement au service. Ordre de priorité :
    `--env-file` > `EnvironmentFile=` de l'unité > défaut d'installation > `.env`
    du répertoire courant.

    Retourne (unité, fichier d'env ou None, env explicite, User= de l'unité).
    """
    unit_path = options.systemd_unit or DEFAULT_SYSTEMD_UNIT
    directives = parse_systemd_unit(unit_path) if unit_path.is_file() else {}
    service_user = unit_value(directives, "User")

    if options.env_file is not None:
        return unit_path, options.env_file, True, service_user

    candidates: list[Path] = []
    for raw in directives.get("EnvironmentFile", []):
        # systemd accepte le préfixe « - » (fichier optionnel).
        candidates.append(Path(raw.lstrip("-").strip()))
    candidates.append(DEFAULT_ENV_FILE)
    candidates.append(Path.cwd() / ".env")
    for candidate in candidates:
        if candidate.is_file():
            return unit_path, candidate, False, service_user
    return unit_path, None, False, service_user


async def run_doctor(options: DoctorOptions | None = None) -> DoctorReport:
    """
    Exécute tous les contrôles et renvoie un rapport. Ne lève jamais.

    Aucun service n'est contacté, aucun modèle n'est chargé, aucun secret ne
    transite par `argv`. Les appels système sont synchrones : doctor est une
    commande CLI, pas un chemin chaud — le cache de `readiness.py` est
    délibérément contourné (`use_cache=False`) pour refléter l'instant présent.
    """
    options = options or DoctorOptions()
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        unit_path, env_path, env_explicit, service_user = resolve_paths(options)
    except Exception:  # pragma: no cover - garde-fou : cwd supprimé, etc.
        unit_path = options.systemd_unit or DEFAULT_SYSTEMD_UNIT
        env_path, env_explicit, service_user = options.env_file, True, None

    # Le fichier d'environnement est lu AVANT le chargement de la configuration :
    # la liste de rédaction doit exister avant qu'un message d'erreur pydantic
    # puisse citer une valeur.
    env_values = parse_env_file(env_path) if env_path is not None else {}
    secrets = collect_secret_values(None, env_values)

    env_check = _safe1(
        check_env_file, "config_env_file",
        env_path, explicit=env_explicit, service_user=service_user,
    )

    if env_path is not None and env_path.is_file() and os.access(env_path, os.R_OK):
        try:
            config: Any = load_settings_from_env_file(env_path)
            config_source = str(env_path)
        except Exception as exc:
            # Sans configuration valide, aucun autre contrôle n'a de sens : le
            # service lui-même refuserait de démarrer sur cette erreur. On rend
            # un rapport minimal mais complet sur ce point, permissions incluses.
            check = CheckResult(
                "config_load", "fail", "config_load_failed",
                f"Configuration illisible depuis {env_path} : "
                f"{type(exc).__name__} — {exc} {_config_load_hint(exc)}",
            )
            return DoctorReport(
                mode="unknown",
                config_source=str(env_path),
                checks=tuple(
                    _redact_check(c, secrets) for c in (env_check, check)
                ),
                generated_at=generated_at,
            )
    else:
        config = _ambient_settings
        config_source = "environnement ambiant (aucun fichier d'environnement lu)"

    secrets = collect_secret_values(config, env_values)
    mode = str(getattr(config, "cluster_mode", "local"))

    checks: dict[str, CheckResult] = {"config_env_file": env_check}

    registry_check, registry = _safe(check_models_registry, "models_registry", config)
    checks["models_registry"] = registry_check
    if registry is None:
        registry = _EmptyRegistry()

    try:
        models = list(registry.list_enabled())
    except Exception:
        models = []

    # Contrôles structurels réutilisés tels quels (COR-005).
    try:
        report = await readiness.evaluate_readiness(
            _OfflineManager(registry, config), config=config, use_cache=False
        )
        for check in report.checks:
            checks[check.name] = check
    except Exception as exc:  # pragma: no cover - readiness ne lève pas
        checks["readiness"] = CheckResult(
            "readiness", "fail", "readiness_unavailable",
            f"Contrôles de readiness indisponibles : {type(exc).__name__}.",
            critical=False,
        )

    for name, message in _LIVE_ONLY_CHECKS.items():
        _override(checks, name, "service_not_running", message)

    if registry_check.status == "fail":
        # Éviter un doublon trompeur : le verdict est porté par models_registry.
        _override(
            checks, "enabled_models", "registry_unavailable",
            "Modèles activés non énumérables : le registre n'a pas pu être "
            "chargé (voir models_registry).",
        )

    checks["database_permissions"] = _safe1(check_database_permissions, "database_permissions", config)
    checks["disk_space"] = _safe1(check_disk_space, "disk_space", config)
    checks["llama_server_version"] = await _safe_async(
        check_llama_server_version, "llama_server_version", config
    )

    gpus: list[GpuInfo] | None = None
    gpu_reason = ""
    if mode == "local":
        try:
            gpus, gpu_reason = await probe_nvidia_smi()
        except Exception as exc:  # pragma: no cover - probe_nvidia_smi attrape tout
            gpus, gpu_reason = None, type(exc).__name__
    checks["gpu_inventory"] = _safe1(
        check_gpu_inventory, "gpu_inventory", config, gpus, gpu_reason,
        waived=gpu_waiver_declared(env_values),
    )
    checks["vram_detected"] = _safe1(check_vram_detected, "vram_detected", config, gpus)

    artifacts, sizes = _safe(
        check_model_artifacts, "model_artifacts", config, models,
        verify_hashes=options.verify_hashes,
    )
    checks["model_artifacts"] = artifacts
    checks["port_pool"] = _safe1(check_port_pool, "port_pool", config)

    nginx_path = options.nginx_conf or DEFAULT_NGINX_CONF
    servers = _read_nginx(nginx_path)
    if servers is None:
        for name in ("nginx_timeouts", "tls_certificate"):
            checks[name] = CheckResult(
                name, "skip", "nginx_conf_not_found",
                f"Configuration nginx introuvable ou illisible ({nginx_path}) : "
                "passez --nginx-conf, ou ignorez si aucun reverse-proxy n'est "
                "déployé sur cet hôte.", critical=False,
            )
    else:
        checks["nginx_timeouts"] = _safe1(
            check_nginx_timeouts, "nginx_timeouts", servers, config, models
        )
        checks["tls_certificate"] = _safe1(
            check_tls_certificate, "tls_certificate", servers
        )

    checks["systemd_limits"] = _safe1(
        check_systemd_limits, "systemd_limits", unit_path, config, models, sizes or {}
    )
    checks["admin_secret"] = _safe1(
        check_admin_secret, "admin_secret", config
    )
    checks["cluster_agent_secret"] = _safe1(
        check_cluster_agent_secret, "cluster_agent_secret", config
    )
    checks["cluster_nodes_inventory"] = _safe1(
        check_cluster_nodes_inventory, "cluster_nodes_inventory", config
    )

    ordered = tuple(
        _redact_check(checks[name], secrets)
        for name in CHECK_ORDER if name in checks
    )
    # Filet de sécurité : un contrôle ajouté sans être déclaré dans CHECK_ORDER
    # ne doit jamais disparaître silencieusement du rapport.
    extra = tuple(
        _redact_check(check, secrets)
        for name, check in checks.items() if name not in CHECK_ORDER
    )

    return DoctorReport(
        mode=mode,
        config_source=config_source,
        checks=ordered + extra,
        generated_at=generated_at,
    )


def _crash_result(name: str, exc: BaseException) -> CheckResult:
    """
    Un contrôle qui explose est un bug de doctor, pas un défaut de l'hôte.

    Il est rapporté en échec NON critique : visible et compté, mais il ne fait
    pas échouer une installation par ailleurs saine.
    """
    return CheckResult(
        name, "fail", "check_crashed",
        f"Contrôle « {name} » interrompu par une erreur interne de doctor "
        f"({type(exc).__name__}: {exc}). Signalez-le ; relancez sans ce "
        f"contrôle en cas de blocage.",
        critical=False,
    )


def _safe1(func, name: str, *args, **kwargs) -> CheckResult:
    """Exécute un contrôle renvoyant un unique `CheckResult`."""
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        return _crash_result(name, exc)


def _safe(func, name: str, *args, **kwargs):
    """Exécute un contrôle renvoyant `(CheckResult, données)`."""
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        return _crash_result(name, exc), None


async def _safe_async(func, name: str, *args, **kwargs) -> CheckResult:
    try:
        return await func(*args, **kwargs)
    except Exception as exc:
        return _crash_result(name, exc)


def _read_nginx(path: Path) -> list[NginxServer] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        return parse_nginx_servers(text)
    except Exception:
        return []


# ── Rendu ─────────────────────────────────────────────────────────────────────

_STATUS_LABELS = {
    "pass": "[ OK ]",
    "warn": "[WARN]",
    "fail": "[FAIL]",
    "skip": "[SKIP]",
}

_VERDICTS = {
    "ok": "TOUT EST CONFORME",
    "warn": "AVERTISSEMENTS SEULEMENT",
    "fail": "ÉCHEC BLOQUANT",
}


def render_human(report: DoctorReport, *, strict: bool = False) -> str:
    """Rapport lisible par un opérateur, sans couleur ni markup (grep-friendly)."""
    width = max((len(c.name) for c in report.checks), default=0)
    lines = [
        f"EVARuntime doctor — mode {report.mode}",
        f"  Configuration : {report.config_source}",
        f"  Généré le     : {report.generated_at}",
        "",
    ]
    for check in report.checks:
        label = _STATUS_LABELS.get(check.status, "[????]")
        lines.append(f"  {label} {check.name.ljust(width)}  {check.message}")

    counts = report.counts()
    state = report.status(strict=strict)
    lines += [
        "",
        f"  Résumé  : {counts['pass']} conforme(s), {counts['warn']} "
        f"avertissement(s), {counts['fail']} échec(s) dont "
        f"{counts['blocking']} bloquant(s), {counts['skip']} ignoré(s)",
        f"  Verdict : {_VERDICTS.get(state, state)}"
        + (f" — premier code bloquant : {report.reason}" if report.reason else "")
        + (" (mode --strict : les avertissements bloquent)"
           if strict and counts["warn"] else ""),
        f"  Exit code : {report.exit_code(strict=strict)}",
    ]
    return "\n".join(lines)


def render_json(report: DoctorReport, *, strict: bool = False) -> str:
    """Document JSON stable, consommable par install.sh / update.sh."""
    return json.dumps(
        report.to_dict(strict=strict), ensure_ascii=False, indent=2, sort_keys=False
    )
