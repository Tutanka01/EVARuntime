"""
Tests du proxy streaming SSE (`proxy._stream_proxy`) et du client HTTP partagé.

Contexte : `_stream_proxy` concentre la logique critique de pin/unpin sous
déconnexion client, la propagation d'erreur upstream en SSE, et le parsing
tolérant des chunks. Depuis COR-013, la connexion upstream est ouverte en
pré-flight par `proxy_request` (via `_open_upstream_stream`) : un 4xx/5xx du
backend est converti en vraie réponse HTTP AVANT le premier octet SSE au lieu
d'être relayé comme un flux 200 contenant un JSON d'erreur brut. Le générateur
ne fait plus que relayer les chunks, et planifie dans son finally la ligne
d'usage terminale — exactement UNE par requête, y compris sur déconnexion
client (ACC-001, 499).

Technique :
  - Un `httpx.MockTransport` est injecté dans un `httpx.AsyncClient` partagé via
    `proxy.set_http_client(...)` → aucun vrai llama-server nécessaire.
  - `_open_upstream` mime le pré-flight de `proxy_request` : injection du
    client de test puis ouverture du contexte upstream, sans consommer le
    corps (pas de pin — le pin de garde est posé par `proxy_request`, les
    tests directs n'en ont pas).
  - Un `FakeManager` minimal expose `pin()/unpin()/llama_url()/auth_headers()`
    et un `.model` avec un `.id`, et compte les pin/unpin pour vérifier l'équilibre.
  - La fixture `usage_recorder` capture les lignes d'usage au lieu de les
    écrire en DB ; `proxy_request` est appelé directement avec un faux Request
    et un faux ModelManager pour couvrir le pré-flight.
"""
from __future__ import annotations

import asyncio
import json
import time

import anyio
import httpx
import pytest
from fastapi.responses import JSONResponse

import proxy
import telemetry


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


USER = {"user_id": "u1", "key_id": "k1"}


class _FakeRequest:
    """Request minimal : `proxy_request` ne lit que `request.body()`."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _EnsureModelManager:
    """ModelManager minimal : résout toujours vers le ServerManager factice."""

    def __init__(self, manager: FakeManager) -> None:
        self._manager = manager

    async def ensure_model_loaded(self, model_id: str) -> FakeManager:
        return self._manager


def _stream_request_body(**extra) -> bytes:
    """Corps d'une requête /v1/chat/completions en mode stream."""
    payload: dict = {
        "model": "test-model",
        "stream": True,
        "messages": [{"role": "user", "content": "coucou"}],
    }
    payload.update(extra)
    return json.dumps(payload).encode()


class UsageRecorder:
    """Lignes d'usage capturées, avec flush déterministe des tâches planifiées."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._tasks: list[asyncio.Task] = []

    async def flush(self) -> None:
        """Laisse l'ordonnanceur exécuter les tâches d'usage planifiées."""
        tasks, self._tasks = self._tasks, []
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture(autouse=True)
def _no_db_logging(monkeypatch):
    """Neutralise le log d'usage fire-and-forget (pas d'accès DB en test)."""
    def _swallow(coro, name=None):
        # Ferme le coroutine non planifié pour éviter le RuntimeWarning.
        if asyncio.iscoroutine(coro):
            coro.close()
        return None

    monkeypatch.setattr(proxy, "fire_and_forget", _swallow)
    telemetry.reset_all()
    yield
    telemetry.reset_all()


@pytest.fixture
def usage_recorder(monkeypatch, _no_db_logging) -> UsageRecorder:
    """
    Capture les lignes d'usage au lieu de les écrire en DB.

    Surcharge le monkeypatch de la fixture autouse `_no_db_logging` (posé avant)
    : `fire_and_forget` planifie réellement la coroutine et `db.log_usage`
    enregistre ses kwargs. `recorder.flush()` attend les tâches planifiées pour
    rendre les lignes observables de façon déterministe.
    """
    recorder = UsageRecorder()

    async def _capture_log_usage(**kwargs) -> None:
        recorder.rows.append(dict(kwargs))

    def _plan(coro, name=None):
        task = asyncio.get_running_loop().create_task(coro, name=name or "log_usage")
        recorder._tasks.append(task)
        return task

    monkeypatch.setattr(proxy.db, "log_usage", _capture_log_usage)
    monkeypatch.setattr(proxy, "fire_and_forget", _plan)
    return recorder


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


async def _open_upstream(
    transport: httpx.MockTransport,
    manager: FakeManager,
    path: str = "/v1/chat/completions",
    body: dict | None = None,
) -> proxy._OpenedStream:
    """
    Mime le pré-flight de `proxy_request` : injecte le client de test puis
    ouvre le contexte upstream sans consommer le corps, comme le fait
    `_open_upstream_stream` en production (sans pin de garde ici).
    """
    _inject_client(transport)
    return await proxy._open_upstream_stream(
        manager,
        path,
        {**(body or {}), "stream_options": {"include_usage": True}},
    )


def _sse_stream(*events: str) -> bytes:
    """
    Construit un corps SSE : chaque événement (ligne `data: ...`) est suivi d'une
    ligne vide. httpx.Response(content=...) le rejoue et `aiter_lines()` le
    redécoupe comme le ferait un vrai llama-server.
    """
    return ("".join(f"{ev}\n\n" for ev in events)).encode()


def _json_events(stream: bytes | str) -> list[dict]:
    """Extrait les objets JSON d'un flux SSE collecté."""
    text = stream.decode() if isinstance(stream, bytes) else stream
    return [
        json.loads(line[6:])
        for line in text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


class GatedSSEStream(httpx.AsyncByteStream):
    """Backend SSE qui attend un signal après son premier événement."""

    def __init__(self) -> None:
        self.waiting_for_release = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self):
        yield _sse_stream(
            'data: {"model":"upstream","choices":[{"delta":'
            '{"reasoning_content":"Je réfléchis"}}]}',
        )
        self.waiting_for_release.set()
        await self.release.wait()
        yield _sse_stream(
            'data: {"model":"upstream","choices":[{"delta":{"tool_calls":['
            '{"index":0,"id":"call-1","type":"function","function":'
            '{"name":"search","arguments":"{}"}}]}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":7}}',
            "data: [DONE]",
        )


