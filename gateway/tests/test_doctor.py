"""
Tests d'AUT-012 — commande de diagnostic `evaruntime doctor`.

Ce que ces tests verrouillent
-----------------------------
- la grille d'exit codes (0 conforme / 1 bloquant / 3 avertissements / --strict) ;
- le fait qu'un hôte sain sorte réellement en 0 ;
- chaque contrôle ajouté par doctor, en échec ET en cas nominal ;
- la NON-DIVULGATION : aucun secret, aucun token ne doit apparaître dans les deux
  sorties, y compris lorsqu'une valeur sensible se retrouve dans un chemin ;
- la politique fail-closed de `LLAMA_SERVER_MIN_BUILD` (spécification §6) ;
- l'absence de calcul d'empreinte SHA-256 par défaut (un GGUF factice suffit) ;
- la dégradation propre quand `nvidia-smi`, nginx, systemd ou les droits root
  manquent : `skip`/`warn`, jamais de traceback ;
- le mode cluster : les contrôles délégués aux nœuds sont `skip`, pas `fail`.

Style repris de `test_readiness.py` (fabrication d'un environnement
structurellement sain dans `tmp_path`) et de `test_deploy_memory_profiles.py`
(lecture des artefacts de déploiement réels).

Déterminisme
------------
Un fixture autouse neutralise les trois chemins par défaut d'installation
(`/etc/llm-gateway/env`, la configuration nginx, l'unité systemd) et la sonde de
port. Sans cela le résultat dépendrait de l'hôte qui exécute la suite.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
from pathlib import Path

import pytest

import doctor
import readiness
from model_registry import ModelDefinition


REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_NGINX_CONF = REPO_ROOT / "gateway" / "deploy" / "nginx.conf"
SHIPPED_LOCAL_UNIT = REPO_ROOT / "gateway" / "deploy" / "llm-gateway.service"

# Secrets reconnaissables injectés dans la configuration de test : aucune de ces
# valeurs ne doit apparaître dans une sortie de doctor.
ADMIN_SECRET = "ADMINSECRET-a1b2c3d4e5f6-ne-doit-pas-fuiter"
INTERNAL_KEY = "INTERNALKEY-9f8e7d6c5b4a-ne-doit-pas-fuiter"
AGENT_SECRET = "AGENTSECRET-0011223344556677-ne-doit-pas-fuiter"
HF_TOKEN = "hf_TOKENDEXEMPLEQUINEDOITPASFUIR0123"
ALL_SECRETS = (ADMIN_SECRET, INTERNAL_KEY, AGENT_SECRET, HF_TOKEN)

# Certificats auto-signés générés hors ligne pour ces tests (CN et SAN =
# llm.example.test). Aucune clé privée n'est embarquée : les contrôles de clé
# portent uniquement sur les permissions du fichier, jamais sur son contenu.
VALID_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIDSjCCAjKgAwIBAgIUXhI5QDjhWxK+cbymFmGUr7Viz/8wDQYJKoZIhvcNAQEL
BQAwGzEZMBcGA1UEAwwQbGxtLmV4YW1wbGUudGVzdDAgFw0yMDAxMDEwMDAwMDBa
GA8yMDk5MTIzMTIzNTk1OVowGzEZMBcGA1UEAwwQbGxtLmV4YW1wbGUudGVzdDCC
ASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBAK13CuW6ZpWopf6ubLKEdu9x
ePQOa2WQeTjJib1+Vh9ZM0pzcC8t5O2hna+FVFnmiRo7N7kEsLCbwuBS9mM7b2xM
xhUg4dtzgUJ0375vHU0Kf8cTmbx2Ur1hoV/qF3DYSrHHodRI+QyscTF1of5rOgb2
eEW1cG06wfYiPnbHEy0RxT9q4E87/3ui6f8CFi/Qnd6rPfLu1198yWaZ9c9HyIbg
5XeV1Tj238lb7cowFaUvrUhQZqOox63l1Z71kf7qv4BLhWwFw4XvjQhKLwA5QKdK
Cp1+fA2ZVmH5b/qrxkAm9sZM6oTDA8qZlGnbEla/pwj2EY/rkKIr1lzjnlFcj88C
AwEAAaOBgzCBgDAdBgNVHQ4EFgQUq6x7rzelHuWDhrIcSP3CWyozrXQwHwYDVR0j
BBgwFoAUq6x7rzelHuWDhrIcSP3CWyozrXQwDwYDVR0TAQH/BAUwAwEB/zAtBgNV
HREEJjAkghBsbG0uZXhhbXBsZS50ZXN0ghBldmEuZXhhbXBsZS50ZXN0MA0GCSqG
SIb3DQEBCwUAA4IBAQBGl48ydsmmKVHbYefgsxVjiyzQnXb7TN4Rlc5ZysNPk5at
L2AvwEq1lmSlVRR/qQODazl9gE5in4iH6Q85+CuCR8dvA7RUj5UVFI7oesP6IozI
jEWqaGNWjLsa/N3ZYugwikBZGMB4wya+v95DWAuTIJOaQAn3i1xpHwqzhF8g271H
yfdhTYwcGx62kfUUfdcdno9QoYQAURhOUa31vXAws6z3aABL4JpV4LFEXyDd0v+e
t6PBHZL78NKSYKrhxXxlHyw6qxp6QW+u3nvbXLxMfuvMmpfRbGqI0MuFXYyrQWdc
PHKXjzMv6k5ggDGjLJQqIyQudAMQAl8faQiNXyKG
-----END CERTIFICATE-----
"""

EXPIRED_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIDSDCCAjCgAwIBAgIUN612nlvtJwWf5OuRpzWFj2ZSZIYwDQYJKoZIhvcNAQEL
BQAwGzEZMBcGA1UEAwwQbGxtLmV4YW1wbGUudGVzdDAeFw0yMDAxMDEwMDAwMDBa
Fw0yMDA2MDEwMDAwMDBaMBsxGTAXBgNVBAMMEGxsbS5leGFtcGxlLnRlc3QwggEi
MA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQCtyOxBgU+OeR3Gd/Ta552v4qlW
YN8Ly57GxZrqXWuOx/K315LJB1zGoMbpuHqsr+eSiY5ysyTYrEOeNYM9Cb/BCiWI
FOyE1e9E6oy5xASQeyOJ3B+Yn0QsiRJC0PdEPkVhZWqIQpoCkGnxGrYgl7fJ/zy1
M0Yg5DcelbPnPpmUoy8k24cGon0U0L2t16v/uY/NSUd4RvcxKhFcGHNhwbbZblFK
f7PWnwacCgsqrk0k+A6mme8Icu+ae64w/XrJPwXD3rAlKeGUmnPLdwdVkSq18yq/
/V0Sp3waxmno4UEn9idAeRpsWdLlG/8wxCHSzalJDNHIuFUBghiWbq7cEl6RAgMB
AAGjgYMwgYAwHQYDVR0OBBYEFFNzt9UQWoyfQAxlh0cQ69fgfmVaMB8GA1UdIwQY
MBaAFFNzt9UQWoyfQAxlh0cQ69fgfmVaMA8GA1UdEwEB/wQFMAMBAf8wLQYDVR0R
BCYwJIIQbGxtLmV4YW1wbGUudGVzdIIQZXZhLmV4YW1wbGUudGVzdDANBgkqhkiG
9w0BAQsFAAOCAQEAjZyoO9cv+waAIX4J8/KlKKv4qwxm9OXdGbVKsOBY5rXzcmN3
FnEpZz0SQ9wAmaglUdOutoPrtzz3ZwNTfgLfd5nNrQ8/HNiNBqp1+DLWBivWu/eV
U/TvnIzhLlUjVINHDedtVrj0Bm8DYsNaTMrljr14YVhF8iSF3fb50kWmx0uIxmOV
uoY/jHOUV2ReeIdIUl8QK4Jz6ms4gFpeueaFSeTwPRMNKOYDdeQ0qU6KirEYhRLf
7YcRarajkXLtm1pvY/Fa9Q1w5YNjLY9iHaWCPkJchDYheSd/k8Df/zlXek/qGzyh
Kn+u+PB0Bp29ZHINRiZVRd0E9hNzEONIsyMT6A==
-----END CERTIFICATE-----
"""

SKIP_AS_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignore les bits de permission"
)

# Sondes réelles capturées AVANT que le fixture d'isolation ne les remplace.
REAL_PORT_PROBE = doctor.port_is_occupied


# ── Neutralisation de l'environnement de l'hôte ───────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_host(monkeypatch, tmp_path_factory):
    """
    doctor lit des chemins d'installation et sonde des ports : sans isolation,
    le résultat dépendrait de la machine qui exécute la suite.
    """
    absent = tmp_path_factory.mktemp("absent")
    monkeypatch.setattr(doctor, "DEFAULT_ENV_FILE", absent / "etc-env")
    monkeypatch.setattr(doctor, "DEFAULT_NGINX_CONF", absent / "nginx.conf")
    monkeypatch.setattr(doctor, "DEFAULT_SYSTEMD_UNIT", absent / "unit.service")
    # Pool de ports libre par défaut (le test dédié teste la vraie sonde).
    monkeypatch.setattr(doctor, "port_is_occupied", lambda port, host, timeout=0.2: False)
    readiness.clear_cache()
    yield
    readiness.clear_cache()


def fake_gpus(monkeypatch, gpus, reason: str = "") -> None:
    """Remplace la sonde nvidia-smi par un inventaire déterministe."""
    async def _probe(timeout: float = 5.0):
        return gpus, reason
    monkeypatch.setattr(doctor, "probe_nvidia_smi", _probe)


def gpu(index: int, memory_mib: float = 49152.0) -> doctor.GpuInfo:
    return doctor.GpuInfo(
        index=index,
        uuid=f"GPU-0000000{index}",
        name="NVIDIA L40S",
        memory_total_mib=memory_mib,
        driver_version="550.54.15",
        compute_capability="8.9",
    )


# ── Fabrication d'un hôte sain ────────────────────────────────────────────────

class Host:
    """Hôte de test : artefacts réels sur disque + options doctor associées."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.env_file = root / "env"
        self.nginx_conf = root / "nginx.conf"
        self.unit = root / "llm-gateway.service"
        self.models_yaml = root / "models.yaml"
        self.gguf = root / "models" / "m1.gguf"
        self.binary = root / "llama-server"
        self.log_dir = root / "logs"
        self.db_dir = root / "data"
        self.cert = root / "tls.crt"
        self.key = root / "tls.key"

    def options(self, **overrides) -> doctor.DoctorOptions:
        base = {
            "env_file": self.env_file,
            "nginx_conf": self.nginx_conf,
            "systemd_unit": self.unit,
        }
        base.update(overrides)
        return doctor.DoctorOptions(**base)


