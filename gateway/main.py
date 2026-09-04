"""
Point d'entrée FastAPI — Inference Gateway UPPA L40S.

Lancement (développement) :
    uvicorn main:app --host 127.0.0.1 --port 8000 --reload

Lancement (production via systemd) :
    uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 --loop uvloop
"""
from __future__ import annotations

import logging
import logging.config
import time
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import database as db
from admin import router as admin_router
from auth import get_current_user
from background import drain_pending
from config import SECRET_MIN_LENGTH, settings
from integrity import attest_gguf
from llama_version import enforce_llama_min_build
from metrics import router as metrics_router
from model_manager import model_manager
from model_registry import IntegrityError
from proxy import aclose_http_client, init_http_client, models_response, proxy_request
from rate_limiter import check_rate_limit
from readiness import caller_is_privileged, evaluate_readiness

# ── Logging ───────────────────────────────────────────────────────────────────

logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
        },
    },
    "root": {
        "level": "INFO",
        "handlers": ["console"],
    },
    "loggers": {
        "llama-server": {"level": "WARNING"},
        "httpx": {"level": "WARNING"},
        "uvicorn.access": {"level": "INFO"},
    },
})

log = logging.getLogger(__name__)


# ── Validation du runtime d'inférence ─────────────────────────────────────────

async def _validate_inference_runtime(enabled_models) -> None:
    """
    Valide les artefacts qui exécutent réellement l'inférence sur CET hôte.

    En mode local, la gateway possède le binaire llama-server et les GGUF : elle
    applique donc les garde-fous de version et d'intégrité avant d'accepter du
    trafic. En mode cluster, l'orchestrateur ne doit pas exiger que ces fichiers
    existent localement : chaque node-agent applique les mêmes contrôles au
    chargement, sur le nœud qui possède effectivement le binaire et les modèles.

    Le hachage SHA-256 est hors event loop (asyncio.to_thread via attest_gguf)
    et alimente le cache attesté partagé avec la vérification pré-chargement
    (ServerManager._verify_artifact_integrity) : un GGUF inchangé n'est haché
    qu'une fois par processus, toute mutation ultérieure est re-vérifiée au
    chargement (SEC-ART-001).
    """
    if settings.cluster_mode == "cluster":
        log.info(
            "Mode cluster : validation llama-server/GGUF déléguée aux node-agents."
        )
        return

    ok = await enforce_llama_min_build(
        settings.llama_server_bin, settings.llama_server_min_build
    )
    if not ok:
        raise RuntimeError(
            "llama-server ne satisfait pas LLAMA_SERVER_MIN_BUILD — "
            "démarrage refusé (binaire potentiellement vulnérable)."
        )

    for model in enabled_models:
        if model.sha256 is None:
            continue
        try:
            await attest_gguf(model)
            log.info("Intégrité SHA-256 vérifiée : %s", model.id)
        except IntegrityError as exc:
            log.critical("Intégrité GGUF compromise : %s", exc)
            raise RuntimeError(
                f"Vérification d'intégrité échouée pour '{model.id}' — "
                "démarrage refusé."
            ) from exc


# ── Lifespan ──────────────────────────────────────────────────────────────────

_RETENTION_INTERVAL_SECONDS = 24 * 3600
_retention_task: asyncio.Task | None = None


