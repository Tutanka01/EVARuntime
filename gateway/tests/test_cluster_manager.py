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
from cluster.node_client import (
    LocalNodeAdapter,
    NodeProtocolError,
    NodeUnreachableError,
)
from cluster.node_protocol import (
    LoadResponse,
    ModelStateOnNode,
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
        health_exc: Exception | None = None,
        fail_unload: bool = False,
        fail_unload_all: bool = False,
        load_exc: Exception | None = None,
        reserved_vram: float = 0.0,
        preloaded: dict[str, float] | None = None,
    ):
        self._nid = node_id
        self._total = total_vram
        self._max_ports = max_ports
        self._loaded: dict[str, float] = dict(preloaded or {})  # model_id → vram_gb
        self.fail_health = fail_health
        # Si fourni, health() lève cette exception « inattendue » (ni
        # NodeUnreachableError ni NodeProtocolError) — pour tester le catch-all.
        self.health_exc = health_exc
        # Si True, unload_model lève NodeUnreachableError (nœud flaky) sans
        # retirer le modèle de l'inventaire (le llama-server tourne toujours).
        self.fail_unload = fail_unload
        self.fail_unload_all = fail_unload_all
        self.load_exc = load_exc
        self._reserved_vram = reserved_vram
        self.load_started: asyncio.Event | None = None
        self.release_load: asyncio.Event | None = None
        self.load_calls: list[dict] = []
        self.unload_calls: list[str] = []
        self.unload_all_called = False

    @property
    def _used(self) -> float:
        return sum(self._loaded.values())

    async def health(self) -> NodeHealth:
        if self.health_exc is not None:
            raise self.health_exc
        if self.fail_health:
            raise NodeUnreachableError("simulated network failure")
        return NodeHealth(
            status="ok",
            total_vram_gb=self._total,
            used_vram_gb=self._used,
            available_vram_gb=max(
                0.0, self._total - self._used - self._reserved_vram
            ),
            loaded_model_ids=list(self._loaded),
            free_ports=self._max_ports - len(self._loaded),
        )

    async def status(self) -> NodeStatus:
        models = [
            ModelStateOnNode(id=mid, state="ready", port=8081, vram_gb=vram)
            for mid, vram in self._loaded.items()
        ]
        return NodeStatus(node_id=self._nid, health=await self.health(), models=models)

    async def load_model(self, model_dict: dict) -> LoadResponse:
        self.load_calls.append(model_dict)
        if self.load_started is not None:
            self.load_started.set()
        if self.release_load is not None:
            await self.release_load.wait()
        if self.load_exc is not None:
            raise self.load_exc
        mid = model_dict["id"]
        vram = float(model_dict.get("vram_gb", 0.0))
        already = mid in self._loaded
        self._loaded[mid] = vram
        return LoadResponse(
            model_id=mid,
            llama_url=f"http://{self._nid}:8081",
            internal_api_key="internal-key",
            port=8081,
            already_loaded=already,
        )

    async def unload_model(self, model_id: str) -> UnloadResponse:
        self.unload_calls.append(model_id)
        if self.fail_unload:
            # Nœud flaky : l'appel réseau échoue et le modèle reste chargé.
            raise NodeUnreachableError("simulated unload failure")
        freed = self._loaded.pop(model_id, 0.0)
        return UnloadResponse(model_id=model_id, unloaded=True, freed_vram_gb=freed)

    async def unload_all(self) -> None:
        if self.fail_unload_all:
            raise NodeUnreachableError("simulated unload-all failure")
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


# ── Réconciliation d'état au démarrage ────────────────────────────────────────

