"""
Tests de l'identifiant de requête propagé de bout en bout (OPS-004).

Ce que ces tests protègent :

  - le client reçoit toujours un `X-Request-Id`, y compris sur une erreur ;
  - un identifiant fourni par le client est réutilisé tel quel s'il est
    exploitable, et remplacé — jamais nettoyé en partie — sinon ;
  - un identifiant hostile (retour chariot, octet de contrôle, longueur
    arbitraire) ne peut pas atteindre les journaux ni la base ;
  - la ligne `usage_log` porte le MÊME identifiant que celui renvoyé au
    client, sinon la corrélation promise n'existe pas ;
  - le backend d'inférence reçoit l'identifiant, pour que ses propres
    journaux soient rattachables.

Technique : les tests de bout en bout réutilisent le harnais de
`test_stream_proxy.py` (MockTransport httpx + ServerManager factice), parce
que `proxy_request` est le seul endroit où l'identifiant rejoint la ligne
d'usage.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import main
import proxy
import request_context
import telemetry
from auth import get_current_user
from config import settings
from request_context import (
    REQUEST_ID_HEADER,
    REQUEST_ID_MAX_LENGTH,
    resolve_request_id,
    sanitize_request_id,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── Validation de l'en-tête entrant ──────────────────────────────────────────

@pytest.mark.parametrize(
    "value",
    [
        "3f0c1f6e-7b1a-4c2e-9a3d-0b5e8f1d2c34",   # UUID canonique
        "01J9ZQK8N4T7V2M5X8Y0A1B2C3",             # ULID
        "req_12345",                               # identifiant applicatif
        "trace.span:1-2",                          # séparateurs admis
        "a",                                       # longueur minimale
        "x" * REQUEST_ID_MAX_LENGTH,               # longueur maximale admise
    ],
)
def test_sanitize_accepte_les_identifiants_techniques(value):
    assert sanitize_request_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "x" * (REQUEST_ID_MAX_LENGTH + 1),
        "avec espace",
        "retour\nchariot",
        "retour\rchariot",
        "tabulation\tinterne",
        "nul\x00octet",
        "échappé",            # non ASCII
        "slash/interdit",
        "quote\"interdite",
        "point-virgule;",
    ],
)
def test_sanitize_refuse_tout_le_reste(value):
    assert sanitize_request_id(value) is None


@pytest.mark.parametrize("brut", ["  req_1  ", "req_1\t", "\nreq_1\r\n"])
def test_sanitize_tolere_le_blanc_autour(brut):
    """
    Un en-tête recopié depuis un journal traîne souvent du blanc, et la couche
    HTTP elle-même en retire déjà. Le blanc EXTÉRIEUR est donc toléré, mais
    seulement lui : un blanc intérieur reste un refus (cas voisins ci-dessus),
    donc aucune ligne de journal ne peut être forgée.
    """
    assert sanitize_request_id(brut) == "req_1"


def test_sanitize_ne_tronque_ni_ne_nettoie_partiellement():
    """
    Un identifiant trop long est refusé, pas coupé : tronqué, il ressemblerait
    à un identifiant valide tout en ne correspondant plus à celui que
    l'appelant conserve de son côté.
    """
    trop_long = "a" * (REQUEST_ID_MAX_LENGTH + 10)
    remplacant = resolve_request_id(trop_long)
    assert remplacant != trop_long
    assert not trop_long.startswith(remplacant)
    assert len(remplacant) <= REQUEST_ID_MAX_LENGTH


def test_resolve_genere_un_identifiant_neuf_et_unique():
    premier = resolve_request_id(None)
    second = resolve_request_id(None)
    assert premier and second
    assert premier != second
    assert sanitize_request_id(premier) == premier


# ── En-tête de réponse ────────────────────────────────────────────────────────

async def _fake_user() -> dict:
    return {"user_id": 1, "key_id": 1, "username": "test"}


class _FakeStatusManager:
    """model_manager minimal : /health ne lit que `status()`."""

    def status(self) -> dict:
        return {
            "models": [],
            "vram_budget": {"used_gb": 0.0, "available_gb": 42.0},
        }


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(main, "model_manager", _FakeStatusManager())
    return TestClient(main.app)


def test_reponse_porte_toujours_un_identifiant(client):
    response = client.get("/health")

    assert response.status_code == 200
    renvoye = response.headers[REQUEST_ID_HEADER]
    assert sanitize_request_id(renvoye) == renvoye


def test_identifiant_client_exploitable_est_reutilise(client):
    response = client.get("/health", headers={REQUEST_ID_HEADER: "req_abc-123"})

    assert response.headers[REQUEST_ID_HEADER] == "req_abc-123"


def test_identifiant_client_hostile_est_remplace(client):
    """
    Un `X-Request-Id` contenant un retour chariot permettrait de forger une
    fausse ligne de journal. La requête reste servie — c'est un défaut du
    client, pas une raison de refuser une inférence valide — mais la valeur
    ne traverse pas la gateway.
    """
    hostile = "legit\n2026-01-01 [ERROR] fausse ligne"
    response = client.get("/health", headers={REQUEST_ID_HEADER: hostile})

    assert response.status_code == 200
    renvoye = response.headers[REQUEST_ID_HEADER]
    assert renvoye != hostile
    assert "\n" not in renvoye
    assert sanitize_request_id(renvoye) == renvoye


def test_identifiant_present_sur_une_reponse_d_erreur(client):
    """
    C'est le cas qui justifie la fonctionnalité : l'utilisateur dont la requête
    échoue doit pouvoir citer un identifiant.
    """
    response = client.get("/v1/capacity")  # 401 : aucune clé fournie

    assert response.status_code == 401
    assert sanitize_request_id(response.headers[REQUEST_ID_HEADER]) is not None


def test_identifiant_present_sur_une_route_authentifiee(monkeypatch):
    """Contrôle positif : une route servie normalement porte aussi l'en-tête."""
    monkeypatch.setattr(main, "model_manager", _FakeStatusManager())
    monkeypatch.setattr(settings, "capacity_queue_retry_after_seconds", 10)
    main.app.dependency_overrides[get_current_user] = _fake_user
    try:
        response = TestClient(main.app).get(
            "/v1/capacity",
            headers={"Authorization": "Bearer test", REQUEST_ID_HEADER: "req_ok"},
        )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == "req_ok"


