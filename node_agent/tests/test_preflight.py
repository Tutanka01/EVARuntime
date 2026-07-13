"""Tests du préflight de déploiement node-agent."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from config import AgentSettings
from preflight import (
    load_settings_file,
    local_health_url,
    validate_runtime_files,
    validate_sensitive_file,
)


def test_preflight_accepts_complete_runtime(tmp_path):
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o750)
    cert = tmp_path / "agent.crt"
    key = tmp_path / "agent.key"
    cert.write_text("test-cert", encoding="utf-8")
    key.write_text("test-key", encoding="utf-8")
    key.chmod(0o640)
    models = tmp_path / "models"
    models.mkdir()

    configured = AgentSettings(
        _env_file=None,
        agent_secret="a" * 32,
        internal_api_key="b" * 32,
        llama_server_bin=binary,
        agent_tls_cert=cert,
        agent_tls_key=key,
        allowed_model_dirs=str(models),
    )

    assert validate_runtime_files(configured) == []


def test_preflight_reports_missing_binary_tls_and_model_dir(tmp_path):
    configured = AgentSettings(
        _env_file=None,
        agent_secret="a" * 32,
        internal_api_key="b" * 32,
        llama_server_bin=tmp_path / "missing-llama",
        agent_tls_cert=tmp_path / "missing.crt",
        agent_tls_key=tmp_path / "missing.key",
        allowed_model_dirs=str(tmp_path / "missing-models"),
    )

    errors = validate_runtime_files(configured)
    assert any("llama-server introuvable" in error for error in errors)
    assert any("certificat TLS introuvable" in error for error in errors)
    assert any("clé TLS introuvable" in error for error in errors)
    assert any("répertoire de modèles introuvable" in error for error in errors)


def test_environment_file_is_not_overridden_by_calling_shell(tmp_path, monkeypatch):
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        "NODE_ID=node-from-file\n"
        f"AGENT_SECRET={'a' * 32}\n"
        f"INTERNAL_API_KEY={'b' * 32}\n"
        "ALLOWED_MODEL_DIRS=/models,/srv/gguf\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NODE_ID", "ambient-shell-value")

    configured = load_settings_file(env_file)

    assert configured.node_id == "node-from-file"
    assert configured.allowed_model_dirs_list() == ["/models", "/srv/gguf"]


def test_preflight_cli_ignores_invalid_ambient_settings(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    cert = tmp_path / "agent.crt"
    key = tmp_path / "agent.key"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    key.chmod(0o640)
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        "NODE_ID=node-from-file\n"
        f"AGENT_SECRET={'a' * 32}\n"
        f"INTERNAL_API_KEY={'b' * 32}\n"
        f"LLAMA_SERVER_BIN={sys.executable}\n"
        f"AGENT_TLS_CERT={cert}\n"
        f"AGENT_TLS_KEY={key}\n"
        f"ALLOWED_MODEL_DIRS={models}\n",
        encoding="utf-8",
    )
    env_file.chmod(0o640)
    process_env = os.environ.copy()
    process_env["AGENT_PORT"] = "valeur-ambiante-invalide"

    completed = subprocess.run(
        [sys.executable, "preflight.py", "--env", str(env_file)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=process_env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "node-from-file" in completed.stdout


def test_sensitive_files_reject_world_readable_and_group_writable(tmp_path):
    secret_file = tmp_path / "secret"
    secret_file.write_text("secret", encoding="utf-8")

    secret_file.chmod(0o644)
    assert "permissions trop ouvertes" in validate_sensitive_file(secret_file, "secret")[0]

    secret_file.chmod(0o660)
    assert "permissions trop ouvertes" in validate_sensitive_file(secret_file, "secret")[0]

    secret_file.chmod(0o640)
    assert validate_sensitive_file(secret_file, "secret") == []


def test_local_health_url_handles_wildcard_and_ipv6():
    assert local_health_url(AgentSettings(_env_file=None, agent_host="0.0.0.0")) == (
        "https://127.0.0.1:9443/agent/health"
    )
    assert local_health_url(AgentSettings(_env_file=None, agent_host="::")) == (
        "https://[::1]:9443/agent/health"
    )
