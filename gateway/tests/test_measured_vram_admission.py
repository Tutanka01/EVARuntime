"""
Admission locale plafonnée par la VRAM MESURÉE : la mesure ne doit jamais être
périmée au moment où elle décide.

`_available_vram_gb()` prend le minimum du budget déclaré et de la VRAM libre
relevée par la sonde périodique (60 s par défaut). Cette mesure compte encore
la mémoire d'un modèle évincé ou déchargé depuis. Défaut observé en production
après une mise à jour : changer de modèle évinçait l'ancien, la mesure périmée
annonçait toujours un GPU plein, et la requête attendait tout le timeout de la
queue (120 s) avant d'échouer en 503 — devant un GPU libre.

Contrat vérifié ici : quand seule la mesure bloque, la gateway re-sonde avant
d'évincer ou d'attendre ; elle ne sonde pas sur le chemin nominal ; et le
plafond physique reste appliqué quand le GPU est réellement occupé.
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

import gpu_inventory
import model_manager as model_manager_module
from config import settings
from model_manager import CapacityQueueTimeout, LocalModelManager
from server_manager import ModelState
from tests.test_local_capacity_queue import FakeModelDef, add_loaded, make_manager


class FakeGpu:
    """GPU simulé : la mémoire utilisée suit les modèles réellement chargés."""

    def __init__(
        self,
        manager: LocalModelManager,
        *,
        total_gb: float,
        foreign_gb: float = 0.0,
    ) -> None:
        self._manager = manager
        self.total_gb = total_gb
        self.foreign_gb = foreign_gb
        self.probes = 0
        # Nombre de sondes qui renvoient encore l'ancien état (latence du
        # pilote à rendre la mémoire d'un processus tué).
        self.lagging_probes = 0
        self._lagging_used_gb = 0.0

    def used_gb(self) -> float:
        loaded = sum(
            server.model.vram_gb
            for server in self._manager._managers.values()
            if server.state in (ModelState.READY, ModelState.LOADING)
        )
        return loaded + self.foreign_gb

    def measurement(self, used_gb: float | None = None):
        used = self.used_gb() if used_gb is None else used_gb
        csv = (
            f"0, GPU-aaa, NVIDIA A, {used * 1024:.0f}, {self.total_gb * 1024:.0f}, "
            "550.54.15, 8.9\n"
        )
        samples = tuple(gpu_inventory.parse_nvidia_smi_csv(csv))
        return gpu_inventory.GpuVramMeasurement.measured("0", samples, ("GPU-aaa",))

    def lag(self, probes: int) -> None:
        self.lagging_probes = probes
        self._lagging_used_gb = self.used_gb()

    async def probe(self):
        self.probes += 1
        if self.lagging_probes > 0:
            self.lagging_probes -= 1
            return self.measurement(self._lagging_used_gb)
        return self.measurement()


@pytest.fixture
def vram_settings(monkeypatch):
    monkeypatch.setattr(settings, "total_vram_gb", 30.0)
    monkeypatch.setattr(settings, "vram_overhead_gb", 0.0)
    monkeypatch.setattr(settings, "vram_safety_margin", 0.0)
    monkeypatch.setattr(settings, "max_loaded_models", 3)
    monkeypatch.setattr(settings, "base_llama_port", 18081)
    monkeypatch.setattr(settings, "capacity_queue_enabled", True)
    monkeypatch.setattr(settings, "capacity_queue_timeout_seconds", 1)
    monkeypatch.setattr(settings, "capacity_queue_max_waiters", 10)
    monkeypatch.setattr(settings, "capacity_queue_retry_after_seconds", 10)
    monkeypatch.setattr(model_manager_module, "GPU_REPROBE_INTERVAL_SECONDS", 0.05)


def install_gpu(monkeypatch, manager: LocalModelManager, **kwargs) -> FakeGpu:
    """Branche le GPU simulé et pose la mesure de la dernière sonde périodique."""
    gpu = FakeGpu(manager, **kwargs)
    monkeypatch.setattr(model_manager_module, "_probe_gpu_measurement", gpu.probe)
    manager._last_gpu_measurement = gpu.measurement()
    return gpu


@pytest.mark.anyio
async def test_stale_measurement_after_eviction_does_not_queue(vram_settings, monkeypatch):
    """Le défaut de production : éviction, puis attente devant un GPU libre."""
    old = FakeModelDef("old", 25.0)
    new = FakeModelDef("new", 25.0)
    manager = make_manager(monkeypatch, [old, new])
    add_loaded(manager, old, last_request_time=time.monotonic() - 100)
    gpu = install_gpu(monkeypatch, manager, total_gb=30.0)

    started = time.monotonic()
    server = await manager.ensure_model_loaded("new")
    elapsed = time.monotonic() - started

    assert server.model.id == "new"
    assert "old" not in manager._managers
    assert elapsed < 0.5, f"admission retardée de {elapsed:.2f}s par une mesure périmée"
    assert manager.status()["capacity_queue"]["waiters"] == 0
    assert gpu.probes == 1, "une seule re-sonde, juste après l'éviction"


@pytest.mark.anyio
async def test_stale_measurement_does_not_evict_more_than_needed(vram_settings, monkeypatch):
    """Une éviction suffit : la mesure périmée ne doit pas en réclamer une seconde."""
    lru = FakeModelDef("lru", 10.0)
    keep = FakeModelDef("keep", 10.0)
    new = FakeModelDef("new", 15.0)
    manager = make_manager(monkeypatch, [lru, keep, new])
    add_loaded(manager, lru, last_request_time=time.monotonic() - 200)
    add_loaded(manager, keep, last_request_time=time.monotonic() - 100)
    install_gpu(monkeypatch, manager, total_gb=30.0)

    await manager.ensure_model_loaded("new")

    assert "lru" not in manager._managers
    assert "keep" in manager._managers, "modèle évincé à tort sur une mesure périmée"


@pytest.mark.anyio
async def test_stale_measurement_after_idle_unload_evicts_nothing(vram_settings, monkeypatch):
    """La mesure date d'avant un déchargement par inactivité : re-sonder d'abord."""
    gone = FakeModelDef("gone", 20.0)
    keep = FakeModelDef("keep", 5.0)
    new = FakeModelDef("new", 20.0)
    manager = make_manager(monkeypatch, [gone, keep, new])
    gone_server = add_loaded(manager, gone)
    add_loaded(manager, keep, last_request_time=time.monotonic() - 100)
    gpu = install_gpu(monkeypatch, manager, total_gb=30.0)  # mesure : 25 Go pris
    await gone_server.unload(reason="inactivité")

    await manager.ensure_model_loaded("new")

    assert "keep" in manager._managers, "éviction inutile : le GPU avait déjà la place"
    assert gpu.probes == 1


@pytest.mark.anyio
async def test_driver_release_lag_is_polled_not_timed_out(vram_settings, monkeypatch):
    """Le pilote rend la mémoire avec retard : re-sonder, ne pas attendre le timeout."""
    old = FakeModelDef("old", 25.0)
    new = FakeModelDef("new", 25.0)
    manager = make_manager(monkeypatch, [old, new])
    add_loaded(manager, old, last_request_time=time.monotonic() - 100)
    gpu = install_gpu(monkeypatch, manager, total_gb=30.0)
    gpu.lag(3)  # les trois prochaines sondes voient encore "old"

    started = time.monotonic()
    server = await manager.ensure_model_loaded("new")
    elapsed = time.monotonic() - started

    assert server.model.id == "new"
    assert gpu.probes == 4
    assert elapsed < settings.capacity_queue_timeout_seconds
    assert manager.status()["capacity_queue"]["waiters"] == 0


@pytest.mark.anyio
async def test_physical_cap_still_blocks_when_gpu_is_really_busy(
    vram_settings, monkeypatch, caplog
):
    """Contrôle positif : un GPU occupé par un tiers refuse toujours l'admission."""
    new = FakeModelDef("new", 25.0)
    manager = make_manager(monkeypatch, [new])
    gpu = install_gpu(monkeypatch, manager, total_gb=30.0, foreign_gb=20.0)

    with caplog.at_level(logging.WARNING, logger="model_manager"):
        with pytest.raises(CapacityQueueTimeout):
            await manager.ensure_model_loaded("new")

    assert "new" not in manager._managers
    assert gpu.probes >= 2, "la tête de queue doit re-sonder pendant l'attente"
    warnings = [r for r in caplog.records if "Capacité insuffisante" in r.getMessage()]
    assert len(warnings) == 1, "un seul avertissement par motif, pas un par re-sonde"


