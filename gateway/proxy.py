"""
Proxy OpenAI-compatible vers llama-server.

Endpoints gérés :
  POST /v1/chat/completions   — streaming SSE + non-streaming
  POST /v1/completions        — legacy completions
  GET  /v1/models             — liste statique des modèles configurés

Design :
- On forward le body JSON tel quel vers llama-server (compatibilité maximale)
- On injecte l'Authorization interne (clé gateway ↔ llama-server)
- On log l'usage en fire-and-forget après chaque requête terminée
- Pour le streaming : on désactive tout buffering nginx/uvicorn via les headers

Point critique SSE :
  nginx doit avoir proxy_buffering off et X-Accel-Buffering: no
  pour que les chunks arrivent en temps réel chez le client.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncGenerator

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

import database as db
from config import settings
from server_manager import ModelState, ServerManager

log = logging.getLogger(__name__)

# Timeout total pour une génération (10 minutes)
_INFERENCE_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=5.0)

_INTERNAL_HEADERS = {
    "Authorization": f"Bearer {settings.internal_api_key}",
}


def _llama_url(path: str) -> str:
    return f"{settings.llama_server_url()}{path}"


# ── Handler principal ─────────────────────────────────────────────────────────

async def proxy_request(
    request: Request,
    path: str,
    user: dict,
    manager: ServerManager,
) -> StreamingResponse | JSONResponse:
    """
    Point d'entrée générique.
    - Assure que le modèle est chargé (charge si nécessaire)
    - Proxy la requête vers llama-server
    - Log l'usage
    """
    # ── Chargement du modèle si nécessaire ────────────────────────────────────
    try:
        await manager.ensure_loaded()
    except TimeoutError as exc:
        return _openai_error(503, str(exc), "server_error")
    except RuntimeError as exc:
        return _openai_error(503, str(exc), "server_error")
    except Exception as exc:
        log.exception("Erreur inattendue lors du chargement du modèle")
        return _openai_error(500, "Erreur interne du serveur.", "server_error")

    # ── Lire le body ──────────────────────────────────────────────────────────
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        return _openai_error(400, "Corps JSON invalide.", "invalid_request_error")

    is_streaming = body.get("stream", False)
    request_id = str(uuid.uuid4())
    start_time = time.monotonic()

    if is_streaming:
        return StreamingResponse(
            _stream_proxy(path, body, user, request_id, start_time),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # Signale à nginx de ne pas bufferiser cette réponse
                "X-Accel-Buffering": "no",
            },
        )
    else:
        return await _non_stream_proxy(path, body, user, request_id, start_time)


# ── Proxy non-streaming ───────────────────────────────────────────────────────

async def _non_stream_proxy(
    path: str,
    body: dict,
    user: dict,
    request_id: str,
    start_time: float,
) -> JSONResponse:
    try:
        async with httpx.AsyncClient(timeout=_INFERENCE_TIMEOUT) as client:
            response = await client.post(
                _llama_url(path),
                json=body,
                headers=_INTERNAL_HEADERS,
            )
    except httpx.TimeoutException:
        return _openai_error(504, "Timeout : le modèle n'a pas répondu à temps.", "server_error")
    except httpx.RequestError as exc:
        log.error("Erreur de connexion à llama-server : %s", exc)
        return _openai_error(502, "Impossible de joindre le backend d'inférence.", "server_error")

    duration_ms = int((time.monotonic() - start_time) * 1000)
    data = response.json()

    usage = data.get("usage", {})
    asyncio.create_task(db.log_usage(
        user_id=user["user_id"],
        key_id=user["key_id"],
        model=body.get("model", settings.model_public_name),
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        duration_ms=duration_ms,
        status_code=response.status_code,
        request_id=request_id,
    ))

    return JSONResponse(content=data, status_code=response.status_code)


# ── Proxy streaming SSE ───────────────────────────────────────────────────────

async def _stream_proxy(
    path: str,
    body: dict,
    user: dict,
    request_id: str,
    start_time: float,
) -> AsyncGenerator[bytes, None]:
    """
    Générateur async qui pipe les chunks SSE de llama-server vers le client.
    Capture l'usage depuis le dernier chunk (OpenAI stream_options.include_usage).
    """
    prompt_tokens = 0
    completion_tokens = 0
    status_code = 200

    # Demander à llama-server d'inclure l'usage dans le stream
    body_with_usage = {**body, "stream_options": {"include_usage": True}}

    try:
        async with httpx.AsyncClient(timeout=_INFERENCE_TIMEOUT) as client:
            async with client.stream(
                "POST",
                _llama_url(path),
                json=body_with_usage,
                headers=_INTERNAL_HEADERS,
            ) as response:
                status_code = response.status_code

                async for line in response.aiter_lines():
                    if not line:
                        # Ligne vide = séparateur SSE, on la transmet
                        yield b"\n"
                        continue

                    yield (line + "\n\n").encode()

                    # Parser les chunks pour extraire l'usage final
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            chunk = json.loads(line[6:])
                            if usage := chunk.get("usage"):
                                prompt_tokens = usage.get("prompt_tokens", 0)
                                completion_tokens = usage.get("completion_tokens", 0)
                        except json.JSONDecodeError:
                            pass

    except httpx.TimeoutException:
        err = _sse_error("Timeout d'inférence dépassé.")
        yield err.encode()
        status_code = 504
    except httpx.RequestError as exc:
        log.error("Erreur stream llama-server : %s", exc)
        err = _sse_error("Erreur de connexion au backend d'inférence.")
        yield err.encode()
        status_code = 502

    duration_ms = int((time.monotonic() - start_time) * 1000)

    asyncio.create_task(db.log_usage(
        user_id=user["user_id"],
        key_id=user["key_id"],
        model=body.get("model", settings.model_public_name),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        duration_ms=duration_ms,
        status_code=status_code,
        request_id=request_id,
    ))


# ── /v1/models ────────────────────────────────────────────────────────────────

def models_response() -> JSONResponse:
    """
    Retourne une liste statique du modèle configuré.
    Compatible avec openai.models.list().
    """
    return JSONResponse(content={
        "object": "list",
        "data": [
            {
                "id": settings.model_public_name,
                "object": "model",
                "created": 1704067200,
                "owned_by": "local-uppa",
            }
        ],
    })


# ── Helpers ───────────────────────────────────────────────────────────────────

def _openai_error(status_code: int, message: str, error_type: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "code": str(status_code),
            }
        },
    )


def _sse_error(message: str) -> str:
    """Formate une erreur comme chunk SSE final."""
    payload = json.dumps({
        "error": {
            "message": message,
            "type": "server_error",
        }
    })
    return f"data: {payload}\n\ndata: [DONE]\n\n"
