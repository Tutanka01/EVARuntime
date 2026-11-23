"""
Proxy non streamant (`proxy._non_stream_proxy`).

Couvre :
  - la déconnexion du client pendant la génération : uvicorn n'annule pas un
    handler non streamant, la génération continuait donc jusqu'à son terme
    (slot GPU occupé, modèle pinné). La requête upstream est désormais
    annulée, une ligne d'usage 499 écrite, et le pin relâché ;
  - le même scénario sous un VRAI uvicorn, avec le middleware HTTP de
    main.py (`BaseHTTPMiddleware`), dont le relais de `http.disconnect` a
    varié selon les versions de Starlette ;
  - les grosses réponses JSON (embeddings) : parse/sérialisation par
    tranches, octets identiques à `JSONResponse`, boucle non gelée.
"""
from __future__ import annotations

import asyncio
import json
import random
import socket
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import main
import proxy


class FakeModel:
    id = "public-model"


class FakeManager:
    def __init__(self) -> None:
        self.model = FakeModel()
        self.pin_calls = 0
        self.unpin_calls = 0
        self.backend_failure_calls = 0

    def pin(self) -> None:
        self.pin_calls += 1

    def unpin(self) -> None:
        self.unpin_calls += 1

    def llama_url(self, path: str) -> str:
        return f"http://127.0.0.1:8081{path}"

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-internal"}

    async def report_backend_failure(self) -> None:
        self.backend_failure_calls += 1


class _EnsureModelManager:
    def __init__(self, manager: FakeManager) -> None:
        self._manager = manager

    async def ensure_model_loaded(self, model_id: str) -> FakeManager:
        return self._manager


USER = {"user_id": 1, "key_id": 2}
BODY = json.dumps({"model": "public-model", "messages": [{"role": "user", "content": "x"}]})


class _Request:
    """Requête ASGI minimale : corps déjà lu, puis `receive` piloté par le test."""

    def __init__(self, receive) -> None:
        self._receive = receive

    async def body(self) -> bytes:
        return BODY.encode()

    @property
    def receive(self):
        return self._receive


@pytest.fixture
def usage_rows(monkeypatch) -> list[dict]:
    rows: list[dict] = []
    tasks: list[asyncio.Task] = []

    async def _log_usage(**kwargs) -> None:
        rows.append(kwargs)

    def _plan(coro, name=None):
        task = asyncio.get_running_loop().create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(proxy.db, "log_usage", _log_usage)
    monkeypatch.setattr(proxy, "fire_and_forget", _plan)
    return rows


@pytest.fixture
def restore_http_client():
    saved = proxy._http_client
    proxy.set_http_client(None)
    yield
    proxy.set_http_client(saved)


