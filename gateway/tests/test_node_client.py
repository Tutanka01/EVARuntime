"""
Tests pour cluster/node_client.py — RemoteNodeClient + LocalNodeAdapter.

Le RemoteNodeClient est testé via httpx.MockTransport (pas de vrai HTTPS),
ce qui permet de simuler des réponses 2xx/4xx/5xx, des timeouts et des
schémas invalides en restant 100% offline.
"""
from __future__ import annotations

import json

import httpx
import pytest

from cluster.node_client import (
    LocalNodeAdapter,
    NodeProtocolError,
    NodeUnreachableError,
    RemoteNodeClient,
)
from cluster.node_protocol import (
    LoadResponse,
    NodeHealth,
    NodeStatus,
    UnloadResponse,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_client(handler) -> RemoteNodeClient:
    """Construit un RemoteNodeClient avec un MockTransport — pas de réseau."""
    client = RemoteNodeClient(
        node_id="test-node",
        base_url="https://node.test:9443",
        agent_secret="s3cret",
        timeout_seconds=2.0,
        verify=False,
    )
    # Remplacer le transport interne par le mock
    client._client = httpx.AsyncClient(
        base_url="https://node.test:9443",
        transport=httpx.MockTransport(handler),
        headers={
            "Authorization": "Bearer s3cret",
            "User-Agent": "llm-gateway-orchestrator",
        },
    )
    return client


HEALTH_OK = {
    "status": "ok",
    "agent_version": "1.0.0",
    "total_vram_gb": 120.0,
    "used_vram_gb": 0.0,
    "available_vram_gb": 120.0,
    "loaded_model_ids": [],
    "free_ports": 5,
}


# ── RemoteNodeClient — Happy paths ───────────────────────────────────────────

class TestRemoteClientHappyPath:
    @pytest.mark.anyio
    async def test_health_parses_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/agent/health"
            assert request.headers["Authorization"] == "Bearer s3cret"
            return httpx.Response(200, json=HEALTH_OK)

        client = make_client(handler)
        try:
            h = await client.health()
        finally:
            await client.close()

        assert isinstance(h, NodeHealth)
        assert h.total_vram_gb == 120.0
        assert h.free_ports == 5

    @pytest.mark.anyio
    async def test_status_parses_response(self):
        body = {
            "node_id": "test-node",
            "health": HEALTH_OK,
            "models": [
                {
                    "id": "llama-70b",
                    "state": "ready",
                    "port": 8081,
                    "pid": 12345,
                    "vram_gb": 42.0,
                    "active_requests": 0,
                },
            ],
        }

        def handler(request):
            assert request.url.path == "/agent/status"
            return httpx.Response(200, json=body)

        client = make_client(handler)
        try:
            s = await client.status()
        finally:
            await client.close()

        assert isinstance(s, NodeStatus)
        assert s.node_id == "test-node"
        assert len(s.models) == 1
        assert s.models[0].id == "llama-70b"

    @pytest.mark.anyio
    async def test_load_model_sends_model_dict(self):
        captured = {}

        def handler(request):
            assert request.url.path == "/agent/models/load"
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "model_id": "m1",
                    "llama_url": "http://node.test:8081",
                    "internal_api_key": "key",
                    "port": 8081,
                    "pid": 999,
                    "already_loaded": False,
                },
            )

        client = make_client(handler)
        try:
            resp = await client.load_model({"id": "m1", "path": "/x/y.gguf"})
        finally:
            await client.close()

        assert isinstance(resp, LoadResponse)
        assert resp.llama_url == "http://node.test:8081"
        assert resp.port == 8081
        assert captured["body"]["model"] == {"id": "m1", "path": "/x/y.gguf"}

    @pytest.mark.anyio
    async def test_unload_model_uses_path_segment(self):
        def handler(request):
            assert request.url.path == "/agent/models/llama-70b/unload"
            return httpx.Response(
                200,
                json={"model_id": "llama-70b", "unloaded": True, "freed_vram_gb": 42.0},
            )

        client = make_client(handler)
        try:
            resp = await client.unload_model("llama-70b")
        finally:
            await client.close()

        assert isinstance(resp, UnloadResponse)
        assert resp.unloaded is True
        assert resp.freed_vram_gb == 42.0

    @pytest.mark.anyio
    async def test_unload_all(self):
        called = {"count": 0}

        def handler(request):
            called["count"] += 1
            assert request.url.path == "/agent/unload-all"
            return httpx.Response(200, json={})

        client = make_client(handler)
        try:
            await client.unload_all()
        finally:
            await client.close()

        assert called["count"] == 1