class TimeoutAfterFirstChunk(httpx.AsyncByteStream):
    """Backend SSE qui lève un timeout en plein milieu du flux (pas au connect)."""

    def __init__(self, captured: list[httpx.Request]) -> None:
        self._captured = captured

    async def __aiter__(self):
        yield _sse_stream('data: {"choices":[{"delta":{"content":"a"}}]}')
        raise httpx.ReadTimeout("timeout simulé", request=self._captured[0])


# ── 0. Raisonnement et tools : streaming réel, sans réécriture ──────────────────

@pytest.mark.anyio
async def test_stream_with_tools_emits_reasoning_before_backend_finishes(
    restore_http_client,
):
    """
    Un harness agentique envoie presque toujours ``tools``. Le premier delta de
    raisonnement doit lui parvenir pendant que le backend produit encore la
    suite, et garder son champ DeepSeek ``reasoning_content`` distinct.

    Le verrou du faux backend est un contrôle positif : un proxy qui lirait
    tout le flux avant son premier yield atteint ``waiting_for_release`` avant
    que ``first_chunk`` soit disponible et fait donc échouer ce test sans
    dépendre d'un sleep ou de la vitesse de la machine.
    """
    upstream = GatedSSEStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=upstream)

    manager = FakeManager("deepseek-reasoner")
    opened = await _open_upstream(httpx.MockTransport(handler), manager)
    gen = proxy._stream_proxy(
        user=USER,
        request_id="req-reasoning-tools",
        start_time=time.monotonic(),
        manager=manager,
        opened=opened,
    )

    first_chunk = asyncio.create_task(anext(gen))
    backend_blocked = asyncio.create_task(upstream.waiting_for_release.wait())
    done, _ = await asyncio.wait(
        {first_chunk, backend_blocked},
        timeout=1,
        return_when=asyncio.FIRST_COMPLETED,
    )
    emitted_before_backend_finished = first_chunk in done

    # Termine toujours proprement le double, y compris avec l'ancien code
    # bufferisé, afin de ne laisser ni tâche ni générateur pendant après l'assert.
    upstream.release.set()
    first = await first_chunk
    remainder = b"".join([chunk async for chunk in gen])
    await backend_blocked

    assert emitted_before_backend_finished, (
        "le proxy a attendu la fin du backend avant d'émettre le raisonnement"
    )
    assert _json_events(first)[0]["choices"][0]["delta"] == {
        "reasoning_content": "Je réfléchis",
    }
    assert _json_events(first)[0]["model"] == "deepseek-reasoner"
    assert _json_events(remainder)[0]["choices"][0]["delta"]["tool_calls"][0][
        "id"
    ] == "call-1"
    assert manager.pin_calls == manager.unpin_calls == 1


