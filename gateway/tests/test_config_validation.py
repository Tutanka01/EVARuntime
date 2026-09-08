"""Bornes et invariants croisés de la configuration de la gateway (CFG-001)."""
from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from config import Settings
from schemas import UserCreate, UserUpdate


def make_settings(**overrides: object) -> Settings:
    """Construit une configuration isolée de l'environnement du runner."""
    return Settings(_env_file=None, **overrides)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llama_server_min_build", -1),
        ("base_llama_port", 0),
        ("base_llama_port", 65536),
        ("gateway_port", 0),
        ("gateway_port", 65536),
        ("max_loaded_models", 0),
        ("total_vram_gb", 0),
        ("total_vram_gb", -1),
        ("total_vram_gb", math.inf),
        ("vram_overhead_gb", -1),
        ("vram_overhead_gb", math.inf),
        ("vram_safety_margin", -0.01),
        ("vram_safety_margin", 1.0),
        ("idle_timeout_seconds", 0),
        ("model_load_timeout_seconds", 0),
        ("idle_check_interval_seconds", 0),
        ("capacity_queue_timeout_seconds", 0),
        ("capacity_queue_max_waiters", 0),
        ("capacity_queue_retry_after_seconds", 0),
        ("default_rpm_limit", 0),
        ("default_rpm_limit", 1001),
        ("default_monthly_token_limit", -1),
        ("default_monthly_token_limit", 2**63),
        ("cluster_request_timeout", 0),
        ("cluster_load_timeout", 0),
        ("cluster_health_interval", 0),
        ("cluster_health_failures_to_offline", 0),
    ],
)
def test_rejects_out_of_range_scalar_settings(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_rejects_boolean_disguised_as_numeric_setting() -> None:
    with pytest.raises(ValidationError, match="valeur booléenne"):
        make_settings(total_vram_gb=True)


def test_rejects_non_positive_net_vram_budget() -> None:
    with pytest.raises(ValidationError, match="budget VRAM net"):
        make_settings(total_vram_gb=10.0, vram_overhead_gb=9.5, vram_safety_margin=0.05)


def test_rejects_overhead_equal_to_total_vram() -> None:
    with pytest.raises(ValidationError, match="VRAM_OVERHEAD_GB"):
        make_settings(total_vram_gb=10.0, vram_overhead_gb=10.0, vram_safety_margin=0.0)


def test_rejects_data_plane_port_pool_overflow() -> None:
    with pytest.raises(ValidationError, match="plage.*dépasse 65535"):
        make_settings(base_llama_port=65534, max_loaded_models=3)


def test_rejects_gateway_and_data_plane_port_collision() -> None:
    with pytest.raises(ValidationError, match="GATEWAY_PORT.*chevaucher"):
        make_settings(gateway_port=8083, base_llama_port=8081, max_loaded_models=5)


def test_rejects_idle_poll_interval_longer_than_idle_timeout() -> None:
    with pytest.raises(ValidationError, match="IDLE_CHECK_INTERVAL_SECONDS"):
        make_settings(idle_timeout_seconds=10, idle_check_interval_seconds=11)


def test_rejects_cluster_load_timeout_shorter_than_other_load_bounds() -> None:
    with pytest.raises(ValidationError, match="CLUSTER_LOAD_TIMEOUT"):
        make_settings(
            cluster_mode="cluster",
            cluster_request_timeout=20.0,
            cluster_load_timeout=10.0,
        )

    with pytest.raises(ValidationError, match="MODEL_LOAD_TIMEOUT_SECONDS"):
        make_settings(
            cluster_mode="cluster",
            model_load_timeout_seconds=180,
            cluster_load_timeout=179.0,
        )


def test_local_mode_does_not_apply_cluster_timeout_invariants() -> None:
    configured = make_settings(model_load_timeout_seconds=600, cluster_load_timeout=1.0)
    assert configured.model_load_timeout_seconds == 600


def test_rejects_keepalive_pool_larger_than_finite_connection_pool() -> None:
    with pytest.raises(ValidationError, match="HTTPX_MAX_KEEPALIVE"):
        make_settings(httpx_max_connections=10, httpx_max_keepalive=11)


def test_allows_httpx_unlimited_total_pool() -> None:
    configured = make_settings(httpx_max_connections=0, httpx_max_keepalive=100)
    assert configured.httpx_max_connections == 0


def test_allows_documented_zero_disable_values() -> None:
    configured = make_settings(
        shutdown_drain_timeout_seconds=0,
        admin_unload_drain_timeout_seconds=0,
        shutdown_background_flush_seconds=0,
        vram_reconcile_interval_seconds=0,
        readiness_cache_ttl_seconds=0,
        default_monthly_token_limit=0,
    )
    assert configured.shutdown_drain_timeout_seconds == 0
    assert configured.vram_reconcile_interval_seconds == 0


def test_rejects_non_positive_poll_and_probe_timeouts() -> None:
    with pytest.raises(ValidationError, match="shutdown_drain_poll_seconds"):
        make_settings(shutdown_drain_poll_seconds=0)
    with pytest.raises(ValidationError, match="vram_reconcile_probe_timeout_seconds"):
        make_settings(vram_reconcile_probe_timeout_seconds=0)


def test_rejects_poll_interval_that_would_break_drain_deadline() -> None:
    with pytest.raises(ValidationError, match="SHUTDOWN_DRAIN_POLL_SECONDS"):
        make_settings(shutdown_drain_timeout_seconds=0.1, shutdown_drain_poll_seconds=0.2)


def test_defaults_are_a_valid_configuration() -> None:
    configured = make_settings()
    assert configured.effective_vram_budget_gb() == pytest.approx(43.6)
    assert configured.base_llama_port + configured.max_loaded_models - 1 == 8085


@pytest.mark.parametrize("schema", [UserCreate, UserUpdate])
@pytest.mark.parametrize("value", [-1, 2**63, True, "100"])
def test_user_monthly_quota_respects_sqlite_integer_contract(schema, value) -> None:
    payload = {"monthly_token_limit": value}
    if schema is UserCreate:
        payload["username"] = "quota-test"
    with pytest.raises(ValidationError):
        schema.model_validate(payload)