def write_env(host: Host, **overrides) -> None:
    """EnvironmentFile complet et sain, en mode 0640 comme le fait install.sh."""
    values: dict[str, object] = {
        "MODELS_CONFIG_PATH": host.models_yaml,
        "LLAMA_SERVER_BIN": host.binary,
        "DB_PATH": host.db_dir / "gateway.db",
        "LOG_DIR": host.log_dir,
        "ADMIN_SECRET": ADMIN_SECRET,
        "INTERNAL_API_KEY": INTERNAL_KEY,
        "AGENT_SECRET": AGENT_SECRET,
        "HUGGING_FACE_HUB_TOKEN": HF_TOKEN,
        "LLAMA_SERVER_MIN_BUILD": 4000,
        "TOTAL_VRAM_GB": 48.0,
        "CUDA_VISIBLE_DEVICES": "0",
        "BASE_LLAMA_PORT": 18081,
        "MAX_LOADED_MODELS": 2,
        "GATEWAY_PORT": 18000,
        "MODEL_LOAD_TIMEOUT_SECONDS": 180,
    }
    values.update(overrides)
    lines = [f"{key}={value}" for key, value in values.items() if value is not None]
    host.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    host.env_file.chmod(0o640)


def write_models_yaml(host: Host, *, entries: list[dict] | None = None) -> None:
    if entries is None:
        entries = [{"id": "m1", "path": str(host.gguf), "vram_gb": 5.0}]
    blocks = []
    for entry in entries:
        lines = [f"  - id: \"{entry['id']}\"", f"    path: \"{entry['path']}\""]
        for key, value in entry.items():
            if key in ("id", "path"):
                continue
            if isinstance(value, dict):
                lines.append(f"    {key}:")
                lines.extend(f"      {k}: {json.dumps(v)}" for k, v in value.items())
            else:
                lines.append(f"    {key}: {json.dumps(value)}")
        blocks.append("\n".join(lines))
    host.models_yaml.write_text("models:\n" + "\n".join(blocks) + "\n", encoding="utf-8")


NGINX_TEMPLATE = """
upstream llm_gateway {{ server 127.0.0.1:18000; }}
server {{
    listen 443 ssl;
    server_name llm.example.test;
    ssl_certificate     {cert};
    ssl_certificate_key {key};

    location ~ ^/v1/(chat/completions|completions)$ {{
        proxy_pass http://llm_gateway;
        proxy_read_timeout {inference};
    }}
    location /admin/ {{
        proxy_pass http://llm_gateway;
        proxy_read_timeout {admin};
    }}
    location / {{ return 404; }}
}}
server {{
    listen 80;
    server_name llm.example.test;
    location / {{ return 301 https://$host$request_uri; }}
}}
"""

UNIT_TEMPLATE = """[Unit]
Description=Test gateway

[Service]
EnvironmentFile={env_file}
ReadWritePaths={data} {logs}
ReadWritePaths=-{models}
MemoryHigh={memory_high}
MemoryMax={memory_max}
MemorySwapMax=0
TasksMax=4096
LimitNOFILE=65536
"""


def healthy_host(root: Path, monkeypatch, **env_overrides) -> Host:
    """
    Hôte structurellement sain : tout existe, rien n'est en défaut.

    Doit produire un exit code 0 ; chaque test de défaut part de cette base et
    n'en casse qu'un point à la fois.
    """
    host = Host(root)
    host.log_dir.mkdir(exist_ok=True)
    # 0750 comme install.sh : le répertoire d'état ne doit pas être traversable.
    host.db_dir.mkdir(exist_ok=True)
    host.db_dir.chmod(0o750)
    host.gguf.parent.mkdir(exist_ok=True)
    # 1 Mio : au-dessus du seuil de plausibilité, sans coût de stockage réel.
    host.gguf.write_bytes(b"\0" * doctor._MIN_PLAUSIBLE_GGUF_BYTES)
    # Binaire factice : la sonde de version l'exécute réellement.
    host.binary.write_text("#!/bin/sh\necho 'version: 5000 (abc1234)'\n", encoding="utf-8")
    host.binary.chmod(0o755)
    host.cert.write_text(VALID_CERT_PEM, encoding="utf-8")
    host.key.write_text("clé privée factice — jamais parsée\n", encoding="utf-8")
    host.key.chmod(0o600)
    host.nginx_conf.write_text(
        NGINX_TEMPLATE.format(
            cert=host.cert, key=host.key, admin="900s", inference="900s"
        ),
        encoding="utf-8",
    )
    host.unit.write_text(
        UNIT_TEMPLATE.format(
            env_file=host.env_file, data=host.db_dir, logs=host.log_dir,
            models=host.gguf.parent, memory_high="80%", memory_max="90%",
        ),
        encoding="utf-8",
    )
    write_models_yaml(host)
    write_env(host, **env_overrides)
    fake_gpus(monkeypatch, [gpu(0)])
    return host


def run(options: doctor.DoctorOptions | None = None) -> doctor.DoctorReport:
    """doctor est une commande synchrone : on l'exécute comme la CLI le fait."""
    return asyncio.run(doctor.run_doctor(options))


def by_name(report: doctor.DoctorReport) -> dict[str, object]:
    return {check.name: check for check in report.checks}


def check(report: doctor.DoctorReport, name: str):
    result = by_name(report).get(name)
    assert result is not None, f"contrôle absent du rapport : {name}"
    return result


# ── Hôte sain → exit 0 ────────────────────────────────────────────────────────

