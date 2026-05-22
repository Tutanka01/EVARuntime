"""
Tests du ClusterManager — utilise LocalNodeAdapter + FakeBackend pour
rester 100% offline et sans sous-processus llama-server.

Couvre :
  - Chargement d'un modèle sur le nœud avec le plus de VRAM libre (best-fit).
  - Éviction LRU automatique quand la VRAM est insuffisante.
  - Nœud offline : exclusion du placement, retour online.
  - Heartbeat : marquage offline après N échecs, retour online.
  - Déchargement admin via unload_model().
  - Rétro-compat registry : status() agrège correctement.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cluster.cluster_manager import ClusterManager, ClusterModelHandle
from cluster.node_client import LocalNodeAdapter, NodeUnreachableError
from cluster.node_protocol import (
    LoadResponse,
    NodeHealth,
    NodeStatus,
    UnloadResponse,
)


# ── Fixtures registry ─────────────────────────────────────────────────────────
# On n'a pas besoin d'un vrai fichier YAML — on injecte un faux registry.

class FakeModelDef:
    """Simule ModelDefinition avec les champs utilisés par ClusterManager."""
    def __init__(self, mid: str, vram: float, enabled: bool = True):
        self.id = mid
        self.vram_gb = vram
        self.enabled = enabled
        self.description = ""
        self.path = Path(f"/models/{mid}.gguf")
        self.capabilities = ["text_generation"]

    def to_dict(self) -> dict:
        return {"id": self.id, "path": str(self.path), "vram_gb": self.vram_gb}


class FakeRegistry:
    def __init__(self, models: list[FakeModelDef]):
        self._models = {m.id: m for m in models}

    def get(self, model_id: str):
        return self._models.get(model_id)

    def list_all(self):
        return list(self._models.values())

    def list_enabled(self):
        return [m for m in self._models.values() if m.enabled]

    def first_enabled_id(self):
        for m in self._models.values():
            if m.enabled:
                return m.id
        return None


# ── FakeNodeBackend ───────────────────────────────────────────────────────────

class FakeNodeBackend:
    """
    Simule un nœud GPU : répond aux appels load/unload/health,
    tient un inventaire de modèles chargés.
    """

    def __init__(
        self,
        node_id: str,
        *,
        total_vram: float = 48.0,
        max_ports: int = 5,
        fail_health: bool = False,
    ):
        self._nid = node_id
        self._total = total_vram
        self._max_ports = max_ports
        self._loaded: dict[str, float] = {}  # model_id → vram_gb
        self.fail_health = fail_health
        self.load_calls: list[dict] = []
        self.unload_calls: list[str] = []
        self.unload_all_called = False

    @property
    def _used(self) -> float:
        return sum(self._loaded.values())

    async def health(self) -> NodeHealth:
        if self.fail_health:
            raise NodeUnreachableError("simulated network failure")
        return NodeHealth(
            status="ok",
            total_vram_gb=self._total,
            used_vram_gb=self._used,
            available_vram_gb=self._total - self._used,
            loaded_model_ids=list(self._loaded),
            free_ports=self._max_ports - len(self._loaded),
        )

    async def status(self) -> NodeStatus:
        return NodeStatus(node_id=self._nid, health=await self.health(), models=[])

    async def load_model(self, model_dict: dict) -> LoadResponse:
        self.load_calls.append(model_dict)
        mid = model_dict["id"]
        vram = float(model_dict.get("vram_gb", 0.0))
        self._loaded[mid] = vram
        return LoadResponse(
            model_id=mid,
            llama_url=f"http://{self._nid}:8081",
            internal_api_key="internal-key",
            port=8081,
        )

    async def unload_model(self, model_id: str) -> UnloadResponse:
        self.unload_calls.append(model_id)
        freed = self._loaded.pop(model_id, 0.0)
        return UnloadResponse(model_id=model_id, unloaded=True, freed_vram_gb=freed)

    async def unload_all(self) -> None:
        self.unload_all_called = True
        self._loaded.clear()


def make_adapter(backend: FakeNodeBackend) -> LocalNodeAdapter:
    return LocalNodeAdapter(backend._nid, backend)


def make_manager(
    backends: list[FakeNodeBackend],
    models: list[FakeModelDef] | None = None,
    *,
    health_interval: int = 1,
    failures_threshold: int = 3,
) -> ClusterManager:
    if models is None:
        models = [FakeModelDef("m1", 20.0), FakeModelDef("m2", 40.0)]
    registry = FakeRegistry(models)
    adapters = [make_adapter(b) for b in backends]
    return ClusterManager(
        registry=registry,
        nodes=adapters,
        health_interval=health_interval,
        health_failures_to_offline=failures_threshold,
    )


# ── Tests de placement ────────────────────────────────────────────────────────

class TestPlacement:
    @pytest.mark.anyio
    async def test_loads_on_single_node(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        try:
            handle = await mgr.ensure_model_loaded("m1")
        finally:
            await mgr.shutdown()

        assert isinstance(handle, ClusterModelHandle)
        assert handle.model.id == "m1"
        assert len(backend.load_calls) == 1
        assert backend.load_calls[0]["id"] == "m1"

    @pytest.mark.anyio
    async def test_best_fit_picks_tighter_node(self):
        """m1 nécessite 20 GB — le nœud 'small' (30 GB libre) est préféré au nœud 'large'."""
        small = FakeNodeBackend("small", total_vram=30.0)  # résidu 10
        large = FakeNodeBackend("large", total_vram=80.0)  # résidu 60
        mgr = make_manager([large, small])
        await mgr.start_health_monitor()
        try:
            handle = await mgr.ensure_model_loaded("m1")
        finally:
            await mgr.shutdown()

        assert handle.llama_url("/v1/chat/completions") == "http://small:8081/v1/chat/completions"
        assert len(small.load_calls) == 1
        assert len(large.load_calls) == 0

    @pytest.mark.anyio
    async def test_second_model_goes_to_other_node(self):
        """m1 sur node-a, m2 sur node-b (best-fit, node-a est déjà plein)."""
        a = FakeNodeBackend("a", total_vram=48.0)
        b = FakeNodeBackend("b", total_vram=48.0)
        models = [FakeModelDef("m1", 40.0), FakeModelDef("m2", 40.0)]
        mgr = make_manager([a, b], models=models)
        await mgr.start_health_monitor()
        try:
            await mgr.ensure_model_loaded("m1")  # atterrit sur a ou b (best-fit)
            await mgr.ensure_model_loaded("m2")  # atterrit sur l'autre
        finally:
            await mgr.shutdown()

        # Les deux nœuds doivent chacun avoir exactement 1 modèle chargé
        assert len(a.load_calls) + len(b.load_calls) == 2
        assert len(a.load_calls) == 1
        assert len(b.load_calls) == 1

    @pytest.mark.anyio
    async def test_returns_cached_handle_on_second_call(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        try:
            h1 = await mgr.ensure_model_loaded("m1")
            h2 = await mgr.ensure_model_loaded("m1")
        finally:
            await mgr.shutdown()

        # Load n'est appelé qu'une seule fois
        assert len(backend.load_calls) == 1
        assert h1.model.id == h2.model.id

    @pytest.mark.anyio
    async def test_unknown_model_raises_lookup_error(self):
        mgr = make_manager([FakeNodeBackend("a")])
        await mgr.start_health_monitor()
        try:
            with pytest.raises(LookupError, match="inconnu"):
                await mgr.ensure_model_loaded("does-not-exist")
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_disabled_model_raises_permission_error(self):
        models = [FakeModelDef("m1", 20.0, enabled=False)]
        mgr = make_manager([FakeNodeBackend("a")], models=models)
        await mgr.start_health_monitor()
        try:
            with pytest.raises(PermissionError, match="désactivé"):
                await mgr.ensure_model_loaded("m1")
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_no_online_node_raises_runtime_error(self):
        backend = FakeNodeBackend("a", total_vram=48.0, fail_health=True)
        mgr = make_manager([backend], failures_threshold=1)
        await mgr.start_health_monitor()
        try:
            with pytest.raises(RuntimeError, match="nœud"):
                await mgr.ensure_model_loaded("m1")
        finally:
            await mgr.shutdown()


# ── Éviction LRU ─────────────────────────────────────────────────────────────

class TestEviction:
    @pytest.mark.anyio
    async def test_evicts_lru_model_when_full(self):
        """
        Node a 40 GB de VRAM. m_old (20 GB, idle) est chargé.
        m_new (30 GB) ne rentre pas sans évincer → m_old doit être évincé.
        """
        backend = FakeNodeBackend("a", total_vram=48.0)
        models = [
            FakeModelDef("m-old", 40.0),
            FakeModelDef("m-new", 40.0),
        ]
        mgr = make_manager([backend], models=models)
        await mgr.start_health_monitor()
        try:
            await mgr.ensure_model_loaded("m-old")
            # m-old est maintenant chargé (40 GB), reste 8 GB libres sur 48 GB
            await mgr.ensure_model_loaded("m-new")  # a besoin de 40 GB → éviction
        finally:
            await mgr.shutdown()

        # m-old doit avoir été évincé avant que m-new soit chargé
        assert "m-old" in backend.unload_calls
        assert backend.load_calls[-1]["id"] == "m-new"


# ── Heartbeat & offline ───────────────────────────────────────────────────────

class TestHeartbeat:
    @pytest.mark.anyio
    async def test_node_marked_offline_after_failures(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend], failures_threshold=3, health_interval=999)
        await mgr.start_health_monitor()
        assert mgr._nodes["a"].online is True

        backend.fail_health = True
        # Simuler 3 échecs manuellement
        for _ in range(3):
            await mgr._check_node(mgr._nodes["a"])

        assert mgr._nodes["a"].online is False
        assert mgr._nodes["a"].consecutive_failures == 3
        await mgr.shutdown()

    @pytest.mark.anyio
    async def test_node_comes_back_online(self):
        backend = FakeNodeBackend("a", total_vram=48.0, fail_health=True)
        mgr = make_manager([backend], failures_threshold=2, health_interval=999)
        await mgr.start_health_monitor()

        for _ in range(2):
            await mgr._check_node(mgr._nodes["a"])
        assert mgr._nodes["a"].online is False

        backend.fail_health = False
        await mgr._check_node(mgr._nodes["a"])
        assert mgr._nodes["a"].online is True
        assert mgr._nodes["a"].consecutive_failures == 0
        await mgr.shutdown()

    @pytest.mark.anyio
    async def test_offline_node_excluded_from_placement(self):
        offline_backend = FakeNodeBackend("offline", total_vram=48.0, fail_health=True)
        online_backend = FakeNodeBackend("online", total_vram=48.0)
        mgr = make_manager([offline_backend, online_backend], failures_threshold=1)
        await mgr.start_health_monitor()
        # offline est déjà offline après le check initial

        handle = await mgr.ensure_model_loaded("m1")
        await mgr.shutdown()

        assert handle.llama_url("/") == "http://online:8081/"
        assert len(offline_backend.load_calls) == 0
        assert len(online_backend.load_calls) == 1


# ── Déchargement admin ────────────────────────────────────────────────────────

class TestAdminUnload:
    @pytest.mark.anyio
    async def test_unload_model_calls_node_client(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        await mgr.ensure_model_loaded("m1")
        await mgr.unload_model("m1")
        await mgr.shutdown()

        assert "m1" in backend.unload_calls
        assert "m1" not in mgr._placement

    @pytest.mark.anyio
    async def test_unload_nonexistent_is_noop(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        try:
            await mgr.unload_model("does-not-exist")  # ne doit pas lever
        finally:
            await mgr.shutdown()


# ── Pin / Unpin ───────────────────────────────────────────────────────────────

class TestPinUnpin:
    @pytest.mark.anyio
    async def test_pin_increments_active_requests(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        handle = await mgr.ensure_model_loaded("m1")

        assert handle.active_requests == 0
        handle.pin()
        assert handle.active_requests == 1
        handle.pin()
        assert handle.active_requests == 2
        handle.unpin()
        assert handle.active_requests == 1
        handle.unpin()
        assert handle.active_requests == 0
        handle.unpin()  # sous zéro doit rester 0
        assert handle.active_requests == 0

        await mgr.shutdown()

    @pytest.mark.anyio
    async def test_llama_url_concatenates_path(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        handle = await mgr.ensure_model_loaded("m1")
        await mgr.shutdown()

        assert handle.llama_url("/v1/chat/completions") == "http://a:8081/v1/chat/completions"


# ── Status ────────────────────────────────────────────────────────────────────

class TestStatus:
    @pytest.mark.anyio
    async def test_status_shows_loaded_and_unloaded(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        models = [FakeModelDef("m1", 20.0), FakeModelDef("m2", 20.0)]
        mgr = make_manager([backend], models=models)
        await mgr.start_health_monitor()
        await mgr.ensure_model_loaded("m1")
        st = mgr.status()
        await mgr.shutdown()

        ids_by_state = {e["id"]: e["state"] for e in st["models"]}
        assert ids_by_state["m1"] == "ready"
        assert ids_by_state["m2"] == "unloaded"

    @pytest.mark.anyio
    async def test_cluster_status_shows_nodes(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        cluster = mgr.cluster_status()
        await mgr.shutdown()

        assert len(cluster) == 1
        assert cluster[0]["node_id"] == "a"
        assert cluster[0]["online"] is True
        assert cluster[0]["total_vram_gb"] == 48.0