class SlowBackend:
    """Backend qui génère « longtemps » et note s'il a été annulé."""

    def __init__(self, delay: float = 10.0, payload: dict | None = None) -> None:
        self.delay = delay
        self.payload = payload or {
            "model": "upstream", "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.started.set()
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return httpx.Response(200, json=self.payload)


def _inject(handler) -> None:
    proxy.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ── Déconnexion client (unitaire) ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_disconnect_cancels_upstream_and_logs_499(restore_http_client, usage_rows):
    backend = SlowBackend()
    _inject(backend)
    manager = FakeManager()

    async def receive():
        await backend.started.wait()
        await asyncio.sleep(0.05)
        return {"type": "http.disconnect"}

    started = time.monotonic()
    response = await proxy.proxy_request(
        _Request(receive), "/v1/chat/completions", USER, _EnsureModelManager(manager)
    )
    await _settle()

    assert time.monotonic() - started < 2.0, "la génération ne doit pas aller à son terme"
    assert backend.cancelled.is_set(), "la requête upstream doit être annulée"
    assert response.status_code == 499
    assert manager.pin_calls == manager.unpin_calls == 1
    assert [row["status_code"] for row in usage_rows] == [499]
    assert usage_rows[0]["prompt_tokens"] == usage_rows[0]["completion_tokens"] == 0


@pytest.mark.anyio
async def test_connected_client_gets_full_response(restore_http_client, usage_rows):
    """Client resté connecté : réponse intacte, surveillance annulée proprement."""
    backend = SlowBackend(delay=0.05)
    _inject(backend)
    manager = FakeManager()
    watched = asyncio.Event()
    watcher_cancelled = asyncio.Event()

    async def receive():
        watched.set()
        try:
            await asyncio.Event().wait()  # le client ne raccroche jamais
        except asyncio.CancelledError:
            watcher_cancelled.set()
            raise

    response = await proxy.proxy_request(
        _Request(receive), "/v1/chat/completions", USER, _EnsureModelManager(manager)
    )
    await _settle()

    assert response.status_code == 200
    assert json.loads(response.body)["model"] == "public-model"
    assert watched.is_set() and watcher_cancelled.is_set()
    assert not backend.cancelled.is_set()
    assert manager.pin_calls == manager.unpin_calls == 1
    assert [row["status_code"] for row in usage_rows] == [200]


@pytest.mark.anyio
async def test_unexpected_receive_message_never_cancels(restore_http_client, usage_rows):
    """Un message autre que `http.disconnect` (ou une erreur) n'annule rien."""
    backend = SlowBackend(delay=0.05)
    _inject(backend)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    response = await proxy.proxy_request(
        _Request(receive), "/v1/chat/completions", USER, _EnsureModelManager(FakeManager())
    )
    assert response.status_code == 200
    assert not backend.cancelled.is_set()

    async def broken_receive():
        raise RuntimeError("Receive channel has not been made available")

    response = await proxy.proxy_request(
        _Request(broken_receive), "/v1/chat/completions", USER,
        _EnsureModelManager(FakeManager()),
    )
    assert response.status_code == 200


# ── Déconnexion client sous un vrai uvicorn ───────────────────────────────────

def _real_app(manager: FakeManager) -> FastAPI:
    """Même forme que main.app : middleware HTTP de main.py + route proxifiée."""
    app = FastAPI()
    app.middleware("http")(main.log_requests)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await proxy.proxy_request(
            request, "/v1/chat/completions", USER, _EnsureModelManager(manager)
        )

    return app


# Les deux implémentations HTTP d'uvicorn : la production prend httptools
# (`uvicorn[standard]`), h11 est le repli. Le relais de la déconnexion est
# propre à chacune.
HTTP_IMPLEMENTATIONS = ["h11", "httptools"]


async def _serve(app: FastAPI, http: str) -> tuple[uvicorn.Server, asyncio.Task, int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, log_level="warning", lifespan="off", access_log=False, ws="none", http=http,
    ))
    task = asyncio.ensure_future(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task, port


@pytest.mark.anyio
@pytest.mark.parametrize("http", HTTP_IMPLEMENTATIONS)
async def test_real_uvicorn_disconnect_cancels_upstream(restore_http_client, usage_rows, http):
    backend = SlowBackend()
    _inject(backend)
    manager = FakeManager()
    server, task, port = await _serve(_real_app(manager), http)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: test\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(BODY)}\r\n\r\n{BODY}".encode()
        )
        await writer.drain()
        await asyncio.wait_for(backend.started.wait(), timeout=5)
        writer.close()  # le client abandonne en pleine génération

        await asyncio.wait_for(backend.cancelled.wait(), timeout=5)
        deadline = time.monotonic() + 5
        while manager.unpin_calls < 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await _settle()
        assert manager.pin_calls == manager.unpin_calls == 1
        assert [row["status_code"] for row in usage_rows] == [499]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


@pytest.mark.anyio
@pytest.mark.parametrize("http", HTTP_IMPLEMENTATIONS)
async def test_real_uvicorn_connected_request_unchanged(restore_http_client, usage_rows, http):
    """Sous uvicorn + middleware, un client connecté reçoit sa réponse complète."""
    backend = SlowBackend(delay=0.2)
    _inject(backend)
    manager = FakeManager()
    server, task, port = await _serve(_real_app(manager), http)
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/v1/chat/completions", content=BODY,
                headers={"Content-Type": "application/json"},
            )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "ok"
        assert response.json()["model"] == "public-model"
        assert "x-request-id" in response.headers
        assert not backend.cancelled.is_set()
        await _settle()
        assert [row["status_code"] for row in usage_rows] == [200]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


# ── Grosses réponses JSON : parse/sérialisation par tranches ──────────────────

def _embeddings_payload(inputs: int, dims: int) -> dict:
    rng = random.Random(42)
    return {
        "object": "list",
        "model": "/srv/models/embed.gguf",
        "data": [
            {"object": "embedding", "index": i,
             "embedding": [rng.uniform(-1, 1) for _ in range(dims)]}
            for i in range(inputs)
        ],
        "usage": {"prompt_tokens": 12, "total_tokens": 12},
    }