async def _retention_loop() -> None:
    """
    Boucle de rétention RGPD : une passe immédiate, puis toutes les 24 h.

    Supprime les entrées `usage_log` au-delà de USAGE_RETENTION_DAYS (0 =
    désactivé) et borne les sauvegardes `*.pre-migration.*.bak` à
    MIGRATION_BACKUPS_TO_KEEP — sans quoi une copie des données personnelles
    survivait indéfiniment à `anonymize_user` (DEC-001). Un échec n'est jamais
    fatal : la passe suivante retentera.
    """
    while True:
        try:
            counts = await db.run_retention_pass(
                settings.usage_retention_days,
                settings.migration_backups_to_keep,
            )
            if counts["usage_deleted"] or counts["backups_purged"]:
                log.info(
                    "Rétention RGPD : %d entrée(s) usage_log supprimée(s), "
                    "%d sauvegarde(s) pre-migration purgée(s).",
                    counts["usage_deleted"],
                    counts["backups_purged"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Passe de rétention RGPD échouée (reprise dans 24 h).")
        await asyncio.sleep(_RETENTION_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialisation au démarrage, nettoyage à l'arrêt."""
    log.info("=== LLM Gateway UPPA démarrage ===")

    # Vérification des secrets — fail-closed côté routes, alerte côté logs.
    if settings.admin_secret_is_placeholder():
        log.critical(
            "ADMIN_SECRET non configuré (vide ou CHANGE_ME_*) — les routes /admin "
            "sont DÉSACTIVÉES tant qu'un secret fort n'est pas défini."
        )
    elif settings.admin_secret_is_weak():
        # Secret non-placeholder mais trivial (« password ») : même fail-closed
        # que le placeholder. La longueur est loguée, jamais la valeur.
        log.critical(
            "ADMIN_SECRET trop faible (%d caractères ; %d minimum) — les routes "
            "/admin sont DÉSACTIVÉES tant qu'un secret fort n'est pas défini.",
            len(settings.admin_secret),
            SECRET_MIN_LENGTH,
        )
    if settings.cluster_mode == "local" and settings.internal_api_key_is_placeholder():
        log.critical(
            "INTERNAL_API_KEY non configurée (vide ou CHANGE_ME_*) — la clé "
            "gateway ↔ llama-server est prévisible. Définissez un secret fort."
        )

    # Afficher le registre des modèles et le budget VRAM
    registry = model_manager.registry
    all_models = registry.list_all()
    enabled_models = registry.list_enabled()
    log.info(
        "Registre : %d modèle(s) total, %d activé(s) — config : %s",
        len(all_models), len(enabled_models), settings.models_config_path,
    )
    for model in all_models:
        status = "ACTIVÉ " if model.enabled else "désactivé"
        log.info(
            "  [%s] %s — %.1f GB VRAM — %s",
            status, model.id, model.vram_gb, model.path,
        )

    if settings.cluster_mode == "local":
        budget = settings.effective_vram_budget_gb()
        log.info(
            "Budget VRAM : %.1f GB total — %.1f GB overhead — %.0f%% marge "
            "→ %.1f GB net disponible",
            settings.total_vram_gb,
            settings.vram_overhead_gb,
            settings.vram_safety_margin * 100,
            budget,
        )
        log.info(
            "Pool de ports : %d-%d (%d modèles max simultanés)",
            settings.base_llama_port,
            settings.base_llama_port + settings.max_loaded_models - 1,
            settings.max_loaded_models,
        )
        log.info("Idle timeout  : %ds", settings.idle_timeout_seconds)
    else:
        log.info(
            "Capacité cluster : budgets VRAM et ports lus dynamiquement depuis "
            "les node-agents; les paramètres GPU locaux sont ignorés."
        )

    # ── Garde-fou supply-chain : version du binaire llama-server ──────────────
    # Inerte tant que LLAMA_SERVER_MIN_BUILD=0 (défaut) : en test/CI il n'y a
    # aucun binaire llama-server réel, la sonde se contente d'un avertissement et
    # le démarrage continue. Dès qu'un plancher est exigé, la politique est
    # FAIL-CLOSED (SEC-009) : build lu inférieur au plancher OU version illisible
    # → refus. Même sémantique que `doctor`, quel que soit le chemin de démarrage.
    # En local, ces artefacts vivent sur la gateway. En cluster, ils vivent sur
    # les nœuds et sont validés par le node-agent au moment du chargement.
    await _validate_inference_runtime(enabled_models)

    await db.init_db()
    log.info("Base de données initialisée : %s", settings.db_path)

    # ── Rétention RGPD (usage_log + sauvegardes pre-migration) ────────────────
    # Une passe au démarrage (borne aussi les fichiers hérités d'avant ce
    # correctif), puis toutes les 24 h. Suppression seule : jamais de VACUUM
    # sous trafic. 0 désactive la rétention usage_log.
    global _retention_task
    _retention_task = asyncio.create_task(_retention_loop(), name="rgpd-retention")

    log.info("Mode déploiement : CLUSTER_MODE=%s", settings.cluster_mode)
    await model_manager.start_health_monitor()

    # ── Robustesse cycle de vie (mode local uniquement) ───────────────────────
    # Détection best-effort des llama-server orphelins tenant un port du pool
    # (survivants d'un crash gateway). LOG seulement par défaut — ne tue rien.
    # En test (ports libres) : aucune détection, retour immédiat.
    if hasattr(model_manager, "detect_orphan_ports"):
        try:
            await model_manager.detect_orphan_ports()
        except Exception as exc:  # best-effort — jamais fatal au démarrage
            log.warning("Détection des orphelins au démarrage ignorée : %s", exc)

    # Réconciliation VRAM périodique (nvidia-smi) — inerte sans GPU/nvidia-smi.
    if hasattr(model_manager, "start_vram_reconcile"):
        try:
            await model_manager.start_vram_reconcile()
        except Exception as exc:
            log.warning("Réconciliation VRAM non démarrée (non fatal) : %s", exc)

    # Client HTTP partagé vers les llama-server (chemin chaud d'inférence).
    # Créé une fois ici, réutilisé par toutes les requêtes proxy (keep-alive),
    # fermé au shutdown. Jamais recréé par requête.
    init_http_client()

    yield

    if _retention_task is not None:
        _retention_task.cancel()
        try:
            await _retention_task
        except asyncio.CancelledError:
            pass
        _retention_task = None

    if settings.cluster_mode == "cluster":
        log.info(
            "Arrêt de l'orchestrateur — modèles distants préservés pour le "
            "redémarrage et la réconciliation."
        )
    else:
        log.info("Arrêt de la gateway — déchargement de tous les modèles locaux…")
    await model_manager.shutdown()
    # Flush borné des tâches fire-and-forget (lignes d'usage planifiées juste
    # avant le SIGTERM) : après le déchargement des modèles, avant la fermeture
    # du client HTTP et de la DB, sinon les dernières lignes d'usage sont
    # perdues au redémarrage. Les tâches restantes ne sont pas annulées.
    remaining = await drain_pending(settings.shutdown_background_flush_seconds)
    if remaining:
        log.info("Flush des tâches d'arrière-plan : %d encore en cours.", remaining)
    await aclose_http_client()
    log.info("=== LLM Gateway UPPA arrêt propre ===")


# ── Application ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="LLM Inference Gateway UPPA",
    description=(
        "Inference gateway souverain du cluster EVA (hébergé à l'UPPA). "
        "Compatible API OpenAI. Multi-modèles avec gestion VRAM automatique. "
        "Accès réservé aux membres authentifiés."
    ),
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    # Configurable via CORS_ALLOW_ORIGINS (liste séparée par des virgules).
    # En production, restreindre aux domaines clients connus.
    allow_origins=settings.cors_allow_origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)

app.include_router(admin_router)
app.include_router(metrics_router)


# ── Middleware de logging des requêtes ────────────────────────────────────────

# Routes dont le chemin porte un nom d'utilisateur : `/admin/users/<username>`
# et `/admin/users/<username>/keys`. Le nom est une donnée personnelle, et
# l'anonymisation RGPD (COR-002) serait vaine si le journal d'accès en gardait
# une copie — c'est aussi une exigence explicite de la Definition of Done
# (« aucune donnée sensible n'est journalisée »). Le segment est donc remplacé
# avant écriture, sans masquer la route elle-même, qui reste exploitable.
_USER_PATH_PREFIX = "/admin/users/"


# Paramètres de requête dont la VALEUR peut rester lisible : structurels,
# jamais personnels. Tout le reste est rédigé (SEC-010). La liste est une
# autorisation explicite, pas une interdiction : un paramètre ajouté demain est
# rédigé par défaut, et c'est le bon sens de la faute. `GET /admin/usage`
# accepte `username`, employé par `deploy/smoke_test.sh` — le nom y est éphémère
# et généré, mais un opérateur qui interroge la route avec un vrai nom écrirait
# ce nom au journal, ce que SEC-008 interdit.
_LOGGABLE_QUERY_PARAMS = frozenset({
    "from_date", "to_date", "limit", "force", "period",
})

_REDACTED = "<redacted>"


def _redact_path(path: str) -> str:
    """
    Remplace un nom d'utilisateur présent dans le chemin par `<redacted>`.

    Conserve la forme de la route (méthode, ressource, sous-ressource) pour que
    le journal reste utile au diagnostic, sans conserver l'identifiant.
    """
    if not path.startswith(_USER_PATH_PREFIX):
        return path
    remainder = path[len(_USER_PATH_PREFIX):]
    if not remainder:
        return path
    # Seul le premier segment est un nom ; le reste (`/keys`) est structurel.
    _, sep, tail = remainder.partition("/")
    return f"{_USER_PATH_PREFIX}<redacted>{sep}{tail}"


def _redact_query(query: str) -> str:
    """
    Rédige la valeur de tout paramètre qui n'est pas explicitement structurel.

    Les NOMS de paramètres sont conservés : ils décrivent la forme de l'appel et
    n'identifient personne. Seules les valeurs disparaissent, sauf autorisation
    explicite dans `_LOGGABLE_QUERY_PARAMS`.
    """
    if not query:
        return query
    morceaux: list[str] = []
    for paire in query.split("&"):
        if not paire:
            continue
        nom, sep, _valeur = paire.partition("=")
        if not sep:
            # Paramètre sans valeur : le nom seul ne porte pas de donnée.
            morceaux.append(nom)
        elif nom.lower() in _LOGGABLE_QUERY_PARAMS:
            morceaux.append(paire)
        else:
            morceaux.append(f"{nom}={_REDACTED}")
    return "&".join(morceaux)


def _redact_target(target: str) -> str:
    """
    Rédige une cible complète « chemin[?requête] », telle qu'un journal d'accès l'écrit.

    `_redact_path` seul ne suffisait pas : le journal d'accès d'uvicorn écrit
    `get_path_with_query_string(scope)`, donc chemin ET requête (SEC-010).
    """
    chemin, sep, requete = target.partition("?")
    if not sep:
        return _redact_path(chemin)
    return f"{_redact_path(chemin)}?{_redact_query(requete)}"


class _UvicornAccessRedactor(logging.Filter):
    """
    Rédige la cible dans le journal d'accès d'uvicorn (SEC-010).

    Le middleware ci-dessous ne journalise que `request.url.path` : la requête
    n'y a jamais transité. Mais uvicorn tient SON propre journal d'accès —
    `--access-log` est actif dans les deux unités systemd — et il écrit
    `'%s - "%s %s HTTP/%s" %d'` où le troisième argument est
    `get_path_with_query_string(scope)` : chemin **et** query string, sans
    rédaction. `GET /admin/users/<username>` comme `GET /admin/usage?username=…`
    atterrissaient donc en clair dans journald, ce qui vidait `_redact_path` de
    son sens en production.

    Un filtre est le bon point d'accroche : il s'applique à tout handler du
    logger, ne dépend pas du format, et ne peut pas être contourné par un
    changement de configuration côté uvicorn. Il ne lève jamais — un journal ne
    doit pas casser une requête.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (args[0], args[1], _redact_target(args[2]), args[3], args[4])
        return True


def install_access_log_redaction() -> None:
    """Pose le filtre de rédaction sur `uvicorn.access`, une seule fois."""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _UvicornAccessRedactor) for f in logger.filters):
        logger.addFilter(_UvicornAccessRedactor())


install_access_log_redaction()


_DASHBOARD_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; base-uri 'none'; connect-src 'self'; "
        "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
        "img-src 'self' data:; object-src 'none'; "
        "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'"
    ),
    "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = int((time.monotonic() - start) * 1000)

    _silent = ("/health", "/v1/models")
    if not request.url.path.startswith("/admin/metrics") and request.url.path not in _silent:
        log.info(
            "%s %s %d %dms",
            request.method,
            _redact_target(request.url.path),
            response.status_code,
            duration_ms,
        )
    return response


# ── Routes publiques ──────────────────────────────────────────────────────────

@app.get("/admin/dashboard", include_in_schema=False)
async def dashboard_ui():
    """Sert le dashboard d'administration (SPA HTML)."""
    html_path = Path(__file__).parent / "static" / "dashboard.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers=_DASHBOARD_SECURITY_HEADERS,
    )


@app.get("/admin/assets/chart.umd.js", include_in_schema=False)
async def dashboard_chart_asset():
    """Sert la copie vérifiée de Chart.js sans dépendance réseau tierce."""
    asset_path = Path(__file__).parent / "static" / "chart.umd.js"
    return FileResponse(
        asset_path,
        media_type="text/javascript",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/health", include_in_schema=False)
async def health():
    """Health check utilisé par nginx et le monitoring."""
    status = model_manager.status()
    loaded = [m["id"] for m in status["models"] if m["state"] == "ready"]
    return {
        "status": "ok",
        "models_loaded": loaded,
        "vram_used_gb": status["vram_budget"]["used_gb"],
        "vram_available_gb": status["vram_budget"]["available_gb"],
    }


@app.get("/ready", include_in_schema=False)
async def ready(request: Request):
    """
    Readiness STRUCTURELLE stricte (distincte de la liveness de /health).

    Renvoie 200 seulement si tous les contrôles structurels critiques passent :
    registre lisible, au moins un modèle activé, binaire llama-server exécutable,
    GGUF des modèles activés présents et lisibles, base inscriptible, au moins un
    modèle qui tient dans le budget VRAM, et capacité de service disponible. En
    mode cluster, binaire et GGUF sont délégués aux node-agents : l'équivalent
    structurel est « inventaire de nœuds lisible ET au moins un nœud en ligne ».
    Sinon 503, avec le code du premier contrôle critique en échec dans `reason`.

    Ce que /ready NE garantit PAS : qu'un modèle génère effectivement des tokens.
    C'est la serving readiness, exposée en information (`levels.serving`) mais
    prouvée seulement par le smoke test de mise à jour (COR-006).

    Le corps public ne divulgue aucun chemin de fichier, URL de nœud ni secret :
    seulement des identifiants de contrôle et des codes stables. Un appelant
    présentant `ADMIN_SECRET` reçoit en plus les messages actionnables détaillés
    (qui, eux, contiennent des chemins) — même niveau de confiance que /admin/*.
    """
    report = await evaluate_readiness(model_manager, config=settings)

    if caller_is_privileged(request.headers.get("authorization"), settings):
        body = report.detailed_body()
    else:
        body = report.public_body()

    if report.structural_ok:
        return body
    return JSONResponse(status_code=503, content=body)


@app.get("/v1/models")
async def list_models(user: dict = Depends(get_current_user)):
    """Liste les modèles disponibles — compatible openai.models.list()."""
    return models_response(model_manager)


@app.get("/v1/capacity")
async def capacity_status(user: dict = Depends(get_current_user)):
    """
    État minimal de la queue d'admission VRAM.

    Route authentifiée par clé API utilisateur. Ne révèle ni VRAM détaillée,
    ni modèles chargés, ni chemins fichiers : seulement l'état exploitable par
    une application cliente pour afficher attente/saturation et gérer Retry-After.
    """
    status = model_manager.status()
    queue = status.get("capacity_queue")
    if not queue:
        return {
            "object": "capacity_queue",
            "mode": settings.cluster_mode,
            "available": False,
            "enabled": False,
            "status": "unavailable",
            "waiters": 0,
            "max_waiters": None,
            "timeout_seconds": None,
            "retry_after_seconds": settings.capacity_queue_retry_after_seconds,
        }

    waiters = int(queue.get("waiters", 0))
    max_waiters = int(queue.get("max_waiters", 0))
    enabled = bool(queue.get("enabled", False))

    queue_status = "disabled"
    if enabled:
        if max_waiters > 0 and waiters >= max_waiters:
            queue_status = "full"
        elif waiters > 0:
            queue_status = "waiting"
        else:
            queue_status = "idle"

    return {
        "object": "capacity_queue",
        "mode": settings.cluster_mode,
        "available": True,
        "enabled": enabled,
        "status": queue_status,
        "waiters": waiters,
        "max_waiters": max_waiters,
        "timeout_seconds": queue.get("timeout_seconds"),
        "retry_after_seconds": settings.capacity_queue_retry_after_seconds,
    }


# ── Routes d'inférence (protégées + rate limitées) ────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    user: dict = Depends(check_rate_limit),
):
    """
    Chat completions — compatible OpenAI.
    Supporte le streaming SSE (stream: true) et le mode classique.
    Le modèle est sélectionné via le champ "model" du body JSON.
    Chargé automatiquement si nécessaire, avec éviction LRU si besoin de VRAM.
    """
    return await proxy_request(request, "/v1/chat/completions", user, model_manager)


@app.post("/v1/completions")
async def completions(
    request: Request,
    user: dict = Depends(check_rate_limit),
):
    """Legacy text completions — compatible OpenAI."""
    return await proxy_request(request, "/v1/completions", user, model_manager)


@app.post("/v1/completion")
@app.post("/completion")
async def raw_completion(
    request: Request,
    user: dict = Depends(check_rate_limit),
):
    """
    Endpoint natif llama.cpp. Prend un champ 'prompt' (string) au lieu de 'messages'.
    Tous les paramètres de sampling avancés sont supportés sans configuration particulière :
    mirostat, dry_multiplier, dry_base, xtc_*, repeat_last_n, repeat_penalty, ignore_eos, etc.
    Utile pour les scripts llama.cpp existants ou les cas sans chat template.
    """
    return await proxy_request(request, "/completion", user, model_manager)


@app.post("/v1/tokenize")
async def tokenize(
    request: Request,
    user: dict = Depends(check_rate_limit),
):
    """Tokenise un texte — retourne les token IDs. Body: {"model": "...", "content": "..."}"""
    return await proxy_request(request, "/tokenize", user, model_manager)


@app.post("/v1/detokenize")
async def detokenize(
    request: Request,
    user: dict = Depends(check_rate_limit),
):
    """Reconstruit du texte depuis des token IDs. Body: {"model": "...", "tokens": [...]}"""
    return await proxy_request(request, "/detokenize", user, model_manager)


# ── Gestionnaire d'erreurs global ────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception(
        "Erreur non gérée sur %s %s", request.method, _redact_path(request.url.path)
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": "Erreur interne du serveur.",
                "type": "server_error",
                "code": "500",
            }
        },
    )
