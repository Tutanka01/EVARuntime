"""
Régressions COR-001 — GET /admin/status doit valider `GatewayStatus` dans les
DEUX modes de déploiement.

Avant le correctif, `ClusterManager.status()` ne produisait que `total_gb`,
`used_gb`, `available_gb`, `nodes` et `nodes_online` alors que
`VramBudgetResponse` exigeait aussi `overhead_gb`, `safety_margin` et
`budget_net_gb` : FastAPI levait une `ResponseValidationError` → HTTP 500, et le
dashboard admin (dont /admin/status est la première requête) était inutilisable
en cluster.

Ce fichier couvre :
  - /admin/status en CLUSTER_MODE=cluster → 200 + payload validé par le
    response model, y compris tous nœuds offline ;
  - la sémantique agrégée du budget cluster (total physique, réserve, net) ;
  - /admin/status en mode local → 200 et contrat inchangé (les trois champs
    restent présents et cohérents avec la configuration) ;
  - la validation directe de `VramBudgetResponse` sur les deux formes de payload.

Le montage cluster réutilise les doubles de `tests/test_cluster_manager.py`
(FakeNodeBackend + LocalNodeAdapter) : aucun sous-processus, aucun réseau.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import admin
import main
from config import settings
from schemas import GatewayStatus, VramBudgetResponse
from tests.test_cluster_manager import FakeModelDef, FakeNodeBackend, make_manager


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.admin_secret}"}


@pytest.fixture
def client() -> TestClient:
    # raise_server_exceptions=False : on veut observer le 500 tel que le voit le
    # dashboard, pas une exception remontée dans le test.
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
async def cluster_manager():
    """
    Cluster d'un nœud : 48 GB physiques, 6 GB réservés par l'agent (overhead +
    marge), un modèle de 20 GB chargé.
    → budget net = 42 GB, used = 20 GB, available = 22 GB.
    """
    backend = FakeNodeBackend("node-a", total_vram=48.0, reserved_vram=6.0)
    mgr = make_manager([backend], models=[FakeModelDef("m1", 20.0)])
    await mgr.start_health_monitor()
    await mgr.ensure_model_loaded("m1")
    try:
        yield mgr
    finally:
        await mgr.shutdown()


@pytest.fixture
def as_cluster(monkeypatch, cluster_manager):
    """Bascule la gateway en mode cluster et branche admin.py sur le manager."""
    monkeypatch.setattr(settings, "cluster_mode", "cluster")
    monkeypatch.setattr(admin, "model_manager", cluster_manager)
    return cluster_manager


# ── /admin/status en mode cluster ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_admin_status_cluster_returns_200_and_validates(
    client, admin_headers, as_cluster,
):
    """Le défaut d'origine : 500 par ResponseValidationError en cluster."""
    response = client.get("/admin/status", headers=admin_headers)

    assert response.status_code == 200, response.text
    # Le payload sérialisé doit lui-même être un GatewayStatus valide.
    GatewayStatus.model_validate(response.json())


@pytest.mark.anyio
async def test_admin_status_cluster_budget_is_aggregated_and_coherent(
    client, admin_headers, as_cluster,
):
    """
    Le budget cluster garde la même arithmétique qu'en local :
    total physique − réserve agrégée == budget net allouable.
    """
    budget = client.get("/admin/status", headers=admin_headers).json()["vram_budget"]

    assert budget["total_gb"] == 48.0          # somme des VRAM physiques ONLINE
    assert budget["overhead_gb"] == 6.0        # réserve agrégée des agents
    assert budget["used_gb"] == 20.0
    assert budget["available_gb"] == 22.0
    assert budget["budget_net_gb"] == 42.0     # used + available annoncés
    assert budget["total_gb"] - budget["overhead_gb"] == budget["budget_net_gb"]
    # Ratio de configuration mono-hôte : aucun sens agrégé en cluster.
    assert budget["safety_margin"] is None


@pytest.mark.anyio
async def test_admin_status_cluster_exposes_topology(
    client, admin_headers, as_cluster,
):
    """`nodes`/`nodes_online` ne doivent plus être supprimés par le response model."""
    budget = client.get("/admin/status", headers=admin_headers).json()["vram_budget"]

    assert budget["nodes"] == 1
    assert budget["nodes_online"] == 1


@pytest.mark.anyio
async def test_admin_status_cluster_reports_hosting_node(
    client, admin_headers, as_cluster,
):
    """Le nœud d'hébergement et la charge live survivent à la sérialisation."""
    models = client.get("/admin/status", headers=admin_headers).json()["models"]
    entry = next(m for m in models if m["id"] == "m1")

    assert entry["state"] == "ready"
    assert entry["node"] == "node-a"
    assert entry["active_requests"] == 0
    # Issue #31 : durée du chargement mesurée côté orchestrateur (RPC inclus),
    # le modèle ayant été réellement chargé par ce placement.
    assert entry["last_load_seconds"] is not None
    assert entry["last_load_seconds"] >= 0.0
    # Détail d'infra interne : jamais exposé par /admin/status.
    assert "llama_url" not in entry