async def _proxy_body(raw: bytes, status: int = 200):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=raw, headers={"content-type": "application/json"})

    _inject(handler)
    return await proxy._non_stream_proxy(
        "/v1/embeddings", {}, USER, "req-big", time.monotonic(), FakeManager()
    )


def _expected_body(raw: bytes) -> bytes:
    data = json.loads(raw)
    if isinstance(data, dict):
        data["model"] = "public-model"
    return JSONResponse(content=data).body


@pytest.mark.anyio
@pytest.mark.parametrize(
    "raw",
    [
        # Formatage du backend non compact, unicode, clé dupliquée, imbrication.
        json.dumps(_embeddings_payload(64, 1024), indent=2).encode(),
        (
            b'\xef\xbb\xbf  {"b": [1, 2.50, {"x": "\\u00e9t\\u00e9"}], "a": "\xc3\xa9", '
            b'"b": [ ], "nested": {"k": [1, [2, 3]]}, "model": null, "pad": "'
            + b"x" * 300_000 + b'"}  \n'
        ),
        # Premier niveau qui n'est pas un objet : relayé tel quel, sans réécriture.
        json.dumps([{"i": i} for i in range(40_000)]).encode(),
    ],
    ids=["embeddings-indented", "edge-cases", "top-level-list"],
)
async def test_large_non_stream_body_is_byte_identical_to_jsonresponse(
    restore_http_client, usage_rows, raw,
):
    assert len(raw) >= proxy._COOPERATIVE_JSON_MIN_BYTES
    response = await _proxy_body(raw)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.body == _expected_body(raw)
    assert int(response.headers["content-length"]) == len(response.body)


@pytest.mark.anyio
async def test_large_body_usage_and_status_are_relayed(restore_http_client, usage_rows):
    raw = json.dumps(_embeddings_payload(64, 1024)).encode()
    response = await _proxy_body(raw, status=207)
    await _settle()
    assert response.status_code == 207
    assert usage_rows[0]["prompt_tokens"] == 12
    assert usage_rows[0]["status_code"] == 207


@pytest.mark.anyio
@pytest.mark.parametrize(
    "raw",
    [
        b'{"data": [1, 2,], "pad": "' + b"x" * 300_000 + b'"}',
        b'{"data": [1, 2] "pad": "' + b"x" * 300_000 + b'"}',
        b'{"pad": "' + b"x" * 300_000 + b'"} trailing',
        b'{"pad": "' + b"x" * 300_000 + b'", }',
        b'{"pad": "' + b"x" * 300_000 + b'"',
        b"\xff\xfe" + b"x" * 300_000,
    ],
    ids=["trailing-comma", "missing-comma", "extra-data", "dangling-comma", "truncated", "binary"],
)
async def test_large_invalid_json_becomes_502_like_small_bodies(
    restore_http_client, usage_rows, raw,
):
    with pytest.raises(ValueError):
        json.loads(raw)
    response = await _proxy_body(raw)
    assert response.status_code == 502
    assert json.loads(response.body)["error"]["message"] == (
        "Réponse invalide du backend d'inférence."
    )


@pytest.mark.anyio
async def test_large_body_with_nan_fails_like_jsonresponse(restore_http_client, usage_rows):
    """NaN : `JSONResponse` refusait de sérialiser (500 global) — comportement conservé."""
    raw = b'{"data": [NaN], "pad": "' + b"x" * 300_000 + b'"}'
    with pytest.raises(ValueError):
        JSONResponse(content=json.loads(raw))
    with pytest.raises(ValueError):
        await _proxy_body(raw)


@pytest.mark.anyio
async def test_large_body_does_not_freeze_the_event_loop(restore_http_client, usage_rows):
    """
    Pendant le traitement d'une grosse réponse, les autres tâches avancent.
    Avant : `response.json()` + `JSONResponse` d'un seul tenant (≈ 84 ms pour
    5,5 Mo), quasiment aucun tour de boucle pour les streams concurrents.
    """
    raw = json.dumps(_embeddings_payload(256, 1024)).encode()
    ticks = 0
    at_backend_reply = 0
    stop = asyncio.Event()

    async def _other_stream() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal at_backend_reply
        at_backend_reply = ticks
        return httpx.Response(200, content=raw)

    _inject(handler)
    other = asyncio.ensure_future(_other_stream())
    await asyncio.sleep(0)
    try:
        response = await proxy._non_stream_proxy(
            "/v1/embeddings", {}, USER, "req-big", time.monotonic(), FakeManager()
        )
        during = ticks - at_backend_reply
    finally:
        stop.set()
        await other
    assert response.body == _expected_body(raw)
    assert during >= 15, f"boucle gelée pendant le traitement ({during} tours)"


