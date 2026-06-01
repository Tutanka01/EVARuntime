from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

import model_manager as model_manager_module
import proxy
from config import settings
from model_manager import (
    CapacityQueueFull,
    CapacityQueueTimeout,
    LocalModelManager,
)
from server_manager import ModelState


class FakeModelDef:
    def __init__(self, mid: str, vram: float, enabled: bool = True):
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
    monkeypatch.setattr(settings, "capacity_queue_timeout_seconds", 0.05)
    old = FakeModelDef("old", 10.0)
    new = FakeModelDef("new", 10.0)
    manager = make_manager(monkeypatch, [old, new])
    add_loaded(manager, old, active_requests=1)

    with pytest.raises(CapacityQueueTimeout):
        await manager.ensure_model_loaded("new")

    assert manager.status()["capacity_queue"]["waiters"] == 0


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