class TestReconciliation:
    @pytest.mark.anyio
    async def test_reconciles_already_loaded_model(self):
        """
        Au démarrage, un nœud rapporte déjà m1 chargé via status() →
        _placement / loaded sont reconstruits SANS rechargement.
        """
        backend = FakeNodeBackend("a", total_vram=48.0, preloaded={"m1": 20.0})
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        try:
            # Placement restauré directement depuis status(), aucun load appelé.
            assert mgr._placement.get("m1") == "a"
            assert "m1" in mgr._nodes["a"].loaded
            assert len(backend.load_calls) == 0
            assert mgr._nodes["a"].loaded["m1"].vram_gb == 20.0
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_reconciled_entry_refreshed_on_first_request(self):
        """
        Une entrée réconciliée (internal_api_key vide) est rafraîchie via un
        load_model idempotent (already_loaded) au premier ensure_model_loaded,
        sur le MÊME nœud, sans replacement ailleurs.
        """
        backend = FakeNodeBackend("a", total_vram=48.0, preloaded={"m1": 20.0})
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        try:
            assert mgr._nodes["a"].loaded["m1"].internal_api_key == ""
            handle = await mgr.ensure_model_loaded("m1")
            # Un unique load idempotent, resté sur le nœud 'a'.
            assert len(backend.load_calls) == 1
            assert backend.load_calls[0]["id"] == "m1"
            assert handle.auth_headers()["Authorization"] == "Bearer internal-key"
            assert mgr._placement["m1"] == "a"
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_reconciled_ipv6_node_url_keeps_required_brackets(self):
        backend = FakeNodeBackend("ipv6", preloaded={"m1": 20.0})
        mgr = make_manager([backend])
        mgr._nodes["ipv6"].client.base_url = "https://[fd00::1234]:9443"
        await mgr.start_health_monitor()
        try:
            info = mgr._nodes["ipv6"].loaded["m1"]
            assert info.llama_url == "http://[fd00::1234]:8081"
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_reconciliation_skips_unreachable_node(self):
        """Un nœud dont status() échoue (offline) est sauté proprement."""
        offline = FakeNodeBackend("off", total_vram=48.0, fail_health=True,
                                  preloaded={"m1": 20.0})
        mgr = make_manager([offline], failures_threshold=1)
        await mgr.start_health_monitor()
        try:
            # Nœud offline → aucune réconciliation, placement vide.
            assert "m1" not in mgr._placement
        finally:
            await mgr.shutdown()


# ── Failover rapide (nœud offline entre deux heartbeats) ──────────────────────

class TestFailover:
    @pytest.mark.anyio
    async def test_offline_node_invalidates_fast_path_and_replaces(self):
        """
        m1 est chargé sur node-a. node-a crashe (online=False) SANS heartbeat.
        Le fast-path ne doit PAS renvoyer son handle : ensure_model_loaded
        replace m1 sur node-b (online).
        """
        a = FakeNodeBackend("a", total_vram=48.0)
        b = FakeNodeBackend("b", total_vram=48.0)
        mgr = make_manager([a, b])
        await mgr.start_health_monitor()
        try:
            h1 = await mgr.ensure_model_loaded("m1")
            first_node = mgr._placement["m1"]
            # Simuler le crash du nœud qui héberge m1 (sans attendre le heartbeat).
            mgr._nodes[first_node].online = False

            h2 = await mgr.ensure_model_loaded("m1")
            second_node = mgr._placement["m1"]
        finally:
            await mgr.shutdown()

        # Replacé sur un nœud ONLINE différent du nœud crashé.
        assert second_node != first_node
        assert mgr._nodes[second_node].online is True
        assert h2.llama_url("/") == f"http://{second_node}:8081/"
        # L'ancien handle pointait vers le nœud crashé, le nouveau vers l'autre.
        assert h1.llama_url("/") != h2.llama_url("/")


# ── _check_node robuste à toute exception ─────────────────────────────────────

class TestCheckNodeRobustness:
    @pytest.mark.anyio
    async def test_unexpected_exception_marks_offline(self):
        """
        Un health() qui lève une exception INATTENDUE (ValueError) incrémente
        consecutive_failures et finit par passer le nœud offline.
        """
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend], failures_threshold=3, health_interval=999)
        await mgr.start_health_monitor()
        assert mgr._nodes["a"].online is True

        backend.health_exc = ValueError("boom inattendu")
        for _ in range(3):
            await mgr._check_node(mgr._nodes["a"])

        assert mgr._nodes["a"].consecutive_failures == 3
        assert mgr._nodes["a"].online is False
        await mgr.shutdown()


# ── _do_unload sur nœud flaky (pas de purge optimiste) ────────────────────────