@pytest.mark.anyio
async def test_stream_with_tools_preserves_content_and_tool_call_deltas(
    restore_http_client,
):
    """Le proxy ne supprime aucun delta upstream lorsqu'un tool call survient."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_stream(
            'data: {"choices":[{"delta":{"content":"Préambule"}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"name":"search","arguments":""}}]}}]}',
            "data: [DONE]",
        ))

    manager = FakeManager()
    opened = await _open_upstream(httpx.MockTransport(handler), manager)
    gen = proxy._stream_proxy(
        user=USER,
        request_id="req-content-tools",
        start_time=0.0,
        manager=manager,
        opened=opened,
    )

    deltas = [event["choices"][0]["delta"] for event in _json_events(
        b"".join([chunk async for chunk in gen])
    )]

    assert deltas[0] == {"content": "Préambule"}
    assert deltas[1]["tool_calls"][0]["function"]["name"] == "search"


@pytest.mark.anyio
async def test_stream_reasoning_content_is_preserved_and_records_ttft(
    restore_http_client,
):
    """Un delta de raisonnement reste distinct et compte comme premier token."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_stream(
            'data: {"choices":[{"delta":{"reasoning_content":"Analysons"}}]}',
            "data: [DONE]",
        ))

    manager = FakeManager()
    opened = await _open_upstream(httpx.MockTransport(handler), manager)
    gen = proxy._stream_proxy(
        user=USER,
        request_id="req-reasoning-ttft",
        start_time=time.monotonic(),
        manager=manager,
        opened=opened,
    )

    events = _json_events(b"".join([chunk async for chunk in gen]))

    assert events[0]["choices"][0]["delta"] == {
        "reasoning_content": "Analysons",
    }
    assert telemetry.TTFT_SECONDS.snapshot().series[0].count == 1


@pytest.mark.anyio
async def test_non_stream_preserves_reasoning_content(restore_http_client):
    """La variante non-streaming conserve aussi l'extension DeepSeek."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "reasoning_content": "Calcul interne",
                    "content": "Réponse finale",
                },
            }],
            "usage": {},
        })

    _inject_client(httpx.MockTransport(handler))
    response = await proxy._non_stream_proxy(
        "/v1/chat/completions",
        {},
        USER,
        "req-reasoning-non-stream",
        0.0,
        FakeManager(),
    )

    message = json.loads(response.body)["choices"][0]["message"]
    assert message["reasoning_content"] == "Calcul interne"
    assert message["content"] == "Réponse finale"


@pytest.mark.anyio
async def test_stream_records_ttft_on_first_meaningful_chunk(restore_http_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_stream(
            'data: {"choices":[{"delta":{"role":"assistant"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":1}}',
            'data: {"choices":[{"delta":{"content":"bonjour"}}]}',
            "data: [DONE]",
        ))

    manager = FakeManager()
    opened = await _open_upstream(httpx.MockTransport(handler), manager)
    gen = proxy._stream_proxy(
        user=USER,
        request_id="req-ttft",
        start_time=time.monotonic(),
        manager=manager,
        opened=opened,
    )

    _ = b"".join([chunk async for chunk in gen])

    series = telemetry.TTFT_SECONDS.snapshot().series
    assert len(series) == 1
    assert (series[0].model, series[0].node, series[0].count) == (
        "test-model",
        "local",
        1,
    )


# ── 1. Déconnexion client → unpin équilibré + ligne d'usage 499 ───────────────

@pytest.mark.anyio
async def test_stream_client_disconnect_unpins(restore_http_client, usage_recorder):
    """
    Le client se déconnecte en plein stream (générateur fermé → GeneratorExit).
    Le modèle doit être unpin exactement autant de fois qu'il a été pin, et la
    ligne d'usage terminale doit quand même être planifiée avec le statut 499
    (convention « client cancelled ») — elle vivait après le finally avant
    ACC-001 et n'était donc jamais écrite.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        content = _sse_stream(
            'data: {"choices":[{"delta":{"content":"a"}}]}',
            # Chunk suivant jamais consommé : on ferme le générateur avant.
            'data: {"choices":[{"delta":{"content":"b"}}]}',
        )
        return httpx.Response(200, content=content)

    manager = FakeManager()
    opened = await _open_upstream(httpx.MockTransport(handler), manager)

    gen = proxy._stream_proxy(
        user=USER,
        request_id="req-1",
        start_time=0.0,
        manager=manager,
        opened=opened,
    )

    # Consomme un premier chunk puis ferme prématurément le générateur.
    first = await gen.__anext__()
    assert b"data:" in first
    await gen.aclose()  # déclenche GeneratorExit → finally → unpin
    await usage_recorder.flush()

    assert manager.pin_calls == 1
    assert manager.unpin_calls == 1, "pin/unpin doivent rester équilibrés à la déconnexion"
    assert len(usage_recorder.rows) == 1, "exactement UNE ligne d'usage terminale"
    assert usage_recorder.rows[0]["status_code"] == 499
    assert usage_recorder.rows[0]["request_id"] == "req-1"


