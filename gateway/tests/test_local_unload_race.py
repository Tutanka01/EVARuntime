"""
Course UNLOADING côté gateway locale (audit P0-1).

Le déchargement pour inactivité (`_idle_monitor` → `unload()`) ne tient ni
model_lock ni `_capacity_cond`. Une requête arrivée pendant la fenêtre
UNLOADING (SIGTERM, jusqu'à 10 s) créait un second ServerManager ; le callback
de l'ancien libérait ensuite le port et l'entrée du nouveau, et l'ancien port
fuyait. Ces tests utilisent le VRAI ServerManager : seuls le lancement du
processus, la sonde de santé et le kill sont simulés.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from config import settings
from model_manager import CapacityQueueTimeout, LocalModelManager
from server_manager import ModelState, ServerManager


class _Model:
    def __init__(self, mid: str, vram: float):
        self.id = mid
        self.vram_gb = vram
        self.enabled = True
        self.description = ""
        self.path = Path(f"/models/{mid}.gguf")
        self.capabilities = ["text_generation"]
        self.load_timeout_seconds = 5
        self.sha256 = None


class _Registry:
    def __init__(self, models: list[_Model]):
        self._models = {m.id: m for m in models}

    def get(self, model_id: str):
        return self._models.get(model_id)

    def list_all(self):
        return list(self._models.values())

    def list_enabled(self):
        return list(self._models.values())


@pytest.fixture
def race_settings(monkeypatch):
    monkeypatch.setattr(settings, "total_vram_gb", 10.0)
    monkeypatch.setattr(settings, "vram_overhead_gb", 0.0)
    monkeypatch.setattr(settings, "vram_safety_margin", 0.0)
    # Deux ports : avec un seul, la file de capacité masquait la course.
    monkeypatch.setattr(settings, "max_loaded_models", 2)
    monkeypatch.setattr(settings, "base_llama_port", 18181)
    monkeypatch.setattr(settings, "capacity_queue_enabled", True)
    monkeypatch.setattr(settings, "capacity_queue_timeout_seconds", 1)
    monkeypatch.setattr(settings, "capacity_queue_max_waiters", 10)


@pytest.fixture
def no_subprocess(monkeypatch):
    """Cycle de vie réel, sans llama-server ni moniteur d'arrière-plan."""

    async def _noop(self) -> None:
        return None

    for name in ("_verify_artifact_integrity", "_start_process", "_wait_for_health", "_idle_monitor"):
        monkeypatch.setattr(ServerManager, name, _noop)


def _gate_kill(manager: ServerManager) -> tuple[asyncio.Event, asyncio.Event]:
    """Remplace le kill par une attente contrôlée (SIGTERM lent)."""
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_kill() -> None:
        started.set()
        await release.wait()

    manager._kill_process = slow_kill
    return started, release


def _assert_pool_consistent(mgr: LocalModelManager, all_ports: set[int]) -> None:
    """Chaque port est soit libre, soit alloué à son manager courant — jamais les deux."""
    allocated = list(mgr._allocated_ports.values())
    assert len(allocated) == len(set(allocated))
    assert not set(allocated) & set(mgr._port_pool), "port libre ET alloué"
    assert set(allocated) | set(mgr._port_pool) == all_ports, "port perdu"
    assert len(mgr._port_pool) == len(set(mgr._port_pool)), "port en double dans le pool"
    for model_id, manager in mgr._managers.items():
        assert mgr._allocated_ports[model_id] == manager.port


@pytest.mark.anyio
async def test_request_during_idle_unload_waits_then_reloads_cleanly(
    race_settings, no_subprocess
):
    model = _Model("m", 4.0)
    mgr = LocalModelManager(_Registry([model]))
    all_ports = set(mgr._port_pool)

    first = await mgr.ensure_model_loaded("m")
    assert first.state is ModelState.READY
    kill_started, release_kill = _gate_kill(first)

    # Même chemin que _idle_monitor : ni model_lock ni _capacity_cond.
    unload_task = asyncio.create_task(first.unload(reason="inactivité (test)"))
    await asyncio.wait_for(kill_started.wait(), timeout=1.0)
    assert first.state is ModelState.UNLOADING

    request = asyncio.create_task(mgr.ensure_model_loaded("m"))
    await asyncio.sleep(0.05)
    assert not request.done(), "aucun second manager tant que le premier est UNLOADING"
    assert mgr._managers["m"] is first

    release_kill.set()
    await asyncio.wait_for(unload_task, timeout=1.0)
    second = await asyncio.wait_for(request, timeout=1.0)

    assert second is not first
    assert second.state is ModelState.READY
    assert mgr._managers == {"m": second}
    assert mgr._allocated_ports == {"m": second.port}
    assert mgr._used_vram_gb() == model.vram_gb
    _assert_pool_consistent(mgr, all_ports)


