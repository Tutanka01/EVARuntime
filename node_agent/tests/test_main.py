"""
Tests pour node_agent/main.py — auth, health, et cycle de vie (_AgentState).

Aucun vrai llama-server n'est lancé : ServerManager est entièrement remplacé
par un FakeServerManager (monkeypatch de `main.ServerManager` AVANT toute
construction de `_AgentState`), donc `ensure_loaded`/`unload` ne créent jamais
de sous-processus.

Deux familles de tests :
  - Auth + endpoints HTTP : via `fastapi.testclient.TestClient` (synchrone,
    déclenche le lifespan de l'app).
  - Cycle de vie de `_AgentState` : appels directs aux coroutines via
    `asyncio.run(...)` — pas de plugin pytest-asyncio/anyio dans ce venv.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
from server_manager import ModelState


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_model_dict(model_id: str = "test-model", vram_gb: float = 1.0) -> dict:
    """Entrée YAML minimale valide acceptée par ModelRegistry._parse_entry.

    Chemin .gguf absolu arbitraire : aucun fichier réel requis tant qu'on ne
    lance pas llama-server et que le modèle ne déclare pas de `sha256`
    (allowed_model_dirs est vide par défaut → pas de contrainte de répertoire).
    """
    return {
        "id": model_id,
        "path": f"/models/{model_id}.gguf",
        "vram_gb": vram_gb,
    }


class FakeServerManager:
    """
    Remplace server_manager.ServerManager dans les tests : ne lance JAMAIS de
    vrai sous-processus. `ensure_loaded` bascule directement en état READY.

    Un hook de classe `FAIL_LOAD` (set de model_id) permet de simuler un échec
    de chargement pour un modèle donné, afin de tester le rollback (port +
    manager libérés) dans `_AgentState.load`.
    """

    FAIL_LOAD: set[str] = set()
    LOAD_GATES: dict[str, asyncio.Event] = {}
    UNLOAD_GATES: dict[str, asyncio.Event] = {}
    ENSURE_CALLS: dict[str, int] = {}
    INSTANCES: list["FakeServerManager"] = []

    def __init__(
        self,
        model,
        port,
        on_unload=None,
        on_capacity_change=None,
        idle_unload_enabled=True,
    ) -> None:
        self.model = model
        self._port = port
        self.port = port
        self._on_unload = on_unload
        self._on_capacity_change = on_capacity_change
        self.idle_unload_enabled = idle_unload_enabled
        self.state = ModelState.UNLOADED
        self._process = None
        self.uptime_seconds = None
        self.idle_seconds = 0.0
        self._last_request_time = 0.0
        self.active_requests = 0
        FakeServerManager.INSTANCES.append(self)

    async def ensure_loaded(self) -> None:
        FakeServerManager.ENSURE_CALLS[self.model.id] = (
            FakeServerManager.ENSURE_CALLS.get(self.model.id, 0) + 1
        )
        if self.model.id in FakeServerManager.FAIL_LOAD:
            raise RuntimeError("échec simulé de chargement")
        self.state = ModelState.LOADING
        gate = FakeServerManager.LOAD_GATES.get(self.model.id)
        if gate is not None:
            await gate.wait()
        self.state = ModelState.READY

    async def unload(self, reason: str = "manuel") -> None:
        if self.state in (ModelState.UNLOADED,):
            return
        self.state = ModelState.UNLOADING
        gate = FakeServerManager.UNLOAD_GATES.get(self.model.id)
        if gate is not None:
            await gate.wait()
        self.state = ModelState.UNLOADED
        if self._on_unload:
            self._on_unload(self.model.id)


@pytest.fixture(autouse=True)
def _reset_fake_manager_failures():
    """Isole les scénarios d'échec entre tests (état de classe partagé)."""
    FakeServerManager.FAIL_LOAD = set()
    FakeServerManager.LOAD_GATES = {}
    FakeServerManager.UNLOAD_GATES = {}
    FakeServerManager.ENSURE_CALLS = {}
    FakeServerManager.INSTANCES = []
    yield
    FakeServerManager.FAIL_LOAD = set()
    FakeServerManager.LOAD_GATES = {}
    FakeServerManager.UNLOAD_GATES = {}
    FakeServerManager.ENSURE_CALLS = {}
    FakeServerManager.INSTANCES = []


