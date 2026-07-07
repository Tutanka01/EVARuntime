"""
Tests du proxy streaming SSE (`proxy._stream_proxy`) et du client HTTP partagé.

Contexte : `_stream_proxy` n'avait aucun test alors qu'il concentre la logique
critique de pin/unpin sous déconnexion client, la propagation d'erreur upstream
en SSE, et le parsing tolérant des chunks. On couvre aussi l'invariant clé du
correctif perf : le client `httpx.AsyncClient` partagé n'est JAMAIS fermé à la
fin d'une requête (stream ou non).

Technique :
  - Un `httpx.MockTransport` est injecté dans un `httpx.AsyncClient` partagé via
    `proxy.set_http_client(...)` → aucun vrai llama-server nécessaire.
  - Un `FakeManager` minimal expose `pin()/unpin()/llama_url()/auth_headers()`
    et un `.model` avec un `.id`, et compte les pin/unpin pour vérifier l'équilibre.
  - `proxy.fire_and_forget` est neutralisé pour éviter toute écriture DB réelle.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import proxy


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── Doubles de test ───────────────────────────────────────────────────────────

class FakeModel:
    def __init__(self, mid: str = "test-model") -> None:
        self.id = mid


class FakeManager:
    """ServerManager minimal : compte les pin/unpin pour vérifier l'équilibre."""

    def __init__(self, mid: str = "test-model") -> None:
        self.model = FakeModel(mid)
        self.pin_calls = 0
        self.unpin_calls = 0

    def pin(self) -> None:
        self.pin_calls += 1

    def unpin(self) -> None:
        self.unpin_calls += 1

    def llama_url(self, path: str) -> str:
        return f"http://127.0.0.1:8081{path}"

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-internal"}


USER = {"user_id": "u1", "key_id": "k1"}


@pytest.fixture(autouse=True)
def _no_db_logging(monkeypatch):
    """Neutralise le log d'usage fire-and-forget (pas d'accès DB en test)."""
    def _swallow(coro, name=None):
        # Ferme le coroutine non planifié pour éviter le RuntimeWarning.
        if asyncio.iscoroutine(coro):
            coro.close()
        return None

    monkeypatch.setattr(proxy, "fire_and_forget", _swallow)


@pytest.fixture
def restore_http_client():
    """Restaure l'état du client partagé après injection d'un MockTransport."""
    saved = proxy._http_client
    proxy.set_http_client(None)
    yield
    proxy.set_http_client(saved)


def _inject_client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=transport, timeout=proxy._INFERENCE_TIMEOUT)
    proxy.set_http_client(client)
    return client


def _sse_stream(*events: str) -> bytes:
    """
    Construit un corps SSE : chaque événement (ligne `data: ...`) est suivi d'une
    ligne vide. httpx.Response(content=...) le rejoue et `aiter_lines()` le
    redécoupe comme le ferait un vrai llama-server.
    """
    return ("".join(f"{ev}\n\n" for ev in events)).encode()


# ── 1. Déconnexion client → unpin équilibré ───────────────────────────────────

