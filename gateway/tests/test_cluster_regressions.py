"""
Régressions du mode cluster (audit 2026-11) — chacune reproduit un défaut
vérifié du ClusterManager sur le chemin de production (contrat d'opérations
`start_load_model` / `get_operation_status`, cf. FakeNodeBackend).

Invariant central : un modèle qui traite une requête active n'est jamais
évincé, même après replacement, heartbeat périmé ou réadoption.
"""
from __future__ import annotations

import asyncio
import signal
import time

import pytest

from cluster.cluster_manager import (
    ClusterLoadConflictError,
    ClusterUnloadUncertainError,
    _LoadedInfo,
    _PendingLoad,
)
from cluster.node_client import LocalNodeAdapter
from cluster.node_protocol import LoadResponse, NodeHealth, deployment_digest
from tests.test_cluster_manager import (
    FakeModelDef,
    FakeNodeBackend,
    OperationNodeBackend,
    make_manager,
)


class SlowHealthBackend(FakeNodeBackend):
    """health() photographié tout de suite, réponse retenue sur le fil."""

    gate: asyncio.Event | None = None

    async def health(self) -> NodeHealth:
        snapshot = await super().health()
        if self.gate is not None:
            gate, self.gate = self.gate, None
            await gate.wait()
        return snapshot


class BlockingUnloadBackend(FakeNodeBackend):
    block: asyncio.Event | None = None

    async def unload_model(self, model_id: str):
        if self.block is not None:
            await self.block.wait()
        return await super().unload_model(model_id)


class DigestModelDef(FakeModelDef):
    """Définition dont un champ éditable (description) entre dans le digest."""

    def to_dict(self) -> dict:
        return {**super().to_dict(), "description": self.description}


# ── 1. Replacement sur le même nœud : les pins survivent ─────────────────────

