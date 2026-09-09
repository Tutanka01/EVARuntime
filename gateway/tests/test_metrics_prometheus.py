"""
Tests de l'exposition Prometheus (/admin/metrics/prometheus) et de la readiness
(/ready), tous deux ADDITIFS.

Patterns réutilisés de test_admin_routes.py :
  - admin_headers : Bearer avec le vrai secret de test (conftest.py) ;
  - TestClient(main.app) déclenche le lifespan (sans GPU) sans échouer.

Aucun de ces tests ne modifie de route/format existant : ils vérifient
uniquement les ajouts et la non-régression de /health.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

import main
import metrics as metrics_mod
import readiness
import telemetry
from config import settings


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.admin_secret}"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


# Regex d'une ligne d'échantillon Prometheus : nom{labels} valeur  (ou sans labels)
_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?"
    r"\s+(?P<value>-?(?:[0-9]+\.?[0-9]*(?:[eE][+-]?[0-9]+)?|[0-9]*\.?[0-9]+|nan|inf|-inf))$"
)


# ── Prometheus : format & robustesse ──────────────────────────────────────────

def test_prometheus_ok_content_type_and_types(client, admin_headers):
    resp = client.get("/admin/metrics/prometheus", headers=admin_headers)
    assert resp.status_code == 200

    ctype = resp.headers["content-type"]
    assert ctype.startswith("text/plain")
    assert "version=0.0.4" in ctype

    body = resp.text
    # Les métriques déclarées doivent apparaître via leur ligne # TYPE.
    for name, mtype in [
        ("eva_requests_total", "counter"),
        ("eva_tokens_total", "counter"),
        ("eva_request_latency_seconds", "gauge"),
        ("eva_vram_used_gb", "gauge"),
        ("eva_vram_total_gb", "gauge"),
        ("eva_vram_available_gb", "gauge"),
        ("eva_models_loaded", "gauge"),
        ("eva_inference_ttft_seconds", "histogram"),
        ("eva_model_load_seconds", "histogram"),
        ("eva_capacity_queue_wait_seconds", "histogram"),
    ]:
        assert f"# TYPE {name} {mtype}" in body, f"# TYPE manquant pour {name}"


def test_prometheus_every_sample_line_is_well_formed(client, admin_headers):
    resp = client.get("/admin/metrics/prometheus", headers=admin_headers)
    assert resp.status_code == 200

    non_comment = [
        ln for ln in resp.text.splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    # Au moins les gauges VRAM/modèles sont toujours émis (0 par défaut).
    assert non_comment, "Aucune ligne d'échantillon émise"
    for line in non_comment:
        assert _SAMPLE_RE.match(line), f"Ligne mal formée : {line!r}"


def test_prometheus_declared_types_appear_before_samples(client, admin_headers):
    """Chaque métrique échantillonnée doit avoir une déclaration # TYPE."""
    resp = client.get("/admin/metrics/prometheus", headers=admin_headers)
    lines = resp.text.splitlines()

    declared: dict[str, str] = {}
    for line in lines:
        if line.startswith("# TYPE "):
            _, _, name, mtype = line.split()
            declared[name] = mtype
    for line in lines:
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        assert m is not None
        name = m.group("name")
        if name in declared:
            continue
        base = next(
            (
                name.removesuffix(suffix)
                for suffix in ("_bucket", "_sum", "_count")
                if name.endswith(suffix)
            ),
            None,
        )
        assert base is not None and declared.get(base) == "histogram"


def test_prometheus_exports_runtime_histogram(client, admin_headers):
    telemetry.TTFT_SECONDS.observe(0.2, model="m1", node="node-a")

    resp = client.get("/admin/metrics/prometheus", headers=admin_headers)

    assert resp.status_code == 200
    labels = 'model="m1",node="node-a",outcome="success"'
    assert f'eva_inference_ttft_seconds_bucket{{{labels},le="0.25"}} 1' in resp.text
    assert f'eva_inference_ttft_seconds_bucket{{{labels},le="+Inf"}} 1' in resp.text
    assert f"eva_inference_ttft_seconds_count{{{labels}}} 1" in resp.text


def test_prometheus_no_crash_when_no_data(client, admin_headers):
    """
    Service fraîchement démarré (DB de test :memory: vide, aucun modèle chargé,
    pas de nvidia-smi) : l'endpoint ne doit jamais lever 500.
    """
    resp = client.get("/admin/metrics/prometheus", headers=admin_headers)
    assert resp.status_code == 200
    # Les gauges VRAM sont toujours présents même sans données d'usage.
    assert "eva_vram_used_gb" in resp.text
    assert "eva_models_loaded" in resp.text


