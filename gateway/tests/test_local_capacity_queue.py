from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import model_manager as model_manager_module
import proxy
import telemetry
from config import settings
from model_manager import (
    CapacityQueueFull,
    CapacityQueueTimeout,
    LocalModelManager,
)
from server_manager import LOAD_CAPACITY_MARKERS, ModelState, ServerManager


class FakeModelDef:
    def __init__(
        self,
        mid: str,
        vram: float,
        enabled: bool = True,
        fail_load_once: bool = False,
        fail_message: str | None = None,
    ):
        self.id = mid
        self.vram_gb = vram
        self.enabled = enabled
        self.fail_load_once = fail_load_once
        # Échec persistant (non consommé au premier appel) : simule un refus
        # permanent comme un refus d'intégrité SEC-ART-001.
        self.fail_message = fail_message
        self.load_attempts = 0
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

    def first_enabled_id(self):
        for model in self._models.values():
            if model.enabled:
                return model.id
        return None


class FakeServerManager:
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
    ):
        self._model = model
        self._port = port
        self._on_unload = on_unload
        self._on_capacity_change = on_capacity_change
        self._state = ModelState.READY if ready else ModelState.UNLOADED
        self._active_requests = active_requests
        self._last_request_time = last_request_time or time.monotonic()
        self.unload_calls = 0

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

    @property
    def active_requests(self):
        return self._active_requests

    async def ensure_loaded(self):
        self._model.load_attempts += 1
        fail_message = getattr(self._model, "fail_message", None)
        if fail_message is not None:
            self._state = ModelState.UNLOADED
            raise RuntimeError(fail_message)
        if getattr(self._model, "fail_load_once", False):
            self._model.fail_load_once = False
            self._state = ModelState.UNLOADED
            raise RuntimeError("cudaMalloc failed: out of memory")
        self._state = ModelState.READY
        self._last_request_time = time.monotonic()
        if self._on_capacity_change:
            self._on_capacity_change()

    async def unload(self, reason: str = "test"):
        self.unload_calls += 1
        self._state = ModelState.UNLOADED
        if self._on_unload:
            self._on_unload(self._model.id)

    def unpin(self):
        was_pinned = self._active_requests > 0
        self._active_requests = max(0, self._active_requests - 1)
        if was_pinned and self._active_requests == 0 and self._on_capacity_change:
            self._on_capacity_change()

    def status(self):
        return {
            "id": self._model.id,
            "description": self._model.description,
            "enabled": self._model.enabled,
            "vram_gb": self._model.vram_gb,
            "capabilities": self._model.capabilities,
            "state": self._state.value,
            "path": str(self._model.path),
            "pid": None,
            "port": self._port,
            "uptime_seconds": None,
            "idle_seconds": self.idle_seconds,
            "llama_params": None,
        }


class FakeRequest:
    def __init__(self, body: bytes):
        self._body = body

    async def body(self):
        return self._body


async def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached before timeout")


@pytest.fixture
def capacity_settings(monkeypatch):
    monkeypatch.setattr(settings, "total_vram_gb", 10.0)
    monkeypatch.setattr(settings, "vram_overhead_gb", 0.0)
    monkeypatch.setattr(settings, "vram_safety_margin", 0.0)
    monkeypatch.setattr(settings, "max_loaded_models", 1)
    monkeypatch.setattr(settings, "base_llama_port", 18081)
    monkeypatch.setattr(settings, "capacity_queue_enabled", True)
    monkeypatch.setattr(settings, "capacity_queue_timeout_seconds", 1)
    monkeypatch.setattr(settings, "capacity_queue_max_waiters", 10)
    monkeypatch.setattr(settings, "capacity_queue_retry_after_seconds", 10)


def make_manager(monkeypatch, models: list[FakeModelDef]) -> LocalModelManager:
    monkeypatch.setattr(model_manager_module, "ServerManager", FakeServerManager)
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


