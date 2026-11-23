"""
Régressions du cycle de vie des opérations de l'agent (audit cluster 2026-11).

- Une opération READY n'est valable que tant que SON manager reste le manager
  READY du modèle : après un unload, son port retourne au pool et sert le
  modèle suivant.
- Un `failed` porte un statut (`error_status`) qui distingue un échec du
  modèle d'une capacité transitoire.
- unload-all décharge les modèles en parallèle.
- La collecte des métriques réutilise un client partagé, fermé à l'arrêt, et
  interroge les llama-server en parallèle.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import main
from server_manager import ModelState
from test_main import FakeServerManager, load_and_wait, make_model_dict


@pytest.fixture
def state(monkeypatch) -> "main._AgentState":
    monkeypatch.setattr(main, "ServerManager", FakeServerManager)
    monkeypatch.setattr(main, "_validate_model_files", lambda model: None)
    return main._AgentState()


class TestStaleReadyOperation:
    def test_operation_status_after_unload_is_not_ready_on_reused_port(self, state):
        async def scenario():
            first = await load_and_wait(
                state, make_model_dict("model-a"), operation_id="op-a"
            )
            # Contrôle positif : tant que le manager sert, l'opération est READY.
            still = await state.operation_status("op-a")
            await state.unload("model-a")
            second = await load_and_wait(
                state, make_model_dict("model-b"), operation_id="op-b"
            )
            late = await state.operation_status("op-a")
            return first, still, second, late

        first, still, second, late = asyncio.run(scenario())
        assert still.state == "ready"
        assert second.port == first.port  # port réutilisé par un autre modèle
        assert late.state == "failed"
        assert late.port is None and late.llama_url is None
        assert late.error_code == "unloaded"

    def test_replayed_post_after_unload_does_not_answer_ready(self, state):
        model = make_model_dict("model-a")

        async def scenario():
            await load_and_wait(state, model, operation_id="op-a")
            await state.unload("model-a")
            return await state.start_load(model, operation_id="op-a")

        replay = asyncio.run(scenario())
        assert replay.state == "failed"
        assert replay.llama_url is None

    def test_already_loaded_operation_tracks_the_serving_manager(self, state):
        model = make_model_dict("model-a")

        async def scenario():
            await load_and_wait(state, model, operation_id="op-1")
            again = await state.start_load(model, operation_id="op-2")
            ready = await state.operation_status("op-2")
            await state.unload("model-a")
            gone = await state.operation_status("op-2")
            return again, ready, gone

        again, ready, gone = asyncio.run(scenario())
        assert again.already_loaded is True
        assert ready.state == "ready"
        assert gone.state == "failed"


class TestFailedOperationStatus:
    def test_model_specific_failure_carries_error_status(self, state, monkeypatch):
        def missing(_model):
            raise HTTPException(status_code=422, detail="Fichier GGUF introuvable sur le nœud.")

        monkeypatch.setattr(main, "_validate_model_files", missing)

        async def scenario():
            accepted = await state.start_load(make_model_dict("broken"), operation_id="op")
            await state._operations["op"].task
            return accepted, await state.operation_status("op")

        accepted, failed = asyncio.run(scenario())
        assert accepted.state == "accepted"  # le POST est bien accepté (202)
        assert failed.state == "failed"
        assert failed.error_status == 422

    def test_llama_server_failure_is_500_and_capacity_is_503(self, state, monkeypatch):
        FakeServerManager.FAIL_LOAD.add("crashing")
        huge = make_model_dict("huge", vram_gb=main.settings.effective_vram_budget_gb() + 1)

        async def scenario():
            await state.start_load(make_model_dict("crashing"), operation_id="op-crash")
            await state._operations["op-crash"].task
            await state.start_load(huge, operation_id="op-huge")
            await state._operations["op-huge"].task
            return (
                await state.operation_status("op-crash"),
                await state.operation_status("op-huge"),
            )

        crashed, capacity = asyncio.run(scenario())
        assert (crashed.state, crashed.error_status) == ("failed", 500)
        assert (capacity.state, capacity.error_status) == ("failed", 503)


class TestUnloadAllConcurrent:
    def test_unload_all_stops_models_in_parallel(self, state):
        async def scenario():
            for model_id in ("m-a", "m-b"):
                await load_and_wait(state, make_model_dict(model_id))
                FakeServerManager.UNLOAD_GATES[model_id] = asyncio.Event()
            managers = [state._managers["m-a"], state._managers["m-b"]]
            task = asyncio.create_task(state.unload_all())
            for _ in range(100):
                if all(m.state == ModelState.UNLOADING for m in managers):
                    break
                await asyncio.sleep(0)
            # En série, le second resterait READY tant que le premier attend.
            both_stopping = all(m.state == ModelState.UNLOADING for m in managers)
            for gate in FakeServerManager.UNLOAD_GATES.values():
                gate.set()
            await task
            return both_stopping

        assert asyncio.run(scenario()) is True
        assert state._managers == {}
        assert len(state._port_pool) == main.settings.max_loaded_models


class _GatedMetricsClient:
    """Client httpx factice : chaque GET attend que tous soient en vol."""

    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.in_flight = 0
        self.all_in_flight = asyncio.Event()
        self.closed = False

    async def get(self, url, headers=None):
        self.in_flight += 1
        if self.in_flight == self.expected:
            self.all_in_flight.set()
        await asyncio.wait_for(self.all_in_flight.wait(), 1.0)

        class _Response:
            status_code = 200
            text = "llamacpp:requests_processing 2\n"

        return _Response()

    async def aclose(self) -> None:
        self.closed = True


class TestMetricsClient:
    def test_metrics_reuse_shared_client_and_scrape_concurrently(self, state):
        async def scenario():
            for model_id in ("m-a", "m-b"):
                await load_and_wait(state, make_model_dict(model_id))
                manager = state._managers[model_id]
                manager.llama_url = lambda path, mid=model_id: f"http://{mid}{path}"
                manager.auth_headers = lambda: {}
            client = _GatedMetricsClient(expected=2)
            state._metrics_client = client
            first = await state.agent_metrics()
            client.in_flight = 0
            client.all_in_flight = asyncio.Event()
            second = await state.agent_metrics()
            reused = state._metrics_client is client
            await state.shutdown()
            return first, second, reused, client.closed, state._metrics_client

        first, second, reused, closed, after = asyncio.run(scenario())
        assert first["m-a"]["requests_processing"] == 2.0
        assert set(second) == {"m-a", "m-b"}
        assert reused is True
        assert closed is True
        assert after is None

    def test_metrics_after_shutdown_do_not_recreate_client(self, state):
        async def scenario():
            await load_and_wait(state, make_model_dict("m-a"))
            manager = state._managers["m-a"]
            await state.shutdown()
            # Un appel tardif (requête arrivée pendant l'arrêt) sur un manager
            # encore référencé : aucun client ne doit être recréé.
            state._managers["m-a"] = manager
            manager.state = ModelState.READY
            manager.llama_url = lambda path: f"http://m-a{path}"
            manager.auth_headers = lambda: {}
            result = await state.agent_metrics()
            return result, state._metrics_client

        result, client = asyncio.run(scenario())
        assert result == {}
        assert client is None
