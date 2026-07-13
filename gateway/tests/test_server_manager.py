"""
Tests de concurrence du VRAI ServerManager (pas un fake).

Les tests existants n'exercent qu'un FakeServerManager côté ModelManager, ce qui
laisse passer les bugs de concurrence internes à ServerManager. Ici on instancie
le vrai objet et on monkeypatch UNIQUEMENT :
  - _start_process : crée un faux self._process (pid/returncode contrôlables),
    sans lancer de vrai llama-server ;
  - _wait_for_health : rend la main immédiatement, ou attend un asyncio.Event
    contrôlé par le test pour ouvrir la fenêtre de race LOADING/unload.

On garde le vrai machinisme d'état, de pin/unpin, de tasks et de callbacks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from config import settings
from server_manager import ModelState, ServerManager


# ── Doubles de test ───────────────────────────────────────────────────────────

class FakeModelDef:
    """Définition de modèle minimale suffisante pour ServerManager."""

    def __init__(self, mid: str = "m1", vram: float = 10.0):
        self.id = mid
        self.vram_gb = vram
        self.enabled = True
        self.description = ""
        self.path = Path(f"/models/{mid}.gguf")
        self.capabilities = ["text_generation"]
        self.llama_params = None
        self.speculative = None
        self.load_timeout_seconds = 5


class FakeProcess:
    """Faux sous-processus : returncode/pid contrôlables par le test."""

    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.returncode: int | None = None

    async def wait(self):
        self.returncode = 0
        return self.returncode


def make_manager(
    on_unload=None,
    on_capacity_change=None,
    mid="m1",
    idle_unload_enabled=True,
) -> ServerManager:
    return ServerManager(
        FakeModelDef(mid),
        port=9001,
        on_unload=on_unload,
        on_capacity_change=on_capacity_change,
        idle_unload_enabled=idle_unload_enabled,
    )


def patch_process_and_health(mgr: ServerManager, monkeypatch, *, health_event=None):
    """
    Remplace _start_process (crée un FakeProcess) et _wait_for_health.
    Si health_event est fourni, _wait_for_health attend cet Event : le test
    contrôle ainsi la fenêtre de race pendant le chargement.
    """
    async def fake_start():
        mgr._process = FakeProcess()

    async def fake_health():
        if health_event is not None:
            await health_event.wait()

    async def fake_kill():
        # Idempotent, comme le vrai _kill_process.
        mgr._process = None

    monkeypatch.setattr(mgr, "_start_process", fake_start)
    monkeypatch.setattr(mgr, "_wait_for_health", fake_health)
    monkeypatch.setattr(mgr, "_kill_process", fake_kill)


@pytest.fixture(autouse=True)
def fast_idle(monkeypatch):
    """Rend le moniteur d'inactivité rapide pour les tests temporels."""
    monkeypatch.setattr(settings, "idle_check_interval_seconds", 0.01)
    monkeypatch.setattr(settings, "idle_timeout_seconds", 0.02)


# ── Test 1 — ensure_loaded concurrent ─────────────────────────────────────────

@pytest.mark.anyio
async def test_ensure_loaded_concurrent_starts_process_once(monkeypatch):
    mgr = make_manager()
    calls = {"start": 0}

    async def fake_start():
        calls["start"] += 1
        mgr._process = FakeProcess()

    async def fake_health():
        return

    monkeypatch.setattr(mgr, "_start_process", fake_start)
    monkeypatch.setattr(mgr, "_wait_for_health", fake_health)

    await asyncio.gather(*[mgr.ensure_loaded() for _ in range(5)])

    assert calls["start"] == 1
    assert mgr.state == ModelState.READY
    await mgr.unload()


# ── Test 2 — is_pinned interdit l'éviction par inactivité ─────────────────────

@pytest.mark.anyio
async def test_idle_monitor_never_unloads_pinned(monkeypatch):
    unloaded = []
    mgr = make_manager(on_unload=lambda mid: unloaded.append(mid))
    patch_process_and_health(mgr, monkeypatch)

    await mgr.ensure_loaded()
    assert mgr.state == ModelState.READY

    mgr.pin()  # une requête active → jamais d'éviction
    # Laisser tourner le moniteur bien au-delà du timeout d'inactivité.
    await asyncio.sleep(0.1)

    assert mgr.state == ModelState.READY, "un modèle pinned ne doit jamais être déchargé"
    assert unloaded == []

    mgr.unpin()
    await mgr.unload()


# ── Test 3 — race unload() pendant LOADING : pas de READY fantôme ─────────────