def test_journal_d_acces_porte_l_identifiant(client, caplog):
    """L'opérateur doit retrouver en journal l'identifiant cité par le client."""
    with caplog.at_level("INFO", logger="main"):
        client.get("/ready", headers={REQUEST_ID_HEADER: "req_journal"})

    lignes = [r.getMessage() for r in caplog.records]
    assert any("rid=req_journal" in ligne for ligne in lignes), lignes


# ── Corrélation jusqu'à la ligne d'usage et au backend ───────────────────────

class _FakeModel:
    id = "test-model"


class _FakeManager:
    """ServerManager minimal pour le chemin non-streamant."""

    def __init__(self) -> None:
        self.model = _FakeModel()

    def pin(self) -> None:
        pass

    def unpin(self) -> None:
        pass

    def llama_url(self, path: str) -> str:
        return f"http://127.0.0.1:8081{path}"

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-internal"}

    async def report_backend_failure(self) -> None:
        pass


class _EnsureManager:
    def __init__(self, manager: _FakeManager) -> None:
        self._manager = manager

    async def ensure_model_loaded(self, model_id: str) -> _FakeManager:
        return self._manager


class _FakeRequest:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def body(self) -> bytes:
        return self._body


@pytest.fixture
def restore_http_client():
    saved = proxy._http_client
    proxy.set_http_client(None)
    yield
    proxy.set_http_client(saved)


@pytest.mark.anyio
async def test_usage_log_et_backend_partagent_l_identifiant_du_client(
    monkeypatch, restore_http_client
):
    """
    Bout en bout : l'identifiant posé par le middleware est celui écrit en
    base ET celui transmis au backend. Trois identifiants différents pour une
    seule requête était exactement le défaut d'origine.
    """
    telemetry.reset_all()
    vues: list[httpx.Request] = []
    lignes: list[dict] = []
    taches: list[asyncio.Task] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        vues.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
        )

    async def _capture(**kwargs) -> None:
        lignes.append(dict(kwargs))

    def _plan(coro, name=None):
        taches.append(asyncio.get_running_loop().create_task(coro, name=name))
        return taches[-1]

    monkeypatch.setattr(proxy.db, "log_usage", _capture)
    monkeypatch.setattr(proxy, "fire_and_forget", _plan)
    proxy.set_http_client(
        httpx.AsyncClient(transport=httpx.MockTransport(_handler), timeout=5.0)
    )

    manager = _FakeManager()
    request_context.set_request_id("req_bout_en_bout")
    try:
        response = await proxy.proxy_request(
            _FakeRequest(json.dumps({"model": "test-model"}).encode()),
            "/v1/chat/completions",
            {"user_id": "u1", "key_id": "k1"},
            _EnsureManager(manager),
        )
    finally:
        request_context.set_request_id("")

    assert response.status_code == 200
    await asyncio.gather(*taches, return_exceptions=True)

    assert len(lignes) == 1
    assert lignes[0]["request_id"] == "req_bout_en_bout"

    assert len(vues) == 1
    assert vues[0].headers[REQUEST_ID_HEADER] == "req_bout_en_bout"
    # Contrôle positif : l'authentification interne est toujours là, donc
    # l'ajout de l'en-tête n'a pas remplacé les en-têtes existants.
    assert vues[0].headers["Authorization"] == "Bearer test-internal"
    telemetry.reset_all()


def test_upstream_headers_sans_identifiant_reste_valide():
    """
    Hors contexte HTTP (tâche de fond, test unitaire), l'en-tête n'est pas
    ajouté plutôt que posé vide : un en-tête vide se journalise comme une
    corrélation existante qui ne mène nulle part.
    """
    headers = proxy._upstream_headers(_FakeManager(), "")

    assert REQUEST_ID_HEADER not in headers
    assert headers["Authorization"] == "Bearer test-internal"