@pytest.mark.anyio
async def test_waits_for_pinned_model_then_evicts(capacity_settings, monkeypatch):
    old = FakeModelDef("old", 10.0)
    new = FakeModelDef("new", 10.0)
    manager = make_manager(monkeypatch, [old, new])
    old_server = add_loaded(manager, old, active_requests=1)

    task = asyncio.create_task(manager.ensure_model_loaded("new"))
    await wait_until(lambda: len(manager._capacity_waiters) == 1)

    old_server.unpin()
    new_server = await asyncio.wait_for(task, timeout=1.0)

    assert new_server.model.id == "new"
    assert "old" not in manager._managers
    assert manager.status()["capacity_queue"]["waiters"] == 0


@pytest.mark.anyio
async def test_evicts_idle_model_without_queue(capacity_settings, monkeypatch):
    old = FakeModelDef("old", 10.0)
    new = FakeModelDef("new", 10.0)
    manager = make_manager(monkeypatch, [old, new])
    add_loaded(manager, old, active_requests=0, last_request_time=time.monotonic() - 100)

    new_server = await manager.ensure_model_loaded("new")

    assert new_server.model.id == "new"
    assert "old" not in manager._managers
    assert manager.status()["capacity_queue"]["waiters"] == 0


@pytest.mark.anyio
async def test_load_oom_retries_after_extra_eviction(capacity_settings, monkeypatch):
    monkeypatch.setattr(settings, "total_vram_gb", 20.0)
    monkeypatch.setattr(settings, "max_loaded_models", 2)
    old = FakeModelDef("old", 10.0)
    new = FakeModelDef("new", 5.0, fail_load_once=True)
    manager = make_manager(monkeypatch, [old, new])
    add_loaded(manager, old, active_requests=0, last_request_time=time.monotonic() - 100)

    new_server = await manager.ensure_model_loaded("new")

    assert new_server.model.id == "new"
    assert new_server.state == ModelState.READY
    assert "old" not in manager._managers
    assert manager.status()["capacity_queue"]["waiters"] == 0


@pytest.mark.anyio
async def test_queue_full_raises(capacity_settings, monkeypatch):
    monkeypatch.setattr(settings, "capacity_queue_max_waiters", 1)
    old = FakeModelDef("old", 10.0)
    new = FakeModelDef("new", 10.0)
    other = FakeModelDef("other", 10.0)
    manager = make_manager(monkeypatch, [old, new, other])
    old_server = add_loaded(manager, old, active_requests=1)

    first = asyncio.create_task(manager.ensure_model_loaded("new"))
    await wait_until(lambda: len(manager._capacity_waiters) == 1)

    with pytest.raises(CapacityQueueFull):
        await manager.ensure_model_loaded("other")

    old_server.unpin()
    await asyncio.wait_for(first, timeout=1.0)


@pytest.mark.anyio
async def test_queue_timeout_raises(capacity_settings, monkeypatch):
    telemetry.reset_all()
    monkeypatch.setattr(settings, "capacity_queue_timeout_seconds", 0.05)
    old = FakeModelDef("old", 10.0)
    new = FakeModelDef("new", 10.0)
    manager = make_manager(monkeypatch, [old, new])
    add_loaded(manager, old, active_requests=1)

    with pytest.raises(CapacityQueueTimeout):
        await manager.ensure_model_loaded("new")

    assert manager.status()["capacity_queue"]["waiters"] == 0
    series = telemetry.CAPACITY_QUEUE_SECONDS.snapshot().series
    assert len(series) == 1
    assert (series[0].model, series[0].outcome, series[0].count) == (
        "new",
        "timeout",
        1,
    )


@pytest.mark.anyio
async def test_model_too_large_never_enters_queue(capacity_settings, monkeypatch):
    huge = FakeModelDef("huge", 11.0)
    manager = make_manager(monkeypatch, [huge])

    with pytest.raises(RuntimeError, match="ne peut pas tenir seul"):
        await manager.ensure_model_loaded("huge")

    assert manager.status()["capacity_queue"]["waiters"] == 0