def test_prometheus_exports_gpu_measurement_by_uuid(client, admin_headers, monkeypatch):
    """Les séries GPU gardent l'UUID et n'exposent que les mesures valides."""
    class _Manager:
        def status(self):
            return {
                "vram_budget": {
                    "gpu_measurement": {
                        "status": "measured",
                        "devices": [
                            {
                                "uuid": "GPU-hidden",
                                "memory_used_mb": 700.0,
                                "memory_total_mb": 1000.0,
                                "memory_used_bytes": 734003200,
                                "memory_total_bytes": 1048576000,
                                "visible": False,
                            },
                            {
                                "uuid": "GPU-visible",
                                "memory_used_mb": 300.0,
                                "memory_total_mb": 2000.0,
                                "memory_used_bytes": 314572800,
                                "memory_total_bytes": 2097152000,
                                "visible": True,
                            },
                        ],
                    },
                },
                "models": [],
            }

    async def no_llama_metrics():
        return {}

    monkeypatch.setattr(metrics_mod, "model_manager", _Manager())
    monkeypatch.setattr(metrics_mod, "_collect_llama_metrics", no_llama_metrics)

    resp = client.get("/admin/metrics/prometheus", headers=admin_headers)

    assert resp.status_code == 200
    assert 'eva_gpu_probe_success{node="local"} 1' in resp.text
    assert (
        'eva_gpu_memory_used_bytes{node="local",gpu_uuid="GPU-visible"} 314572800'
        in resp.text
    )
    assert (
        'eva_gpu_memory_total_bytes{node="local",gpu_uuid="GPU-visible"} 2097152000'
        in resp.text
    )
    assert (
        'eva_gpu_memory_available_bytes{node="local",gpu_uuid="GPU-visible"} 1782579200'
        in resp.text
    )
    assert "GPU-hidden" not in resp.text


def test_prometheus_hides_partial_gpu_values_when_unavailable(
    client, admin_headers, monkeypatch,
):
    """Un statut unavailable ne transforme pas un inventaire partiel en métrique."""
    class _Manager:
        def status(self):
            return {
                "vram_budget": {
                    "gpu_measurement": {
                        "status": "unavailable",
                        "reason": "nvidia_smi_timeout",
                        "devices": [
                            {
                                "uuid": "GPU-partial",
                                "memory_used_mb": 300.0,
                                "memory_total_mb": 2000.0,
                                "visible": True,
                            },
                        ],
                    },
                },
                "models": [],
            }

    async def no_llama_metrics():
        return {}

    monkeypatch.setattr(metrics_mod, "model_manager", _Manager())
    monkeypatch.setattr(metrics_mod, "_collect_llama_metrics", no_llama_metrics)

    resp = client.get("/admin/metrics/prometheus", headers=admin_headers)

    assert resp.status_code == 200
    assert 'eva_gpu_probe_success{node="local"} 0' in resp.text
    assert "GPU-partial" not in resp.text


def test_prometheus_exports_cluster_gpu_measurement_with_node_label(
    client, admin_headers, monkeypatch,
):
    class _ClusterManager:
        def status(self):
            return {"vram_budget": {}, "models": []}

        def cluster_status(self):
            return [
                {
                    "node_id": "gpu-a",
                    "gpu_measurement": {
                        "status": "measured",
                        "devices": [
                            {
                                "uuid": "GPU-cluster",
                                "memory_used_bytes": 1048576,
                                "memory_total_bytes": 4194304,
                                "visible": True,
                            }
                        ],
                    },
                }
            ]

    async def no_llama_metrics():
        return {}

    monkeypatch.setattr(metrics_mod, "model_manager", _ClusterManager())
    monkeypatch.setattr(metrics_mod, "_collect_llama_metrics", no_llama_metrics)

    response = client.get("/admin/metrics/prometheus", headers=admin_headers)

    assert response.status_code == 200
    assert 'eva_gpu_probe_success{node="gpu-a"} 1' in response.text
    assert (
        'eva_gpu_memory_available_bytes{node="gpu-a",gpu_uuid="GPU-cluster"} '
        "3145728" in response.text
    )


def test_prometheus_does_not_publish_stale_gpu_values_for_offline_node(
    client, admin_headers, monkeypatch,
):
    class _ClusterManager:
        def status(self):
            return {"vram_budget": {}, "models": []}

        def cluster_status(self):
            return [{
                "node_id": "gpu-offline",
                "online": False,
                "gpu_measurement": {
                    "status": "measured",
                    "devices": [{
                        "uuid": "GPU-stale",
                        "memory_used_bytes": 1,
                        "memory_total_bytes": 2,
                        "visible": True,
                    }],
                },
            }]

    async def no_llama_metrics():
        return {}

    monkeypatch.setattr(metrics_mod, "model_manager", _ClusterManager())
    monkeypatch.setattr(metrics_mod, "_collect_llama_metrics", no_llama_metrics)

    response = client.get("/admin/metrics/prometheus", headers=admin_headers)

    assert response.status_code == 200
    assert 'eva_gpu_probe_success{node="gpu-offline"} 0' in response.text
    assert "GPU-stale" not in response.text


