"""Tests pour la fonctionnalité ALWAYS_ON_MODELS."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

import model_manager as model_manager_module
from config import settings
from model_manager import LocalModelManager
from server_manager import ModelState


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeModelDef:
    def __init__(self, mid: str, vram: float = 5.0, enabled: bool = True):
        self.id = mid
        self.vram_gb = vram
        self.enabled = enabled
        self.description = ""
        self.path = Path(f"/models/{mid}.gguf")
        self.capabilities = ["text_generation"]
        self.llama_params = None
        self.load_timeout_seconds = None


class FakeRegistry:
    def __init__(self, models: list[FakeModelDef]):
        self._models = {m.id: m for m in models}

    def get(self, model_id: str):
        return self._models.get(model_id)

    def list_all(self):
        return list(self._models.values())

    def list_enabled(self):
        return [m for m in self._models.values() if m.enabled]


class FakeServerManager:
    """Double de ServerManager pour les tests ALWAYS_ON_MODELS."""

    def __init__(
        self,
        model,
        port: int,
        on_unload=None,
        on_capacity_change=None,
        *,
        ready: bool = False,
        active_requests: int = 0,
        last_request_time: float | None = None,
        idle_unload_enabled: bool = True,
    ):
        self._model = model
        self._port = port
        self._on_unload = on_unload
        self._on_capacity_change = on_capacity_change
        self._state = ModelState.READY if ready else ModelState.UNLOADED
        self._active_requests = active_requests
        self._last_request_time = last_request_time or time.monotonic()
        self.idle_unload_enabled = idle_unload_enabled
        self.unload_calls: list[str] = []

    @property
    def state(self):
        return self._state

    @property
    def model(self):
        return self._model

    @property
    def is_pinned(self):
        return self._active_requests > 0

    @property
    def idle_seconds(self):
        return time.monotonic() - self._last_request_time

    async def ensure_loaded(self):
        self._state = ModelState.READY
        self._last_request_time = time.monotonic()
        if self._on_capacity_change:
            self._on_capacity_change()

    async def unload(self, reason: str = "test"):
        self.unload_calls.append(reason)
        self._state = ModelState.UNLOADED
        if self._on_unload:
            self._on_unload(self._model.id, reason)


def make_manager(monkeypatch, models: list[FakeModelDef], always_on: list[str] | None = None) -> LocalModelManager:
    """Crée un LocalModelManager avec des fakes et config ALWAYS_ON_MODELS."""
    monkeypatch.setattr(model_manager_module, "ServerManager", FakeServerManager)
    if always_on is not None:
        monkeypatch.setattr(settings, "always_on_models", always_on)
    else:
        monkeypatch.setattr(settings, "always_on_models", [])
    return LocalModelManager(FakeRegistry(models))


def add_loaded(
    manager: LocalModelManager,
    model: FakeModelDef,
    *,
    active_requests: int = 0,
    last_request_time: float | None = None,
) -> FakeServerManager:
    port = manager._port_pool.pop(0)
    manager._allocated_ports[model.id] = port
    server = FakeServerManager(
        model,
        port,
        on_unload=manager._on_model_unloaded,
        on_capacity_change=manager._notify_capacity_changed,
        ready=True,
        active_requests=active_requests,
        last_request_time=last_request_time,
    )
    manager._managers[model.id] = server
    return server


@pytest.fixture
def capacity_settings(monkeypatch):
    monkeypatch.setattr(settings, "total_vram_gb", 10.0)
    monkeypatch.setattr(settings, "vram_overhead_gb", 0.0)
    monkeypatch.setattr(settings, "vram_safety_margin", 0.0)
    monkeypatch.setattr(settings, "max_loaded_models", 2)
    monkeypatch.setattr(settings, "base_llama_port", 18081)


# ── Tests de configuration ───────────────────────────────────────────────────

class TestConfigParsing:
    """Vérifie que ALWAYS_ON_MODELS est correctement parsé depuis l'environnement."""

    def test_always_on_models_list_from_env(self, monkeypatch):
        """ALWAYS_ON_MODELS=foo,bar → ['foo', 'bar']."""
        monkeypatch.setattr(settings, "always_on_models", ["foo", "bar"])
        assert settings.always_on_models == ["foo", "bar"]

    def test_always_on_models_empty(self, monkeypatch):
        """ALWAYS_ON_MODELS vide → liste vide."""
        monkeypatch.setattr(settings, "always_on_models", [])
        assert settings.always_on_models == []


# ── Tests de chargement au démarrage ─────────────────────────────────────────

@pytest.mark.anyio
async def test_load_always_on_models_at_startup(capacity_settings, monkeypatch):
    """load_always_on_models() charge les modèles always-on."""
    foo = FakeModelDef("foo", 5.0)
    bar = FakeModelDef("bar", 5.0)
    manager = make_manager(monkeypatch, [foo, bar], always_on=["foo"])

    await manager.load_always_on_models()

    assert "foo" in manager._managers
    assert manager._managers["foo"].state == ModelState.READY


@pytest.mark.anyio
async def test_load_always_on_models_multiple(capacity_settings, monkeypatch):
    """Plusieurs always-on sont tous chargés."""
    foo = FakeModelDef("foo", 5.0)
    bar = FakeModelDef("bar", 5.0)
    manager = make_manager(monkeypatch, [foo, bar], always_on=["foo", "bar"])

    await manager.load_always_on_models()

    assert "foo" in manager._managers
    assert "bar" in manager._managers