# ── RemoteNodeClient — Sécurité : reconstruction de llama_url (anti-SSRF) ─────

class TestRemoteClientTrustedLlamaUrl:
    """
    Le llama_url renvoyé par l'agent n'est JAMAIS utilisé tel quel : il est
    reconstruit à partir de l'hôte réel du nœud (base_url) + le port retourné.
    """

    def _load_handler(self, llama_url: str, port: int = 8081, already: bool = False):
        def handler(request):
            assert request.url.path == "/agent/models/load"
            return httpx.Response(
                200,
                json={
                    "model_id": "m1",
                    "llama_url": llama_url,
                    "internal_api_key": "key",
                    "port": port,
                    "pid": 999,
                    "already_loaded": already,
                },
            )

        return handler

    @pytest.mark.anyio
    async def test_loopback_url_is_rebound_to_node_host(self):
        # L'agent renvoie 127.0.0.1 (défaut) → doit être remplacé par l'hôte du nœud.
        client = make_client(self._load_handler("http://127.0.0.1:8081"))
        try:
            resp = await client.load_model({"id": "m1"})
        finally:
            await client.close()
        assert resp.llama_url == "http://node.test:8081"

    @pytest.mark.anyio
    async def test_attacker_url_is_ignored(self):
        # Un agent compromis / MITM renvoie une URL arbitraire → ignorée.
        client = make_client(self._load_handler("http://attacker.example:80"))
        try:
            resp = await client.load_model({"id": "m1"})
        finally:
            await client.close()
        assert resp.llama_url == "http://node.test:8081"
        assert "attacker.example" not in resp.llama_url

    @pytest.mark.anyio
    async def test_already_loaded_url_is_also_rebound(self):
        # already_loaded=True passe par le même load_model → même correction.
        client = make_client(
            self._load_handler("http://attacker.example:80", port=8082, already=True)
        )
        try:
            resp = await client.load_model({"id": "m1"})
        finally:
            await client.close()
        assert resp.already_loaded is True
        assert resp.llama_url == "http://node.test:8082"

    @pytest.mark.anyio
    async def test_port_out_of_range_zero_raises_protocol_error(self):
        client = make_client(self._load_handler("http://node.test:8081", port=0))
        try:
            with pytest.raises(NodeProtocolError, match="port"):
                await client.load_model({"id": "m1"})
        finally:
            await client.close()

    @pytest.mark.anyio
    async def test_port_out_of_range_high_raises_protocol_error(self):
        client = make_client(self._load_handler("http://node.test:8081", port=999999))
        try:
            with pytest.raises(NodeProtocolError, match="port"):
                await client.load_model({"id": "m1"})
        finally:
            await client.close()


# ── RemoteNodeClient — Timeout de chargement dédié ───────────────────────────

class TestRemoteClientLoadTimeout:
    """
    load_model doit utiliser le timeout long (self._load_timeout), pas le
    timeout court du plan de contrôle. Les autres POST gardent le défaut.
    """

    @pytest.mark.anyio
    async def test_load_uses_long_timeout_others_default(self):
        captured: list = []

        client = RemoteNodeClient(
            node_id="test-node",
            base_url="https://node.test:9443",
            agent_secret="s3cret",
            timeout_seconds=2.0,
            load_timeout_seconds=300.0,
            verify=False,
        )

        original_post = client._post

        async def spy_post(path, *, json=None, timeout=None):
            captured.append((path, timeout))
            return await original_post(path, json=json, timeout=timeout)

        client._post = spy_post

        def handler(request):
            path = request.url.path
            if path == "/agent/models/load":
                return httpx.Response(
                    200,
                    json={
                        "model_id": "m1",
                        "llama_url": "http://node.test:8081",
                        "internal_api_key": "key",
                        "port": 8081,
                        "already_loaded": False,
                    },
                )
            # unload-all
            return httpx.Response(200, json={})

        client._client = httpx.AsyncClient(
            base_url="https://node.test:9443",
            transport=httpx.MockTransport(handler),
            headers={
                "Authorization": "Bearer s3cret",
                "User-Agent": "llm-gateway-orchestrator",
            },
        )

        try:
            await client.load_model({"id": "m1"})
            await client.unload_all()
        finally:
            await client.close()

        by_path = dict(captured)
        assert by_path["/agent/models/load"] == 300.0
        # unload-all : pas d'override → None (timeout par défaut du client)
        assert by_path["/agent/unload-all"] is None