class TestFlakyUnload:
    @pytest.mark.anyio
    async def test_failed_unload_keeps_state_when_model_still_loaded(self):
        """
        unload_model échoue (nœud flaky) ET le modèle tourne toujours côté nœud.
        L'état local NE doit PAS être purgé de façon optimiste (VRAM occupée),
        pour ne pas sur-réserver au placement suivant.
        """
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        try:
            await mgr.ensure_model_loaded("m1")
            assert "m1" in mgr._placement

            # Rendre le nœud flaky : unload lèvera, mais health() confirme que
            # m1 est toujours chargé → l'entrée doit être conservée.
            backend.fail_unload = True
            await mgr.unload_model("m1")

            # Non purgé : placement + comptabilité VRAM conservés.
            assert mgr._placement.get("m1") == "a"
            assert "m1" in mgr._nodes["a"].loaded
        finally:
            backend.fail_unload = False
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_failed_unload_purges_when_node_confirms_gone(self):
        """
        unload_model échoue MAIS health() montre que le modèle n'est plus chargé
        (parti quand même côté nœud) → purge sûre de l'état local.
        """
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        try:
            await mgr.ensure_model_loaded("m1")

            backend.fail_unload = True
            backend._loaded.pop("m1", None)
            await mgr.unload_model("m1")

            assert "m1" not in mgr._placement
            assert "m1" not in mgr._nodes["a"].loaded
        finally:
            backend.fail_unload = False
            await mgr.shutdown()


# ── Régressions production multi-nœuds ──────────────────────────────────────────

class TestEffectiveCapacity:
    @pytest.mark.anyio
    async def test_uses_agent_effective_available_not_total_minus_used(self):
        # physical-big annonce seulement 10 GB effectifs (110 GB réservés).
        # Le calcul historique total-used l'aurait choisi à tort.
        physical_big = FakeNodeBackend(
            "physical-big", total_vram=120.0, reserved_vram=110.0
        )
        effective_fit = FakeNodeBackend(
            "effective-fit", total_vram=48.0, reserved_vram=18.0
        )
        mgr = make_manager([physical_big, effective_fit])
        await mgr.start_health_monitor()
        try:
            handle = await mgr.ensure_model_loaded("m1")
            assert handle._info.node_id == "effective-fit"
            assert not physical_big.load_calls
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_offline_nodes_do_not_contribute_status_capacity_or_readiness(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend], failures_threshold=1)
        await mgr.start_health_monitor()
        await mgr.ensure_model_loaded("m1")
        backend.fail_health = True
        await mgr._check_node(mgr._nodes["a"])

        status = mgr.status()
        assert status["vram_budget"]["total_gb"] == 0.0
        assert status["vram_budget"]["used_gb"] == 0.0
        assert status["vram_budget"]["available_gb"] == 0.0
        assert status["models"][0]["state"] == "unloaded"
        await mgr.shutdown()


class TestLoadFailover:
    @pytest.mark.anyio
    async def test_load_failure_retries_another_node(self):
        first = FakeNodeBackend(
            "a", total_vram=30.0, load_exc=NodeProtocolError("load refusé")
        )
        fallback = FakeNodeBackend("b", total_vram=80.0)
        mgr = make_manager([first, fallback])
        await mgr.start_health_monitor()
        try:
            handle = await mgr.ensure_model_loaded("m1")
            assert handle._info.node_id == "b"
            assert len(first.load_calls) == 1
            assert len(fallback.load_calls) == 1
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_concurrent_same_model_load_is_deduplicated(self):
        backend = FakeNodeBackend("a")
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        try:
            first, second = await asyncio.gather(
                mgr.ensure_model_loaded("m1"),
                mgr.ensure_model_loaded("m1"),
            )
            assert first._info is second._info
            assert len(backend.load_calls) == 1
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_slow_load_does_not_hold_global_lock_or_block_heartbeat(self):
        backend = FakeNodeBackend("a")
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        backend.load_started = asyncio.Event()
        backend.release_load = asyncio.Event()
        load_task = asyncio.create_task(mgr.ensure_model_loaded("m1"))
        await asyncio.wait_for(backend.load_started.wait(), timeout=0.5)
        try:
            # Le heartbeat doit pouvoir acquérir _lock pendant le load réseau.
            await asyncio.wait_for(
                mgr._check_node(mgr._nodes["a"]), timeout=0.1
            )
        finally:
            backend.release_load.set()
        await load_task
        await mgr.shutdown()