# ── 2. Erreur upstream en plein stream → chunk SSE d'erreur + unpin ───────────

@pytest.mark.anyio
async def test_stream_upstream_error_yields_sse_error_and_unpins(
    restore_http_client, usage_recorder,
):
    """
    Le transport lève une RequestError (ex. ReadTimeout) en plein stream —
    après les premiers chunks : un chunk d'erreur SSE propre est émis
    (+ [DONE]), unpin est appelé, et la ligne d'usage porte le statut 504.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, stream=TimeoutAfterFirstChunk(captured))

    manager = FakeManager()
    opened = await _open_upstream(httpx.MockTransport(handler), manager)

    gen = proxy._stream_proxy(
        user=USER,
        request_id="req-2",
        start_time=0.0,
        manager=manager,
        opened=opened,
    )

    collected = b"".join([chunk async for chunk in gen])
    await usage_recorder.flush()

    text = collected.decode()
    assert '"error"' in text, "un chunk d'erreur SSE doit être émis"
    assert "data: [DONE]" in text, "le stream d'erreur doit se terminer par [DONE]"
    assert manager.pin_calls == 1
    assert manager.unpin_calls == 1, "unpin doit être appelé malgré l'exception upstream"
    assert manager.backend_failure_calls == 1
    assert len(usage_recorder.rows) == 1
    assert usage_recorder.rows[0]["status_code"] == 504


@pytest.mark.anyio
async def test_non_stream_upstream_error_reports_backend_failure(restore_http_client):
    """Le chemin non-stream invalide lui aussi immédiatement le placement cluster."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connexion refusée", request=request)

    _inject_client(httpx.MockTransport(handler))
    manager = FakeManager()

    response = await proxy._non_stream_proxy(
        "/v1/chat/completions", {}, USER, "req-2b", 0.0, manager
    )

    assert response.status_code == 502
    assert manager.backend_failure_calls == 1


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

    manager = FakeManager("real-model")
    opened = await _open_upstream(httpx.MockTransport(handler), manager)
    gen = proxy._stream_proxy(
        user=USER,
        request_id="req-3",
        start_time=0.0,
        manager=manager,
        opened=opened,
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
    par requête). Fermer le contexte du stream ne ferme que la connexion
    empruntée, pas le client.
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
    opened = await proxy._open_upstream_stream(
        manager,
        "/v1/chat/completions",
        {"stream": True, "stream_options": {"include_usage": True}},
    )
    gen = proxy._stream_proxy(
        user=USER,
        request_id="req-4a",
        start_time=0.0,
        manager=manager,
        opened=opened,
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


# ── 5. Pré-flight COR-013 : erreurs upstream AVANT le premier octet SSE ───────
#
# Ces tests passent par `proxy_request` avec un faux Request et un faux
# ModelManager : c'est le pré-flight qui décide désormais du sort d'un 4xx/5xx
# upstream, avant la création de la StreamingResponse.

@pytest.mark.anyio
async def test_preflight_upstream_500_json_relaid_as_http_error(
    restore_http_client, usage_recorder,
):
    """
    COR-013 : un 500 upstream ne doit PAS être relayé comme un flux SSE 200.
    Le pré-flight voit le statut avant le premier octet et renvoie une vraie
    réponse HTTP application/json qui relaie le corps d'erreur verbatim.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    _inject_client(httpx.MockTransport(handler))
    manager = FakeManager()
    response = await proxy.proxy_request(
        _FakeRequest(_stream_request_body()),
        "/v1/chat/completions",
        USER,
        _EnsureModelManager(manager),
    )
    await usage_recorder.flush()

    assert isinstance(response, JSONResponse), "pas de StreamingResponse pour une erreur upstream"
    assert response.headers["content-type"].startswith("application/json")
    assert response.status_code == 500
    assert json.loads(response.body) == {"error": {"message": "boom"}}
    # Le pin de garde a été relâché malgré l'erreur.
    assert manager.pin_calls == manager.unpin_calls == 1
    assert len(usage_recorder.rows) == 1
    assert usage_recorder.rows[0]["status_code"] == 500


@pytest.mark.anyio
async def test_preflight_upstream_400_non_json_becomes_502(
    restore_http_client, usage_recorder,
):
    """
    Corps d'erreur non-JSON du backend (ex. page HTML d'un proxy intermédiaire)
    → enveloppe `_openai_error` 502, mêmes sémantiques que _non_stream_proxy.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, content=b"<html>Bad Gateway</html>",
            headers={"content-type": "text/html"},
        )

    _inject_client(httpx.MockTransport(handler))
    manager = FakeManager()
    response = await proxy.proxy_request(
        _FakeRequest(_stream_request_body()),
        "/v1/chat/completions",
        USER,
        _EnsureModelManager(manager),
    )
    await usage_recorder.flush()

    assert isinstance(response, JSONResponse)
    assert response.status_code == 502
    error = json.loads(response.body)["error"]
    assert error["message"] == "Réponse invalide du backend d'inférence."
    assert error["type"] == "server_error"
    assert error["code"] == "502"
    assert len(usage_recorder.rows) == 1
    assert usage_recorder.rows[0]["status_code"] == 502


