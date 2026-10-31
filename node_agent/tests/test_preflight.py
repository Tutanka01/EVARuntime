"""Tests du préflight de déploiement node-agent."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import preflight
from config import AgentSettings
from preflight import (
    load_settings_file,
    local_health_url,
    validate_runtime_files,
    validate_runtime_version,
    validate_sensitive_file,
)


def test_preflight_accepts_complete_runtime(tmp_path):
    binary = tmp_path / "llama-server"
    binary.write_text(
        "#!/bin/sh\necho 'version: 7000 (a1b2c3d)'\n",
        encoding="utf-8",
    )
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
        llama_server_min_build=6000,
    )

    assert validate_runtime_files(configured) == []
    assert asyncio.run(validate_runtime_version(configured)) == []


def test_check_local_health_epingle_le_certificat_et_ignore_les_proxies(
    monkeypatch, tmp_path
):
    """
    La sonde porte AGENT_SECRET : elle doit vérifier le certificat épinglé et
    ne jamais passer par HTTPS_PROXY (httpx route aussi 127.0.0.1/::1).
    """
    cert = tmp_path / "agent.crt"
    key = tmp_path / "agent.key"
    cert.write_text("certificat", encoding="utf-8")
    key.write_text("cle", encoding="utf-8")
    key.chmod(0o640)
    configured = AgentSettings(
        _env_file=None,
        agent_secret="a" * 32,
        internal_api_key="b" * 32,
        agent_tls_cert=cert,
        agent_tls_key=key,
        allowed_model_dirs="/models",
    )

    captured: dict = {}
    seen: dict = {}

    class FakeContext:
        check_hostname = True
        verify_flags = preflight.ssl.VerifyFlags(0)

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"status": "ok", "loaded_model_ids": []}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> bool:
            return False

        async def get(self, url: str, headers: dict | None = None):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    def fake_context(*, cafile: str):
        seen["cafile"] = cafile
        return FakeContext()

    monkeypatch.setattr(preflight.ssl, "create_default_context", fake_context)
    monkeypatch.setattr(preflight.httpx, "AsyncClient", FakeClient)

    asyncio.run(preflight.check_local_health(configured, 1.0))

    assert captured["trust_env"] is False
    assert captured["verify"].check_hostname is False
    assert seen["cafile"] == str(cert)
    assert captured["headers"] == {"Authorization": f"Bearer {configured.agent_secret}"}


def test_preflight_requires_positive_minimum_build(tmp_path):
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\necho 'version: 7000 (a1b2c3d)'\n", encoding="utf-8")
    binary.chmod(0o750)
    configured = AgentSettings(
        _env_file=None,
        llama_server_bin=binary,
        llama_server_min_build=0,
    )

    errors = asyncio.run(validate_runtime_version(configured))

    assert len(errors) == 1
    assert "strictement positif" in errors[0]


def test_preflight_rejects_runtime_below_minimum_build(tmp_path):
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\necho 'version: 5999 (a1b2c3d)'\n", encoding="utf-8")
    binary.chmod(0o750)
    configured = AgentSettings(
        _env_file=None,
        llama_server_bin=binary,
        llama_server_min_build=6000,
    )

    errors = asyncio.run(validate_runtime_version(configured))

    assert len(errors) == 1
    assert "inférieure au build minimal 6000" in errors[0]


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
    binary = tmp_path / "llama-server"
    binary.write_text(
        "#!/bin/sh\necho 'version: 7000 (a1b2c3d)'\n",
        encoding="utf-8",
    )
    binary.chmod(0o750)
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        "NODE_ID=node-from-file\n"
        f"AGENT_SECRET={'a' * 32}\n"
        f"INTERNAL_API_KEY={'b' * 32}\n"
        f"LLAMA_SERVER_BIN={binary}\n"
        "LLAMA_SERVER_MIN_BUILD=6000\n"
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