def test_healthy_host_exits_zero(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    report = run(host.options())

    assert report.exit_code() == doctor.EXIT_OK, doctor.render_human(report)
    assert report.status() == "ok"
    assert report.reason is None
    assert report.mode == "local"
    assert report.config_source == str(host.env_file)
    # Aucun contrôle en échec ni en avertissement, et tous les contrôles
    # attendus sont présents.
    assert [c.name for c in report.failed] == []
    assert [c.name for c in report.warnings] == []
    for name in (
        "config_env_file", "models_registry", "database_permissions",
        "disk_space", "llama_server_version", "gpu_inventory", "vram_detected",
        "model_artifacts", "port_pool", "nginx_timeouts", "tls_certificate",
        "systemd_limits",
    ):
        assert name in by_name(report)


def test_report_order_is_stable_and_covers_reused_checks(tmp_path, monkeypatch):
    """Les contrôles de readiness (COR-005) sont consommés, pas réécrits."""
    host = healthy_host(tmp_path, monkeypatch)
    report = run(host.options())

    names = [c.name for c in report.checks]
    assert names == [n for n in doctor.CHECK_ORDER if n in names]
    for reused in (
        "models_config", "enabled_models", "secrets", "llama_server_binary",
        "model_files", "database", "log_dir", "vram_budget_fit",
        "cluster_nodes_config", "cluster_nodes_online", "serving_capacity",
    ):
        assert reused in names


def test_live_only_checks_are_skipped_not_failed(tmp_path, monkeypatch):
    """doctor doit fonctionner SANS service : rien de vivant n'est exigé."""
    host = healthy_host(tmp_path, monkeypatch)
    report = run(host.options())

    for name in ("serving_capacity", "cluster_nodes_online"):
        result = check(report, name)
        assert result.status == "skip"
        assert result.code == "service_not_running"
        assert result.is_blocking is False


# ── Grille des exit codes ─────────────────────────────────────────────────────

def _report(*checks) -> doctor.DoctorReport:
    return doctor.DoctorReport(mode="local", config_source="test", checks=checks)


def test_exit_code_grid():
    ok = readiness.CheckResult("a", "pass", "ok", "ok")
    warn = readiness.CheckResult("b", "warn", "w", "w", critical=False)
    blocking = readiness.CheckResult("c", "fail", "boom", "boom")
    soft_fail = readiness.CheckResult("d", "fail", "soft", "soft", critical=False)

    assert _report(ok).exit_code() == doctor.EXIT_OK == 0
    assert _report(ok, warn).exit_code() == doctor.EXIT_WARNINGS == 3
    assert _report(ok, soft_fail).exit_code() == doctor.EXIT_WARNINGS
    assert _report(ok, blocking).exit_code() == doctor.EXIT_BLOCKING == 1
    assert _report(ok, warn, blocking).exit_code() == doctor.EXIT_BLOCKING
    # --strict remonte les avertissements au rang d'échec bloquant.
    assert _report(ok, warn).exit_code(strict=True) == doctor.EXIT_BLOCKING
    assert _report(ok).exit_code(strict=True) == doctor.EXIT_OK
    # 2 reste réservé aux erreurs d'usage de la CLI, 4 aux erreurs internes.
    assert (doctor.EXIT_USAGE, doctor.EXIT_ERROR) == (2, 4)


def test_reason_is_the_first_blocking_code_in_report_order():
    report = _report(
        readiness.CheckResult("a", "warn", "premier_warn", "w", critical=False),
        readiness.CheckResult("b", "fail", "premier_bloquant", "f"),
        readiness.CheckResult("c", "fail", "second_bloquant", "f"),
    )
    assert report.reason == "premier_bloquant"
    assert report.to_dict()["reason"] == "premier_bloquant"


def test_strict_mode_is_reflected_in_both_outputs(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, LLAMA_SERVER_MIN_BUILD=0)
    report = run(host.options())
    assert report.exit_code() == doctor.EXIT_WARNINGS
    assert report.exit_code(strict=True) == doctor.EXIT_BLOCKING

    body = json.loads(doctor.render_json(report, strict=True))
    assert body["strict"] is True
    assert body["status"] == "fail"
    assert body["exit_code"] == 1
    assert "--strict" in doctor.render_human(report, strict=True)


# ── Formats de sortie ─────────────────────────────────────────────────────────

def test_json_output_schema_is_stable(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    report = run(host.options())

    body = json.loads(doctor.render_json(report))
    assert body["tool"] == "evaruntime-doctor"
    assert body["schema_version"] == doctor.SCHEMA_VERSION == 1
    assert set(body) == {
        "tool", "schema_version", "generated_at", "mode", "config_source",
        "strict", "status", "exit_code", "summary", "checks",
    }
    assert body["status"] == "ok"
    assert body["exit_code"] == 0
    assert set(body["summary"]) == {"pass", "warn", "fail", "skip", "blocking"}
    assert body["summary"]["pass"] == len([c for c in report.checks if c.status == "pass"])
    assert body["generated_at"].startswith("20")
    for entry in body["checks"]:
        assert set(entry) == {"name", "status", "code", "message", "critical"}
        assert entry["status"] in ("pass", "fail", "warn", "skip")
        assert isinstance(entry["critical"], bool)
        assert entry["code"] and entry["message"]


def test_human_output_is_readable(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, LLAMA_SERVER_BIN=tmp_path / "absent")
    text = doctor.render_human(run(host.options()))

    assert "EVARuntime doctor — mode local" in text
    assert "[FAIL] llama_server_binary" in text
    assert "[SKIP] cluster_nodes_config" in text
    assert "ÉCHEC BLOQUANT" in text
    assert "premier code bloquant : llama_server_missing" in text
    assert "Exit code : 1" in text
    # Chaque contrôle occupe exactement une ligne, avec son message.
    for check_result in run(host.options()).checks:
        assert any(
            check_result.name in line and check_result.message[:40] in line
            for line in text.splitlines()
        )


# ── Non-divulgation des secrets ───────────────────────────────────────────────

def test_no_secret_in_json_nor_human_output(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    report = run(host.options())

    human = doctor.render_human(report)
    raw_json = doctor.render_json(report)
    for secret in ALL_SECRETS:
        assert secret not in human
        assert secret not in raw_json
    # La configuration a bien été lue : les secrets sont non-placeholder.
    assert check(report, "secrets").status == "pass"


def test_secret_value_leaked_into_a_path_is_redacted(tmp_path, monkeypatch):
    """
    Preuve de la passe de rédaction : même une valeur sensible qui arrive dans
    un message par un chemin de fichier est masquée.
    """
    host = Host(tmp_path)
    leaked_dir = tmp_path / ADMIN_SECRET
    healthy_host(tmp_path, monkeypatch, LOG_DIR=leaked_dir)
    report = run(host.options())

    log_dir = check(report, "log_dir")
    assert log_dir.status == "warn"  # répertoire absent → avertissement
    assert ADMIN_SECRET not in log_dir.message
    assert doctor._REDACTED in log_dir.message
    for rendered in (doctor.render_human(report), doctor.render_json(report)):
        assert ADMIN_SECRET not in rendered


def test_redact_ignores_short_values_and_masks_long_ones():
    secrets = doctor.collect_secret_values(
        None, {"ADMIN_SECRET": ADMIN_SECRET, "DEBUG_KEY": "0", "PLAIN": "value"}
    )
    assert ADMIN_SECRET in secrets
    assert "0" not in secrets      # trop court pour être masqué
    assert "value" not in secrets  # nom non sensible
    assert doctor.redact(f"clé={ADMIN_SECRET} fin", secrets) == "clé=*** fin"


def test_collect_secret_values_covers_token_variables():
    """Un token Hugging Face n'est pas un champ de Settings : il vient du fichier."""
    secrets = doctor.collect_secret_values(
        None,
        {
            "HUGGING_FACE_HUB_TOKEN": HF_TOKEN,
            "PROXY_PASSWORD": "motdepasseproxy",
            "TOTAL_VRAM_GB": "48.0",
        },
    )
    assert HF_TOKEN in secrets
    assert "motdepasseproxy" in secrets
    assert "48.0" not in secrets


# ── Fichier de secrets ────────────────────────────────────────────────────────

@SKIP_AS_ROOT
def test_world_readable_env_file_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    host.env_file.chmod(0o644)
    report = run(host.options())

    result = check(report, "config_env_file")
    assert result.status == "fail"
    assert result.code == "env_file_permissions_too_open"
    assert result.is_blocking is True
    assert "chmod 640" in result.message
    assert report.exit_code() == doctor.EXIT_BLOCKING


@SKIP_AS_ROOT
def test_group_writable_env_file_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    host.env_file.chmod(0o660)
    result = check(run(host.options()), "config_env_file")
    assert result.code == "env_file_permissions_too_open"


def test_explicit_missing_env_file_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    report = run(host.options(env_file=tmp_path / "jamais-cree"))
    result = check(report, "config_env_file")
    assert result.status == "fail"
    assert result.code == "env_file_missing"
    assert result.is_blocking is True


def test_absent_default_env_file_degrades_to_ambient_config(tmp_path, monkeypatch):
    """Sans fichier d'environnement, doctor reste utilisable et le dit."""
    host = healthy_host(tmp_path, monkeypatch)
    host.unit.write_text("[Service]\nMemoryHigh=80%\n", encoding="utf-8")
    report = run(doctor.DoctorOptions(systemd_unit=host.unit))

    result = check(report, "config_env_file")
    assert result.status == "skip"
    assert result.code == "env_file_not_found"
    assert result.is_blocking is False
    assert "ambiant" in report.config_source


def test_env_file_is_resolved_from_the_systemd_unit(tmp_path, monkeypatch):
    """Le chemin est LU dans l'unité, pas supposé."""
    host = healthy_host(tmp_path, monkeypatch)
    report = run(doctor.DoctorOptions(
        nginx_conf=host.nginx_conf, systemd_unit=host.unit
    ))
    assert report.config_source == str(host.env_file)


def test_ambient_environment_never_overrides_the_targeted_file(tmp_path, monkeypatch):
    """
    Le shell appelant ne doit pas fausser le diagnostic : c'est la configuration
    que systemd donnera au service qui est validée.
    """
    host = healthy_host(tmp_path, monkeypatch)
    monkeypatch.setenv("TOTAL_VRAM_GB", "9999.0")
    monkeypatch.setenv("ADMIN_SECRET", "CHANGE_ME_ADMIN_SECRET")

    config = doctor.load_settings_from_env_file(host.env_file)
    assert config.total_vram_gb == 48.0
    assert config.admin_secret == ADMIN_SECRET
    assert check(run(host.options()), "secrets").status == "pass"


def test_documented_csv_list_variable_loads(tmp_path, monkeypatch):
    """
    `ALLOWED_MODEL_DIRS=/models,/data/models` — la syntaxe documentée — doit
    charger.

    Ce test constatait auparavant l'inverse : le format CSV faisait échouer le
    DÉMARRAGE du service sur une `SettingsError` de pydantic-settings, et doctor
    servait à le signaler avant. COR-014 a corrigé la cause à la racine, donc
    doctor n'a plus rien à signaler ici — mais l'ancien comportement ne doit pas
    revenir, d'où ce test en miroir.
    """
    host = healthy_host(tmp_path, monkeypatch)
    with host.env_file.open("a", encoding="utf-8") as f:
        f.write("ALLOWED_MODEL_DIRS=/models,/data/models\n")

    # `config_load` n'est rendu qu'en cas d'échec (arrêt précoce du diagnostic) :
    # son absence est donc la preuve que la configuration a chargé.
    report = run(host.options())
    assert "config_load" not in by_name(report)

    config = doctor.load_settings_from_env_file(host.env_file)
    assert config.allowed_model_dirs == ["/models", "/data/models"]

    # Effet de bord attendu, et souhaité : l'allowlist étant enfin réglable, elle
    # s'applique. Les GGUF de la fixture vivent sous tmp_path, donc hors des
    # répertoires autorisés — le registre doit désormais les refuser. Avant
    # COR-014, ce garde-fou était inatteignable puisque la variable ne pouvait
    # pas être renseignée sans tuer le service.
    assert check(report, "models_registry").status == "fail"


def test_invalid_list_variable_blocks_with_actionable_message(tmp_path, monkeypatch):
    """
    Une valeur de liste réellement invalide doit rester bloquante et actionnable.

    Un objet JSON est refusé explicitement plutôt que traité comme un unique
    élément CSV : sinon l'allowlist de répertoires contiendrait une entrée qui ne
    correspond à rien, donc un contrôle de sécurité inerte et silencieux.
    """
    host = healthy_host(tmp_path, monkeypatch)
    with host.env_file.open("a", encoding="utf-8") as f:
        f.write('ALLOWED_MODEL_DIRS={"repertoire": "/models"}\n')

    report = run(host.options())
    result = check(report, "config_load")
    assert result.status == "fail"
    assert result.code == "config_load_failed"
    assert result.is_blocking is True
    assert report.exit_code() == doctor.EXIT_BLOCKING
    # Le message doit orienter vers une syntaxe utilisable.
    assert "a,b" in result.message or "JSON" in result.message
    # Le contrôle de permissions du fichier reste rendu malgré l'arrêt précoce.
    assert "config_env_file" in by_name(report)


# ── Base de données ───────────────────────────────────────────────────────────

def _make_world_reachable(monkeypatch) -> None:
    """
    Les répertoires temporaires de pytest ne sont jamais traversables par tous
    (`pytest-of-<user>` est en 0700) : on force la réponse de `world_reachable`
    pour tester la logique de MODE, celle de la traversée l'étant à part.
    """
    monkeypatch.setattr(doctor, "world_reachable", lambda path: True)


@SKIP_AS_ROOT
def test_world_readable_database_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    _make_world_reachable(monkeypatch)
    db = host.db_dir / "gateway.db"
    db.write_bytes(b"")
    db.chmod(0o644)

    result = check(run(host.options()), "database_permissions")
    assert result.status == "fail"
    assert result.code == "database_world_readable"
    assert result.is_blocking is True
    assert "chmod 640" in result.message


@SKIP_AS_ROOT
def test_database_mode_is_judged_with_directory_traversal(tmp_path, monkeypatch):
    """
    Un 0644 dans un répertoire 0750 n'est pas exposé : le signaler serait un faux
    positif sur une installation correcte.
    """
    host = healthy_host(tmp_path, monkeypatch)
    host.db_dir.chmod(0o750)
    db = host.db_dir / "gateway.db"
    db.write_bytes(b"")
    db.chmod(0o644)

    result = check(run(host.options()), "database_permissions")
    assert result.status == "pass"
    assert doctor.world_reachable(db) is False


@SKIP_AS_ROOT
def test_wal_sidecar_is_examined_too(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    _make_world_reachable(monkeypatch)
    (host.db_dir / "gateway.db").write_bytes(b"")
    (host.db_dir / "gateway.db").chmod(0o640)
    wal = host.db_dir / "gateway.db-wal"
    wal.write_bytes(b"")
    wal.chmod(0o644)

    result = check(run(host.options()), "database_permissions")
    assert result.code == "database_world_readable"
    assert "gateway.db-wal" in result.message


def test_absent_database_is_skipped_not_failed(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    result = check(run(host.options()), "database_permissions")
    assert result.status == "skip"
    assert result.code == "database_absent"


# ── Version du binaire llama-server (fail-closed) ─────────────────────────────

def test_min_build_required_but_version_unreadable_fails_closed(tmp_path, monkeypatch):
    """Spécification §6 : version illisible + build minimal exigé → échec."""
    host = healthy_host(tmp_path, monkeypatch)
    host.binary.write_text("#!/bin/sh\necho 'sortie sans numero'\n", encoding="utf-8")
    host.binary.chmod(0o755)

    report = run(host.options())
    result = check(report, "llama_server_version")
    assert result.status == "fail"
    assert result.code == "llama_server_version_unreadable"
    assert result.is_blocking is True
    assert report.exit_code() == doctor.EXIT_BLOCKING


def test_version_unreadable_without_enforcement_only_warns(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, LLAMA_SERVER_MIN_BUILD=0)
    host.binary.write_text("#!/bin/sh\necho 'sortie sans numero'\n", encoding="utf-8")
    host.binary.chmod(0o755)

    report = run(host.options())
    result = check(report, "llama_server_version")
    assert result.status == "warn"
    assert result.code == "llama_server_version_unreadable"
    assert report.exit_code() == doctor.EXIT_WARNINGS


def test_build_older_than_minimum_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, LLAMA_SERVER_MIN_BUILD=9000)
    report = run(host.options())
    result = check(report, "llama_server_version")
    assert result.status == "fail"
    assert result.code == "llama_server_build_too_old"
    assert "5000" in result.message and "9000" in result.message


def test_no_min_build_configured_warns_about_inert_guard(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, LLAMA_SERVER_MIN_BUILD=0)
    result = check(run(host.options()), "llama_server_version")
    assert result.status == "warn"
    assert result.code == "min_build_not_enforced"
    assert "LLAMA_SERVER_MIN_BUILD=5000" in result.message


def test_missing_binary_skips_the_version_probe(tmp_path, monkeypatch):
    """Pas de cascade : le verdict est porté par llama_server_binary."""
    host = healthy_host(tmp_path, monkeypatch, LLAMA_SERVER_BIN=tmp_path / "absent")
    report = run(host.options())
    assert check(report, "llama_server_binary").status == "fail"
    version = check(report, "llama_server_version")
    assert version.status == "skip"
    assert version.code == "binary_unavailable"


# ── GPU, CUDA_VISIBLE_DEVICES et VRAM ─────────────────────────────────────────

def test_missing_nvidia_smi_degrades_cleanly(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    fake_gpus(monkeypatch, None, "nvidia-smi introuvable dans le PATH")

    report = run(host.options())
    inventory = check(report, "gpu_inventory")
    assert inventory.status == "warn"
    assert inventory.code == "nvidia_smi_unavailable"
    assert inventory.is_blocking is False
    vram = check(report, "vram_detected")
    assert vram.status == "skip"
    assert vram.code == "gpu_inventory_unavailable"
    assert report.exit_code() == doctor.EXIT_WARNINGS


def test_real_nvidia_smi_probe_returns_none_without_the_tool(monkeypatch):
    """La vraie sonde ne lève jamais, même sans nvidia-smi installé."""
    monkeypatch.setenv("PATH", "")
    gpus, reason = asyncio.run(doctor.probe_nvidia_smi())
    assert gpus is None
    assert reason


def test_nvidia_smi_csv_parsing_is_defensive():
    parsed = doctor.parse_nvidia_smi_csv(
        "0, GPU-aaa, NVIDIA L40S, 46068, 550.54.15, 8.9\n"
        "ligne, incomplete\n"
        "x, GPU-bbb, NVIDIA A100, pas-un-nombre, 550.54.15, 8.0\n"
        "1, GPU-ccc, NVIDIA L40S, 46068, 550.54.15, 8.9\n"
        "2, GPU-ddd, NVIDIA GB10, [N/A], 580.159.03, 12.1\n"
    )
    assert [g.index for g in parsed] == [0, 1, 2]
    assert parsed[0].name == "NVIDIA L40S"
    assert parsed[0].memory_total_mib == 46068.0
    assert parsed[2].memory_total_mib is None


def test_unreported_vram_is_skipped_without_hiding_the_gpu(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, TOTAL_VRAM_GB=120.0)
    gb10 = doctor.GpuInfo(
        index=0,
        uuid="GPU-gb10",
        name="NVIDIA GB10",
        memory_total_mib=None,
        driver_version="580.159.03",
        compute_capability="12.1",
    )
    fake_gpus(monkeypatch, [gb10])

    report = run(host.options())

    inventory = check(report, "gpu_inventory")
    assert inventory.status == "pass"
    assert "mémoire non rapportée" in inventory.message
    vram = check(report, "vram_detected")
    assert vram.status == "skip"
    assert vram.code == "vram_not_reported"


def test_only_vram_of_exposed_devices_is_counted(tmp_path, monkeypatch):
    """
    Point explicite de la spécification §5 : la VRAM retenue vient des devices
    exposés par CUDA_VISIBLE_DEVICES, pas de tous les GPU de l'hôte.
    """
    host = healthy_host(tmp_path, monkeypatch, TOTAL_VRAM_GB=48.0)
    fake_gpus(monkeypatch, [gpu(0), gpu(1)])  # 2 × 48 Go présents

    report = run(host.options())
    result = check(report, "vram_detected")
    assert result.status == "pass"
    assert "1/2 GPU" in result.message
    assert "48.0 Go détectés" in result.message
    assert check(report, "gpu_inventory").status == "pass"
    assert report.exit_code() == doctor.EXIT_OK


def test_total_vram_gb_counting_hidden_devices_is_blocking(tmp_path, monkeypatch):
    """TOTAL_VRAM_GB dimensionné sur les 2 GPU alors qu'un seul est exposé."""
    host = healthy_host(tmp_path, monkeypatch, TOTAL_VRAM_GB=96.0)
    fake_gpus(monkeypatch, [gpu(0), gpu(1)])

    result = check(run(host.options()), "vram_detected")
    assert result.status == "fail"
    assert result.code == "vram_budget_exceeds_hardware"
    assert result.is_blocking is True


def test_nominal_vram_slightly_above_hardware_only_warns(tmp_path, monkeypatch):
    """
    Cas de la configuration livrée : `TOTAL_VRAM_GB=48.0` face aux 46068 MiB
    réellement exposés par une L40S. L'overhead et la marge absorbent l'écart —
    le signaler est utile, bloquer serait un faux positif.
    """
    host = healthy_host(tmp_path, monkeypatch, TOTAL_VRAM_GB=48.0)
    fake_gpus(monkeypatch, [gpu(0, 46068.0)])

    report = run(host.options())
    result = check(report, "vram_detected")
    assert result.status == "warn"
    assert result.code == "total_vram_gb_overstated"
    assert result.is_blocking is False
    assert "45.0" in result.message
    assert report.exit_code() == doctor.EXIT_WARNINGS


def test_invalid_cuda_visible_devices_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, CUDA_VISIBLE_DEVICES="0,3")
    fake_gpus(monkeypatch, [gpu(0), gpu(1)])

    result = check(run(host.options()), "vram_detected")
    assert result.status == "fail"
    assert result.code == "cuda_visible_devices_invalid"
    assert "3" in result.message


def test_empty_cuda_visible_devices_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, CUDA_VISIBLE_DEVICES='""')
    fake_gpus(monkeypatch, [gpu(0)])

    result = check(run(host.options()), "vram_detected")
    assert result.status == "fail"
    assert result.code == "no_visible_gpu"


def test_cuda_visible_devices_accepts_uuids(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, CUDA_VISIBLE_DEVICES="GPU-00000001")
    fake_gpus(monkeypatch, [gpu(0, 24576.0), gpu(1, 49152.0)])

    result = check(run(host.options()), "vram_detected")
    assert result.status == "pass"
    assert "48.0 Go détectés" in result.message


def test_understated_total_vram_only_warns(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, TOTAL_VRAM_GB=10.0)
    result = check(run(host.options()), "vram_detected")
    assert result.status == "warn"
    assert result.code == "total_vram_gb_understated"


# ── Artefacts de modèles ──────────────────────────────────────────────────────

def test_no_hash_is_computed_by_default(tmp_path, monkeypatch):
    """
    Un GGUF factice suffit : doctor ne lit jamais le contenu par défaut. Le
    sha256 déclaré ci-dessous ne correspond évidemment pas au fichier.
    """
    host = Host(tmp_path)
    healthy_host(tmp_path, monkeypatch)
    write_models_yaml(host, entries=[{
        "id": "m1", "path": str(host.gguf), "vram_gb": 5.0, "sha256": "a" * 64,
    }])

    def _explode(self):  # pragma: no cover - ne doit jamais être appelé
        raise AssertionError("verify_integrity ne doit pas être appelé par défaut")

    monkeypatch.setattr(ModelDefinition, "verify_integrity", _explode)

    report = run(host.options())
    result = check(report, "model_artifacts")
    assert result.status == "warn"
    assert result.code == "integrity_not_verified"
    assert "--verify-hashes" in result.message
    assert result.is_blocking is False


def test_verify_hashes_detects_a_mismatch(tmp_path, monkeypatch):
    host = Host(tmp_path)
    healthy_host(tmp_path, monkeypatch)
    write_models_yaml(host, entries=[{
        "id": "m1", "path": str(host.gguf), "vram_gb": 5.0, "sha256": "b" * 64,
    }])

    report = run(host.options(verify_hashes=True))
    result = check(report, "model_artifacts")
    assert result.status == "fail"
    assert result.code == "model_integrity_mismatch"
    assert result.is_blocking is True


def test_empty_gguf_is_detected(tmp_path, monkeypatch):
    """`/ready` accepte un fichier vide par conception ; doctor doit le refuser."""
    host = healthy_host(tmp_path, monkeypatch)
    host.gguf.write_bytes(b"")

    report = run(host.options())
    assert check(report, "model_files").status == "pass"  # le stat passe
    result = check(report, "model_artifacts")
    assert result.status == "fail"
    assert result.code == "model_file_empty"
    assert result.is_blocking is True


def test_suspiciously_small_gguf_warns(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    host.gguf.write_bytes(b"GGUF" * 16)

    result = check(run(host.options()), "model_artifacts")
    assert result.status == "warn"
    assert result.code == "model_file_suspiciously_small"


def test_invalid_models_yaml_is_blocking_without_cascade(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    host.models_yaml.write_text(
        "models:\n  - id: \"m1\"\n    path: \"relatif.gguf\"\n    vram_gb: 5.0\n",
        encoding="utf-8",
    )

    report = run(host.options())
    result = check(report, "models_registry")
    assert result.status == "fail"
    assert result.code == "models_registry_invalid"
    assert result.is_blocking is True
    # Pas de doublon trompeur sur les modèles activés.
    enabled = check(report, "enabled_models")
    assert enabled.status == "skip"
    assert enabled.code == "registry_unavailable"


# ── Pool de ports ─────────────────────────────────────────────────────────────

def test_gateway_port_inside_the_pool_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, GATEWAY_PORT=18081)
    result = check(run(host.options()), "config_load")
    assert result.status == "fail"
    assert result.code == "config_load_failed"
    assert result.is_blocking is True
    assert "GATEWAY_PORT" in result.message
    assert "chevaucher" in result.message


def test_occupied_pool_port_only_warns(tmp_path, monkeypatch):
    """
    Non bloquant à dessein : `update.sh` appelle doctor après bascule, où un port
    du pool peut être occupé par un modèle légitimement chargé.
    """
    host = healthy_host(tmp_path, monkeypatch)
    monkeypatch.setattr(
        doctor, "port_is_occupied", lambda port, host_, timeout=0.2: port == 18081
    )
    report = run(host.options())
    result = check(report, "port_pool")
    assert result.status == "warn"
    assert result.code == "port_pool_partially_occupied"
    assert "18081" in result.message
    assert report.exit_code() == doctor.EXIT_WARNINGS


def test_real_port_probe_detects_a_listening_socket():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert REAL_PORT_PROBE(port, "127.0.0.1") is True
    assert REAL_PORT_PROBE(port, "127.0.0.1") is False
    # Hôte wildcard → sonde sur la loopback, comme le fait model_manager.
    assert doctor._probe_host("0.0.0.0") == "127.0.0.1"
    assert doctor._probe_host("::") == "::1"


# ── Timeouts nginx ────────────────────────────────────────────────────────────

def test_admin_timeout_shorter_than_model_load_is_detected(tmp_path, monkeypatch):
    """Défaut EVA-004 : `/admin/` à 30s alors qu'un chargement dure jusqu'à 190s."""
    host = healthy_host(tmp_path, monkeypatch)
    host.nginx_conf.write_text(
        NGINX_TEMPLATE.format(
            cert=host.cert, key=host.key, admin="30s", inference="600s"
        ),
        encoding="utf-8",
    )

    report = run(host.options())
    result = check(report, "nginx_timeouts")
    assert result.status == "warn"
    assert result.code == "nginx_admin_timeout_too_short"
    # 180 (MODEL_LOAD_TIMEOUT_SECONDS) + 10 de grâce, comme ServerManager.
    assert "190s" in result.message
    assert "COR-009" in result.message
    # Signalé sans bloquer : install.sh ne doit pas refuser d'installer pour ça.
    assert result.is_blocking is False
    assert report.exit_code() == doctor.EXIT_WARNINGS


def test_per_model_load_timeout_raises_the_requirement(tmp_path, monkeypatch):
    host = Host(tmp_path)
    healthy_host(tmp_path, monkeypatch)
    write_models_yaml(host, entries=[{
        "id": "m1", "path": str(host.gguf), "vram_gb": 5.0,
        "load_timeout_seconds": 600,
    }])
    host.nginx_conf.write_text(
        NGINX_TEMPLATE.format(
            cert=host.cert, key=host.key, admin="900s", inference="600s"
        ),
        encoding="utf-8",
    )

    result = check(run(host.options()), "nginx_timeouts")
    assert result.status == "warn"
    assert result.code == "nginx_inference_timeout_too_short"
    assert "610s" in result.message


def test_non_proxying_location_is_not_flagged(tmp_path, monkeypatch):
    """
    `proxy_pass` n'est pas héritable : une route qui renvoie 404 ou une
    redirection n'a aucun timeout d'upstream à aligner, même si elle capte le
    chemin sondé (`location /` en repli).
    """
    host = healthy_host(tmp_path, monkeypatch)
    host.nginx_conf.write_text(
        f"server {{\n"
        f"  listen 443 ssl;\n"
        f"  server_name llm.example.test;\n"
        f"  ssl_certificate     {host.cert};\n"
        f"  ssl_certificate_key {host.key};\n"
        f"  location /v1/ {{ proxy_pass http://x; proxy_read_timeout 900s; }}\n"
        f"  location / {{ return 404; }}\n"
        f"}}\n",
        encoding="utf-8",
    )
    report = run(host.options())
    assert check(report, "nginx_timeouts").status == "pass"
    assert report.exit_code() == doctor.EXIT_OK


def test_missing_proxy_read_timeout_falls_back_to_nginx_default(tmp_path, monkeypatch):
    """Le défaut nginx (60s) est déjà trop court pour un chargement de modèle."""
    host = healthy_host(tmp_path, monkeypatch)
    host.nginx_conf.write_text(
        f"server {{\n"
        f"  listen 443 ssl;\n"
        f"  server_name llm.example.test;\n"
        f"  ssl_certificate     {host.cert};\n"
        f"  ssl_certificate_key {host.key};\n"
        f"  location /admin/ {{ proxy_pass http://x; }}\n"
        f"}}\n",
        encoding="utf-8",
    )
    result = check(run(host.options()), "nginx_timeouts")
    assert result.status == "warn"
    assert result.code == "nginx_admin_timeout_too_short"
    assert "défaut nginx" in result.message


def test_absent_nginx_conf_is_skipped(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    report = run(host.options(nginx_conf=tmp_path / "pas-de-nginx.conf"))
    for name in ("nginx_timeouts", "tls_certificate"):
        result = check(report, name)
        assert result.status == "skip"
        assert result.code == "nginx_conf_not_found"
    assert report.exit_code() == doctor.EXIT_OK


def test_http_redirect_server_is_not_flagged(tmp_path, monkeypatch):
    """Un bloc de redirection 301 n'a aucun timeout d'upstream à aligner."""
    host = healthy_host(tmp_path, monkeypatch)
    servers = doctor.parse_nginx_servers(host.nginx_conf.read_text(encoding="utf-8"))
    assert len(servers) == 2
    result = check(run(host.options()), "nginx_timeouts")
    assert result.status == "pass"


def test_shipped_nginx_conf_is_parsable():
    """
    Le parser doit tenir sur l'artefact réellement livré (regex, `^~`, `=`,
    directives multi-mots), sans figer la valeur des timeouts : COR-009 va les
    faire évoluer.
    """
    servers = doctor.parse_nginx_servers(
        SHIPPED_NGINX_CONF.read_text(encoding="utf-8")
    )
    https = next(s for s in servers if s.directives.get("ssl_certificate"))
    assert https.server_names == ("gateway.example.com",)

    admin = doctor.match_location(https.locations, "/admin/models/m1/load")
    assert admin is not None and admin.pattern == "/admin/"
    assert doctor.parse_nginx_time(admin.directives["proxy_read_timeout"]) is not None

    inference = doctor.match_location(https.locations, "/v1/chat/completions")
    assert inference is not None and inference.modifier == "~"
    models = doctor.match_location(https.locations, "/v1/models")
    assert models is not None and models.modifier == "="


@pytest.mark.parametrize(
    "value,expected",
    [("30s", 30.0), ("10m", 600.0), ("600", 600.0), ("1h", 3600.0),
     ("500ms", 0.5), ("pas-une-duree", None)],
)
def test_nginx_time_parsing(value, expected):
    assert doctor.parse_nginx_time(value) == expected


# ── Certificats TLS ───────────────────────────────────────────────────────────

def test_expired_certificate_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    host.cert.write_text(EXPIRED_CERT_PEM, encoding="utf-8")

    report = run(host.options())
    result = check(report, "tls_certificate")
    assert result.status == "fail"
    assert result.code == "tls_cert_expired"
    assert result.is_blocking is True
    assert report.exit_code() == doctor.EXIT_BLOCKING


def test_missing_certificate_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    host.cert.unlink()

    result = check(run(host.options()), "tls_certificate")
    assert result.status == "fail"
    assert result.code == "tls_cert_missing"
    # doctor n'émet jamais de certificat : il dit seulement quoi déposer.
    assert "PKI" in result.message


def test_missing_private_key_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    host.key.unlink()

    result = check(run(host.options()), "tls_certificate")
    assert result.status == "fail"
    assert result.code == "tls_key_missing"


@SKIP_AS_ROOT
def test_world_readable_private_key_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    host.key.chmod(0o644)

    result = check(run(host.options()), "tls_certificate")
    assert result.status == "fail"
    assert result.code == "tls_key_permissions_too_open"
    assert "chmod 600" in result.message


def test_certificate_hostname_mismatch_warns(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    host.nginx_conf.write_text(
        host.nginx_conf.read_text(encoding="utf-8").replace(
            "llm.example.test", "autre.domaine.test"
        ),
        encoding="utf-8",
    )
    result = check(run(host.options()), "tls_certificate")
    assert result.status == "warn"
    assert result.code == "tls_cert_hostname_mismatch"
    assert result.is_blocking is False


def test_unparsable_certificate_warns_without_traceback(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    host.cert.write_text("ceci n'est pas un certificat\n", encoding="utf-8")

    result = check(run(host.options()), "tls_certificate")
    assert result.status == "warn"
    assert result.code == "tls_cert_unparsable"
    assert "openssl x509" in result.message


def test_nginx_without_tls_is_skipped(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    host.nginx_conf.write_text(
        "server {\n  listen 80;\n  location / { proxy_pass http://x; "
        "proxy_read_timeout 900s; }\n}\n",
        encoding="utf-8",
    )
    result = check(run(host.options()), "tls_certificate")
    assert result.status == "skip"
    assert result.code == "tls_not_configured"


@pytest.mark.parametrize(
    "pattern,host_name,expected",
    [
        ("llm.example.test", "llm.example.test", True),
        ("*.example.test", "llm.example.test", True),
        ("*.example.test", "a.b.example.test", False),
        ("llm.example.test", "autre.example.test", False),
    ],
)
def test_hostname_matching(pattern, host_name, expected):
    assert doctor._hostname_matches(pattern, host_name) is expected


# ── Limites systemd ───────────────────────────────────────────────────────────

def test_memory_limit_below_cpu_moe_profile_is_blocking(tmp_path, monkeypatch):
    """
    Un modèle `cpu_moe: true` garde ses experts résidents en RAM hôte. Une limite
    de cgroup inférieure à ce working set ne provoque pas d'OOM-kill mais un
    thrashing NVMe silencieux : doctor doit bloquer avant.
    """
    host = Host(tmp_path)
    healthy_host(tmp_path, monkeypatch)
    write_models_yaml(host, entries=[{
        "id": "m1", "path": str(host.gguf), "vram_gb": 5.0,
        "llama_params": {"cpu_moe": True},
    }])
    host.unit.write_text(
        UNIT_TEMPLATE.format(
            env_file=host.env_file, data=host.db_dir, logs=host.log_dir,
            models=host.gguf.parent, memory_high="64M", memory_max="128M",
        ),
        encoding="utf-8",
    )

    report = run(host.options())
    result = check(report, "systemd_limits")
    assert result.status == "fail"
    assert result.code == "systemd_memory_below_model_profile"
    assert result.is_blocking is True
    assert "cpu_moe" in result.message


def test_fully_offloaded_model_is_not_flagged(tmp_path, monkeypatch):
    """Sans cpu_moe, les pages mmap sont propres : aucune exigence de résidence."""
    host = healthy_host(tmp_path, monkeypatch)
    host.unit.write_text(
        UNIT_TEMPLATE.format(
            env_file=host.env_file, data=host.db_dir, logs=host.log_dir,
            models=host.gguf.parent, memory_high="64M", memory_max="128M",
        ),
        encoding="utf-8",
    )
    assert check(run(host.options()), "systemd_limits").status == "pass"


def test_tasks_max_is_derived_from_max_loaded_models(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, MAX_LOADED_MODELS=8)
    host.unit.write_text(
        UNIT_TEMPLATE.format(
            env_file=host.env_file, data=host.db_dir, logs=host.log_dir,
            models=host.gguf.parent, memory_high="80%", memory_max="90%",
        ).replace("TasksMax=4096", "TasksMax=128"),
        encoding="utf-8",
    )
    result = check(run(host.options()), "systemd_limits")
    assert result.status == "warn"
    assert result.code == "systemd_tasks_max_too_low"
    assert "512" in result.message  # 8 modèles × 64 tâches


def test_undeclared_model_directory_warns(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    host.unit.write_text(
        UNIT_TEMPLATE.format(
            env_file=host.env_file, data=host.db_dir, logs=host.log_dir,
            models=tmp_path / "un-autre-repertoire",
            memory_high="80%", memory_max="90%",
        ),
        encoding="utf-8",
    )
    result = check(run(host.options()), "systemd_limits")
    assert result.status == "warn"
    assert result.code == "systemd_model_dir_not_declared"
    assert result.is_blocking is False


def test_missing_memory_policy_warns(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    host.unit.write_text(
        f"[Service]\nEnvironmentFile={host.env_file}\n", encoding="utf-8"
    )
    result = check(run(host.options()), "systemd_limits")
    assert result.status == "warn"
    assert result.code == "systemd_memory_policy_missing"


def test_absent_systemd_unit_is_skipped(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    report = run(host.options(systemd_unit=tmp_path / "pas-dunite.service"))
    result = check(report, "systemd_limits")
    assert result.status == "skip"
    assert result.code == "systemd_unit_not_found"
    assert report.exit_code() == doctor.EXIT_OK


def test_shipped_unit_is_parsable_and_declares_its_policy():
    """
    Contrôle DÉRIVÉ, pas figé : on vérifie que le parser lit l'unité livrée et
    que la politique mémoire y est déclarée, sans coder les valeurs de COR-007.
    """
    directives = doctor.parse_systemd_unit(SHIPPED_LOCAL_UNIT)
    assert doctor.unit_value(directives, "User") == "llmservice"
    assert doctor.unit_value(directives, "EnvironmentFile") == "/etc/llm-gateway/env"
    for key in ("MemoryHigh", "MemoryMax", "TasksMax"):
        assert doctor.unit_value(directives, key) is not None
    # Les continuations de ligne de ExecStart sont recollées.
    assert "uvicorn" in (doctor.unit_value(directives, "ExecStart") or "")


@pytest.mark.parametrize(
    "value,host_ram,expected",
    [("80%", 100.0, 80.0), ("8G", 100.0, 8.0), ("infinity", 100.0, None),
     ("512M", 100.0, 0.5), ("80%", None, None), ("n'importe quoi", 100.0, None)],
)
def test_memory_allowance_parsing(value, host_ram, expected):
    assert doctor.memory_allowance_gb(value, host_ram) == expected


# ── Mode cluster ──────────────────────────────────────────────────────────────

def cluster_host(root: Path, monkeypatch, **env_overrides) -> Host:
    """
    Orchestrateur cluster : ni binaire, ni GGUF, ni GPU local. Rien de tout cela
    ne doit produire d'échec — les nœuds en sont responsables.
    """
    host = Host(root)
    nodes_yaml = root / "nodes.yaml"
    nodes_yaml.write_text(
        "cluster:\n  tls_verify: true\nnodes:\n"
        "  - id: gpu-01\n    base_url: https://gpu-01.example.test:9443\n",
        encoding="utf-8",
    )
    overrides = {
        "CLUSTER_MODE": "cluster",
        "CLUSTER_NODES_PATH": nodes_yaml,
        "LLAMA_SERVER_BIN": root / "aucun-binaire-ici",
    }
    overrides.update(env_overrides)
    healthy_host(root, monkeypatch, **overrides)
    # Le GGUF vit sur le nœud, pas ici.
    write_models_yaml(host, entries=[
        {"id": "m1", "path": str(root / "sur-le-noeud.gguf"), "vram_gb": 5.0}
    ])
    fake_gpus(monkeypatch, None, "nvidia-smi introuvable dans le PATH")
    return host


def test_cluster_mode_skips_node_delegated_checks(tmp_path, monkeypatch):
    host = cluster_host(tmp_path, monkeypatch)
    report = run(host.options())

    assert report.mode == "cluster"
    for name in (
        "llama_server_binary", "llama_server_version", "model_files",
        "model_artifacts", "gpu_inventory", "vram_detected", "vram_budget_fit",
        "port_pool",
    ):
        result = check(report, name)
        assert result.status == "skip", f"{name} devrait être ignoré en cluster"
        assert result.is_blocking is False
    assert check(report, "cluster_nodes_inventory").status == "pass"
    assert check(report, "cluster_agent_secret").status == "pass"
    assert report.exit_code() == doctor.EXIT_OK, doctor.render_human(report)


def test_cluster_placeholder_agent_secret_is_blocking(tmp_path, monkeypatch):
    host = cluster_host(tmp_path, monkeypatch, AGENT_SECRET="CHANGE_ME_AGENT_SECRET")
    report = run(host.options())

    result = check(report, "cluster_agent_secret")
    assert result.status == "fail"
    assert result.code == "agent_secret_placeholder"
    assert result.is_blocking is True
    # Le secret placeholder n'est qu'un avertissement pour /ready ; doctor doit
    # bloquer, car _build_manager refuse de démarrer.
    assert check(report, "secrets").status == "warn"
    assert report.exit_code() == doctor.EXIT_BLOCKING


def test_cluster_short_agent_secret_is_blocking(tmp_path, monkeypatch):
    host = cluster_host(tmp_path, monkeypatch, AGENT_SECRET="trop-court")
    result = check(run(host.options()), "cluster_agent_secret")
    assert result.status == "fail"
    assert result.code == "agent_secret_too_short"
    assert "trop-court" not in result.message  # jamais la valeur, juste la taille


def test_admin_secret_placeholder_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, ADMIN_SECRET="CHANGE_ME_ADMIN_SECRET")
    report = run(host.options())

    result = check(report, "admin_secret")
    assert result.status == "fail"
    assert result.code == "admin_secret_placeholder"
    assert result.is_blocking is True
    assert report.exit_code() == doctor.EXIT_BLOCKING


def test_admin_secret_short_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, ADMIN_SECRET="password")
    result = check(run(host.options()), "admin_secret")
    assert result.status == "fail"
    assert result.code == "admin_secret_too_short"
    assert result.is_blocking is True
    # Jamais la valeur dans le rapport, seulement la taille jugée.
    assert "password" not in result.message
    assert "8 caractères" in result.message


def test_admin_secret_strong_passes(tmp_path, monkeypatch):
    """Contrôle positif : le secret par défaut du hôte sain passe le check."""
    host = healthy_host(tmp_path, monkeypatch)
    result = check(run(host.options()), "admin_secret")
    assert result.status == "pass"
    assert result.is_blocking is False


def test_cluster_invalid_nodes_inventory_is_blocking(tmp_path, monkeypatch):
    host = cluster_host(tmp_path, monkeypatch)
    (tmp_path / "nodes.yaml").write_text("pas: un inventaire\n", encoding="utf-8")

    report = run(host.options())
    result = check(report, "cluster_nodes_inventory")
    assert result.status == "fail"
    assert result.code == "nodes_config_invalid"
    assert report.exit_code() == doctor.EXIT_BLOCKING


def test_cluster_tls_verify_disabled_warns(tmp_path, monkeypatch):
    host = cluster_host(tmp_path, monkeypatch)
    (tmp_path / "nodes.yaml").write_text(
        "cluster:\n  tls_verify: false\nnodes:\n"
        "  - id: gpu-01\n    base_url: https://gpu-01.example.test:9443\n",
        encoding="utf-8",
    )
    result = check(run(host.options()), "cluster_nodes_inventory")
    assert result.status == "warn"
    assert result.code == "cluster_tls_verify_disabled"
    assert "gpu-01" in result.message
    # Aucune URL de nœud dans le rapport.
    assert "9443" not in result.message


def test_local_mode_skips_cluster_checks(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    report = run(host.options())
    for name in ("cluster_agent_secret", "cluster_nodes_inventory"):
        result = check(report, name)
        assert result.status == "skip"
        assert result.code == "local_mode"


# ── Robustesse ────────────────────────────────────────────────────────────────

def test_a_crashing_check_is_reported_without_blocking(tmp_path, monkeypatch):
    """Un bug de doctor ne doit ni lever ni faire échouer un hôte sain."""
    host = healthy_host(tmp_path, monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("bug interne de doctor")

    monkeypatch.setattr(doctor, "check_disk_space", _boom)
    report = run(host.options())

    result = check(report, "disk_space")
    assert result.status == "fail"
    assert result.code == "check_crashed"
    assert result.is_blocking is False
    assert report.exit_code() == doctor.EXIT_WARNINGS


def test_run_doctor_without_options_never_raises():
    """Cas « premier démarrage » : rien n'est installé, doctor doit tenir."""
    report = run()
    assert report.checks
    assert report.exit_code() in (
        doctor.EXIT_OK, doctor.EXIT_BLOCKING, doctor.EXIT_WARNINGS
    )
    assert json.loads(doctor.render_json(report))["tool"] == "evaruntime-doctor"


def test_disk_space_exhaustion_is_blocking(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)

    class _Usage:
        total = 100 * 1024**3
        used = total
        free = 10 * 1024**2  # 10 Mio

    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda path: _Usage())
    report = run(host.options())
    result = check(report, "disk_space")
    assert result.status == "fail"
    assert result.code == "disk_space_exhausted"
    assert report.exit_code() == doctor.EXIT_BLOCKING


def test_low_disk_space_only_warns(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)

    class _Usage:
        total = 100 * 1024**3
        used = 0
        free = 2 * 1024**3  # 2 Gio

    monkeypatch.setattr(doctor.shutil, "disk_usage", lambda path: _Usage())
    result = check(run(host.options()), "disk_space")
    assert result.status == "warn"
    assert result.code == "disk_space_low"


def test_every_check_has_an_actionable_message(tmp_path, monkeypatch):
    """Un doctor qui dit « échec » sans dire quoi faire ne sert à rien."""
    host = healthy_host(tmp_path, monkeypatch, LLAMA_SERVER_MIN_BUILD=9000)
    host.cert.write_text(EXPIRED_CERT_PEM, encoding="utf-8")
    host.gguf.write_bytes(b"")

    report = run(host.options())
    assert report.failed
    for result in report.checks:
        assert result.name and result.code and result.message
        assert len(result.message) > 20, result
        assert result.message.strip().endswith((".", ")"))
    # Tout constat non conforme doit dire QUOI FAIRE, pas seulement ce qui casse.
    for result in report.failed + report.warnings:
        assert any(
            hint in result.message.lower()
            for hint in (
                "corrigez", "relancez", "vérifiez", "mettez", "retéléchargez",
                "renouvelez", "portez", "désactivez", "abaissez", "relevez",
                "fixez", "générez", "déposez", "libérez", "ajoutez", "écrivez",
                "déplacez", "renseignez", "alignez", "créez", "réinstallez",
            )
        ), result


@SKIP_AS_ROOT
def test_unreadable_env_file_degrades_without_root(tmp_path, monkeypatch):
    """Sans privilèges, doctor dégrade au lieu de mentir."""
    host = healthy_host(tmp_path, monkeypatch)
    host.env_file.chmod(0o000)
    try:
        report = run(host.options())
    finally:
        host.env_file.chmod(0o640)

    result = check(report, "config_env_file")
    assert result.status in ("fail", "skip")
    assert "sudo" in result.message or "propriétaire" in result.message
    # La configuration ambiante prend le relais : le rapport reste exploitable.
    assert report.checks


def test_world_reachable_walks_the_whole_parent_chain(tmp_path, monkeypatch):
    """
    Un seul répertoire sans `o+x` dans la chaîne suffit à rendre un fichier
    inatteignable : c'est ce qui évite de crier au loup sur une base en 0644
    posée dans un /var/lib/llm-gateway en 0750.
    """
    target = tmp_path / "a" / "b" / "gateway.db"
    modes = {str(tmp_path / "a"): 0o755, str(tmp_path / "a" / "b"): 0o755}
    monkeypatch.setattr(
        doctor, "_file_mode", lambda path: modes.get(str(path), 0o755)
    )
    assert doctor.world_reachable(target) is True

    modes[str(tmp_path / "a")] = 0o750
    assert doctor.world_reachable(target) is False

    # Répertoire dont le mode est illisible : on reste prudent (atteignable).
    monkeypatch.setattr(doctor, "_file_mode", lambda path: None)
    assert doctor.world_reachable(target) is True


def test_parse_env_file_handles_comments_quotes_and_export():
    path = Path(os.environ.get("PYTEST_TMP", "/tmp")) / "doctor-env-parse-test"
    path.write_text(
        "# commentaire\n"
        "\n"
        "ADMIN_SECRET=\"valeur entre guillemets\"\n"
        "export AGENT_SECRET='valeur simple'\n"
        "SANS_EGAL\n"
        "TOTAL_VRAM_GB=48.0\n",
        encoding="utf-8",
    )
    try:
        values = doctor.parse_env_file(path)
    finally:
        path.unlink()
    assert values == {
        "ADMIN_SECRET": "valeur entre guillemets",
        "AGENT_SECRET": "valeur simple",
        "TOTAL_VRAM_GB": "48.0",
    }


def test_parse_env_file_on_unreadable_path_returns_empty(tmp_path):
    assert doctor.parse_env_file(tmp_path / "inexistant") == {}


# ── Intégration CLI (`python cli.py doctor`) ──────────────────────────────────

def _invoke(host: Host, *extra: str):
    """Appelle la commande comme le feront install.sh et update.sh."""
    from typer.testing import CliRunner

    import cli

    return CliRunner().invoke(
        cli.app,
        [
            "doctor",
            "--env-file", str(host.env_file),
            "--nginx-conf", str(host.nginx_conf),
            "--systemd-unit", str(host.unit),
            *extra,
        ],
    )


def test_cli_json_mode_on_a_healthy_host(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    result = _invoke(host, "--json")

    assert result.exit_code == doctor.EXIT_OK, result.output
    body = json.loads(result.output)
    assert body["status"] == "ok"
    assert body["exit_code"] == 0
    for secret in ALL_SECRETS:
        assert secret not in result.output


def test_cli_human_mode_returns_blocking_exit_code(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)
    host.cert.write_text(EXPIRED_CERT_PEM, encoding="utf-8")
    result = _invoke(host)

    assert result.exit_code == doctor.EXIT_BLOCKING
    assert "[FAIL] tls_certificate" in result.output
    assert "Exit code : 1" in result.output
    for secret in ALL_SECRETS:
        assert secret not in result.output


def test_cli_strict_promotes_warnings(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch, LLAMA_SERVER_MIN_BUILD=0)
    assert _invoke(host).exit_code == doctor.EXIT_WARNINGS
    assert _invoke(host, "--strict").exit_code == doctor.EXIT_BLOCKING


def test_cli_internal_error_returns_dedicated_exit_code(tmp_path, monkeypatch):
    host = healthy_host(tmp_path, monkeypatch)

    async def _boom(options=None):
        raise RuntimeError("panne interne")

    monkeypatch.setattr(doctor, "run_doctor", _boom)
    result = _invoke(host)
    assert result.exit_code == doctor.EXIT_ERROR == 4


def test_file_mode_helper_reads_permission_bits(tmp_path):
    target = tmp_path / "fichier"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o640)
    assert doctor._file_mode(target) == stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
    assert doctor._file_mode(tmp_path / "absent") is None