@pytest.mark.anyio
async def test_unload_during_loading_no_phantom_ready(monkeypatch):
    mgr = make_manager()
    health_event = asyncio.Event()
    patch_process_and_health(mgr, monkeypatch, health_event=health_event)

    # Démarre le chargement mais bloque dans _wait_for_health.
    load_waiter = asyncio.create_task(mgr.ensure_loaded())
    while mgr.state != ModelState.LOADING:
        await asyncio.sleep(0)

    # unload() concurrent pendant LOADING → UNLOADING puis UNLOADED.
    await mgr.unload(reason="test race")
    assert mgr.state == ModelState.UNLOADED

    # Débloque _wait_for_health : _load_and_signal reprend APRÈS l'unload.
    # (La task de chargement a normalement déjà été annulée par unload — ce set
    # est sans effet dans ce cas, mais on couvre aussi le cas non annulé.)
    health_event.set()

    # ensure_loaded doit retourner (event.set garanti par finally), sans exiger READY.
    try:
        await asyncio.wait_for(load_waiter, timeout=2.0)
    except Exception:
        pass

    # L'état final NE DOIT PAS être un READY fantôme.
    assert mgr.state == ModelState.UNLOADED
    assert mgr._process is None
    assert mgr.state != ModelState.READY


# ── Test 4 — crash en READY détecté par le moniteur ───────────────────────────

@pytest.mark.anyio
async def test_crash_in_ready_transitions_to_unloaded(monkeypatch):
    unloaded = []
    capacity = []
    mgr = make_manager(
        on_unload=lambda mid: unloaded.append(mid),
        on_capacity_change=lambda: capacity.append(True),
    )
    patch_process_and_health(mgr, monkeypatch)

    await mgr.ensure_loaded()
    assert mgr.state == ModelState.READY

    # Simule un crash de llama-server (segfault/CUDA OOM) en état READY.
    mgr._stderr_tail.append("CUDA error: out of memory")
    mgr._process.returncode = 1

    # Le moniteur doit détecter le process mort et transitionner vers UNLOADED.
    for _ in range(200):
        if mgr.state == ModelState.UNLOADED:
            break
        await asyncio.sleep(0.01)

    assert mgr.state == ModelState.UNLOADED, "le crash doit transitionner vers UNLOADED"
    assert mgr._process is None
    assert unloaded == ["m1"], "on_unload doit libérer le port"
    assert capacity, "on_capacity_change doit être notifié"


# ── Test 5 — unload pendant LOADING annule la task de chargement ──────────────

@pytest.mark.anyio
async def test_unload_cancels_load_task(monkeypatch):
    mgr = make_manager()
    health_event = asyncio.Event()
    patch_process_and_health(mgr, monkeypatch, health_event=health_event)

    load_waiter = asyncio.create_task(mgr.ensure_loaded())
    while mgr.state != ModelState.LOADING:
        await asyncio.sleep(0)

    load_task = mgr._load_task
    assert load_task is not None and not load_task.done()

    await mgr.unload(reason="test cancel")

    # La task de chargement ne doit plus tourner (annulée/terminée),
    # aucun poll qui traîne sur un port mort.
    assert load_task.done()
    assert mgr.state == ModelState.UNLOADED

    # ensure_loaded doit se débloquer (event.set garanti par finally).
    try:
        await asyncio.wait_for(load_waiter, timeout=2.0)
    except Exception:
        pass


# ── Test 6 — unload par inactivité via le vrai moniteur (pas d'auto-annulation) ─

@pytest.mark.anyio
async def test_idle_unload_via_real_monitor(monkeypatch):
    unloaded = []
    mgr = make_manager(on_unload=lambda mid: unloaded.append(mid))
    patch_process_and_health(mgr, monkeypatch)

    await mgr.ensure_loaded()
    assert mgr.state == ModelState.READY

    # Aucune requête → le moniteur déclenche unload() par inactivité.
    # unload() ne doit PAS s'auto-annuler/attendre (elle tourne dans _idle_task).
    for _ in range(200):
        if mgr.state == ModelState.UNLOADED:
            break
        await asyncio.sleep(0.01)

    assert mgr.state == ModelState.UNLOADED, (
        "l'unload par inactivité doit aboutir proprement (pas d'auto-annulation)"
    )
    assert mgr._process is None
    assert unloaded == ["m1"], "on_unload doit libérer le port après inactivité"


@pytest.mark.anyio
async def test_idle_unload_disabled_keeps_watchdog_without_idle_eviction(monkeypatch):
    """Mode agent : pas d'éviction idle aveugle, mais les crashs restent détectés."""
    unloaded = []
    mgr = make_manager(
        on_unload=lambda mid: unloaded.append(mid),
        idle_unload_enabled=False,
    )
    patch_process_and_health(mgr, monkeypatch)

    await mgr.ensure_loaded()
    await asyncio.sleep(0.1)  # bien au-delà du timeout de la fixture

    assert mgr.state == ModelState.READY
    assert unloaded == []

    mgr._process.returncode = 1
    for _ in range(200):
        if mgr.state == ModelState.UNLOADED:
            break
        await asyncio.sleep(0.01)

    assert mgr.state == ModelState.UNLOADED
    assert unloaded == ["m1"]


def test_llama_url_brackets_ipv6_health_host(monkeypatch):
    monkeypatch.setattr(settings, "llama_server_host", "::1")
    mgr = make_manager()

    assert mgr.llama_url("/health") == "http://[::1]:9001/health"