# ── RemoteNodeClient — Erreurs réseau ────────────────────────────────────────

class TestRemoteClientNetworkErrors:
    @pytest.mark.anyio
    async def test_connect_error_raises_unreachable(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        client = make_client(handler)
        try:
            with pytest.raises(NodeUnreachableError, match="injoignable"):
                await client.health()
        finally:
            await client.close()

    @pytest.mark.anyio
    async def test_timeout_raises_unreachable(self):
        def handler(request):
            raise httpx.ReadTimeout("timeout")

        client = make_client(handler)
        try:
            with pytest.raises(NodeUnreachableError):
                await client.status()
        finally:
            await client.close()

    @pytest.mark.anyio
    async def test_read_error_on_post_raises_unreachable(self):
        def handler(request):
            raise httpx.ReadError("network down")

        client = make_client(handler)
        try:
            with pytest.raises(NodeUnreachableError):
                await client.load_model({"id": "x"})
        finally:
            await client.close()


# ── RemoteNodeClient — Erreurs protocole ─────────────────────────────────────

class TestRemoteClientProtocolErrors:
    @pytest.mark.anyio
    async def test_500_raises_protocol_error(self):
        def handler(request):
            return httpx.Response(500, text="boom")

        client = make_client(handler)
        try:
            with pytest.raises(NodeProtocolError, match="500"):
                await client.health()
        finally:
            await client.close()

    @pytest.mark.anyio
    async def test_403_raises_protocol_error(self):
        def handler(request):
            return httpx.Response(403, text="bad secret")

        client = make_client(handler)
        try:
            with pytest.raises(NodeProtocolError, match="403"):
                await client.health()
        finally:
            await client.close()

    @pytest.mark.anyio
    async def test_invalid_json_raises_protocol_error(self):
        def handler(request):
            return httpx.Response(200, text="not json")

        client = make_client(handler)
        try:
            with pytest.raises(NodeProtocolError):
                await client.health()
        finally:
            await client.close()

    @pytest.mark.anyio
    async def test_schema_mismatch_raises_protocol_error(self):
        def handler(request):
            # Manque total_vram_gb (champ obligatoire)
            return httpx.Response(200, json={"status": "ok"})

        client = make_client(handler)
        try:
            with pytest.raises(NodeProtocolError, match="schéma"):
                await client.health()
        finally:
            await client.close()


# ── LocalNodeAdapter ─────────────────────────────────────────────────────────

class FakeBackend:
    """Backend trivial pour exercer LocalNodeAdapter sans dépendre d'asyncio I/O."""

    def __init__(self):
        self.loaded: list[str] = []
        self.unloaded: list[str] = []
        self.all_unloaded = False

    async def health(self) -> NodeHealth:
        return NodeHealth(
            total_vram_gb=48.0,
            used_vram_gb=10.0,
            available_vram_gb=38.0,
            loaded_model_ids=list(self.loaded),
            free_ports=4,
        )

    async def status(self) -> NodeStatus:
        return NodeStatus(node_id="local", health=await self.health(), models=[])

    async def load_model(self, model_dict: dict) -> LoadResponse:
        self.loaded.append(model_dict["id"])
        return LoadResponse(
            model_id=model_dict["id"],
            llama_url="http://127.0.0.1:8081",
            internal_api_key="local-key",
            port=8081,
        )

    async def unload_model(self, model_id: str) -> UnloadResponse:
        self.unloaded.append(model_id)
        return UnloadResponse(model_id=model_id, unloaded=True, freed_vram_gb=10.0)

    async def unload_all(self) -> None:
        self.all_unloaded = True


class TestLocalNodeAdapter:
    @pytest.mark.anyio
    async def test_routes_to_backend(self):
        backend = FakeBackend()
        adapter = LocalNodeAdapter("local", backend)

        h = await adapter.health()
        assert h.total_vram_gb == 48.0

        load = await adapter.load_model({"id": "m1"})
        assert load.model_id == "m1"
        assert backend.loaded == ["m1"]

        unload = await adapter.unload_model("m1")
        assert unload.unloaded is True
        assert backend.unloaded == ["m1"]

        await adapter.unload_all()
        assert backend.all_unloaded is True

        # close() est un no-op pour le local — ne doit pas lever
        await adapter.close()

    def test_base_url_is_inprocess(self):
        adapter = LocalNodeAdapter("local", FakeBackend())
        assert adapter.base_url == "in-process"