@pytest.mark.anyio
async def test_late_callback_of_replaced_manager_is_a_noop(race_settings, no_subprocess):
    """Défense en profondeur : un callback périmé ne libère jamais le manager courant."""
    mgr = LocalModelManager(_Registry([_Model("m", 4.0)]))
    all_ports = set(mgr._port_pool)

    first = await mgr.ensure_model_loaded("m")
    await first.unload(reason="test")
    second = await mgr.ensure_model_loaded("m")
    assert second is not first

    # Rejoue le callback du premier : avant correctif, il retirait `second`
    # du pool et rendait son port alors qu'il est en service.
    first._on_unload("m")
    assert mgr._managers["m"] is second
    assert mgr._allocated_ports["m"] == second.port
    assert second.port not in mgr._port_pool
    _assert_pool_consistent(mgr, all_ports)

    # Contrôle positif : le callback du manager courant libère bien.
    second._on_unload("m")
    assert "m" not in mgr._managers
    assert set(mgr._port_pool) == all_ports


@pytest.mark.anyio
async def test_stuck_unload_refuses_admission_without_touching_pool(
    race_settings, no_subprocess
):
    """Un unload qui ne converge pas : 503 borné, jamais un second llama-server."""
    mgr = LocalModelManager(_Registry([_Model("m", 4.0)]))
    all_ports = set(mgr._port_pool)
    first = await mgr.ensure_model_loaded("m")
    kill_started, release_kill = _gate_kill(first)
    unload_task = asyncio.create_task(first.unload(reason="test"))
    await asyncio.wait_for(kill_started.wait(), timeout=1.0)
    try:
        with pytest.raises(CapacityQueueTimeout, match="déchargement précédent"):
            await mgr.ensure_model_loaded("m")
        assert mgr._managers == {"m": first}
        assert mgr._allocated_ports == {"m": first.port}
        _assert_pool_consistent(mgr, all_ports)
    finally:
        release_kill.set()
        await asyncio.wait_for(unload_task, timeout=1.0)
    assert mgr._managers == {}
    assert set(mgr._port_pool) == all_ports


@pytest.mark.anyio
async def test_unloading_without_capacity_queue_fails_fast(
    race_settings, no_subprocess, monkeypatch
):
    monkeypatch.setattr(settings, "capacity_queue_enabled", False)
    mgr = LocalModelManager(_Registry([_Model("m", 4.0)]))
    first = await mgr.ensure_model_loaded("m")
    kill_started, release_kill = _gate_kill(first)
    unload_task = asyncio.create_task(first.unload(reason="test"))
    await asyncio.wait_for(kill_started.wait(), timeout=1.0)
    try:
        with pytest.raises(RuntimeError, match="en cours de déchargement"):
            await asyncio.wait_for(mgr.ensure_model_loaded("m"), timeout=0.5)
        assert mgr._managers == {"m": first}
    finally:
        release_kill.set()
        await asyncio.wait_for(unload_task, timeout=1.0)


@pytest.mark.anyio
async def test_forget_failed_manager_only_releases_its_own_port(
    race_settings, no_subprocess
):
    mgr = LocalModelManager(_Registry([_Model("m", 4.0)]))
    all_ports = set(mgr._port_pool)
    current = await mgr.ensure_model_loaded("m")
    stale = ServerManager(model=current.model, port=current.port)

    await mgr._forget_failed_manager("m", stale)
    assert mgr._managers["m"] is current
    assert mgr._allocated_ports["m"] == current.port
    _assert_pool_consistent(mgr, all_ports)

    # Contrôle positif.
    await mgr._forget_failed_manager("m", current)
    assert mgr._managers == {}
    assert set(mgr._port_pool) == all_ports
