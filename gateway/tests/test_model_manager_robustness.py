"""
Tests de robustesse du LocalModelManager — SANS GPU, avec mocks.

Couvre :
  1. Drain des requêtes actives au shutdown (invariant : pas d'éviction d'un
     modèle pinné ; retour immédiat si rien n'est pinné).
  2. Réconciliation VRAM nvidia-smi (dérive → warning + status() enrichi ;
     sonde None → aucun warning, status() inchangé).
  3. Détection des ports orphelins (warning, aucun kill par défaut).

Réutilise les fakes/patterns de test_local_capacity_queue.py.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from pathlib import Path

import pytest

import model_manager as model_manager_module
from config import settings
from model_manager import LocalModelManager
from server_manager import ModelState


# ── Fakes (mêmes patterns que test_local_capacity_queue.py, inlinés car le
#    dossier tests/ n'est pas sur sys.path — pas de cross-import entre modules) ──

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


class FakeServerManager:
    def __init__(
        self, model, port, on_unload=None, on_capacity_change=None, *,
        ready: bool = False, active_requests: int = 0, last_request_time: float | None = None,
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

    async def unload(self, reason: str = "test"):
        self.unload_calls += 1
        self._state = ModelState.UNLOADED
        if self._on_unload:
            self._on_unload(self._model.id)

    def unpin(self):
        self._active_requests = max(0, self._active_requests - 1)

    def status(self):
        return {
            "id": self._model.id, "state": self._state.value, "port": self._port,
            "vram_gb": self._model.vram_gb,
        }


def make_manager(monkeypatch, models: list[FakeModelDef]) -> LocalModelManager:
    monkeypatch.setattr(model_manager_module, "ServerManager", FakeServerManager)
    return LocalModelManager(FakeRegistry(models))


def add_loaded(manager, model, *, active_requests: int = 0, last_request_time=None):
    port = manager._port_pool.pop(0)
    manager._allocated_ports[model.id] = port
    server = FakeServerManager(
        model, port,
        on_unload=manager._on_model_unloaded,
        on_capacity_change=manager._notify_capacity_changed,
        ready=True, active_requests=active_requests, last_request_time=last_request_time,
    )
    manager._managers[model.id] = server
    return server


@pytest.fixture
def capacity_settings(monkeypatch):
    monkeypatch.setattr(settings, "total_vram_gb", 10.0)
    monkeypatch.setattr(settings, "vram_overhead_gb", 0.0)
    monkeypatch.setattr(settings, "vram_safety_margin", 0.0)
    monkeypatch.setattr(settings, "max_loaded_models", 1)
    monkeypatch.setattr(settings, "base_llama_port", 18081)


# ── 1. Drain des requêtes actives au shutdown ─────────────────────────────────

@pytest.mark.anyio
async def test_shutdown_waits_for_pinned_then_unloads(capacity_settings, monkeypatch):
    """Un modèle pinné doit être drainé (attendu) avant déchargement."""
    monkeypatch.setattr(settings, "shutdown_drain_timeout_seconds", 2.0)
    monkeypatch.setattr(settings, "shutdown_drain_poll_seconds", 0.02)

    old = FakeModelDef("old", 10.0)
    manager = make_manager(monkeypatch, [old])
    server = add_loaded(manager, old, active_requests=1)

    shutdown_task = asyncio.create_task(manager.shutdown())

    # Tant que le modèle est pinné, il ne doit PAS avoir été déchargé.
    await asyncio.sleep(0.1)
    assert server.unload_calls == 0
    assert not shutdown_task.done()

    # Libération de la requête active → le drain doit se débloquer et décharger.
    server.unpin()
    await asyncio.wait_for(shutdown_task, timeout=2.0)
    assert server.unload_calls == 1


@pytest.mark.anyio
async def test_shutdown_returns_immediately_when_nothing_pinned(capacity_settings, monkeypatch):
    """Aucun modèle pinné → shutdown() ne doit pas attendre (lifespan rapide)."""
    # Timeout de drain élevé : s'il était appliqué à tort, le test serait lent.
    monkeypatch.setattr(settings, "shutdown_drain_timeout_seconds", 30.0)

    old = FakeModelDef("old", 10.0)
    manager = make_manager(monkeypatch, [old])
    server = add_loaded(manager, old, active_requests=0)

    start = time.monotonic()
    await manager.shutdown()
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"shutdown() a attendu {elapsed:.2f}s sans modèle pinné"
    assert server.unload_calls == 1


@pytest.mark.anyio
async def test_shutdown_empty_manager_is_fast(capacity_settings, monkeypatch):
    """Aucun modèle du tout → retour immédiat (cas TestClient sans GPU)."""
    monkeypatch.setattr(settings, "shutdown_drain_timeout_seconds", 30.0)
    manager = make_manager(monkeypatch, [])

    start = time.monotonic()
    await manager.shutdown()
    assert time.monotonic() - start < 0.5


@pytest.mark.anyio
async def test_shutdown_force_unloads_after_drain_timeout(capacity_settings, monkeypatch, caplog):
    """Si un modèle reste pinné au-delà du timeout, on force le déchargement."""
    monkeypatch.setattr(settings, "shutdown_drain_timeout_seconds", 0.2)
    monkeypatch.setattr(settings, "shutdown_drain_poll_seconds", 0.02)

    old = FakeModelDef("old", 10.0)
    manager = make_manager(monkeypatch, [old])
    server = add_loaded(manager, old, active_requests=1)  # reste pinné

    with caplog.at_level(logging.WARNING, logger="model_manager"):
        await asyncio.wait_for(manager.shutdown(), timeout=2.0)

    # Malgré le pin persistant, le déchargement forcé a bien eu lieu après timeout.
    assert server.unload_calls == 1
    assert any("timeout de drain" in r.message.lower() for r in caplog.records)


# ── 2. Réconciliation VRAM (nvidia-smi) ───────────────────────────────────────

@pytest.mark.anyio
async def test_vram_reconcile_drift_emits_warning_and_status(capacity_settings, monkeypatch, caplog):
    """Sonde renvoyant une VRAM élevée → warning de dérive + status() enrichi."""
    old = FakeModelDef("old", 10.0)  # 10 GB déclarés = 10240 Mo
    manager = make_manager(monkeypatch, [old])
    add_loaded(manager, old, active_requests=0)  # READY → compté dans _used_vram_gb

    # Sonde mockée : 20 GB réels utilisés → très au-dessus des 10 GB déclarés.
    async def fake_probe():
        return 20480.0

    monkeypatch.setattr(model_manager_module, "probe_gpu_used_mb", fake_probe)

    with caplog.at_level(logging.WARNING, logger="model_manager"):
        await manager._reconcile_vram_once()

    assert any("dérive vram" in r.message.lower() for r in caplog.records)

    budget = manager.status()["vram_budget"]
    assert budget["gpu_used_mb_measured"] == pytest.approx(20480.0)
    assert budget["vram_drift_mb"] == pytest.approx(20480.0 - 10240.0)


@pytest.mark.anyio
async def test_vram_reconcile_none_probe_is_inert(capacity_settings, monkeypatch, caplog):
    """Sonde None (nvidia-smi absent) → aucun warning, status() inchangé."""
    old = FakeModelDef("old", 10.0)
    manager = make_manager(monkeypatch, [old])
    add_loaded(manager, old, active_requests=0)

    async def fake_probe():
        return None

    monkeypatch.setattr(model_manager_module, "probe_gpu_used_mb", fake_probe)

    with caplog.at_level(logging.WARNING, logger="model_manager"):
        await manager._reconcile_vram_once()

    assert not any("dérive" in r.message.lower() for r in caplog.records)

    budget = manager.status()["vram_budget"]
    # Aucune clé additive trompeuse quand la sonde n'a rien renvoyé.
    assert "gpu_used_mb_measured" not in budget
    assert "vram_drift_mb" not in budget


@pytest.mark.anyio
async def test_vram_reconcile_no_drift_no_warning(capacity_settings, monkeypatch, caplog):
    """VRAM réelle proche du déclaré → status enrichi mais aucun warning."""
    old = FakeModelDef("old", 10.0)  # 10240 Mo déclarés
    manager = make_manager(monkeypatch, [old])
    add_loaded(manager, old, active_requests=0)

    async def fake_probe():
        return 10300.0  # +60 Mo seulement, sous le seuil

    monkeypatch.setattr(model_manager_module, "probe_gpu_used_mb", fake_probe)

    with caplog.at_level(logging.WARNING, logger="model_manager"):
        await manager._reconcile_vram_once()

    assert not any("dérive" in r.message.lower() for r in caplog.records)
    assert manager.status()["vram_budget"]["gpu_used_mb_measured"] == pytest.approx(10300.0)


@pytest.mark.anyio
async def test_probe_gpu_used_mb_returns_none_without_nvidia_smi(monkeypatch):
    """La sonde réelle retourne None si nvidia-smi est absent (attrape tout)."""
    async def boom(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    assert await model_manager_module.probe_gpu_used_mb() is None


# ── 3. Détection des ports orphelins ──────────────────────────────────────────

@pytest.mark.anyio
async def test_detect_orphan_ports_warns_without_killing(capacity_settings, monkeypatch, caplog):
    """Un port du pool occupé → warning loggé, RIEN n'est tué (défaut)."""
    assert settings.kill_orphan_llama_on_startup is False

    manager = make_manager(monkeypatch, [])

    # Ouvre un vrai socket d'écoute sur un port libre et l'injecte dans le pool.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    busy_port = listener.getsockname()[1]

    try:
        monkeypatch.setattr(manager, "_port_pool", [busy_port])
        with caplog.at_level(logging.WARNING, logger="model_manager"):
            occupied = await manager.detect_orphan_ports()

        assert occupied == [busy_port]
        assert any("orphelin" in r.message.lower() for r in caplog.records)
    finally:
        listener.close()


@pytest.mark.anyio
async def test_detect_orphan_ports_empty_when_free(capacity_settings, monkeypatch):
    """Ports libres (cas des tests) → aucune détection, retour immédiat."""
    manager = make_manager(monkeypatch, [])
    # Le pool par défaut pointe sur des ports non écoutés → rien d'occupé.
    occupied = await manager.detect_orphan_ports()
    assert occupied == []