@pytest.mark.anyio
async def test_busy_gpu_admits_as_soon_as_foreign_memory_is_released(
    vram_settings, monkeypatch
):
    new = FakeModelDef("new", 25.0)
    manager = make_manager(monkeypatch, [new])
    gpu = install_gpu(monkeypatch, manager, total_gb=30.0, foreign_gb=20.0)

    task = asyncio.create_task(manager.ensure_model_loaded("new"))
    await asyncio.sleep(0.15)
    assert not task.done()
    gpu.foreign_gb = 0.0

    server = await asyncio.wait_for(task, timeout=0.5)
    assert server.model.id == "new"


@pytest.mark.anyio
async def test_no_probe_when_measurement_already_admits(vram_settings, monkeypatch):
    new = FakeModelDef("new", 10.0)
    manager = make_manager(monkeypatch, [new])
    gpu = install_gpu(monkeypatch, manager, total_gb=30.0)

    await manager.ensure_model_loaded("new")

    assert gpu.probes == 0, "aucune sonde nvidia-smi sur le chemin nominal"


@pytest.mark.anyio
async def test_no_probe_when_declared_budget_is_the_blocker(vram_settings, monkeypatch):
    """Un modèle épinglé bloque par le budget déclaré : la mesure n'y change rien."""
    busy = FakeModelDef("busy", 25.0)
    new = FakeModelDef("new", 25.0)
    manager = make_manager(monkeypatch, [busy, new])
    add_loaded(manager, busy, active_requests=1)
    gpu = install_gpu(monkeypatch, manager, total_gb=30.0)

    with pytest.raises(CapacityQueueTimeout):
        await manager.ensure_model_loaded("new")

    assert "busy" in manager._managers
    assert gpu.probes == 0


@pytest.mark.anyio
async def test_no_probe_without_a_measurement(vram_settings, monkeypatch):
    """Sonde indisponible (pas de nvidia-smi) : budget déclaré seul, comme avant."""
    old = FakeModelDef("old", 25.0)
    new = FakeModelDef("new", 25.0)
    manager = make_manager(monkeypatch, [old, new])
    add_loaded(manager, old, last_request_time=time.monotonic() - 100)
    gpu = FakeGpu(manager, total_gb=30.0)
    monkeypatch.setattr(model_manager_module, "_probe_gpu_measurement", gpu.probe)

    server = await manager.ensure_model_loaded("new")

    assert server.model.id == "new"
    assert gpu.probes == 0