@pytest.mark.anyio
async def test_queue_can_be_disabled(capacity_settings, monkeypatch):
    monkeypatch.setattr(settings, "capacity_queue_enabled", False)
    old = FakeModelDef("old", 10.0)
    new = FakeModelDef("new", 10.0)
    manager = make_manager(monkeypatch, [old, new])
    add_loaded(manager, old, active_requests=1)

    with pytest.raises(RuntimeError, match="requêtes en cours"):
        await manager.ensure_model_loaded("new")

    assert manager.status()["capacity_queue"]["waiters"] == 0


@pytest.mark.anyio
async def test_proxy_capacity_errors_include_retry_after(capacity_settings):
    class TimeoutManager:
        registry = FakeRegistry([FakeModelDef("new", 10.0)])

        async def ensure_model_loaded(self, model_id: str):
            raise CapacityQueueTimeout("capacity timeout")

    response = await proxy.proxy_request(
        FakeRequest(b'{"model":"new","messages":[]}'),
        "/v1/chat/completions",
        {"user_id": 1, "key_id": 1},
        TimeoutManager(),
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "10"


@pytest.mark.anyio
async def test_proxy_queue_full_includes_retry_after(capacity_settings):
    class FullManager:
        registry = FakeRegistry([FakeModelDef("new", 10.0)])

        async def ensure_model_loaded(self, model_id: str):
            raise CapacityQueueFull("queue full")

    response = await proxy.proxy_request(
        FakeRequest(b'{"model":"new","messages":[]}'),
        "/v1/chat/completions",
        {"user_id": 1, "key_id": 1},
        FullManager(),
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "10"


# ── Refus d'intégrité (SEC-ART-001) : jamais classé erreur de capacité ────────

@pytest.mark.anyio
async def test_integrity_refusal_is_not_capacity_error(capacity_settings, monkeypatch, tmp_path):
    """
    SEC-ART-001 : le message de refus d'intégrité ne contient aucun marqueur
    de capacité, donc LocalModelManager ne déclenche PAS sa retry après
    éviction — il remonte le refus tel quel en une seule tentative, sans
    évict d'autres modèles, et libère le port.
    """
    # Le message produit par le VRAI ServerManager._verify_artifact_integrity
    # sur un GGUF dont l'empreinte déclarée ne correspond pas au fichier.
    gguf = tmp_path / "falsifie.gguf"
    gguf.write_bytes(b"contenu legitime")
    probe = ServerManager(
        SimpleNamespace(id="victim", path=gguf, sha256="0" * 64),
        port=0,
        idle_unload_enabled=False,
    )
    with pytest.raises(RuntimeError) as exc_info:
        await probe._verify_artifact_integrity()
    refusal = exc_info.value

    text = str(refusal).lower()
    for marker in LOAD_CAPACITY_MARKERS:
        assert marker not in text, f"marqueur de capacité '{marker}' dans le refus"
    assert LocalModelManager._is_load_capacity_error(refusal) is False

    # Bout en bout au niveau LocalModelManager : le FakeServerManager échoue
    # avec ce message exact (le RuntimeError est la seule forme atteignant
    # LocalModelManager, l'IntegrityError étant convertie dans ServerManager).
    monkeypatch.setattr(settings, "total_vram_gb", 20.0)
    monkeypatch.setattr(settings, "max_loaded_models", 2)
    old = FakeModelDef("old", 10.0)
    new = FakeModelDef("new", 10.0, fail_message=str(refusal))
    manager = make_manager(monkeypatch, [old, new])
    add_loaded(manager, old, active_requests=0, last_request_time=time.monotonic() - 100)
    ports_avant = list(manager._port_pool)

    with pytest.raises(RuntimeError) as err_info:
        await manager.ensure_model_loaded("new")

    assert str(err_info.value) == str(refusal)
    assert new.load_attempts == 1, "un refus d'intégrité ne doit pas être retenté"
    assert "old" in manager._managers, "un refus d'intégrité ne doit rien évincer"
    assert "new" not in manager._managers, "le manager en échec est oublié"
    assert "new" not in manager._allocated_ports, "le port du manager en échec est libéré"
    assert sorted(manager._port_pool) == sorted(ports_avant), "port rendu au pool"
