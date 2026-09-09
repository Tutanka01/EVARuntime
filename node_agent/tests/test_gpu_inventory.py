"""Contrat GPU du node-agent, sans dépendre d'un hôte NVIDIA réel."""
from __future__ import annotations

import asyncio

import main
from gpu_inventory import GpuVramMeasurement, GpuVramSample


def _samples() -> tuple[GpuVramSample, ...]:
    return (
        GpuVramSample(
            index=0,
            uuid="GPU-aaa",
            name="NVIDIA A",
            memory_used_mb=100.0,
            memory_total_mb=1000.0,
        ),
        GpuVramSample(
            index=1,
            uuid="GPU-bbb",
            name="NVIDIA B",
            memory_used_mb=900.0,
            memory_total_mb=2000.0,
        ),
    )


def test_state_passes_configured_cuda_scope_to_shared_probe(monkeypatch):
    calls: list[dict[str, object]] = []
    measurement = GpuVramMeasurement.measured(
        "1", _samples(), ("GPU-bbb",)
    )

    async def fake_probe(timeout, *, cuda_visible_devices=None):
        calls.append(
            {
                "timeout": timeout,
                "cuda_visible_devices": cuda_visible_devices,
            }
        )
        return measurement

    monkeypatch.setattr(main.settings, "cuda_visible_devices", "1")
    monkeypatch.setattr(main, "probe_gpu_memory", fake_probe)
    state = main._AgentState()

    result = asyncio.run(state.refresh_gpu_measurement(force=True))

    assert result.status == "measured"
    assert result.visible_uuids == ("GPU-bbb",)
    assert result.visible_used_mb == 900.0
    assert result.visible_total_mb == 2000.0
    assert calls == [
        {
            "timeout": main.settings.gpu_probe_timeout_seconds,
            "cuda_visible_devices": "1",
        }
    ]
    health = state.health()
    assert health.gpu_measurement is not None
    assert health.gpu_measurement.visible_uuids == ["GPU-bbb"]


def test_state_caches_measurement_for_health_heartbeats(monkeypatch):
    calls = 0
    measurement = GpuVramMeasurement.unavailable(
        "nvidia_smi_unavailable", "0"
    )

    async def fake_probe(*args, **kwargs):
        nonlocal calls
        calls += 1
        return measurement

    monkeypatch.setattr(main, "probe_gpu_memory", fake_probe)
    state = main._AgentState()

    async def scenario():
        first = await state.refresh_gpu_measurement()
        second = await state.refresh_gpu_measurement()
        return first, second

    first, second = asyncio.run(scenario())

    assert calls == 1
    assert first is second
    assert state.gpu_measurement().to_dict()["status"] == "unavailable"
    assert state.gpu_measurement().visible_used_mb is None


def test_health_only_reads_snapshot(monkeypatch):
    """Le heartbeat ne doit pas attendre une sonde potentiellement lente."""
    state = main._AgentState()
    state._gpu_measurement = GpuVramMeasurement.unavailable(
        "nvidia_smi_timeout", "0"
    )

    async def probe_that_must_not_run(*args, **kwargs):
        raise AssertionError("/health ne doit pas lancer la sonde")

    monkeypatch.setattr(main, "probe_gpu_memory", probe_that_must_not_run)

    health = state.health()

    assert health.status == "ok"
    assert state.gpu_measurement().reason == "nvidia_smi_timeout"


def test_gpu_endpoint_exposes_uuid_inventory_and_scope(monkeypatch):
    measurement = GpuVramMeasurement.measured(
        "1", _samples(), ("GPU-bbb",)
    )

    async def fake_probe(*args, **kwargs):
        return measurement

    monkeypatch.setattr(main.settings, "cuda_visible_devices", "1")
    monkeypatch.setattr(main, "probe_gpu_memory", fake_probe)
    monkeypatch.setattr(main.settings, "agent_secret", "s3cret-fort-de-test-assez-long-123")
    monkeypatch.setattr(main.settings, "internal_api_key", "b" * 32)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        response = client.get(
            "/agent/gpus",
            headers={
                "Authorization": "Bearer s3cret-fort-de-test-assez-long-123"
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "measured"
    assert body["cuda_visible_devices"] == "1"
    assert body["visible_uuids"] == ["GPU-bbb"]
    assert [device["uuid"] for device in body["devices"]] == [
        "GPU-aaa", "GPU-bbb"
    ]
    assert [device["visible"] for device in body["devices"]] == [False, True]
    assert body["visible_used_mb"] == 900.0
    assert body["visible_total_mb"] == 2000.0


def test_gpu_endpoint_keeps_unavailable_state_explicit(monkeypatch):
    async def unavailable_probe(*args, **kwargs):
        return GpuVramMeasurement.unavailable(
            "nvidia_smi_unavailable", main.settings.cuda_visible_devices
        )

    monkeypatch.setattr(main, "probe_gpu_memory", unavailable_probe)
    monkeypatch.setattr(main.settings, "agent_secret", "s3cret-fort-de-test-assez-long-123")
    monkeypatch.setattr(main.settings, "internal_api_key", "b" * 32)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        response = client.get(
            "/agent/gpus",
            headers={
                "Authorization": "Bearer s3cret-fort-de-test-assez-long-123"
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["reason"] == "nvidia_smi_unavailable"
    assert body["visible_used_mb"] is None
    assert body["visible_total_mb"] is None


def test_background_probe_can_be_disabled(monkeypatch):
    monkeypatch.setattr(main.settings, "gpu_probe_interval_seconds", 0.0)
    state = main._AgentState()

    async def scenario():
        state.start_gpu_measurement_monitor()
        assert state._gpu_measurement_task is None
        await state.shutdown()

    asyncio.run(scenario())


def test_background_probe_retries_after_unexpected_failure(monkeypatch):
    monkeypatch.setattr(main.settings, "gpu_probe_interval_seconds", 0.0)
    state = main._AgentState()
    calls = 0

    async def flaky_refresh(*, force=False):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        state._closing = True
        return state.gpu_measurement()

    state.refresh_gpu_measurement = flaky_refresh

    asyncio.run(state._gpu_measurement_loop())

    assert calls == 2


def test_agent_admission_is_capped_by_measured_free_vram(monkeypatch):
    monkeypatch.setattr(
        type(main.settings), "effective_vram_budget_gb", lambda self: 10.0
    )
    state = main._AgentState()
    state._gpu_measurement = GpuVramMeasurement.measured(
        "1", _samples(), ("GPU-bbb",)
    )

    assert state._available_vram() == 1100.0 / 1024.0


def test_agent_admission_falls_back_to_config_when_probe_unavailable(monkeypatch):
    monkeypatch.setattr(
        type(main.settings), "effective_vram_budget_gb", lambda self: 10.0
    )
    state = main._AgentState()

    assert state._available_vram() == 10.0
