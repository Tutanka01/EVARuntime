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

    declared: set[str] = set()
    for line in lines:
        if line.startswith("# TYPE "):
            declared.add(line.split()[2])
    for line in lines:
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        assert m is not None
        assert m.group("name") in declared


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


def test_ready_503_when_no_model_and_no_capacity(client, monkeypatch):
    monkeypatch.setattr(main.model_manager, "status", _status_no_capacity)
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["reason"] == "no_model_ready_and_no_capacity"


def test_ready_200_when_model_ready(client, monkeypatch):
    monkeypatch.setattr(main.model_manager, "status", _status_with_ready_model)
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert "m1" in body["models_ready"]


def test_ready_200_when_capacity_available(client, monkeypatch):
    """Aucun modèle ready mais de la VRAM disponible → prêt à charger."""
    status = {
        "vram_budget": {"total_gb": 48.0, "used_gb": 0.0, "available_gb": 48.0},
        "models": [{"id": "m1", "state": "unloaded"}],
        "capacity_queue": {},
    }
    monkeypatch.setattr(main.model_manager, "status", lambda: status)
    resp = client.get("/ready")
    assert resp.status_code == 200


def test_ready_503_when_all_cluster_nodes_offline(client, monkeypatch):
    monkeypatch.setattr(main.model_manager, "status", _status_all_nodes_offline)
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