@pytest.mark.anyio
async def test_stream_client_disconnect_unpins(restore_http_client):
    """
    Le client se déconnecte en plein stream (générateur fermé → GeneratorExit).
    Le modèle doit être unpin exactement autant de fois qu'il a été pin.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        content = _sse_stream(
            'data: {"choices":[{"delta":{"content":"a"}}]}',
            # Chunk suivant jamais consommé : on ferme le générateur avant.
            'data: {"choices":[{"delta":{"content":"b"}}]}',
        )
        return httpx.Response(200, content=content)

    _inject_client(httpx.MockTransport(handler))
    manager = FakeManager()

    gen = proxy._stream_proxy(
        "/v1/chat/completions", {"stream": True}, USER, "req-1", 0.0, manager
    )

    # Consomme un premier chunk puis ferme prématurément le générateur.
    first = await gen.__anext__()
    assert b"data:" in first
    await gen.aclose()  # déclenche GeneratorExit → finally → unpin

    assert manager.pin_calls == 1
    assert manager.unpin_calls == 1, "pin/unpin doivent rester équilibrés à la déconnexion"


# ── 2. Erreur upstream → chunk SSE d'erreur + unpin ───────────────────────────

@pytest.mark.anyio
async def test_stream_upstream_error_yields_sse_error_and_unpins(restore_http_client):
    """
    Le transport lève une RequestError (ex. ReadTimeout) en plein stream :
    un chunk d'erreur SSE propre est émis (+ [DONE]) ET unpin est appelé.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout simulé", request=request)

    _inject_client(httpx.MockTransport(handler))
    manager = FakeManager()

    gen = proxy._stream_proxy(
        "/v1/chat/completions", {"stream": True}, USER, "req-2", 0.0, manager
    )

    collected = b"".join([chunk async for chunk in gen])

    text = collected.decode()
    assert '"error"' in text, "un chunk d'erreur SSE doit être émis"
    assert "data: [DONE]" in text, "le stream d'erreur doit se terminer par [DONE]"
    assert manager.pin_calls == 1
    assert manager.unpin_calls == 1, "unpin doit être appelé malgré l'exception upstream"


# ── 3. Chunk JSON malformé ignoré, flux non interrompu ────────────────────────

@pytest.mark.anyio
async def test_stream_malformed_json_chunk_skipped(restore_http_client):
    """
    Une ligne `data: {json invalide` ne doit pas interrompre le flux : elle est
    forwardée telle quelle (best-effort) et la ligne valide suivante est émise
    normalement, réécrite avec le bon model id.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        content = _sse_stream(
            "data: {json invalide",
            'data: {"model":"x","choices":[{"delta":{"content":"ok"}}]}',
            "data: [DONE]",
        )
        return httpx.Response(200, content=content)

    _inject_client(httpx.MockTransport(handler))
    manager = FakeManager("real-model")

    gen = proxy._stream_proxy(
        "/v1/chat/completions", {"stream": True}, USER, "req-3", 0.0, manager
    )

    collected = b"".join([chunk async for chunk in gen]).decode()

    # La ligne invalide est présente (non fatale) …
    assert "{json invalide" in collected
    # … et la ligne valide est bien émise, avec le model id réécrit.
    assert '"content": "ok"' in collected or '"content":"ok"' in collected
    assert '"real-model"' in collected
    assert manager.unpin_calls == 1


# ── 4. Le client partagé n'est PAS fermé après une requête ────────────────────

@pytest.mark.anyio
async def test_shared_client_not_closed_after_request(restore_http_client):
    """
    Après une requête stream ET une requête non-stream, le client partagé reste
    ouvert et réutilisable (invariant du correctif perf : jamais fermé/recréé
    par requête).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if b'"stream"' in request.content:
            content = _sse_stream(
                'data: {"choices":[{"delta":{"content":"a"}}]}',
                "data: [DONE]",
            )
            return httpx.Response(200, content=content)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hi"}}], "usage": {}}
        )

    client = _inject_client(httpx.MockTransport(handler))
    manager = FakeManager()

    # ── Stream ────────────────────────────────────────────────────────────────
    gen = proxy._stream_proxy(
        "/v1/chat/completions", {"stream": True}, USER, "req-4a", 0.0, manager
    )
    async for _ in gen:
        pass

    assert not client.is_closed, "le client partagé ne doit pas être fermé après un stream"
    assert proxy.get_http_client() is client, "le client partagé doit rester le même"

    # ── Non-stream ────────────────────────────────────────────────────────────
    manager2 = FakeManager()
    resp = await proxy._non_stream_proxy(
        "/v1/chat/completions", {}, USER, "req-4b", 0.0, manager2
    )
    assert resp.status_code == 200
    assert not client.is_closed, "le client partagé ne doit pas être fermé après un non-stream"
    assert proxy.get_http_client() is client

    # Toujours utilisable pour une requête supplémentaire.
    followup = await client.get("http://127.0.0.1:8081/anything")
    assert followup.status_code == 200