@pytest.mark.anyio
async def test_preflight_upstream_429_relayed_and_logged(
    restore_http_client, usage_recorder,
):
    """Un 429 upstream est relayé tel quel et journalisé avec son vrai statut."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    _inject_client(httpx.MockTransport(handler))
    manager = FakeManager()
    response = await proxy.proxy_request(
        _FakeRequest(_stream_request_body()),
        "/v1/chat/completions",
        USER,
        _EnsureModelManager(manager),
    )
    await usage_recorder.flush()

    assert isinstance(response, JSONResponse)
    assert response.status_code == 429
    assert json.loads(response.body) == {"error": {"message": "slow down"}}
    assert len(usage_recorder.rows) == 1
    assert usage_recorder.rows[0]["status_code"] == 429


@pytest.mark.anyio
async def test_preflight_connect_error_returns_502_and_logs_usage(
    restore_http_client, usage_recorder,
):
    """
    Erreur de transport au pré-flight (backend pas encore prêt, port fermé) →
    502 JSON, placement cluster invalidé, et UNE ligne d'usage 502 journalisée
    bien qu'aucun octet n'ait été envoyé au client.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connexion refusée", request=request)

    _inject_client(httpx.MockTransport(handler))
    manager = FakeManager()
    response = await proxy.proxy_request(
        _FakeRequest(_stream_request_body()),
        "/v1/chat/completions",
        USER,
        _EnsureModelManager(manager),
    )
    await usage_recorder.flush()

    assert isinstance(response, JSONResponse)
    assert response.status_code == 502
    error = json.loads(response.body)["error"]
    assert error["message"] == "Impossible de joindre le backend d'inférence."
    assert manager.backend_failure_calls == 1
    assert manager.pin_calls == manager.unpin_calls == 1
    assert len(usage_recorder.rows) == 1
    assert usage_recorder.rows[0]["status_code"] == 502


# ── 6. ACC-001 : exactement UNE ligne d'usage terminale par requête ───────────