class TestReplacementKeepsPins:
    @pytest.mark.anyio
    async def test_data_plane_failure_then_replacement_never_evicts_pinned_stream(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager(
            [backend], models=[FakeModelDef("m1", 20.0), FakeModelDef("big", 40.0)]
        )
        await mgr.start_health_monitor()
        try:
            stream = await mgr.ensure_model_loaded("m1")
            stream.pin()
            other = await mgr.ensure_model_loaded("m1")
            await other.report_backend_failure()

            await mgr.ensure_model_loaded("m1")
            info = mgr._nodes["a"].loaded["m1"]
            assert info is stream._info
            assert info.active_requests == 1

            with pytest.raises(RuntimeError, match="pinnés"):
                await mgr.ensure_model_loaded("big")
            assert "m1" not in backend.unload_calls
        finally:
            stream.unpin()
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_finish_pending_load_updates_existing_entry_in_place(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        model = FakeModelDef("m1", 20.0)
        mgr = make_manager([backend], models=[model])
        await mgr.start_health_monitor()
        try:
            handle = await mgr.ensure_model_loaded("m1")
            handle.pin()
            state = mgr._nodes["a"]
            pending = _PendingLoad(
                model_id="m1",
                deployment_digest=deployment_digest(model.to_dict()),
                generation_id="generation-2",
                operation_id="operation-2",
                vram_gb=20.0,
            )
            state.pending_loads["m1"] = pending
            response = LoadResponse(
                model_id="m1",
                deployment_digest=pending.deployment_digest,
                generation_id="generation-2",
                operation_id="operation-2",
                state="ready",
                llama_url="http://a:8082",
                internal_api_key="new-key",
                port=8082,
                already_loaded=True,
            )
            refreshed = await mgr._finish_pending_load(state, model, pending, response)

            assert refreshed._info is handle._info
            assert state.loaded["m1"] is handle._info
            assert handle._info.active_requests == 1
            assert handle._info.llama_url == "http://a:8082"
            assert "m1" not in state.pending_loads
        finally:
            handle.unpin()
            await mgr.shutdown()


# ── 2. Heartbeat périmé ───────────────────────────────────────────────────────

class TestStaleHeartbeat:
    @pytest.mark.anyio
    async def test_stale_health_does_not_drop_load_finished_during_round_trip(self):
        backend = SlowHealthBackend("a", total_vram=48.0)
        mgr = make_manager(
            [backend], models=[FakeModelDef("m1", 20.0), FakeModelDef("big", 40.0)]
        )
        await mgr.start_health_monitor()
        try:
            gate = asyncio.Event()
            backend.gate = gate
            heartbeat = asyncio.create_task(mgr._check_node(mgr._nodes["a"]))
            await asyncio.sleep(0.01)  # health() photographié : rien de chargé
            handle = await mgr.ensure_model_loaded("m1")
            handle.pin()
            gate.set()
            await heartbeat

            assert mgr._nodes["a"].loaded.get("m1") is handle._info
            assert mgr._placement.get("m1") == "a"

            await mgr._check_node(mgr._nodes["a"])
            assert mgr._nodes["a"].loaded["m1"] is handle._info
            with pytest.raises(RuntimeError, match="pinnés"):
                await mgr.ensure_model_loaded("big")
            assert "m1" not in backend.unload_calls
        finally:
            handle.unpin()
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_vanished_pinned_entry_is_kept_suspect_and_readopted_in_place(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend], models=[FakeModelDef("m1", 20.0)])
        await mgr.start_health_monitor()
        try:
            handle = await mgr.ensure_model_loaded("m1")
            handle.pin()
            saved = backend._loaded.pop("m1")  # absent d'un health
            await mgr._check_node(mgr._nodes["a"])

            state = mgr._nodes["a"]
            assert state.loaded.get("m1") is handle._info
            assert "m1" in state.suspect_models
            assert "m1" not in mgr._placement

            backend._loaded["m1"] = saved  # de retour, même génération
            await mgr._check_node(mgr._nodes["a"])
            assert state.loaded["m1"] is handle._info
            assert handle._info.active_requests == 1

            # Une fois la requête terminée, une vraie disparition est purgée.
            handle.unpin()
            backend._loaded.pop("m1")
            await mgr._check_node(mgr._nodes["a"])
            assert "m1" not in state.loaded
        finally:
            await mgr.shutdown()


# ── 3. Replacement : réutiliser la copie existante ───────────────────────────

class TestReuseExistingCopy:
    @pytest.mark.anyio
    async def test_single_node_pinned_suspect_is_refreshed_not_rejected(self):
        backend = FakeNodeBackend("a", total_vram=24.0)
        mgr = make_manager(
            [backend], models=[FakeModelDef("x", 16.0), FakeModelDef("y", 6.0)]
        )
        await mgr.start_health_monitor()
        try:
            stream = await mgr.ensure_model_loaded("x")
            stream.pin()
            failing = await mgr.ensure_model_loaded("x")
            await failing.report_backend_failure()

            recovered = await mgr.ensure_model_loaded("x")
            assert recovered._info is stream._info
            assert backend.unload_calls == []
        finally:
            stream.unpin()
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_single_node_suspect_is_not_evicted_and_cold_reloaded(self):
        backend = FakeNodeBackend("a", total_vram=24.0)
        mgr = make_manager(
            [backend], models=[FakeModelDef("x", 16.0), FakeModelDef("y", 6.0)]
        )
        await mgr.start_health_monitor()
        try:
            await mgr.ensure_model_loaded("y")
            handle = await mgr.ensure_model_loaded("x")
            await handle.report_backend_failure()
            await mgr.ensure_model_loaded("x")

            # Une seule erreur data-plane : aucune éviction, aucun rechargement
            # à froid — un load idempotent (already_loaded) sur la copie.
            assert backend.unload_calls == []
            assert [call["id"] for call in backend.load_calls] == ["y", "x", "x"]
            assert mgr._placement["x"] == "a"
            assert mgr._placement["y"] == "a"
        finally:
            await mgr.shutdown()


# ── 4. Double comptage VRAM / ports ──────────────────────────────────────────

class TestNoDoubleCounting:
    @pytest.mark.anyio
    async def test_loading_model_counted_once_during_and_after_load(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        backend.release_load = asyncio.Event()
        backend.load_started = asyncio.Event()
        mgr = make_manager(
            [backend],
            models=[FakeModelDef("a20", 20.0), FakeModelDef("old", 20.0),
                    FakeModelDef("c8", 8.0)],
        )
        await mgr.start_health_monitor()
        try:
            backend.release_load.set()
            await mgr.ensure_model_loaded("old")
            backend.release_load = asyncio.Event()
            backend.load_started = asyncio.Event()

            task = asyncio.create_task(mgr.ensure_model_loaded("a20"))
            await asyncio.wait_for(backend.load_started.wait(), 1.0)
            await mgr._check_node(mgr._nodes["a"])  # heartbeat pendant le load
            agent = await backend.health()
            snapshot = mgr._nodes["a"].snapshot()
            assert snapshot.available_vram_gb == agent.available_vram_gb == 8.0
            assert snapshot.free_ports == agent.free_ports

            backend.release_load.set()
            await task
            agent = await backend.health()
            assert mgr._nodes["a"].snapshot().available_vram_gb == agent.available_vram_gb

            # 8 GB réellement libres : c8 se place sans évincer personne.
            await mgr.ensure_model_loaded("c8")
            assert backend.unload_calls == []
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_unload_delta_not_applied_when_health_already_excludes_model(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend], models=[FakeModelDef("m1", 20.0)])
        await mgr.start_health_monitor()
        try:
            await mgr.ensure_model_loaded("m1")
            state = mgr._nodes["a"]
            # Health plus récent que l'entrée : il ne compte déjà plus m1.
            state.last_health = NodeHealth(
                total_vram_gb=48.0, used_vram_gb=0.0, available_vram_gb=48.0,
                loaded_model_ids=[], free_ports=5,
            )
            await mgr.unload_model("m1")
            assert state.last_health.available_vram_gb == 48.0
            assert state.last_health.free_ports == 5
        finally:
            await mgr.shutdown()


# ── 5. Échec propre au modèle ≠ panne de nœud ────────────────────────────────

class TestModelFailureIsNotNodeFailure:
    @pytest.mark.anyio
    async def test_broken_model_never_takes_nodes_offline_and_waiters_share_failure(self):
        a = FakeNodeBackend("a", total_vram=48.0)
        b = FakeNodeBackend("b", total_vram=48.0)
        mgr = make_manager(
            [a, b],
            models=[FakeModelDef("good", 10.0), FakeModelDef("broken", 10.0)],
            failures_threshold=3,
        )
        await mgr.start_health_monitor()
        try:
            await mgr.ensure_model_loaded("good")
            a.fail_load = b.fail_load = "Fichier GGUF introuvable sur le nœud."
            a.fail_load_status = b.fail_load_status = 422

            results = await asyncio.gather(
                *(mgr.ensure_model_loaded("broken") for _ in range(3)),
                return_exceptions=True,
            )
            assert all(isinstance(result, RuntimeError) for result in results)
            assert all("GGUF" not in str(result) for result in results)
            broken_calls = [
                call for backend in (a, b) for call in backend.load_calls
                if call["id"] == "broken"
            ]
            # Un seul passage sur chaque nœud ; les requêtes en file partagent
            # l'échec au lieu de rejouer le load sur tout le cluster.
            assert len(broken_calls) == 2
            assert {s.online for s in mgr._nodes.values()} == {True}
            assert {s.consecutive_failures for s in mgr._nodes.values()} == {0}

            a.fail_load = b.fail_load = None
            good = await mgr.ensure_model_loaded("good")
            assert good.model.id == "good"
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_negative_cache_expires_and_is_keyed_by_digest(self):
        backend = FakeNodeBackend("a", total_vram=48.0, fail_load="échec")
        model = DigestModelDef("m1", 10.0)
        mgr = make_manager([backend], models=[model])
        mgr._load_failure_ttl_seconds = 0.2
        await mgr.start_health_monitor()
        try:
            with pytest.raises(RuntimeError):
                await mgr.ensure_model_loaded("m1")
            with pytest.raises(RuntimeError, match="échec récent"):
                await mgr.ensure_model_loaded("m1")
            assert len(backend.load_calls) == 1

            # Nouvelle définition : pas de cache, nouvelle tentative réelle.
            backend.fail_load = None
            model.description = "corrigé"
            await mgr.ensure_model_loaded("m1")
            assert len(backend.load_calls) == 2
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_capacity_failure_is_not_cached_as_model_failure(self):
        backend = FakeNodeBackend(
            "a", total_vram=48.0, fail_load="VRAM insuffisante", fail_load_status=503
        )
        mgr = make_manager([backend], models=[FakeModelDef("m1", 10.0)])
        await mgr.start_health_monitor()
        try:
            with pytest.raises(RuntimeError):
                await mgr.ensure_model_loaded("m1")
            backend.fail_load = None
            handle = await mgr.ensure_model_loaded("m1")
            assert handle._info.node_id == "a"
            assert mgr._nodes["a"].consecutive_failures == 0
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_in_process_backend_exception_is_a_terminal_failure(self):
        class LegacyBackend:
            async def load_model(self, model_dict):
                raise RuntimeError("llama-server a quitté")

        adapter = LocalNodeAdapter("legacy", LegacyBackend())
        response = await adapter.start_load_model(
            {"id": "m1"},
            deployment_digest="d" * 64,
            generation_id="g",
            operation_id="o",
            deadline_seconds=5.0,
        )
        assert response.state == "failed"
        assert response.operation_id == "o"


# ── 6. Changement de définition et unload admin ──────────────────────────────

class TestDefinitionChange:
    @pytest.mark.anyio
    async def test_new_definition_evicts_old_generation_on_same_node(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        model = DigestModelDef("m1", 20.0)
        mgr = make_manager([backend], models=[model])
        await mgr.start_health_monitor()
        try:
            await mgr.ensure_model_loaded("m1")
            model.description = "nouveau texte"  # PATCH admin, sans unload
            handle = await mgr.ensure_model_loaded("m1")

            assert backend.unload_calls == ["m1"]
            assert handle._info.deployment_digest == deployment_digest(model.to_dict())
            assert backend._identity["m1"][0] == deployment_digest(model.to_dict())
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_pinned_old_generation_refuses_cleanly(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        model = DigestModelDef("m1", 20.0)
        mgr = make_manager([backend], models=[model])
        await mgr.start_health_monitor()
        try:
            old = await mgr.ensure_model_loaded("m1")
            old.pin()
            model.description = "nouveau texte"
            with pytest.raises(ClusterLoadConflictError, match="génération précédente"):
                await mgr.ensure_model_loaded("m1")
            assert backend.unload_calls == []
            old.unpin()
            handle = await mgr.ensure_model_loaded("m1")
            assert handle._info.deployment_digest == deployment_digest(model.to_dict())
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_admin_unload_reaches_every_tracked_copy(self):
        a = FakeNodeBackend("a", total_vram=48.0)
        b = FakeNodeBackend("b", total_vram=48.0)
        model = DigestModelDef("m1", 20.0)
        mgr = make_manager([a, b], models=[model])
        await mgr.start_health_monitor()
        try:
            await mgr.ensure_model_loaded("m1")
            first = mgr._placement["m1"]
            mgr._nodes[first].online = False
            await mgr.ensure_model_loaded("m1")  # doublon sur l'autre nœud
            mgr._nodes[first].online = True

            await mgr.unload_model("m1")
            assert a.unload_calls == ["m1"] and b.unload_calls == ["m1"]
            assert all("m1" not in s.loaded for s in mgr._nodes.values())
            assert "m1" not in a._loaded and "m1" not in b._loaded
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_admin_unload_of_suspect_copy_without_placement_is_not_a_noop(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend], models=[FakeModelDef("m1", 20.0)])
        await mgr.start_health_monitor()
        try:
            handle = await mgr.ensure_model_loaded("m1")
            await handle.report_backend_failure()
            assert "m1" not in mgr._placement

            await mgr.unload_model("m1")
            assert backend.unload_calls == ["m1"]
            assert "m1" not in mgr._nodes["a"].loaded
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_admin_unload_refuses_when_any_copy_is_busy(self):
        a = FakeNodeBackend("a", total_vram=48.0)
        b = FakeNodeBackend("b", total_vram=48.0)
        mgr = make_manager([a, b], models=[FakeModelDef("m1", 20.0)])
        await mgr.start_health_monitor()
        try:
            stream = await mgr.ensure_model_loaded("m1")
            stream.pin()
            first = mgr._placement["m1"]
            mgr._nodes[first].online = False
            await mgr.ensure_model_loaded("m1")
            mgr._nodes[first].online = True

            with pytest.raises(RuntimeError, match="ne peut pas être déchargé"):
                await mgr.unload_model("m1")
            assert a.unload_calls == [] and b.unload_calls == []
        finally:
            stream.unpin()
            await mgr.shutdown()


# ── 7. unload_uncertain levé quand le nœud sert toujours la génération ──────

class TestUnloadUncertainHeals:
    @pytest.mark.anyio
    async def test_heartbeat_clears_flag_when_same_generation_still_ready(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend], models=[FakeModelDef("m1", 20.0)])
        await mgr.start_health_monitor()
        try:
            await mgr.ensure_model_loaded("m1")
            backend.fail_unload = True
            with pytest.raises(ClusterUnloadUncertainError):
                await mgr.unload_model("m1")
            backend.fail_unload = False

            await mgr._check_node(mgr._nodes["a"])
            assert mgr._nodes["a"].loaded["m1"].unload_uncertain is False
            handle = await mgr.ensure_model_loaded("m1")
            assert handle._info.node_id == "a"
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_flag_kept_when_node_reports_another_generation(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend], models=[FakeModelDef("m1", 20.0)])
        await mgr.start_health_monitor()
        try:
            await mgr.ensure_model_loaded("m1")
            backend.fail_unload = True
            with pytest.raises(ClusterUnloadUncertainError):
                await mgr.unload_model("m1")
            digest, _, operation = backend._identity["m1"]
            backend._identity["m1"] = (digest, "other-generation", operation)

            await mgr._reconcile_node_status(mgr._nodes["a"])
            # Contrôle : le flag n'est levé que sur preuve de même génération.
            assert mgr._nodes["a"].loaded["m1"].generation_id == "other-generation"
            assert mgr._nodes["a"].loaded["m1"].unload_uncertain is True
        finally:
            backend.fail_unload = False
            await mgr.shutdown()