@pytest.mark.anyio
async def test_admin_unload_cluster_returns_503_when_unload_is_uncertain(
    client, admin_headers, as_cluster,
):
    """Une confirmation manquante ne doit jamais être annoncée comme 200."""
    backend = as_cluster._nodes["node-a"].client._backend
    backend.fail_unload = True

    response = client.post("/admin/models/m1/unload", headers=admin_headers)

    assert response.status_code == 503, response.text
    assert "pas été confirmé" in response.json()["detail"]
    assert as_cluster._placement.get("m1") == "node-a"
    assert as_cluster.status()["models"][0]["state"] == "unload_uncertain"


@pytest.mark.anyio
async def test_admin_status_cluster_all_nodes_offline_still_200(
    client, admin_headers, as_cluster,
):
    """
    Cas dégradé : plus aucun nœud ONLINE. La route doit rester exploitable
    (statut à zéro) au lieu de casser l'observabilité.
    """
    node = as_cluster._nodes["node-a"]
    node.online = False

    response = client.get("/admin/status", headers=admin_headers)

    assert response.status_code == 200, response.text
    budget = response.json()["vram_budget"]
    assert budget["total_gb"] == 0.0
    assert budget["overhead_gb"] == 0.0
    assert budget["used_gb"] == 0.0
    assert budget["available_gb"] == 0.0
    assert budget["budget_net_gb"] == 0.0
    assert budget["nodes_online"] == 0
    assert response.json()["models"][0]["state"] == "unloaded"


# ── /admin/status en mode local : contrat inchangé ────────────────────────────

def test_admin_status_local_contract_unchanged(client, admin_headers):
    """Non-régression : les trois champs restent présents ET corrects en local."""
    response = client.get("/admin/status", headers=admin_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    budget = body["vram_budget"]

    assert budget["total_gb"] == settings.total_vram_gb
    assert budget["overhead_gb"] == settings.vram_overhead_gb
    assert budget["safety_margin"] == settings.vram_safety_margin
    assert budget["budget_net_gb"] == pytest.approx(
        round(settings.effective_vram_budget_gb(), 2)
    )
    assert budget["used_gb"] is not None
    assert budget["available_gb"] is not None
    # Champs cluster absents en local (None, jamais des valeurs inventées).
    assert budget["nodes"] is None
    assert budget["nodes_online"] is None
    # La queue d'admission VRAM locale reste exposée.
    assert body["capacity_queue"]["max_waiters"] == settings.capacity_queue_max_waiters


def test_admin_status_local_keeps_vram_drift_fields(
    client, admin_headers, monkeypatch,
):
    """
    Les champs de réconciliation nvidia-smi consommés par le dashboard doivent
    traverser le response model (ils étaient silencieusement supprimés).
    """
    monkeypatch.setattr(admin.model_manager, "_last_gpu_used_mb", 20480.0)
    monkeypatch.setattr(admin.model_manager, "_last_vram_drift_mb", 128.0)

    budget = client.get("/admin/status", headers=admin_headers).json()["vram_budget"]

    assert budget["gpu_used_mb_measured"] == 20480.0
    assert budget["vram_drift_mb"] == 128.0


def test_admin_status_local_exposes_last_load_seconds(client, admin_headers):
    """
    Issue #31 : `last_load_seconds` traverse le response model et reste présent
    dans chaque entrée de `models`, même déchargée (None en mode local hors
    chargement — aucun modèle n'est chargé par les tests).
    """
    body = client.get("/admin/status", headers=admin_headers).json()
    assert body["models"], "le registre local doit contenir des modèles"

    for entry in body["models"]:
        assert "last_load_seconds" in entry
        assert entry["last_load_seconds"] is None


# ── Validation directe de VramBudgetResponse ──────────────────────────────────

LOCAL_BUDGET = {
    "total_gb": 48.0,
    "overhead_gb": 2.0,
    "safety_margin": 0.1,
    "used_gb": 20.0,
    "available_gb": 21.2,
    "budget_net_gb": 41.2,
}

CLUSTER_BUDGET = {
    "total_gb": 48.0,
    "overhead_gb": 6.0,
    "used_gb": 20.0,
    "available_gb": 22.0,
    "budget_net_gb": 42.0,
    "nodes": 1,
    "nodes_online": 1,
}


def test_vram_budget_response_accepts_local_payload():
    budget = VramBudgetResponse.model_validate(LOCAL_BUDGET)

    assert budget.overhead_gb == 2.0
    assert budget.safety_margin == 0.1
    assert budget.budget_net_gb == 41.2
    assert budget.nodes is None


def test_vram_budget_response_accepts_cluster_payload():
    budget = VramBudgetResponse.model_validate(CLUSTER_BUDGET)

    assert budget.safety_margin is None
    assert budget.budget_net_gb == 42.0
    assert budget.nodes_online == 1


def test_vram_budget_response_tolerates_partial_manager():
    """
    Un manager dégradé (aucun nœud interrogeable) ne doit pas transformer une
    route d'observabilité en 500 : les champs mode-spécifiques sont optionnels.
    """
    budget = VramBudgetResponse.model_validate(
        {"total_gb": 0.0, "used_gb": 0.0, "available_gb": 0.0}
    )

    assert budget.overhead_gb is None
    assert budget.budget_net_gb is None


@pytest.mark.parametrize("missing", ["total_gb", "used_gb", "available_gb"])
def test_vram_budget_response_still_requires_common_fields(missing):
    """Les trois champs communs aux deux modes restent obligatoires."""
    payload = {k: v for k, v in LOCAL_BUDGET.items() if k != missing}

    with pytest.raises(ValidationError):
        VramBudgetResponse.model_validate(payload)