class TestContinuousReconciliation:
    @pytest.mark.anyio
    async def test_heartbeat_removes_model_that_crashed_on_node(self):
        backend = FakeNodeBackend("a")
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        await mgr.ensure_model_loaded("m1")

        backend._loaded.pop("m1")  # llama-server crash entre deux heartbeats
        await mgr._check_node(mgr._nodes["a"])

        assert "m1" not in mgr._placement
        assert "m1" not in mgr._nodes["a"].loaded
        await mgr.shutdown()

    @pytest.mark.anyio
    async def test_returned_duplicate_is_accounted_evictable_and_not_primary(self):
        a = FakeNodeBackend("a", total_vram=48.0)
        b = FakeNodeBackend("b", total_vram=48.0)
        models = [FakeModelDef("m1", 20.0), FakeModelDef("m2", 40.0)]
        mgr = make_manager([a, b], models=models, failures_threshold=1)
        await mgr.start_health_monitor()
        try:
            await mgr.ensure_model_loaded("m1")
            assert mgr._placement["m1"] == "a"

            a.fail_health = True
            await mgr._check_node(mgr._nodes["a"])
            await mgr.ensure_model_loaded("m1")
            assert mgr._placement["m1"] == "b"

            # a revient avec son ancienne copie : elle reste dans l'inventaire
            # (VRAM non fantôme), mais ne vole pas le placement sain sur b.
            a.fail_health = False
            await mgr._check_node(mgr._nodes["a"])
            assert "m1" in mgr._nodes["a"].loaded
            assert mgr._placement["m1"] == "b"

            # m2 requiert l'éviction de la copie sur a. Cette éviction d'un
            # doublon ne doit surtout pas supprimer le placement primaire b.
            await mgr.ensure_model_loaded("m2")
            assert "m1" in a.unload_calls
            assert mgr._placement["m1"] == "b"
            assert "m1" in mgr._nodes["b"].loaded
        finally:
            await mgr.shutdown()


class TestHeartbeatProtocolFailures:
    @pytest.mark.anyio
    async def test_protocol_errors_apply_offline_threshold(self):
        backend = FakeNodeBackend("a")
        mgr = make_manager([backend], failures_threshold=2)
        await mgr.start_health_monitor()
        backend.health_exc = NodeProtocolError("schema invalide")

        await mgr._check_node(mgr._nodes["a"])
        assert mgr._nodes["a"].online is True
        await mgr._check_node(mgr._nodes["a"])
        assert mgr._nodes["a"].online is False
        await mgr.shutdown()


