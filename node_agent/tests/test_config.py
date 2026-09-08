"""Validation de la configuration réseau et sécurité du node-agent."""
from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from config import AgentSettings


def make_settings(**overrides) -> AgentSettings:
    values = {
        "agent_secret": "a" * 32,
        "internal_api_key": "b" * 32,
    }
    values.update(overrides)
    return AgentSettings(_env_file=None, **values)


def test_production_defaults_bind_data_plane_and_probe_loopback():
    configured = make_settings()

    assert configured.llama_server_host == "0.0.0.0"
    assert configured.llama_server_health_host == "127.0.0.1"
    configured.validate_runtime_security()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_secret", "CHANGE_ME_AGENT_SECRET"),
        ("agent_secret", "short"),
        ("internal_api_key", "CHANGE_ME_INTERNAL_KEY"),
        ("internal_api_key", "short"),
    ],
)
def test_runtime_security_rejects_placeholder_and_short_secrets(field, value):
    configured = make_settings(**{field: value})

    with pytest.raises(RuntimeError, match=field.upper()):
        configured.validate_runtime_security()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_runtime_security_rejects_loopback_data_plane(host):
    configured = make_settings(llama_server_host=host)

    with pytest.raises(RuntimeError, match="joignable"):
        configured.validate_runtime_security()


def test_runtime_security_requires_tls_pair():
    configured = make_settings(agent_tls_cert=None, agent_tls_key=None)

    with pytest.raises(RuntimeError, match="TLS"):
        configured.validate_runtime_security()


def test_rejects_control_and_data_plane_port_overlap():
    with pytest.raises(ValidationError, match="chevaucher"):
        make_settings(agent_port=8083, base_llama_port=8081, max_loaded_models=5)


def test_rejects_data_plane_port_range_overflow():
    with pytest.raises(ValidationError, match="dépasse 65535"):
        make_settings(base_llama_port=65534, max_loaded_models=3)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_port", 0),
        ("agent_port", 65536),
        ("base_llama_port", 0),
        ("base_llama_port", 65536),
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
    ],
)
def test_rejects_out_of_range_scalar_settings(field, value):
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_rejects_boolean_disguised_as_numeric_setting():
    with pytest.raises(ValidationError, match="valeur booléenne"):
        make_settings(total_vram_gb=True)


def test_rejects_non_positive_net_vram_budget():
    with pytest.raises(ValidationError, match="budget VRAM net"):
        make_settings(total_vram_gb=10.0, vram_overhead_gb=9.5, vram_safety_margin=0.05)


def test_rejects_overhead_equal_to_total_vram():
    with pytest.raises(ValidationError, match="VRAM_OVERHEAD_GB"):
        make_settings(total_vram_gb=10.0, vram_overhead_gb=10.0, vram_safety_margin=0.0)


def test_rejects_idle_poll_interval_longer_than_idle_timeout():
    with pytest.raises(ValidationError, match="IDLE_CHECK_INTERVAL_SECONDS"):
        make_settings(idle_timeout_seconds=10, idle_check_interval_seconds=11)


def test_rejects_relative_allowed_model_directory():
    with pytest.raises(ValidationError, match="chemins absolus"):
        make_settings(allowed_model_dirs="models")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/models", ["/models"]),
        ("/models,/srv/gguf", ["/models", "/srv/gguf"]),
        ('["/models", "/srv/gguf"]', ["/models", "/srv/gguf"]),
    ],
)
def test_allowed_model_dirs_accepts_single_csv_and_json_env(monkeypatch, raw, expected):
    monkeypatch.setenv("ALLOWED_MODEL_DIRS", raw)
    configured = AgentSettings(
        _env_file=None,
        agent_secret="a" * 32,
        internal_api_key="b" * 32,
    )

    assert configured.allowed_model_dirs == expected
    assert configured.allowed_model_dirs_list() == expected


def test_allowed_model_dirs_empty_environment_value_is_valid(monkeypatch):
    monkeypatch.setenv("ALLOWED_MODEL_DIRS", "")
    configured = AgentSettings(
        _env_file=None,
        agent_secret="a" * 32,
        internal_api_key="b" * 32,
    )

    assert configured.allowed_model_dirs == []
    assert configured.allowed_model_dirs_list() == []


@pytest.mark.parametrize("node_id", ["Node-A", "a" * 64])
def test_node_id_matches_orchestrator_grammar(node_id):
    with pytest.raises(ValidationError, match="node_id"):
        make_settings(node_id=node_id)
