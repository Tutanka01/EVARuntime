from __future__ import annotations

import json
import logging
import logging.config
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import audit
import database as db
import upstream
from auth import get_current_user
from background import fire_and_forget
from config import settings
from policy import normalize_chat_body, prompt_char_count
from rate_limiter import (
    burst_limiter,
    check_daily_tokens,
    check_hourly_tokens,
    concurrency,
    rpm_limiter,
)


def _build_logging_config() -> dict:
    audit_path = settings.audit_log_path
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"},
            "audit": {"format": "%(message)s"},
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "default"},
            "audit_file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(audit_path),
                "maxBytes": 100 * 1024 * 1024,
                "backupCount": 90,
                "formatter": "audit",
            },
        },
        "loggers": {
            "audit": {
                "handlers": ["audit_file"],
                "level": "INFO",
                "propagate": False,
            },
        },
        "root": {"level": "INFO", "handlers": ["console"]},
    }


logging.config.dictConfig(_build_logging_config())
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    await upstream.init_client()
    log.info("Gateway student démarrée — modèles autorisés: %s", ",".join(settings.allowed_models))
    yield
    await upstream.close_client()
    log.info("Gateway student arrêtée")


app = FastAPI(
    title="LLM Gateway Student EVA",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception("Erreur non gérée sur %s %s", request.method, request.url.path)
    return openai_error(500, "Erreur interne du serveur.", "server_error")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=exc.headers)
    return openai_error(exc.status_code, str(exc.detail), "invalid_request_error")


@app.get("/health", include_in_schema=False)
async def health():
    try:
        async with db.get_db() as conn:
            await conn.execute("SELECT 1")
        db_status = "ok"
    except Exception:
        log.exception("Health check DB échoué")
        db_status = "error"

    status = "ok" if db_status == "ok" else "degraded"
    return JSONResponse(
        content={"status": status, "db": db_status},
        status_code=200 if status == "ok" else 503,
    )


