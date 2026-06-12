"""
Proxy OpenAI-compatible vers llama-server.

Endpoints gérés :
  POST /v1/chat/completions   — streaming SSE + non-streaming
  POST /v1/completions        — legacy completions
  POST /v1/completion         — endpoint natif llama.cpp (prompt string, sans chat template)
  POST /completion            — alias direct pour les scripts llama.cpp existants
  POST /v1/tokenize           — tokenisation d'un texte
  POST /v1/detokenize         — reconstruction texte depuis token IDs
  GET  /v1/models             — liste dynamique depuis le registre

Design :
- On extrait le champ "model" du body JSON pour router vers le bon llama-server
- Si "model" est absent, on utilise le modèle par défaut configuré
- On injecte l'Authorization interne (clé gateway ↔ llama-server)
- On log l'usage en fire-and-forget après chaque requête terminée
- Pour le streaming : on désactive tout buffering nginx/uvicorn via les headers

Proxy transparent — paramètres llama.cpp natifs :
  Le body JSON est forwardé tel quel vers llama-server. Tous les paramètres de sampling
  avancés sont supportés sans configuration particulière, que ce soit via /v1/chat/completions
  (superset OpenAI) ou /completion (endpoint natif) :
  mirostat, mirostat_tau, mirostat_eta, dry_multiplier, dry_base, dry_allowed_length,
  repeat_last_n, repeat_penalty, top_k, min_p, tfs_z, typical_p,
  xtc_probability, xtc_threshold, ignore_eos, n_predict, seed, etc.

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
from typing import AsyncGenerator, Callable

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

import database as db
from background import fire_and_forget
from config import settings
from model_manager import CapacityQueueFull, CapacityQueueTimeout, ModelManager
from server_manager import ServerManager

log = logging.getLogger(__name__)

# Timeout total pour une génération (10 minutes)
_INFERENCE_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=5.0)


def _resolve_model_id(body: dict, model_manager: ModelManager) -> str:
    """
    Résout l'ID du modèle à utiliser pour une requête.
    Priorité : champ "model" du body → default_model_id → premier modèle enabled.
    """
    requested = (body.get("model") or "").strip()
    if requested:
        return requested

    if settings.default_model_id:
        return settings.default_model_id

    first = model_manager.registry.first_enabled_id()
    if first:
        return first

    return ""


# ── Handler principal ─────────────────────────────────────────────────────────

async def proxy_request(
    request: Request,
    path: str,
    user: dict,
    model_manager: ModelManager,
) -> StreamingResponse | JSONResponse:
    """
    Point d'entrée générique.
    - Lit le body et résout le modèle cible
    - Assure que le modèle est chargé (charge si nécessaire, évinçe LRU si besoin)
    - Proxy la requête vers le bon llama-server
    - Log l'usage
    """
    # ── Lire le body ──────────────────────────────────────────────────────────
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        return _openai_error(400, "Corps JSON invalide.", "invalid_request_error")

    if not isinstance(body, dict):
        return _openai_error(400, "Le corps JSON doit être un objet.", "invalid_request_error")
    if "model" in body and body["model"] is not None and not isinstance(body["model"], str):
        return _openai_error(400, "Le champ 'model' doit être une chaîne.", "invalid_request_error")

    # ── Résoudre le modèle ────────────────────────────────────────────────────
    model_id = _resolve_model_id(body, model_manager)
    if not model_id:
        return _openai_error(
            400,
            "Aucun modèle spécifié et aucun modèle activé dans le registre. "
            "Précisez le champ 'model' dans votre requête.",
            "invalid_request_error",
        )

    # ── Charger le modèle ─────────────────────────────────────────────────────
    try:
        manager = await model_manager.ensure_model_loaded(model_id)
    except LookupError as exc:
        return _openai_error(404, str(exc), "model_not_found")
    except PermissionError as exc:
        return _openai_error(403, str(exc), "model_disabled")
    except (CapacityQueueFull, CapacityQueueTimeout) as exc:
        return _openai_error(
            503,
            str(exc),
            "server_error",
            headers={"Retry-After": str(settings.capacity_queue_retry_after_seconds)},
        )
    except TimeoutError as exc:
        return _openai_error(503, str(exc), "server_error")
    except RuntimeError as exc:
        return _openai_error(503, str(exc), "server_error")
    except Exception:
        log.exception("Erreur inattendue lors du chargement du modèle '%s'", model_id)
        return _openai_error(500, "Erreur interne du serveur.", "server_error")

    is_streaming = body.get("stream", False)
    request_id = str(uuid.uuid4())
    start_time = time.monotonic()

    if is_streaming:
        # Le pin du stream est géré DANS le générateur (_stream_proxy appelle
        # manager.pin() en premier et manager.unpin() dans son finally). Ainsi le
        # modèle reste protégé pendant toute la durée du stream, y compris en cas
        # de déconnexion client (GeneratorExit → finally).
        #
        # Pin de garde : entre le return de cette fonction et le démarrage effectif
        # du générateur par Starlette, le modèle n'est pas encore pinné et pourrait
        # être évincé par une requête concurrente. On pose donc un pin temporaire,
        # relâché dès que le générateur démarre (on_start) — ou par timer si le
        # générateur ne démarre jamais (déconnexion immédiate), pour ne pas
        # bloquer l'éviction indéfiniment.
        manager.pin()
        guard_released = False

        def _release_stream_guard() -> None:
            nonlocal guard_released
            if guard_released:
                return
            guard_released = True
            guard_timer.cancel()
            manager.unpin()

        guard_timer = asyncio.get_running_loop().call_later(30.0, _release_stream_guard)

        return StreamingResponse(
            _stream_proxy(
                path, body, user, request_id, start_time, manager,
                on_start=_release_stream_guard,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        manager.pin()
        try:
            return await _non_stream_proxy(path, body, user, request_id, start_time, manager)
        finally:
            manager.unpin()


# ── Proxy non-streaming ───────────────────────────────────────────────────────

async def _non_stream_proxy(
    path: str,
    body: dict,
    user: dict,
    request_id: str,
    start_time: float,
    manager: ServerManager,
) -> JSONResponse:
    try:
        async with httpx.AsyncClient(timeout=_INFERENCE_TIMEOUT) as client:
            response = await client.post(
                manager.llama_url(path),
                json=body,
                headers=manager.auth_headers(),
            )
    except httpx.TimeoutException:
        return _openai_error(504, "Timeout : le modèle n'a pas répondu à temps.", "server_error")
    except httpx.RequestError as exc:
        log.error("Erreur de connexion à llama-server '%s' : %s", manager.model.id, exc)
        return _openai_error(502, "Impossible de joindre le backend d'inférence.", "server_error")

    duration_ms = int((time.monotonic() - start_time) * 1000)
    try:
        data = response.json()
    except ValueError:
        log.error(
            "Réponse non-JSON de llama-server '%s' (HTTP %d)",
            manager.model.id, response.status_code,
        )
        return _openai_error(502, "Réponse invalide du backend d'inférence.", "server_error")

    usage: dict = {}
    if isinstance(data, dict):
        data["model"] = manager.model.id

        has_any_tool_calls = any(
            choice.get("message", {}).get("tool_calls")
            for choice in data.get("choices", [])
        )
        for choice in data.get("choices", []):
            msg = choice.get("message", {})
            msg.pop("reasoning_content", None)
            if has_any_tool_calls and msg.get("content"):
                msg["content"] = None

        # Supporte le format OpenAI {"usage": {...}} ET le format natif llama.cpp
        # /completion qui retourne {"tokens_predicted": N, "tokens_evaluated": M}.
        usage = data.get("usage") or {
            "prompt_tokens": data.get("tokens_evaluated", 0),
            "completion_tokens": data.get("tokens_predicted", 0),
        }

    fire_and_forget(db.log_usage(
        user_id=user["user_id"],
        key_id=user["key_id"],
        model=manager.model.id,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        duration_ms=duration_ms,
        status_code=response.status_code,
        request_id=request_id,
    ), name="log_usage")

    return JSONResponse(content=data, status_code=response.status_code)


# ── Proxy streaming SSE ───────────────────────────────────────────────────────

async def _stream_proxy(
    path: str,
    body: dict,
    user: dict,
    request_id: str,
    start_time: float,
    manager: ServerManager,
    on_start: Callable[[], None] | None = None,
) -> AsyncGenerator[bytes, None]:
    """
    Générateur async qui pipe les chunks SSE de llama-server vers le client.

    Quand la requête contient des tools : on bufferise tout le stream pour
    détecter si le modèle fait un tool_call. Si oui, on supprime le texte
    "thinking aloud" (content) avant les tool_calls — le SDK Vercel AI
    n'accepte pas un stream avec content + tool_calls mélangés.

    Pin/unpin : manager.pin() est appelé en premier (avant tout yield) et
    manager.unpin() est garanti dans le finally, même en cas de déconnexion
    client (GeneratorExit) ou d'exception réseau. Cela protège le modèle
    contre une éviction LRU pendant toute la durée du stream.

    on_start : callback appelé dès que le pin du stream est posé — utilisé par
    proxy_request pour relâcher son pin de garde.
    """
    manager.pin()
    if on_start is not None:
        on_start()
    prompt_tokens = 0
    completion_tokens = 0
    status_code = 200
    has_tools = bool(body.get("tools"))

    body_with_usage = {**body, "stream_options": {"include_usage": True}}

    try:
        async with httpx.AsyncClient(timeout=_INFERENCE_TIMEOUT) as client:
            async with client.stream(
                "POST",
                manager.llama_url(path),
                json=body_with_usage,
                headers=manager.auth_headers(),
            ) as response:
                status_code = response.status_code

                if has_tools:
                    # ── Mode bufferisé (requête avec tools) ──────────────────
                    chunks: list[dict] = []
                    has_tool_calls = False

                    async for line in response.aiter_lines():
                        if not line or line == "data: [DONE]":
                            continue
                        if line.startswith("data: "):
                            try:
                                chunk = json.loads(line[6:])
                                if "model" in chunk:
                                    chunk["model"] = manager.model.id
                                for choice in chunk.get("choices", []):
                                    delta = choice.get("delta", {})
                                    delta.pop("reasoning_content", None)
                                    if delta.get("tool_calls"):
                                        has_tool_calls = True
                                if usage := chunk.get("usage"):
                                    prompt_tokens = usage.get("prompt_tokens", 0)
                                    completion_tokens = usage.get("completion_tokens", 0)
                                chunks.append(chunk)
                            except json.JSONDecodeError:
                                pass

                    for chunk in chunks:
                        if has_tool_calls:
                            for choice in chunk.get("choices", []):
                                delta = choice.get("delta", {})
                                if delta.get("content") and not delta.get("tool_calls"):
                                    delta.pop("content", None)

                        choices = chunk.get("choices", [])
                        all_empty = all(
                            not choice.get("delta") and choice.get("finish_reason") is None
                            for choice in choices
                        ) if choices else False
                        if not all_empty or not choices:
                            yield ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode()

                    yield b"data: [DONE]\n\n"

                else:
                    # ── Mode streaming direct (sans tools) ───────────────────
                    async for line in response.aiter_lines():
                        if not line:
                            yield b"\n"
                            continue

                        if line.startswith("data: ") and line != "data: [DONE]":
                            try:
                                chunk = json.loads(line[6:])
                                if "model" in chunk:
                                    chunk["model"] = manager.model.id
                                for choice in chunk.get("choices", []):
                                    delta = choice.get("delta", {})
                                    reasoning = delta.pop("reasoning_content", None)
                                    if reasoning and not delta.get("content"):
                                        delta["content"] = reasoning
                                if usage := chunk.get("usage"):
                                    prompt_tokens = usage.get("prompt_tokens", 0)
                                    completion_tokens = usage.get("completion_tokens", 0)
                                line = "data: " + json.dumps(chunk, ensure_ascii=False)
                            except json.JSONDecodeError:
                                pass

                        yield (line + "\n\n").encode()

    except httpx.TimeoutException:
        err = _sse_error("Timeout d'inférence dépassé.")
        yield err.encode()
        status_code = 504
    except httpx.RequestError as exc:
        log.error("Erreur stream llama-server '%s' : %s", manager.model.id, exc)
        err = _sse_error("Erreur de connexion au backend d'inférence.")
        yield err.encode()
        status_code = 502
    finally:
        # Garantie absolue : unpin même si le client se déconnecte (GeneratorExit),
        # si une exception réseau survient, ou si le stream se termine normalement.
        manager.unpin()

    duration_ms = int((time.monotonic() - start_time) * 1000)

    fire_and_forget(db.log_usage(
        user_id=user["user_id"],
        key_id=user["key_id"],
        model=manager.model.id,
        prompt_tokens=int(prompt_tokens or 0),
        completion_tokens=int(completion_tokens or 0),
        duration_ms=duration_ms,
        status_code=status_code,
        request_id=request_id,
    ), name="log_usage_stream")


# ── /v1/models ────────────────────────────────────────────────────────────────

def models_response(model_manager: ModelManager) -> JSONResponse:
    """
    Retourne la liste des modèles activés dans le registre.
    Compatible avec openai.models.list().
    """
    enabled_models = model_manager.registry.list_enabled()
    return JSONResponse(content={
        "object": "list",
        "data": [
            {
                "id": model.id,
                "object": "model",
                "created": 1704067200,
                "owned_by": "local-uppa",
                "description": model.description,
            }
            for model in enabled_models
        ],
    })


# ── Helpers ───────────────────────────────────────────────────────────────────

def _openai_error(
    status_code: int,
    message: str,
    error_type: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers=headers,
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
