from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator

import httpx

from config import settings

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(connect=5.0, read=600.0, write=30.0, pool=5.0)
LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=16)

_client: httpx.AsyncClient | None = None


async def init_client() -> None:
    global _client
    _client = httpx.AsyncClient(
        verify=settings.upstream_verify(),
        cert=settings.upstream_cert(),
        trust_env=False,
        timeout=TIMEOUT,
        limits=LIMITS,
        follow_redirects=False,
    )
    log.info("Upstream client initialisé vers %s", settings.upstream_base_url)


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


def _get_client() -> httpx.AsyncClient:
    if _client is None or _client.is_closed:
        raise RuntimeError("Upstream client non initialisé — appeler init_client() au démarrage")
    return _client


def _headers(user: dict[str, Any]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.upstream_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-Forwarded-User": f"student:{user['user_id']}",
        "X-Student-Key-Prefix": user.get("key_prefix", ""),
    }


async def post_chat(body: dict[str, Any], user: dict[str, Any]) -> httpx.Response:
    url = f"{settings.upstream_base_url}/v1/chat/completions"
    return await _get_client().post(url, json=body, headers=_headers(user))


async def stream_chat(body: dict[str, Any], user: dict[str, Any]) -> AsyncGenerator[bytes, None]:
    url = f"{settings.upstream_base_url}/v1/chat/completions"
    stream_body = {**body, "stream": True, "stream_options": {"include_usage": True}}
    async with _get_client().stream("POST", url, json=stream_body, headers=_headers(user)) as response:
        if response.status_code >= 400:
            payload = await response.aread()
            try:
                error_body = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                error_body = {
                    "error": {
                        "message": "Erreur upstream.",
                        "type": "server_error",
                        "code": str(response.status_code),
                    }
                }
            yield b"data: " + json.dumps(error_body, ensure_ascii=False).encode() + b"\n\n"
            yield b"data: [DONE]\n\n"
            return
        async for line in response.aiter_lines():
            if not line:
                yield b"\n"
                continue
            if line.startswith("data: ") and line != "data: [DONE]":
                line = _strip_reasoning_content(line)
            yield (line + "\n\n").encode()


def _strip_reasoning_content(line: str) -> str:
    try:
        chunk = json.loads(line[6:])
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            delta.pop("reasoning_content", None)
        return "data: " + json.dumps(chunk, ensure_ascii=False)
    except json.JSONDecodeError:
        return line