@pytest.mark.anyio
async def test_load_always_on_models_noop_when_empty(capacity_settings, monkeypatch):
    """Aucun always-on → load_always_on_models ne fait rien."""
    foo = FakeModelDef("foo", 5.0)
    manager = make_manager(monkeypatch, [foo], always_on=[])

    await manager.load_always_on_models()

    assert len(manager._managers) == 0


# ── Tests d'exemption idle timeout ───────────────────────────────────────────

@pytest.mark.anyio
async def test_always_on_model_not_unloaded_by_idle(capacity_settings, monkeypatch):
    """Un modèle always-on ne doit pas être déchargé par le moniteur idle."""
    foo = FakeModelDef("foo", 5.0)
    manager = make_manager(monkeypatch, [foo], always_on=["foo"])

    await manager.load_always_on_models()

    # Vérifier que idle_unload_enabled=False est passé au ServerManager
    server = manager._managers["foo"]
    assert server.idle_unload_enabled is False


@pytest.mark.anyio
async def test_regular_model_idle_unload_still_works(capacity_settings, monkeypatch):
    """Un modèle non always-on conserve idle_unload_enabled=True."""
    foo = FakeModelDef("foo", 5.0)
    bar = FakeModelDef("bar", 5.0)
    manager = make_manager(monkeypatch, [foo, bar], always_on=["foo"])

    await manager.load_always_on_models()

    # Charger bar normalement (pas toujours-on)
    port = manager._port_pool.pop(0)
    manager._allocated_ports["bar"] = port
    bar_server = FakeServerManager(
        bar,
        port,
        on_unload=manager._on_model_unloaded,
        on_capacity_change=manager._notify_capacity_changed,
        ready=True,
        idle_unload_enabled=True,  # bar n'est pas always-on
    )
    manager._managers["bar"] = bar_server

    assert bar_server.idle_unload_enabled is True


# ── Tests de rechargement automatique ────────────────────────────────────────

@pytest.mark.anyio
async def test_always_on_reloaded_after_idle_unload(capacity_settings, monkeypatch):
    """Quand un modèle se décharge pour idle, les always-on non chargés sont rechargés."""
    foo = FakeModelDef("foo", 5.0)  # always-on
    bar = FakeModelDef("bar", 5.0)

    manager = make_manager(monkeypatch, [foo, bar], always_on=["foo"])

    # Charger foo (always-on), puis le décharger manuellement pour simuler une éviction VRAM
    await manager.load_always_on_models()
    assert "foo" in manager._managers
    assert manager._managers["foo"].state == ModelState.READY

    # Décharger foo manuellement (simule une éviction VRAM)
    await manager._managers["foo"].unload(reason="vram pressure")
    assert "foo" not in manager._managers

    # Charger bar pour occuper la capacité
    port = manager._port_pool.pop(0)
    manager._allocated_ports["bar"] = port
    bar_server = FakeServerManager(
        bar,
        port,
        on_unload=manager._on_model_unloaded,
        on_capacity_change=manager._notify_capacity_changed,
        ready=True,
        idle_unload_enabled=True,
    )
    manager._managers["bar"] = bar_server

    # Simuler un unload pour idle de bar → doit déclencher le rechargement de foo
    await bar_server.unload(reason="idle")

    # Attendre que la tâche asynchrone _reload_always_on_models s'exécute
    await asyncio.sleep(0.1)

    assert "foo" in manager._managers
    assert manager._managers["foo"].state == ModelState.READY


@pytest.mark.anyio
async def test_no_reload_when_unload_not_idle(capacity_settings, monkeypatch):
    """Un unload admin ne doit pas déclencher le rechargement des always-on."""
    foo = FakeModelDef("foo", 5.0)
    bar = FakeModelDef("bar", 5.0)

    manager = make_manager(monkeypatch, [foo, bar], always_on=["foo"])

    await manager.load_always_on_models()
    assert "foo" in manager._managers

    # Décharger foo manuellement
    await manager._managers["foo"].unload(reason="vram pressure")
    assert "foo" not in manager._managers

    # Charger bar
    port = manager._port_pool.pop(0)
    manager._allocated_ports["bar"] = port
    bar_server = FakeServerManager(
        bar,
        port,
        on_unload=manager._on_model_unloaded,
        on_capacity_change=manager._notify_capacity_changed,
        ready=True,
        idle_unload_enabled=True,
    )
    manager._managers["bar"] = bar_server

    # Simuler un unload admin de bar → ne doit PAS recharger foo
    await bar_server.unload(reason="admin request")

    # Attendre que la tâche asynchrone s'exécute
    await asyncio.sleep(0.1)

    assert "foo" not in manager._managers


@pytest.mark.anyio
async def test_no_reload_when_always_on_already_loaded(capacity_settings, monkeypatch):
    """Si un always-on est déjà chargé, il ne doit pas être rechargé."""
    foo = FakeModelDef("foo", 5.0)

    manager = make_manager(monkeypatch, [foo], always_on=["foo"])

    await manager.load_always_on_models()
    assert "foo" in manager._managers

    # Charger bar pour occuper la capacité
    bar = FakeModelDef("bar", 5.0)
    port = manager._port_pool.pop(0)
    manager._allocated_ports["bar"] = port
    bar_server = FakeServerManager(
        bar,
        port,
        on_unload=manager._on_model_unloaded,
        on_capacity_change=manager._notify_capacity_changed,
        ready=True,
        idle_unload_enabled=True,
    )
    manager._managers["bar"] = bar_server

    # Simuler un unload pour idle de bar → foo est déjà chargé, ne doit pas être rechargé
    await bar_server.unload(reason="idle")

    await asyncio.sleep(0.1)

    assert "foo" in manager._managers
    assert manager._managers["foo"].state == ModelState.READY
