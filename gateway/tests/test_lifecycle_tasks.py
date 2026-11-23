"""
Cycle de vie des backends locaux : sonde de santé progressive et tâches
d'arrière-plan gardées par référence forte.

  - `_wait_for_health` sondait /health toutes les 2 s fixes : un serveur prêt
    attendait jusqu'à 2 s la sonde suivante, à chaque démarrage à froid.
  - Les tâches de drainage des pipes de llama-server et le réveil des waiters
    de capacité étaient créés sans référence forte : asyncio ne garde qu'une
    référence faible, une tâche pouvait être ramassée par le GC en cours de
    route (pipe plus drainé → llama-server gelé ; waiters jamais réveillés).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import server_manager as server_manager_module
from model_manager import LocalModelManager
from server_manager import ServerManager


class _FakeLlamaParams:
    n_gpu_layers = 999
    ctx_size = 2048
    parallel = 1
    flash_attn = False
    cache_type_k = "f16"
    cache_type_v = "f16"
    cpu_moe = False


class FakeModelDef:
    def __init__(self, mid: str = "m1", load_timeout: float = 5) -> None:
        self.id = mid
        self.vram_gb = 1.0
        self.enabled = True
        self.description = ""
        self.sha256 = None
        self.capabilities = ["text_generation"]
        self.llama_params = _FakeLlamaParams()
        self.speculative = None
        self.load_timeout_seconds = load_timeout

    def build_llama_cmd(self, **_kwargs) -> list[str]:
        return ["llama-server"]


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()


class FakeRegistry:
    def get(self, model_id):
        return None

    def list_enabled(self):
        return []

    def list_all(self):
        return []


def _manager(load_timeout: float = 5) -> ServerManager:
    return ServerManager(FakeModelDef(load_timeout=load_timeout), port=9001, idle_unload_enabled=False)


class _HealthClient:
    """Client httpx factice : /health devient « ok » à la sonde n° `ready_at_call`."""

    calls = 0
    ready_at_call = 10**9

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def get(self, url: str):
        type(self).calls += 1
        if type(self).calls < type(self).ready_at_call:
            raise httpx.ConnectError("pas encore prêt")
        return httpx.Response(200, json={"status": "ok"})


@pytest.fixture
def fake_health(monkeypatch):
    """
    Sonde factice + enregistrement des intervalles DEMANDÉS à la pause entre
    deux sondes : on vérifie la politique d'attente, pas du temps écoulé
    (qui dérive sur une CI chargée).
    """
    _HealthClient.calls = 0
    _HealthClient.ready_at_call = 10**9
    requested: list[float] = []

    async def _sleep(delay: float) -> None:
        requested.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(server_manager_module.httpx, "AsyncClient", _HealthClient)
    monkeypatch.setattr(server_manager_module, "_health_poll_sleep", _sleep)
    _HealthClient.requested = requested
    return _HealthClient


# ── Sonde de santé progressive ────────────────────────────────────────────────

@pytest.mark.anyio
async def test_health_poll_starts_short(fake_health) -> None:
    """
    Prêt à la 4e sonde : trois pauses de 0,1 / 0,15 / 0,225 s, soit moins de
    0,5 s cumulées. Avant : une pause fixe de 2 s entre chaque sonde (6 s).
    """
    fake_health.ready_at_call = 4
    mgr = _manager()
    mgr._process = FakeProcess()

    await mgr._wait_for_health()

    assert fake_health.calls == 4
    assert fake_health.requested == pytest.approx([0.1, 0.15, 0.225])


@pytest.mark.anyio
async def test_health_poll_interval_grows_and_is_capped(fake_health) -> None:
    """Intervalle croissant puis plafonné : pas de martèlement d'un long chargement."""
    fake_health.ready_at_call = 20
    mgr = _manager()
    mgr._process = FakeProcess()

    await mgr._wait_for_health()

    requested = fake_health.requested
    assert len(requested) == 19
    assert requested[0] == server_manager_module.HEALTH_POLL_INITIAL_SECONDS
    assert requested == sorted(requested), "jamais décroissant"
    assert max(requested) == server_manager_module.HEALTH_POLL_MAX_SECONDS
    assert requested[-1] == server_manager_module.HEALTH_POLL_MAX_SECONDS


def test_health_poll_defaults() -> None:
    assert server_manager_module.HEALTH_POLL_INITIAL_SECONDS <= 0.1
    assert server_manager_module.HEALTH_POLL_MAX_SECONDS <= 1.0


# ── Tâches de drainage : référence forte jusqu'à la fin ──────────────────────

@pytest.mark.anyio
async def test_pipe_drain_tasks_are_strongly_referenced(monkeypatch) -> None:
    process = FakeProcess()

    async def fake_exec(*args, **kwargs):
        return process

    monkeypatch.setattr(server_manager_module.asyncio, "create_subprocess_exec", fake_exec)
    mgr = _manager()

    await mgr._start_process()

    assert len(mgr._drain_tasks) == 2
    assert all(not task.done() for task in mgr._drain_tasks)

    process.stderr.feed_data(b"ligne de diagnostic\n")
    process.stderr.feed_eof()
    process.stdout.feed_eof()
    for _ in range(10):
        await asyncio.sleep(0)
    assert mgr._drain_tasks == set(), "retirées une fois terminées"
    assert "ligne de diagnostic" in mgr._stderr_tail


# ── Réveil des waiters de capacité ────────────────────────────────────────────

@pytest.mark.anyio
async def test_capacity_notify_task_is_referenced_and_coalesced() -> None:
    manager = LocalModelManager(FakeRegistry())
    woken = asyncio.Event()

    async def waiter() -> None:
        async with manager._capacity_cond:
            await manager._capacity_cond.wait()
            woken.set()

    waiting = asyncio.ensure_future(waiter())
    for _ in range(3):
        await asyncio.sleep(0)

    manager._notify_capacity_changed()
    first = manager._notify_task
    assert isinstance(first, asyncio.Task), "référence forte gardée"
    manager._notify_capacity_changed()
    manager._notify_capacity_changed()
    assert manager._notify_task is first, "réveil déjà planifié : coalescé"

    await asyncio.wait_for(woken.wait(), timeout=1)
    await waiting
    assert first.done()

    manager._notify_capacity_changed()
    assert manager._notify_task is not first, "après exécution, nouveau réveil"
    await manager._notify_task


def test_capacity_notify_never_reuses_a_task_from_a_closed_loop() -> None:
    """Un réveil planifié dans une boucle fermée (tests, asyncio.run) n'est pas réutilisé."""
    manager = LocalModelManager(FakeRegistry())

    async def _schedule_without_running() -> asyncio.Task:
        manager._notify_capacity_changed()
        return manager._notify_task

    orphan = asyncio.run(_schedule_without_running())

    async def _next() -> None:
        manager._notify_capacity_changed()
        assert manager._notify_task is not orphan
        await manager._notify_task

    asyncio.run(_next())
