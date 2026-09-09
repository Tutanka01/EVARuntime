"""Tests de la sonde VRAM locale par UUID et par périmètre CUDA."""
from __future__ import annotations

import asyncio

import pytest

import gpu_inventory
import model_manager as model_manager_module
from model_manager import LocalModelManager


NVIDIA_OUTPUT = (
    "0, GPU-aaa, NVIDIA A, 100, 1000, 550.54.15, 8.9\n"
    "1, GPU-bbb, NVIDIA B, 900, 2000, 550.54.15, 8.9\n"
)


class _Process:
    returncode = 0

    def __init__(self, stdout: str = NVIDIA_OUTPUT):
        self.stdout = stdout
        self.killed = False

    async def communicate(self):
        return self.stdout, ""

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def test_parser_keeps_uuid_and_memory_values_per_device():
    samples = gpu_inventory.parse_nvidia_smi_csv(NVIDIA_OUTPUT)

    assert [sample.uuid for sample in samples] == ["GPU-aaa", "GPU-bbb"]
    assert [sample.memory_used_mb for sample in samples] == [100.0, 900.0]
    assert [sample.memory_total_mb for sample in samples] == [1000.0, 2000.0]
    assert samples[0].memory_total_bytes == 1000 * 1024 * 1024


def test_resolve_scope_distinguishes_unset_empty_and_uuid():
    samples = gpu_inventory.parse_nvidia_smi_csv(NVIDIA_OUTPUT)

    all_devices = gpu_inventory.resolve_visible_devices(samples, None)
    assert all_devices.declared is False
    assert [device.uuid for device in all_devices.devices] == ["GPU-aaa", "GPU-bbb"]

    empty = gpu_inventory.resolve_visible_devices(samples, " ")
    assert empty.declared is True
    assert empty.devices == ()

    by_uuid = gpu_inventory.resolve_visible_devices(samples, "GPU-bbb")
    assert [device.uuid for device in by_uuid.devices] == ["GPU-bbb"]

    ordered = gpu_inventory.resolve_visible_devices(samples, "1,0")
    assert [device.uuid for device in ordered.devices] == ["GPU-bbb", "GPU-aaa"]

    disabled = gpu_inventory.resolve_visible_devices(samples, "-1")
    assert disabled.declared is True
    assert disabled.devices == ()
    assert disabled.unresolved == ()

    malformed = gpu_inventory.resolve_visible_devices(samples, "0,,1")
    assert malformed.devices == ()
    assert malformed.unresolved == ("empty_token",)


def test_measurement_visible_devices_preserves_cuda_order():
    samples = tuple(gpu_inventory.parse_nvidia_smi_csv(NVIDIA_OUTPUT))

    measurement = gpu_inventory.GpuVramMeasurement.measured(
        "1,0", samples, ("GPU-bbb", "GPU-aaa")
    )

    assert [device.uuid for device in measurement.visible_devices] == [
        "GPU-bbb", "GPU-aaa"
    ]


@pytest.mark.anyio
async def test_probe_uses_uuid_and_only_aggregates_visible_devices(monkeypatch):
    calls: dict[str, object] = {}

    async def fake_create(*args, **kwargs):
        calls["args"] = args
        calls["env"] = kwargs["env"]
        return _Process()

    monkeypatch.setattr(gpu_inventory.asyncio, "create_subprocess_exec", fake_create)

    measurement = await gpu_inventory.probe_gpu_memory(
        cuda_visible_devices="1",
        env={"CUDA_VISIBLE_DEVICES": "0,1", "PATH": "/usr/bin"},
    )

    assert measurement.status == "measured"
    assert measurement.visible_uuids == ("GPU-bbb",)
    assert measurement.visible_used_mb == pytest.approx(900.0)
    assert measurement.visible_total_mb == pytest.approx(2000.0)
    assert [device.visible for device in measurement.devices] == [False, True]
    assert "uuid" in str(calls["args"][1])
    assert "memory.used" in str(calls["args"][1])
    assert "CUDA_VISIBLE_DEVICES" not in calls["env"]