@pytest.fixture
def fake_state(monkeypatch) -> "main._AgentState":
    """
    Construit une _AgentState fraîche avec ServerManager patché — AVANT la
    construction, comme recommandé pour éviter toute fuite vers le vrai
    ServerManager (qui, lui, lancerait un vrai sous-processus).
    """
    monkeypatch.setattr(main, "ServerManager", FakeServerManager)
    monkeypatch.setattr(main, "_validate_model_files", lambda model: None)
    return main._AgentState()


# ── Authentification (require_agent_secret via TestClient) ──────────────────

class TestAuthentication:
    def test_placeholder_secret_rejects_any_bearer_503(self, monkeypatch):
        """Secret laissé au placeholder CHANGE_ME_* → fail-closed, 503 systématique."""
        monkeypatch.setattr(main, "ServerManager", FakeServerManager)
        assert main.settings.agent_secret_is_placeholder() is True

        creds = type("Credentials", (), {"credentials": "whatever-random-token"})()
        with pytest.raises(HTTPException) as exc_info:
            main.require_agent_secret(creds)
        assert exc_info.value.status_code == 503

    def test_placeholder_secret_rejects_even_without_header(self, monkeypatch):
        """Sans en-tête Authorization du tout : HTTPBearer(auto_error=True) refuse
        en 401 avant même d'atteindre require_agent_secret (comportement FastAPI
        documenté, pas un bug de l'agent) — jamais un 200 silencieux."""
        monkeypatch.setattr(main, "ServerManager", FakeServerManager)
        monkeypatch.setattr(main.settings, "agent_secret", "a" * 32)
        monkeypatch.setattr(main.settings, "internal_api_key", "b" * 32)
        with TestClient(main.app) as client:
            resp = client.get("/agent/health")
        assert resp.status_code == 401

    def test_configured_secret_wrong_bearer_401(self, monkeypatch):
        """Secret fort configuré + mauvais token → 401 (pas 503, pas d'auth silencieuse)."""
        monkeypatch.setattr(main, "ServerManager", FakeServerManager)
        monkeypatch.setattr(main.settings, "agent_secret", "s3cret-fort-de-test-assez-long-123")
        monkeypatch.setattr(main.settings, "internal_api_key", "b" * 32)
        assert main.settings.agent_secret_is_placeholder() is False

        with TestClient(main.app) as client:
            resp = client.get(
                "/agent/health",
                headers={"Authorization": "Bearer mauvais-token"},
            )
        assert resp.status_code == 401

    def test_configured_secret_correct_bearer_200(self, monkeypatch):
        """Secret fort + bon token → 200 et un NodeHealth cohérent avec la config."""
        monkeypatch.setattr(main, "ServerManager", FakeServerManager)
        monkeypatch.setattr(main.settings, "agent_secret", "s3cret-fort-de-test-assez-long-123")
        monkeypatch.setattr(main.settings, "internal_api_key", "b" * 32)

        with TestClient(main.app) as client:
            resp = client.get(
                "/agent/health",
                headers={"Authorization": "Bearer s3cret-fort-de-test-assez-long-123"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["total_vram_gb"] == main.settings.total_vram_gb
        assert body["used_vram_gb"] == 0.0
        assert body["loaded_model_ids"] == []
        assert body["free_ports"] == main.settings.max_loaded_models


# ── Cycle de vie de _AgentState (asyncio.run, pas de vrai sous-processus) ───

class TestAgentStateLoadUnload:
    def test_load_already_ready_returns_already_loaded(self, fake_state):
        """Un modèle déjà READY/LOADING est renvoyé tel quel — pas de second lancement."""
        model_dict = make_model_dict("already-ready")

        async def scenario():
            first = await fake_state.load(model_dict)
            # Second appel : le manager existe déjà et est READY.
            second = await fake_state.load(model_dict)
            return first, second

        first, second = asyncio.run(scenario())

        assert first.already_loaded is False
        assert second.already_loaded is True
        assert second.model_id == "already-ready"
        assert second.port == first.port
        # Un seul manager créé au total pour ce modèle.
        assert len(fake_state._managers) == 1

    def test_load_loading_state_also_short_circuits(self, fake_state):
        """already_loaded=True aussi quand l'état existant est LOADING (pas seulement READY)."""
        model_dict = make_model_dict("loading-model")

        async def scenario():
            await fake_state.load(model_dict)
            # Forcer artificiellement l'état à LOADING pour simuler une requête
            # concurrente arrivée pendant un chargement en cours.
            mgr = fake_state._managers["loading-model"]
            mgr.state = ModelState.LOADING
            return await fake_state.load(model_dict)

        second = asyncio.run(scenario())
        assert second.already_loaded is True

    def test_port_pool_exhaustion_raises_503(self, fake_state):
        """Charger max_loaded_models modèles épuise le pool ; un de plus → 503."""
        max_models = main.settings.max_loaded_models

        async def scenario():
            for i in range(max_models):
                await fake_state.load(make_model_dict(f"model-{i}", vram_gb=0.1))
            # Un modèle supplémentaire : plus aucun port disponible.
            with pytest.raises(HTTPException) as exc_info:
                await fake_state.load(make_model_dict("one-too-many", vram_gb=0.1))
            return exc_info.value

        exc = asyncio.run(scenario())
        assert exc.status_code == 503
        assert "port" in exc.detail.lower()

    def test_insufficient_vram_raises_503(self, fake_state):
        """vram_gb du modèle > budget net disponible → 503, pas de crash serveur."""
        huge_vram = main.settings.effective_vram_budget_gb() + 1000.0
        model_dict = make_model_dict("modele-enorme", vram_gb=huge_vram)

        async def scenario():
            with pytest.raises(HTTPException) as exc_info:
                await fake_state.load(model_dict)
            return exc_info.value

        exc = asyncio.run(scenario())
        assert exc.status_code == 503
        assert "vram" in exc.detail.lower()
        # Aucun port ne doit avoir été consommé par cette tentative avortée.
        assert len(fake_state._port_pool) == main.settings.max_loaded_models

    def test_failed_load_releases_port_and_manager_raises_500(self, fake_state):
        """
        Échec de ensure_loaded() (fake) → le port ET le manager sont libérés
        (pas de fuite de port), et l'agent lève une HTTPException 500.
        """
        model_id = "modele-qui-echoue"
        FakeServerManager.FAIL_LOAD.add(model_id)
        model_dict = make_model_dict(model_id)

        async def scenario():
            with pytest.raises(HTTPException) as exc_info:
                await fake_state.load(model_dict)
            return exc_info.value

        exc = asyncio.run(scenario())
        assert exc.status_code == 500
        assert "chargement" in exc.detail.lower()

        # Pas de fuite : le manager a été retiré et le port rendu au pool.
        assert model_id not in fake_state._managers
        assert model_id not in fake_state._allocated_ports
        assert len(fake_state._port_pool) == main.settings.max_loaded_models

        # Un rechargement ultérieur (sans le flag d'échec) doit redevenir possible,
        # preuve que le port a bien été rendu utilisable.
        FakeServerManager.FAIL_LOAD.discard(model_id)

        async def retry():
            return await fake_state.load(model_dict)

        result = asyncio.run(retry())
        assert result.already_loaded is False

    def test_unload_not_loaded_returns_unloaded_false(self, fake_state):
        """unload() sur un modèle jamais chargé → UnloadResponse(unloaded=False)."""
        resp = asyncio.run(fake_state.unload("jamais-charge"))
        assert resp.unloaded is False
        assert resp.model_id == "jamais-charge"
        assert resp.freed_vram_gb == 0.0

    def test_unload_loaded_model_frees_port(self, fake_state):
        """unload() d'un modèle chargé libère bien son port dans le pool."""
        model_dict = make_model_dict("a-decharger", vram_gb=2.0)

        async def scenario():
            await fake_state.load(model_dict)
            ports_before = len(fake_state._port_pool)
            resp = await fake_state.unload("a-decharger")
            return resp, ports_before

        resp, ports_before = asyncio.run(scenario())
        assert resp.unloaded is True
        assert resp.freed_vram_gb == 2.0
        assert len(fake_state._port_pool) == ports_before + 1
        assert "a-decharger" not in fake_state._managers

    def test_concurrent_loads_share_one_manager_and_wait_until_ready(self, fake_state):
        model_id = "concurrent-load"

        async def scenario():
            gate = asyncio.Event()
            FakeServerManager.LOAD_GATES[model_id] = gate
            first_task = asyncio.create_task(fake_state.load(make_model_dict(model_id)))
            while (
                model_id not in fake_state._managers
                or fake_state._managers[model_id].state != ModelState.LOADING
            ):
                await asyncio.sleep(0)

            second_task = asyncio.create_task(fake_state.load(make_model_dict(model_id)))
            await asyncio.sleep(0.01)
            assert not first_task.done()
            assert not second_task.done()
            gate.set()
            return await asyncio.gather(first_task, second_task)

        first, second = asyncio.run(scenario())

        assert first.already_loaded is False
        assert second.already_loaded is True
        assert first.port == second.port
        assert FakeServerManager.ENSURE_CALLS[model_id] == 1
        assert len(FakeServerManager.INSTANCES) == 1
        assert len(fake_state._managers) == 1
        assert len(fake_state._port_pool) == main.settings.max_loaded_models - 1

    def test_load_waits_for_concurrent_unload_without_port_corruption(self, fake_state):
        model_id = "unload-load-race"

        async def scenario():
            first = await fake_state.load(make_model_dict(model_id))
            old_manager = fake_state._managers[model_id]
            gate = asyncio.Event()
            FakeServerManager.UNLOAD_GATES[model_id] = gate

            unload_task = asyncio.create_task(fake_state.unload(model_id))
            while old_manager.state != ModelState.UNLOADING:
                await asyncio.sleep(0)

            reload_task = asyncio.create_task(fake_state.load(make_model_dict(model_id)))
            await asyncio.sleep(0.01)
            assert not reload_task.done()
            assert fake_state._managers[model_id] is old_manager
            assert len(fake_state._allocated_ports) == 1

            gate.set()
            unload_response = await unload_task
            reload_response = await reload_task
            return first, old_manager, unload_response, reload_response

        first, old_manager, unloaded, reloaded = asyncio.run(scenario())

        assert unloaded.unloaded is True
        assert reloaded.already_loaded is False
        assert reloaded.port == first.port
        assert fake_state._managers[model_id] is not old_manager
        assert len(FakeServerManager.INSTANCES) == 2
        assert len(fake_state._allocated_ports) == 1
        assert reloaded.port not in fake_state._port_pool
        assert len(fake_state._port_pool) == main.settings.max_loaded_models - 1
        assert len(fake_state._port_pool) == len(set(fake_state._port_pool))


class TestModelFilesFailFast:
    def test_missing_gguf_is_422_before_port_reservation(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "ServerManager", FakeServerManager)
        state = main._AgentState()
        model = make_model_dict("missing-gguf")
        model["path"] = str(tmp_path / "missing.gguf")

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(state.load(model))

        assert exc_info.value.status_code == 422
        assert "introuvable" in exc_info.value.detail
        assert len(state._port_pool) == main.settings.max_loaded_models

    def test_unreadable_gguf_is_422_before_manager_creation(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "ServerManager", FakeServerManager)
        state = main._AgentState()
        gguf = tmp_path / "unreadable.gguf"
        gguf.write_bytes(b"test")
        monkeypatch.setattr(main.os, "access", lambda path, mode: False)
        model = make_model_dict("unreadable-gguf")
        model["path"] = str(gguf)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(state.load(model))

        assert exc_info.value.status_code == 422
        assert "non lisible" in exc_info.value.detail
        assert FakeServerManager.INSTANCES == []

    def test_missing_mmproj_is_422_before_port_reservation(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "ServerManager", FakeServerManager)
        state = main._AgentState()
        gguf = tmp_path / "vision.gguf"
        gguf.write_bytes(b"model")
        model = make_model_dict("missing-mmproj")
        model.update(
            {
                "path": str(gguf),
                "capabilities": ["text_generation", "vision"],
                "mmproj_path": str(tmp_path / "missing-mmproj.gguf"),
            }
        )

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(state.load(model))

        assert exc_info.value.status_code == 422
        assert "projecteur multimodal" in exc_info.value.detail
        assert len(state._port_pool) == main.settings.max_loaded_models


class TestAgentMetrics:
    """Endpoint additif /agent/metrics + méthode _AgentState.agent_metrics()."""

    def test_metrics_empty_when_no_ready_model(self, fake_state):
        """Aucun modèle READY → dict vide, sans I/O réseau ni exception."""
        result = asyncio.run(fake_state.agent_metrics())
        assert result == {}

    def test_metrics_endpoint_requires_secret(self, monkeypatch):
        """Sans en-tête Authorization → 401 (HTTPBearer auto_error)."""
        monkeypatch.setattr(main, "ServerManager", FakeServerManager)
        monkeypatch.setattr(main.settings, "agent_secret", "a" * 32)
        monkeypatch.setattr(main.settings, "internal_api_key", "b" * 32)
        with TestClient(main.app) as client:
            resp = client.get("/agent/metrics")
        assert resp.status_code == 401

    def test_metrics_endpoint_ok_empty_with_secret(self, monkeypatch):
        """Secret fort + bon token, aucun modèle chargé → 200 {}."""
        monkeypatch.setattr(main, "ServerManager", FakeServerManager)
        monkeypatch.setattr(main.settings, "agent_secret", "s3cret-fort-de-test-assez-long-123")
        monkeypatch.setattr(main.settings, "internal_api_key", "b" * 32)
        with TestClient(main.app) as client:
            resp = client.get(
                "/agent/metrics",
                headers={"Authorization": "Bearer s3cret-fort-de-test-assez-long-123"},
            )
        assert resp.status_code == 200
        assert resp.json() == {}


class TestLoadResponseUrl:
    def test_llama_url_uses_configured_host_and_allocated_port(self, fake_state):
        """
        LoadResponse.llama_url = http://{llama_server_host}:{port}. Documente
        que l'agent ne fait AUCUNE validation d'hôte ici : c'est l'orchestrateur
        (RemoteNodeClient) qui reconstruit/valide cette URL contre le vrai hôte
        du nœud (cf. gateway/tests/test_node_client.py::TestRemoteClientTrustedLlamaUrl).
        """
        model_dict = make_model_dict("url-test")
        resp = asyncio.run(fake_state.load(model_dict))

        expected_port = resp.port
        assert resp.llama_url == f"http://{main.settings.llama_server_host}:{expected_port}"

    def test_agent_disables_blind_idle_unload(self, fake_state):
        asyncio.run(fake_state.load(make_model_dict("cluster-lifecycle")))
        assert fake_state._managers["cluster-lifecycle"].idle_unload_enabled is False

    def test_llama_url_brackets_ipv6_bind_host(self, fake_state, monkeypatch):
        monkeypatch.setattr(main.settings, "llama_server_host", "::")
        resp = asyncio.run(fake_state.load(make_model_dict("ipv6-url")))

        assert resp.llama_url == f"http://[::]:{resp.port}"
