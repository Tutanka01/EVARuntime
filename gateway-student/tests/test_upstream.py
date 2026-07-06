"""
Tests pour upstream.py (proxy SSE) et la logique d'estimation de tokens
côté stream de main.py.

Le client httpx est remplacé par un httpx.MockTransport : aucune connexion
réseau réelle, on simule 2xx/4xx/5xx et divers corps SSE en restant offline.
"""
from __future__ import annotations

import json

import httpx
import pytest

import main
import upstream


USER = {"user_id": 1, "key_id": 10, "key_prefix": "llmstu-test"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _install_mock(handler) -> None:
    """Remplace le client global d'upstream par un MockTransport."""
    upstream._client = httpx.AsyncClient(
        base_url=upstream.settings.upstream_base_url,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture(autouse=True)
def _restore_client():
    saved = upstream._client
    yield
    if upstream._client is not None and upstream._client is not saved:
        # Le client mock n'a pas besoin d'être fermé proprement (offline).
        upstream._client = saved


# ---------------------------------------------------------------------------
# stream_chat : gestion d'erreur upstream (4xx/5xx)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_stream_chat_error_with_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "trop de requetes", "type": "rate_limit", "code": "429"}},
        )

    _install_mock(handler)
    chunks = [c async for c in upstream.stream_chat({"model": "m", "messages": []}, USER)]
    joined = b"".join(chunks).decode()

    assert "data: " in joined
    assert "data: [DONE]" in joined
    # Le corps JSON d'erreur upstream est relayé tel quel.
    assert "trop de requetes" in joined


@pytest.mark.anyio
async def test_stream_chat_error_with_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>bad gateway</html>")

    _install_mock(handler)
    chunks = [c async for c in upstream.stream_chat({"model": "m", "messages": []}, USER)]
    joined = b"".join(chunks).decode()

    assert "data: [DONE]" in joined
    # Corps non-JSON : on émet une erreur générique propre (pas de fuite du HTML).
    assert "Erreur upstream." in joined
    assert "502" in joined
    assert "<html>" not in joined


@pytest.mark.anyio
async def test_stream_chat_passthrough_ok() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    _install_mock(handler)
    chunks = [c async for c in upstream.stream_chat({"model": "m", "messages": []}, USER)]
    joined = b"".join(chunks).decode()
    assert '"content": "Hi"' in joined
    assert "data: [DONE]" in joined


# ---------------------------------------------------------------------------
# _strip_reasoning_content
# ---------------------------------------------------------------------------

def test_strip_reasoning_content_removes_field() -> None:
    line = 'data: ' + json.dumps(
        {"choices": [{"delta": {"content": "salut", "reasoning_content": "secret"}}]}
    )
    out = upstream._strip_reasoning_content(line)
    parsed = json.loads(out[6:])
    assert parsed["choices"][0]["delta"]["content"] == "salut"
    assert "reasoning_content" not in parsed["choices"][0]["delta"]


def test_strip_reasoning_content_passthrough_invalid_json() -> None:
    line = "data: not-json-at-all"
    assert upstream._strip_reasoning_content(line) == line


# ---------------------------------------------------------------------------
# Estimation de tokens en cas de coupure de stream
# ---------------------------------------------------------------------------

def test_delta_content_size_counts_content() -> None:
    chunk = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
    assert main.delta_content_size(chunk) == len("hello")


def test_delta_content_size_ignores_done_and_usage() -> None:
    assert main.delta_content_size(b"data: [DONE]\n\n") == 0
    assert main.delta_content_size(b'data: {"usage":{"completion_tokens":9}}\n\n') == 0


def test_delta_content_size_ignores_invalid_json() -> None:
    assert main.delta_content_size(b"data: {broken") == 0


def test_estimate_tokens_rounds_up() -> None:
    # 5 caractères, ~4 chars/token -> ceil(5/4) = 2
    assert main._estimate_tokens(5) == 2
    assert main._estimate_tokens(0) == 0
    # Un caractère produit au moins 1 token imputé (pas de génération gratuite).
    assert main._estimate_tokens(1) == 1


def test_estimated_completion_imputed_when_no_usage_chunk() -> None:
    """Sans chunk usage (stream coupé), on impute l'estimation issue des deltas."""
    stream_chunks = [
        b'data: {"choices":[{"delta":{"content":"abcd"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"efgh"}}]}\n\n',
    ]
    completion_chars = sum(main.delta_content_size(c) for c in stream_chunks)
    assert completion_chars == 8
    # Aucun usage reçu -> estimation non nulle imputée au quota.
    assert main._estimate_tokens(completion_chars) == 2


# ---------------------------------------------------------------------------
# Intégration end-to-end de stream_response : le finally impute réellement
# l'estimation (coupure) ou la valeur exacte (usage reçu) à log_usage.
# ---------------------------------------------------------------------------

def _patch_stream_env(monkeypatch, fake_stream) -> dict:
    """Neutralise les effets de bord de stream_response et capture log_usage."""
    captured: dict = {}

    def fake_log_usage(user_id, key_id, model, prompt_tokens, completion_tokens,
                       duration_ms, status_code, request_id):
        captured["prompt_tokens"] = prompt_tokens
        captured["completion_tokens"] = completion_tokens

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr(main.upstream, "stream_chat", fake_stream)
    monkeypatch.setattr(main.db, "log_usage", fake_log_usage)
    monkeypatch.setattr(main, "fire_and_forget", lambda coro, **kw: coro.close())
    monkeypatch.setattr(main, "emit_audit", lambda *a, **k: None)

    async def fake_release(user):
        return None

    monkeypatch.setattr(main.concurrency, "release", fake_release)
    return captured


@pytest.mark.anyio
async def test_stream_cut_imputes_estimated_tokens_end_to_end(monkeypatch) -> None:
    """Stream coupé (aucun chunk usage) -> log_usage reçoit l'estimation, pas 0."""
    async def fake_stream(body, user):
        yield b'data: {"choices":[{"delta":{"content":"abcd"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"efgh"}}]}\n\n'
        # Pas de chunk usage : simule une coupure avant la fin.

    captured = _patch_stream_env(monkeypatch, fake_stream)
    gen = main.stream_response({"model": "m"}, USER, None, "req-cut", 0.0, 0)
    _ = [chunk async for chunk in gen]

    # 8 caractères reçus -> tokens estimés (> 0) imputés au quota, jamais 0.
    assert captured["completion_tokens"] == main._estimate_tokens(8)
    assert captured["completion_tokens"] > 0


@pytest.mark.anyio
async def test_stream_with_usage_uses_exact_tokens_end_to_end(monkeypatch) -> None:
    """Chunk usage reçu -> valeur exacte utilisée, pas l'estimation (pas de double comptage)."""
    async def fake_stream(body, user):
        yield b'data: {"choices":[{"delta":{"content":"abcd"}}]}\n\n'
        yield b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":99}}\n\n'

    captured = _patch_stream_env(monkeypatch, fake_stream)
    gen = main.stream_response({"model": "m"}, USER, None, "req-ok", 0.0, 0)
    _ = [chunk async for chunk in gen]

    assert captured["completion_tokens"] == 99
    assert captured["prompt_tokens"] == 3