class TestActiveRequestSafety:
    @pytest.mark.anyio
    async def test_cluster_never_evicts_pinned_model(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        models = [FakeModelDef("old", 40.0), FakeModelDef("new", 40.0)]
        mgr = make_manager([backend], models=models)
        await mgr.start_health_monitor()
        handle = await mgr.ensure_model_loaded("old")
        handle.pin()
        try:
            with pytest.raises(RuntimeError, match="pinnés"):
                await mgr.ensure_model_loaded("new")
            assert "old" not in backend.unload_calls
        finally:
            handle.unpin()
            await mgr.shutdown()


class TestDataPlaneFailureFeedback:
    @pytest.mark.anyio
    async def test_failure_invalidates_current_handle_and_stale_report_is_safe(self):
        a = FakeNodeBackend("a", total_vram=30.0)
        b = FakeNodeBackend("b", total_vram=80.0)
        mgr = make_manager([a, b])
        await mgr.start_health_monitor()
        try:
            old = await mgr.ensure_model_loaded("m1")
            assert old._info.node_id == "a"
            await old.report_backend_failure()
            # Un heartbeat/status control-plane READY ne prouve pas que le port
            # data-plane est joignable et ne doit pas republier a.
            await mgr._check_node(mgr._nodes["a"])
            assert "m1" not in mgr._placement
            assert "m1" in mgr._nodes["a"].suspect_models
            replacement = await mgr.ensure_model_loaded("m1")
            assert replacement._info.node_id == "b"

            # Un signal tardif de l'ancien handle ne touche pas le nouveau.
            await old.report_backend_failure()
            assert mgr._placement["m1"] == "b"
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_suspect_is_retried_when_healthy_alternative_has_no_capacity(self):
        primary = FakeNodeBackend("a", total_vram=30.0)
        too_small = FakeNodeBackend("b", total_vram=10.0)
        mgr = make_manager([primary, too_small])
        await mgr.start_health_monitor()
        try:
            handle = await mgr.ensure_model_loaded("m1")
            await handle.report_backend_failure()
            recovered = await mgr.ensure_model_loaded("m1")

            assert recovered._info.node_id == "a"
            assert len(primary.load_calls) == 2
            assert not too_small.load_calls
            assert "m1" not in mgr._nodes["a"].suspect_models
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_single_node_recovers_and_idempotent_reload_keeps_accounting(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        try:
            handle = await mgr.ensure_model_loaded("m1")
            before = mgr._nodes["a"].last_health
            await handle.report_backend_failure()
            recovered = await mgr.ensure_model_loaded("m1")
            after = mgr._nodes["a"].last_health

            assert recovered._info.node_id == "a"
            assert len(backend.load_calls) == 2
            assert after.used_vram_gb == before.used_vram_gb
            assert after.available_vram_gb == before.available_vram_gb
        finally:
            await mgr.shutdown()


class TestGracefulShutdown:
    @pytest.mark.anyio
    async def test_runtime_unload_all_keeps_manager_reusable(self):
        backend = FakeNodeBackend("a")
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        await mgr.ensure_model_loaded("m1")

        await mgr.unload_all_models()
        assert "m1" not in mgr._placement
        assert mgr._monitor_task is not None and not mgr._monitor_task.done()

        recovered = await mgr.ensure_model_loaded("m1")
        assert recovered._info.node_id == "a"
        assert len(backend.load_calls) == 2
        await mgr.shutdown()

    @pytest.mark.anyio
    async def test_runtime_unload_all_reports_partial_failure_and_keeps_failed_state(self):
        a = FakeNodeBackend("a", total_vram=48.0)
        b = FakeNodeBackend("b", total_vram=48.0)
        models = [FakeModelDef("m1", 40.0), FakeModelDef("m2", 40.0)]
        mgr = make_manager([a, b], models=models)
        await mgr.start_health_monitor()
        await mgr.ensure_model_loaded("m1")
        await mgr.ensure_model_loaded("m2")
        b.fail_unload_all = True

        with pytest.raises(RuntimeError, match="Unload-all incomplet"):
            await mgr.unload_all_models()

        assert "m1" not in mgr._nodes["a"].loaded
        assert mgr._placement.get("m2") == "b"
        assert "m2" in mgr._nodes["b"].loaded
        b.fail_unload_all = False
        await mgr.shutdown()

    @pytest.mark.anyio
    async def test_runtime_unload_all_refuses_active_request(self):
        backend = FakeNodeBackend("a")
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        handle = await mgr.ensure_model_loaded("m1")
        handle.pin()
        try:
            with pytest.raises(RuntimeError, match="requêtes actives"):
                await mgr.unload_all_models()
            assert backend.unload_all_called is False
            assert mgr._placement["m1"] == "a"
        finally:
            handle.unpin()
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_default_shutdown_preserves_hot_remote_models(self):
        backend = FakeNodeBackend("a")
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        await mgr.ensure_model_loaded("m1")
        await mgr.shutdown()

        assert backend.unload_all_called is False
        assert "m1" in backend._loaded

    @pytest.mark.anyio
    async def test_explicit_destructive_shutdown_unloads_nodes(self):
        backend = FakeNodeBackend("a")
        mgr = make_manager([backend])
        await mgr.start_health_monitor()
        await mgr.ensure_model_loaded("m1")
        await mgr.shutdown(unload_nodes=True)

        assert backend.unload_all_called is True
        assert backend._loaded == {}