@pytest.mark.anyio
async def test_stream_normal_completion_logs_exactly_one_usage_row(
    restore_http_client, usage_recorder,
):
    """
    Fin normale : UNE seule ligne, statut 200, tokens issus du chunk usage
    injecté par `stream_options.include_usage`.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse_stream(
            'data: {"choices":[{"delta":{"content":"a"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":7}}',
            "data: [DONE]",
        ))

    manager = FakeManager()
    opened = await _open_upstream(httpx.MockTransport(handler), manager)
    gen = proxy._stream_proxy(
        user=USER,
        request_id="req-usage-ok",
        start_time=0.0,
        manager=manager,
        opened=opened,
    )

    collected = b"".join([chunk async for chunk in gen])
    await usage_recorder.flush()

    assert "data: [DONE]" in collected.decode()
    assert manager.pin_calls == manager.unpin_calls == 1
    assert len(usage_recorder.rows) == 1, "une seule ligne terminale, pas de doublon"
    row = usage_recorder.rows[0]
    assert row["status_code"] == 200
    assert row["prompt_tokens"] == 3
    assert row["completion_tokens"] == 7
    assert row["request_id"] == "req-usage-ok"
    assert row["user_id"] == "u1"
    assert row["key_id"] == "k1"


# ── 7. Annulation anyio (uvicorn spec 2.3) : le finally doit survivre ─────────

class CancellableSSEStream(httpx.AsyncByteStream):
    """
    Backend SSE à déconnexion client : premier chunk immédiat, puis blocage
    définitif. ``aclose`` suspend volontairement un cycle de boucle, comme la
    fermeture d'une vraie connexion réseau : c'est ce point de suspension qui
    laisse anyio RE-livrer l'annulation pendant le finally du générateur
    (re-livraison par ``call_soon`` tant que la scope reste annulée) —
    exactement la condition qui faisait fuir le pin et la ligne 499.
    """

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def __aiter__(self):
        yield _sse_stream('data: {"choices":[{"delta":{"content":"a"}}]}')
        await self.release.wait()

    async def aclose(self) -> None:
        await asyncio.sleep(0)


@pytest.mark.anyio
async def test_stream_client_disconnect_under_cancelled_anyio_scope(
    restore_http_client, usage_recorder,
):
    """
    Sous uvicorn (spec_version 2.3), Starlette itère la réponse dans un task
    group anyio et ANNULE la scope à la déconnexion client. anyio re-livre
    alors CancelledError à chaque await de la scope (boucle call_soon tant
    que la scope reste annulée) : sans bouclier dans le finally, le premier
    await y échouait, sautait manager.unpin() (fuite de _active_requests —
    modèle plus jamais idle-évictable) et la ligne 499.

    Fidélité à la production : on consomme d'abord le chunk data ET son
    séparateur de ligne vide déjà bufferisé par aiter_lines — sinon le
    générateur se re-parque sur son propre yield sans jamais retomber dans
    l'attente upstream, et l'annulation n'est jamais livrée. Une fois le
    buffer vide, le générateur est suspendu DANS release.wait() : c'est là
    que la re-livraison anyio doit le rattraper, pendant que le finally
    nettoie (fermeture du contexte suspendue un cycle de boucle, comme une
    vraie fermeture de socket).
    """
    upstream = CancellableSSEStream()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=upstream)

    manager = FakeManager()
    opened = await _open_upstream(httpx.MockTransport(handler), manager)
    gen = proxy._stream_proxy(
        user=USER,
        request_id="req-anyio-cancel",
        start_time=0.0,
        manager=manager,
        opened=opened,
    )

    first = await gen.__anext__()
    assert b"data:" in first
    separator = await gen.__anext__()  # ligne vide bufferisée du chunk 1
    assert separator == b"\n"
    # À partir d'ici, le prochain __anext__ suspend dans release.wait().

    with anyio.CancelScope() as scope:
        scope.cancel()
        try:
            await gen.__anext__()
        except (asyncio.CancelledError, GeneratorExit):
            pass
    await usage_recorder.flush()

    assert manager.pin_calls == 1
    assert manager.unpin_calls == 1, (
        "le finally doit survivre à la re-livraison anyio : pas de fuite de pin"
    )
    assert len(usage_recorder.rows) == 1, (
        "la ligne d'usage 499 doit être écrite malgré l'annulation"
    )
    assert usage_recorder.rows[0]["status_code"] == 499


@pytest.mark.anyio
async def test_preflight_connect_timeout_returns_504(
    restore_http_client, usage_recorder,
):
    """
    ConnectTimeout (⊂ TimeoutException) au pré-flight → 504 enveloppe OpenAI,
    placement cluster invalidé, et UNE ligne d'usage 504 journalisée alors
    qu'aucun octet n'a atteint le client.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connexion trop lente", request=request)

    _inject_client(httpx.MockTransport(handler))
    manager = FakeManager()
    response = await proxy.proxy_request(
        _FakeRequest(_stream_request_body()),
        "/v1/chat/completions",
        USER,
        _EnsureModelManager(manager),
    )
    await usage_recorder.flush()

    assert isinstance(response, JSONResponse)
    assert response.status_code == 504
    error = json.loads(response.body)["error"]
    assert error["message"] == "Timeout : le modèle n'a pas répondu à temps."
    assert manager.backend_failure_calls == 1
    assert manager.pin_calls == manager.unpin_calls == 1
    assert len(usage_recorder.rows) == 1
    assert usage_recorder.rows[0]["status_code"] == 504