@app.get("/v1/models")
async def list_models(user: dict = Depends(get_current_user)):
    return {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model", "created": 1704067200, "owned_by": "uppa-eva"}
            for model_id in settings.allowed_models
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, user: dict = Depends(get_current_user)):
    started = time.monotonic()
    request_id = str(uuid.uuid4())
    await burst_limiter.check(user, limit=settings.burst_limit)
    await rpm_limiter.check(user, limit_key="rpm_limit")
    await check_hourly_tokens(user)
    await check_daily_tokens(user)

    # Défense en profondeur : refuser tout body non-JSON (conforme OpenAI).
    media_type = (request.headers.get("content-type", "").split(";", 1)[0]).strip().lower()
    if media_type != "application/json":
        return openai_error(415, "Content-Type doit etre application/json.", "invalid_request_error")

    try:
        raw = await request.body()
        if len(raw) > settings.max_body_bytes:
            return openai_error(413, "Corps de requête trop volumineux.", "invalid_request_error")
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return openai_error(400, "Corps JSON invalide.", "invalid_request_error")

    normalized = normalize_chat_body(body, user)
    prompt_chars = prompt_char_count(normalized)
    await concurrency.acquire(user)

    if normalized.get("stream"):
        return StreamingResponse(
            stream_response(normalized, user, request, request_id, started, prompt_chars),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    try:
        response = await upstream.post_chat(normalized, user)
        data = response.json()
    except httpx.TimeoutException:
        return openai_error(504, "Timeout upstream.", "server_error")
    except httpx.RequestError:
        return openai_error(503, "Gateway admin injoignable.", "server_error")
    finally:
        await concurrency.release(user)

    duration_ms = int((time.monotonic() - started) * 1000)
    usage = data.get("usage", {})
    # Hors du chemin de requête : la comptabilisation ne doit pas ajouter de latence.
    fire_and_forget(
        db.log_usage(
            user["user_id"],
            user["key_id"],
            normalized["model"],
            int(usage.get("prompt_tokens", 0) or 0),
            int(usage.get("completion_tokens", 0) or 0),
            duration_ms,
            response.status_code,
            request_id,
        ),
        name=f"log_usage:{request_id}",
    )
    emit_audit(request, user, request_id, normalized["model"], prompt_chars, usage, duration_ms, response.status_code)
    return JSONResponse(content=data, status_code=response.status_code)


async def stream_response(
    body: dict,
    user: dict,
    request: Request,
    request_id: str,
    started: float,
    prompt_chars: int,
):
    status_code = 200
    usage: dict = {}
    # Anti-contournement de quota : on compte les caractères de complétion reçus au
    # fil de l'eau. Si l'étudiant coupe le stream avant le chunk `usage` final,
    # on impute cette estimation au quota au lieu de 0 (génération GPU non gratuite).
    completion_chars = 0
    try:
        async for chunk in upstream.stream_chat(body, user):
            extracted = extract_usage_from_sse_chunk(chunk)
            if extracted:
                usage = extracted
            else:
                completion_chars += delta_content_size(chunk)
            yield chunk
    except httpx.TimeoutException:
        status_code = 504
        yield b'data: {"error":{"message":"Timeout upstream.","type":"server_error"}}\n\ndata: [DONE]\n\n'
    except httpx.RequestError:
        status_code = 503
        yield b'data: {"error":{"message":"Gateway admin injoignable.","type":"server_error"}}\n\ndata: [DONE]\n\n'
    finally:
        await concurrency.release(user)
        duration_ms = int((time.monotonic() - started) * 1000)
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        # Si l'usage exact est arrivé, on l'utilise (pas de double comptage).
        # Sinon (coupure/erreur), on impute l'estimation dérivée des deltas.
        if usage:
            completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        else:
            completion_tokens = _estimate_tokens(completion_chars)
        fire_and_forget(
            db.log_usage(
                user["user_id"],
                user["key_id"],
                body["model"],
                prompt_tokens,
                completion_tokens,
                duration_ms,
                status_code,
                request_id,
            ),
            name=f"log_usage:{request_id}",
        )
        audit_usage = usage or {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
        emit_audit(request, user, request_id, body["model"], prompt_chars, audit_usage, duration_ms, status_code)


def extract_usage_from_sse_chunk(chunk: bytes) -> dict:
    text = chunk.decode(errors="ignore").strip()
    if not text.startswith("data: ") or text == "data: [DONE]":
        return {}
    try:
        payload = json.loads(text[6:])
    except json.JSONDecodeError:
        return {}
    return payload.get("usage") or {}


def delta_content_size(chunk: bytes) -> int:
    """Nombre de caractères de complétion dans un chunk SSE (deltas assistant).

    Utilisé pour estimer le volume généré en cas de coupure de stream avant le
    chunk `usage`. Ignore les chunks non-data, [DONE], et le JSON invalide.
    """
    text = chunk.decode(errors="ignore").strip()
    if not text.startswith("data: ") or text == "data: [DONE]":
        return 0
    try:
        payload = json.loads(text[6:])
    except json.JSONDecodeError:
        return 0
    total = 0
    for choice in payload.get("choices", []):
        if not isinstance(choice, dict):
            continue
        content = choice.get("delta", {}).get("content")
        if isinstance(content, str):
            total += len(content)
    return total


def _estimate_tokens(char_count: int) -> int:
    """Estime un nombre de tokens à partir d'un nombre de caractères.

    Estimation grossière (~N caractères/token) imputée au quota uniquement
    lorsque l'usage exact n'a pas été reçu (stream coupé). Voir config.
    """
    per_token = max(1, settings.est_chars_per_token)
    if char_count <= 0:
        return 0
    return max(1, (char_count + per_token - 1) // per_token)


def emit_audit(
    request: Request,
    user: dict,
    request_id: str,
    model: str,
    prompt_chars: int,
    usage: dict,
    duration_ms: int,
    status_code: int,
) -> None:
    audit.emit({
        "ts": time.time(),
        "request_id": request_id,
        "student_user_id": user["user_id"],
        "key_prefix": user.get("key_prefix"),
        "client_ip_hash": audit.client_ip_hash(request.client.host if request.client else None),
        "model": model,
        "prompt_chars": prompt_chars,
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        "duration_ms": duration_ms,
        "status": status_code,
    })


def openai_error(status_code: int, message: str, error_type: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "code": str(status_code)}},
    )
