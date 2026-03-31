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
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import database as db
from admin import router as admin_router
from auth import get_current_user
from config import settings
from proxy import models_response, proxy_request
from rate_limiter import check_rate_limit
from server_manager import server_manager

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


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialisation au démarrage, nettoyage à l'arrêt."""
    log.info("=== LLM Gateway UPPA démarrage ===")
    log.info("Modèle configuré : %s", settings.model_public_name)
    log.info("Chemin           : %s", settings.model_path)
    log.info("Idle timeout     : %ds", settings.idle_timeout_seconds)

    # Initialiser la base de données (crée les tables si elles n'existent pas)
    await db.init_db()
    log.info("Base de données initialisée : %s", settings.db_path)

    yield

    # Arrêt propre : décharger le modèle et libérer le GPU
    log.info("Arrêt de la gateway — déchargement du modèle…")
    await server_manager.unload(reason="shutdown")
    log.info("=== LLM Gateway UPPA arrêt propre ===")


# ── Application ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="LLM Inference Gateway UPPA",
    description=(
        "Inference gateway souverain du cluster EVA (hébergé à l'UPPA). "
        "Compatible API OpenAI. Accès réservé aux membres authentifiés."
    ),
    version="1.0.0",
    lifespan=lifespan,
    # Désactiver la doc Swagger publique en production
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# CORS — restreindre aux domaines UPPA en production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Remplacer par ["https://your-domain.univ-pau.fr"] en production
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)

# Routes admin
app.include_router(admin_router)


# ── Middleware de logging des requêtes ────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = int((time.monotonic() - start) * 1000)

    # Ne pas loguer les health checks pour ne pas polluer les logs
    if request.url.path not in ("/health", "/v1/models"):
        log.info(
            "%s %s %d %dms",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
    return response


# ── Routes publiques ──────────────────────────────────────────────────────────

@app.get("/health", include_in_schema=False)
async def health():
    """Health check utilisé par nginx et le monitoring."""
    return {
        "status": "ok",
        "model_state": server_manager.state.value,
    }


@app.get("/v1/models")
async def list_models(user: dict = Depends(get_current_user)):
    """Liste les modèles disponibles — compatible openai.models.list()."""
    return models_response()


# ── Routes d'inférence (protégées + rate limitées) ────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    user: dict = Depends(check_rate_limit),
):
    """
    Chat completions — compatible OpenAI.
    Supporte le streaming SSE (stream: true) et le mode classique.
    Le modèle est chargé automatiquement si nécessaire.
    """
    return await proxy_request(request, "/v1/chat/completions", user, server_manager)


@app.post("/v1/completions")
async def completions(
    request: Request,
    user: dict = Depends(check_rate_limit),
):
    """Legacy text completions — compatible OpenAI."""
    return await proxy_request(request, "/v1/completions", user, server_manager)


# ── Gestionnaire d'erreurs global ────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception("Erreur non gérée sur %s %s", request.method, request.url.path)
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