def test_prometheus_labels_escaped(client, admin_headers, monkeypatch):
    """
    Un model_id contenant des caractères spéciaux doit être échappé proprement
    dans les labels (backslash/guillemet) — via le collecteur llama mocké.
    """
    async def fake_collect():
        return {
            'weird"model\\x': {
                "kv_cache_usage_ratio": 0.5,
                "tokens_per_second": 12.0,
                "requests_processing": 1,
                "requests_deferred": 0,
            }
        }

    monkeypatch.setattr(metrics_mod, "_collect_llama_metrics", fake_collect)
    resp = client.get("/admin/metrics/prometheus", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.text
    # Guillemet et backslash échappés dans le label.
    assert 'model="weird\\"model\\\\x"' in body
    # Toutes les lignes restent parseables malgré les caractères spéciaux.
    for line in body.splitlines():
        if line and not line.startswith("#"):
            assert _SAMPLE_RE.match(line), f"Ligne mal formée : {line!r}"


# ── Authentification ──────────────────────────────────────────────────────────

def test_prometheus_requires_admin(client):
    resp = client.get("/admin/metrics/prometheus")
    assert resp.status_code in (401, 403)


def test_prometheus_rejects_bad_secret(client):
    resp = client.get(
        "/admin/metrics/prometheus",
        headers={"Authorization": "Bearer definitely-not-the-secret"},
    )
    assert resp.status_code in (401, 403)


# ── Readiness /ready vs liveness /health ──────────────────────────────────────
#
# COR-005 a rendu /ready STRICTEMENT structurelle : monkeypatcher `status()` ne
# suffit plus, il faut aussi un environnement structurellement sain (binaire,
# GGUF des modèles activés, DB inscriptible). Les quatre tests ci-dessous
# encodaient l'ancien comportement permissif — ils passaient alors que ni le
# binaire llama-server ni les GGUF n'existaient dans l'environnement de test.
# Ils sont conservés (contrat de capacité de service, inchangé) mais adossés à
# l'environnement sain construit par `tests.test_readiness`, où vivent les
# régressions dédiées à la readiness structurelle.

from tests.test_readiness import (  # noqa: E402
    _FakeManager,
    _gguf,
    _healthy_settings,
    _model,
)


@pytest.fixture(autouse=True)
def _clean_readiness_cache():
    telemetry.reset_all()
    readiness.clear_cache()
    yield
    telemetry.reset_all()
    readiness.clear_cache()


def _wire_sound_env(monkeypatch, tmp_path, status: dict) -> None:
    """Environnement structurellement sain + `status()` imposé par le test."""
    cfg = _healthy_settings(tmp_path)
    manager = _FakeManager([_model(_gguf(tmp_path))], status)
    monkeypatch.setattr(main, "model_manager", manager)
    monkeypatch.setattr(main, "settings", cfg)


def _status_no_capacity() -> dict:
    return {
        "vram_budget": {"total_gb": 48.0, "used_gb": 48.0, "available_gb": 0.0},
        "models": [{"id": "m1", "state": "unloaded"}],
        "capacity_queue": {},
    }


def _status_with_ready_model() -> dict:
    return {
        "vram_budget": {"total_gb": 48.0, "used_gb": 20.0, "available_gb": 28.0},
        "models": [{"id": "m1", "state": "ready"}],
        "capacity_queue": {},
    }


def _status_all_nodes_offline() -> dict:
    return {
        "vram_budget": {
            "total_gb": 0.0, "used_gb": 0.0, "available_gb": 0.0,
            "nodes": 2, "nodes_online": 0,
        },
        "models": [{"id": "m1", "state": "unloaded"}],
    }


def test_ready_503_when_no_model_and_no_capacity(client, monkeypatch, tmp_path):
    _wire_sound_env(monkeypatch, tmp_path, _status_no_capacity())
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["reason"] == "no_model_ready_and_no_capacity"


def test_ready_200_when_model_ready(client, monkeypatch, tmp_path):
    _wire_sound_env(monkeypatch, tmp_path, _status_with_ready_model())
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert "m1" in body["models_ready"]


def test_ready_200_when_capacity_available(client, monkeypatch, tmp_path):
    """Aucun modèle ready mais de la VRAM disponible → prêt à charger."""
    _wire_sound_env(monkeypatch, tmp_path, {
        "vram_budget": {"total_gb": 48.0, "used_gb": 0.0, "available_gb": 48.0},
        "models": [{"id": "m1", "state": "unloaded"}],
        "capacity_queue": {},
    })
    resp = client.get("/ready")
    assert resp.status_code == 200


def test_ready_503_when_all_cluster_nodes_offline(client, monkeypatch, tmp_path):
    nodes_yaml = tmp_path / "nodes.yaml"
    nodes_yaml.write_text("nodes: []\n", encoding="utf-8")
    cfg = _healthy_settings(
        tmp_path, cluster_mode="cluster", cluster_nodes_path=nodes_yaml
    )
    manager = _FakeManager([_model(_gguf(tmp_path))], _status_all_nodes_offline())
    monkeypatch.setattr(main, "model_manager", manager)
    monkeypatch.setattr(main, "settings", cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "all_nodes_offline"


def test_health_unchanged_and_ok(client):
    """/health reste une liveness simple, toujours 200 avec le format connu."""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "models_loaded" in body
    assert "vram_used_gb" in body
    assert "vram_available_gb" in body