# ── Coût CPU du chemin par tranches ───────────────────────────────────────────

def _tokenize_payload(n: int) -> dict:
    rng = random.Random(7)
    return {"tokens": [rng.randint(0, 150_000) for _ in range(n)]}


@pytest.mark.anyio
async def test_flat_scalar_array_is_processed_in_few_c_calls(monkeypatch) -> None:
    """
    `/v1/tokenize` : un tableau plat de 200 000 entiers (1,4 Mo). Décodé et
    encodé élément par élément, il coûtait 15 fois le codec C (un appel Python
    par token). On compte les appels aux codecs C — indépendant de la charge
    de la machine : quelques dizaines par fenêtre de ~64 Kio, pas un par token.
    """
    raw = json.dumps(_tokenize_payload(200_000)).encode()
    calls = {"scan": 0, "encode": 0}
    real_scan, real_encode = proxy._json_scan_once, proxy._json_encode

    def _scan(*args):
        calls["scan"] += 1
        return real_scan(*args)

    def _encode(value):
        calls["encode"] += 1
        return real_encode(value)

    monkeypatch.setattr(proxy, "_json_scan_once", _scan)
    monkeypatch.setattr(proxy, "_json_encode", _encode)

    data = await proxy._loads_cooperative(raw)
    data["model"] = "public-model"
    body = await proxy._dumps_cooperative(data)

    windows = len(raw) // proxy._ARRAY_WINDOW_CHARS + 1
    assert calls["scan"] <= 2 * windows + 10, calls
    assert calls["encode"] <= 2 * windows + 10, calls
    expected = json.loads(raw)
    expected["model"] = "public-model"
    assert body == JSONResponse(content=expected).body


@pytest.mark.anyio
async def test_flat_scalar_array_cpu_stays_close_to_plain_codecs() -> None:
    """
    Garde-fou en temps, volontairement large (×4, meilleur de 5 essais) pour
    ne pas dépendre d'une CI chargée : mesuré ×1,1 à ×1,2, contre ×15 avant.
    """
    raw = json.dumps(_tokenize_payload(200_000)).encode()

    def _plain() -> None:
        data = json.loads(raw)
        data["model"] = "public-model"
        JSONResponse(content=data)

    async def _cooperative() -> None:
        data = await proxy._loads_cooperative(raw)
        data["model"] = "public-model"
        await proxy._dumps_cooperative(data)

    plain = cooperative = float("inf")
    for _ in range(5):
        started = time.perf_counter()
        _plain()
        plain = min(plain, time.perf_counter() - started)
        started = time.perf_counter()
        await _cooperative()
        cooperative = min(cooperative, time.perf_counter() - started)
    assert cooperative <= 4 * plain, (cooperative, plain)


@pytest.mark.anyio
@pytest.mark.parametrize("window", [1, 7, 64, 65_536])
@pytest.mark.parametrize(
    "payload",
    [
        {"tokens": list(range(3000)), "model": "x"},
        {"tokens": [{"id": i, "piece": f"a,{i}]"} for i in range(800)], "n": [1]},
        {"pairs": [[i, [i, ","]] for i in range(800)], "tail": [], "s": ["x", "]"]},
        {"mixed": [1, "a,b", None, {"k": [1, 2]}, [3, [4]], 2.5, True] * 200},
    ],
    ids=["ints", "small-objects", "nested-arrays", "mixed"],
)
async def test_windowed_array_codec_matches_plain_json(monkeypatch, window, payload) -> None:
    """
    Fenêtres minuscules à larges : coupes dans des chaînes, dans des éléments
    imbriqués, après la fermeture du tableau — résultat identique à
    `json.loads` et aux octets de `JSONResponse`.
    """
    monkeypatch.setattr(proxy, "_ARRAY_WINDOW_CHARS", window)
    for indent in (None, 1):
        raw = json.dumps(payload, indent=indent).encode()
        data = await proxy._loads_cooperative(raw)
        assert data == json.loads(raw)
        data["model"] = "public-model"
        assert await proxy._dumps_cooperative(data) == JSONResponse(content=data).body