@pytest.mark.anyio
async def test_probe_preserves_declared_cuda_device_order(monkeypatch):
    async def fake_create(*args, **kwargs):
        return _Process()

    monkeypatch.setattr(gpu_inventory.asyncio, "create_subprocess_exec", fake_create)

    measurement = await gpu_inventory.probe_gpu_memory(
        cuda_visible_devices="1,0"
    )

    assert measurement.visible_uuids == ("GPU-bbb", "GPU-aaa")
    assert [device.uuid for device in measurement.visible_devices] == [
        "GPU-bbb", "GPU-aaa",
    ]


@pytest.mark.anyio
async def test_invalid_scope_is_unavailable_and_never_aggregates_suffix(monkeypatch):
    async def fake_create(*args, **kwargs):
        return _Process()

    monkeypatch.setattr(gpu_inventory.asyncio, "create_subprocess_exec", fake_create)

    measurement = await gpu_inventory.probe_gpu_memory(cuda_visible_devices="0,7,1")

    assert measurement.status == "unavailable"
    assert measurement.reason == "cuda_visible_devices_invalid"
    assert measurement.visible_uuids == ("GPU-aaa",)
    assert measurement.visible_used_mb is None


@pytest.mark.anyio
async def test_missing_probe_has_explicit_state(monkeypatch):
    async def missing(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(gpu_inventory.asyncio, "create_subprocess_exec", missing)

    measurement = await gpu_inventory.probe_gpu_memory(cuda_visible_devices="0")

    assert measurement.status == "unavailable"
    assert measurement.reason == "nvidia_smi_unavailable"
    assert measurement.measured_at is not None
    assert measurement.to_dict()["status"] == "unavailable"


@pytest.mark.anyio
async def test_missing_visible_memory_is_not_reported_as_zero(monkeypatch):
    output = "0, GPU-aaa, NVIDIA A, [N/A], 1000, 550.54.15, 8.9\n"

    async def fake_create(*args, **kwargs):
        return _Process(output)

    monkeypatch.setattr(gpu_inventory.asyncio, "create_subprocess_exec", fake_create)

    measurement = await gpu_inventory.probe_gpu_memory(cuda_visible_devices="0")

    assert measurement.status == "unavailable"
    assert measurement.reason == "vram_used_unavailable"
    assert measurement.visible_used_mb is None


@pytest.mark.anyio
async def test_mig_is_explicitly_unsupported(monkeypatch):
    output = "0, MIG-GPU-aaa/GI/CI, NVIDIA A MIG, 100, 1000, 550.54.15, 8.9\n"

    async def fake_create(*args, **kwargs):
        return _Process(output)

    monkeypatch.setattr(gpu_inventory.asyncio, "create_subprocess_exec", fake_create)

    measurement = await gpu_inventory.probe_gpu_memory(cuda_visible_devices="0")

    assert measurement.status == "unavailable"
    assert measurement.reason == "mig_unsupported"


@pytest.mark.anyio
async def test_enabled_mig_mode_on_parent_is_explicitly_unsupported(monkeypatch):
    output = (
        "0, GPU-aaa, NVIDIA A, 100, 1000, 550.54.15, 8.9, Enabled\n"
    )

    async def fake_create(*args, **kwargs):
        return _Process(output)

    monkeypatch.setattr(gpu_inventory.asyncio, "create_subprocess_exec", fake_create)

    measurement = await gpu_inventory.probe_gpu_memory(cuda_visible_devices="0")

    assert measurement.status == "unavailable"
    assert measurement.reason == "mig_unsupported"
    assert measurement.devices[0].mig_mode_current == "Enabled"


@pytest.mark.anyio
async def test_mig_scope_is_rejected_even_when_query_only_reports_parent(monkeypatch):
    async def fake_create(*args, **kwargs):
        return _Process()

    monkeypatch.setattr(gpu_inventory.asyncio, "create_subprocess_exec", fake_create)

    measurement = await gpu_inventory.probe_gpu_memory(
        cuda_visible_devices="MIG-GPU-aaa/1/0"
    )

    assert measurement.status == "unavailable"
    assert measurement.reason == "mig_unsupported"
    assert measurement.visible_used_mb is None


@pytest.mark.anyio
async def test_probe_timeout_kills_process(monkeypatch):
    process = _Process()

    async def fake_create(*args, **kwargs):
        async def never():
            await asyncio.sleep(60)

        process.communicate = never
        return process

    monkeypatch.setattr(gpu_inventory.asyncio, "create_subprocess_exec", fake_create)

    measurement = await gpu_inventory.probe_gpu_memory(timeout=0.001)

    assert measurement.status == "unavailable"
    assert measurement.reason == "nvidia_smi_timeout"
    assert process.killed is True


class _Registry:
    def list_all(self):
        return []


@pytest.mark.anyio
async def test_manager_status_keeps_uuid_inventory_and_visible_aggregate(monkeypatch):
    samples = tuple(gpu_inventory.parse_nvidia_smi_csv(NVIDIA_OUTPUT))
    measured = gpu_inventory.GpuVramMeasurement.measured(
        "1", samples, ("GPU-bbb",)
    )

    async def fake_probe(*args, **kwargs):
        return measured

    monkeypatch.setattr(model_manager_module, "probe_gpu_memory", fake_probe)
    manager = LocalModelManager(_Registry())

    await manager._reconcile_vram_once()

    budget = manager.status()["vram_budget"]
    assert budget["gpu_used_mb_measured"] == pytest.approx(900.0)
    detail = budget["gpu_measurement"]
    assert detail["status"] == "measured"
    assert detail["visible_uuids"] == ["GPU-bbb"]
    assert [device["visible"] for device in detail["devices"]] == [False, True]


@pytest.mark.anyio
async def test_manager_status_replaces_stale_measurement_with_unavailable(monkeypatch):
    samples = tuple(gpu_inventory.parse_nvidia_smi_csv(NVIDIA_OUTPUT))
    results = iter((
        gpu_inventory.GpuVramMeasurement.measured("1", samples, ("GPU-bbb",)),
        gpu_inventory.GpuVramMeasurement.unavailable("nvidia_smi_timeout", "1"),
    ))

    async def fake_probe(*args, **kwargs):
        return next(results)

    monkeypatch.setattr(model_manager_module, "probe_gpu_memory", fake_probe)
    manager = LocalModelManager(_Registry())

    await manager._reconcile_vram_once()
    assert "gpu_used_mb_measured" in manager.status()["vram_budget"]

    await manager._reconcile_vram_once()
    budget = manager.status()["vram_budget"]
    assert "gpu_used_mb_measured" not in budget
    assert budget["gpu_measurement"]["status"] == "unavailable"
    assert budget["gpu_measurement"]["reason"] == "nvidia_smi_timeout"


def test_manager_admission_is_capped_by_measured_free_vram(monkeypatch):
    samples = tuple(gpu_inventory.parse_nvidia_smi_csv(NVIDIA_OUTPUT))
    monkeypatch.setattr(
        type(model_manager_module.settings),
        "effective_vram_budget_gb",
        lambda self: 10.0,
    )
    manager = LocalModelManager(_Registry())
    manager._last_gpu_measurement = gpu_inventory.GpuVramMeasurement.measured(
        "1", samples, ("GPU-bbb",)
    )

    assert manager._available_vram_gb() == pytest.approx(1100.0 / 1024.0)


def test_manager_admission_falls_back_to_config_when_probe_unavailable(monkeypatch):
    monkeypatch.setattr(
        type(model_manager_module.settings),
        "effective_vram_budget_gb",
        lambda self: 10.0,
    )
    manager = LocalModelManager(_Registry())

    assert manager._available_vram_gb() == pytest.approx(10.0)