# ── 8. Réservations orphelines ───────────────────────────────────────────────

class TestPendingPurge:
    @pytest.mark.anyio
    async def test_cancelled_load_reservation_purged_and_stale_operation_not_reused(self):
        backend = OperationNodeBackend("a", total_vram=48.0, ready_after=None)
        mgr = make_manager(
            [backend], models=[FakeModelDef("m1", 20.0), FakeModelDef("m2", 20.0)]
        )
        await mgr.start_health_monitor()
        try:
            task = asyncio.create_task(mgr.ensure_model_loaded("m1"))
            await asyncio.sleep(0.3)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            state = mgr._nodes["a"]
            assert "m1" in state.pending_loads

            backend._loaded["m1"] = 20.0  # l'agent a terminé seul
            await mgr._check_node(state)
            assert "m1" in state.loaded
            assert "m1" not in state.pending_loads
            agent = await backend.health()
            assert state.snapshot().available_vram_gb == agent.available_vram_gb

            await mgr.unload_model("m1")
            assert "m1" not in state.pending_loads
            calls = len(backend.start_requests)
            backend.ready_after = 0
            await mgr.ensure_model_loaded("m1")
            assert len(backend.start_requests) == calls + 1
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_finish_failure_purges_reservation(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager([backend], models=[FakeModelDef("m1", 20.0)])
        await mgr.start_health_monitor()
        original = mgr._finish_pending_load

        async def unload_all_won_the_race(*args, **kwargs):
            mgr._unloading_all = True
            try:
                return await original(*args, **kwargs)
            finally:
                mgr._unloading_all = False

        mgr._finish_pending_load = unload_all_won_the_race
        try:
            with pytest.raises(RuntimeError, match="Déchargement global"):
                await mgr.ensure_model_loaded("m1")
            assert mgr._nodes["a"].pending_loads == {}
        finally:
            await mgr.shutdown()


# ── 9. `evicting` bloqué après annulation, boucle sans point de suspension ──

class TestEvictionCancellation:
    @pytest.mark.anyio
    async def test_cancelled_multi_eviction_resets_flags_and_retry_converges(self):
        backend = BlockingUnloadBackend("a", total_vram=48.0)
        mgr = make_manager(
            [backend],
            models=[FakeModelDef("a", 20.0), FakeModelDef("b", 20.0),
                    FakeModelDef("c", 44.0)],
        )
        await mgr.start_health_monitor()

        def spun(*_args):
            raise TimeoutError("la boucle de placement n'a jamais cédé la main")

        previous = signal.signal(signal.SIGALRM, spun)
        try:
            await mgr.ensure_model_loaded("a")
            await asyncio.sleep(0.02)
            await mgr.ensure_model_loaded("b")
            backend.block = asyncio.Event()
            task = asyncio.create_task(mgr.ensure_model_loaded("c"))
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            backend.block = None

            flags = {mid: info.evicting for mid, info in mgr._nodes["a"].loaded.items()}
            assert not any(flags.values()), flags

            signal.setitimer(signal.ITIMER_REAL, 3.0)
            handle = await mgr.ensure_model_loaded("c")
            assert handle.model.id == "c"
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_unstable_plan_is_bounded_and_yields(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager(
            [backend], models=[FakeModelDef("a", 40.0), FakeModelDef("b", 40.0)]
        )
        mgr._max_placement_attempts = 5
        await mgr.start_health_monitor()

        def spun(*_args):
            raise TimeoutError("la boucle de placement n'a jamais cédé la main")

        previous = signal.signal(signal.SIGALRM, spun)
        try:
            await mgr.ensure_model_loaded("a")
            # Victime marquée `evicting` par une autre opération qui ne finit
            # jamais : chaque plan est obsolète.
            mgr._nodes["a"].loaded["a"].evicting = True
            started = time.monotonic()
            signal.setitimer(signal.ITIMER_REAL, 3.0)
            with pytest.raises(RuntimeError, match="n'a pas convergé"):
                await mgr.ensure_model_loaded("b")
            assert time.monotonic() - started < 1.0
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            mgr._nodes["a"].loaded["a"].evicting = False
            await mgr.shutdown()


# ── 12. Métriques : nœuds interrogés en parallèle ────────────────────────────

@pytest.mark.anyio
async def test_collect_llama_metrics_queries_nodes_concurrently():
    a = FakeNodeBackend("a")
    b = FakeNodeBackend("b")
    both_in_flight = asyncio.Event()
    in_flight = 0

    def metrics_for(node_id):
        async def metrics():
            nonlocal in_flight
            in_flight += 1
            if in_flight == 2:
                both_in_flight.set()
            # En série, le premier nœud attendrait indéfiniment le second.
            await asyncio.wait_for(both_in_flight.wait(), 1.0)
            return {f"m-{node_id}": {"requests_processing": 1.0}}
        return metrics

    a.metrics = metrics_for("a")
    b.metrics = metrics_for("b")
    mgr = make_manager([a, b])
    await mgr.start_health_monitor()
    try:
        result = await mgr.collect_llama_metrics()
    finally:
        await mgr.shutdown()
    assert result["m-a"]["node"] == "a"
    assert result["m-b"]["node"] == "b"


# ── 11. Le fake de test emprunte le chemin de production ─────────────────────

@pytest.mark.anyio
async def test_fake_backend_runs_on_operations_contract_with_loading_visible():
    backend = FakeNodeBackend("a", total_vram=48.0)
    backend.release_load = asyncio.Event()
    backend.load_started = asyncio.Event()
    mgr = make_manager([backend], models=[FakeModelDef("m1", 20.0)])
    await mgr.start_health_monitor()
    try:
        task = asyncio.create_task(mgr.ensure_model_loaded("m1"))
        await asyncio.wait_for(backend.load_started.wait(), 1.0)
        health = await backend.health()
        status = await backend.status()
        assert "m1" in health.loaded_model_ids
        assert [m.state for m in status.models] == ["loading"]
        backend.release_load.set()
        await task
        assert backend.operation_status_calls >= 1
        assert not hasattr(mgr._nodes["a"].client, "load_model")
    finally:
        await mgr.shutdown()


def test_loaded_info_default_has_no_pins():
    # Contrôle positif des tests de pins ci-dessus.
    info = _LoadedInfo(node_id="a", llama_url="http://a:1", internal_api_key="k", vram_gb=1.0)
    assert info.active_requests == 0


# ── 10. Budget d'unload dédié ────────────────────────────────────────────────

class TestUnloadTimeoutSetting:
    @staticmethod
    def _settings(**overrides):
        from config import Settings

        return Settings(_env_file=None, **overrides)

    def test_default_covers_agent_sigterm_and_reap(self):
        assert self._settings().cluster_unload_timeout >= 30.0

    @pytest.mark.parametrize("value", [0, -1.0, float("nan"), float("inf"), True])
    def test_rejects_invalid_values(self, value):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            self._settings(cluster_unload_timeout=value)

    def test_cluster_mode_requires_unload_budget_not_below_request_timeout(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="CLUSTER_UNLOAD_TIMEOUT"):
            self._settings(
                cluster_mode="cluster",
                cluster_request_timeout=20.0,
                cluster_unload_timeout=15.0,
            )
        configured = self._settings(cluster_mode="cluster", cluster_unload_timeout=45.0)
        assert configured.cluster_unload_timeout == 45.0


# ── Revue 2026-11 : correctifs complémentaires ───────────────────────────────

class TestReviewFollowUps:
    @pytest.mark.anyio
    async def test_cancelled_admin_unload_during_resync_resets_evicting(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        mgr = make_manager(
            [backend], models=[FakeModelDef("x", 20.0), FakeModelDef("y", 40.0)]
        )
        await mgr.start_health_monitor()
        try:
            await mgr.ensure_model_loaded("x")
            backend.fail_unload = True
            gate = asyncio.Event()
            real_health = backend.health

            async def slow_health():
                await gate.wait()
                return await real_health()

            backend.health = slow_health
            task = asyncio.create_task(mgr.unload_model("x"))
            await asyncio.sleep(0.05)  # bloqué dans la re-synchronisation health()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            backend.health = real_health
            backend.fail_unload = False

            assert mgr._nodes["a"].loaded["x"].evicting is False
            calls = len(backend.load_calls)
            await mgr.ensure_model_loaded("x")
            assert len(backend.load_calls) == calls  # fast-path, aucun nouveau load
            handle = await mgr.ensure_model_loaded("y")  # doit évincer x
            assert handle.model.id == "y"
            assert "x" in backend.unload_calls
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_orphan_reservation_of_cancelled_then_failed_load_is_purged(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        backend.release_load = asyncio.Event()
        backend.load_started = asyncio.Event()
        mgr = make_manager(
            [backend], models=[FakeModelDef("x", 20.0), FakeModelDef("y", 40.0)]
        )
        await mgr.start_health_monitor()
        try:
            task = asyncio.create_task(mgr.ensure_model_loaded("x"))
            await asyncio.wait_for(backend.load_started.wait(), 1.0)
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            state = mgr._nodes["a"]
            assert "x" in state.pending_loads  # réservation conservée (voulu)

            backend.fail_load, backend.fail_load_status = "GGUF introuvable", 422
            backend.release_load.set()
            await asyncio.sleep(0.05)
            await mgr._check_node(state)

            assert state.pending_loads == {}
            assert state.snapshot().available_vram_gb == 48.0
            backend.fail_load = None
            handle = await mgr.ensure_model_loaded("y")
            assert handle._info.node_id == "a"
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_one_failing_node_does_not_block_a_healthy_busy_node(self):
        a = FakeNodeBackend("a", total_vram=48.0)
        b = FakeNodeBackend("b", total_vram=24.0)
        mgr = make_manager(
            [a, b], models=[FakeModelDef("busy", 20.0), FakeModelDef("x", 20.0)]
        )
        await mgr.start_health_monitor()
        try:
            mgr._nodes["a"].online = False
            busy = await mgr.ensure_model_loaded("busy")
            mgr._nodes["a"].online = True
            assert busy._info.node_id == "b"
            busy.pin()
            a.fail_load, a.fail_load_status = "GGUF introuvable", 422

            with pytest.raises(RuntimeError):
                await mgr.ensure_model_loaded("x")  # a échoue, b plein (pinné)
            busy.unpin()

            handle = await mgr.ensure_model_loaded("x")
            assert handle._info.node_id == "b"
            # a reste écarté le temps du TTL : aucun nouvel essai sur a.
            assert [c["id"] for c in a.load_calls] == ["x"]
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_fail_fast_only_when_every_online_node_recently_failed(self):
        a = FakeNodeBackend("a", total_vram=48.0, fail_load="GGUF introuvable")
        b = FakeNodeBackend("b", total_vram=48.0, fail_load="GGUF introuvable")
        mgr = make_manager([a, b], models=[FakeModelDef("x", 20.0)])
        await mgr.start_health_monitor()
        try:
            with pytest.raises(RuntimeError):
                await mgr.ensure_model_loaded("x")
            with pytest.raises(RuntimeError, match="échec récent"):
                await mgr.ensure_model_loaded("x")
            assert len(a.load_calls) == len(b.load_calls) == 1
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_failure_without_error_status_is_not_cached(self):
        # Agent antérieur : `failed` sans `error_status` (capacité possible).
        backend = FakeNodeBackend(
            "a", total_vram=48.0, fail_load="VRAM insuffisante", fail_load_status=None
        )
        mgr = make_manager([backend], models=[FakeModelDef("x", 20.0)])
        await mgr.start_health_monitor()
        try:
            with pytest.raises(RuntimeError):
                await mgr.ensure_model_loaded("x")
            with pytest.raises(RuntimeError) as caught:
                await mgr.ensure_model_loaded("x")
            assert "échec récent" not in str(caught.value)
            assert len(backend.load_calls) == 2
            assert mgr._nodes["a"].consecutive_failures == 0
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_forget_load_failures_allows_immediate_retry(self):
        backend = FakeNodeBackend("a", total_vram=48.0, fail_load="GGUF introuvable")
        mgr = make_manager([backend], models=[FakeModelDef("x", 20.0)])
        await mgr.start_health_monitor()
        try:
            with pytest.raises(RuntimeError):
                await mgr.ensure_model_loaded("x")
            backend.fail_load = None  # l'opérateur a corrigé le GGUF
            mgr.forget_load_failures("x")
            handle = await mgr.ensure_model_loaded("x")
            assert handle._info.node_id == "a"
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_refresh_in_place_clears_unload_uncertain(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        model = FakeModelDef("x", 20.0)
        mgr = make_manager([backend], models=[model])
        await mgr.start_health_monitor()
        try:
            handle = await mgr.ensure_model_loaded("x")
            handle._info.unload_uncertain = True
            refreshed = await mgr._refresh_reconciled(mgr._nodes["a"], model)
            assert refreshed._info is handle._info
            assert handle._info.unload_uncertain is False
        finally:
            await mgr.shutdown()

    @pytest.mark.anyio
    async def test_unload_all_keeps_orphan_reservation_until_inventory_resolves(self):
        backend = FakeNodeBackend("a", total_vram=48.0)
        backend.release_load = asyncio.Event()
        backend.load_started = asyncio.Event()
        mgr = make_manager([backend], models=[FakeModelDef("x", 20.0)])
        await mgr.start_health_monitor()
        try:
            task = asyncio.create_task(mgr.ensure_model_loaded("x"))
            await asyncio.wait_for(backend.load_started.wait(), 1.0)
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            state = mgr._nodes["a"]

            await mgr.unload_all_models()
            # L'agent peut encore terminer une opération sans manager : la
            # réservation n'est pas jetée à l'aveugle…
            assert "x" in state.pending_loads
            # … mais l'inventaire la purge dès que l'agent ne la publie plus.
            await mgr._check_node(state)
            assert state.pending_loads == {}
        finally:
            backend.release_load.set()
            await mgr.shutdown()

    def test_unset_unload_timeout_is_derived_from_request_timeout(self):
        from config import Settings

        upgraded = Settings(
            _env_file=None, cluster_mode="cluster", cluster_request_timeout=40.0
        )
        assert upgraded.cluster_unload_timeout == 40.0
        assert Settings(_env_file=None).cluster_unload_timeout == 30.0
