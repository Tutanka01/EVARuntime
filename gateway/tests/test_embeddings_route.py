"""
Tests de `POST /v1/embeddings` et `GET /v1/models/{model_id}`.

Contexte : le registre accepte la capability `embeddings`, le catalogue
bootstrap la propose et les deux fichiers nginx routent explicitement
`/v1/embeddings` — mais la route n'existait pas côté FastAPI. Un opérateur
pouvait déclarer un modèle d'embedding, le voir `ready` et recevoir un 404.

Ce que ces tests verrouillent :
  - la route existe, est authentifiée (401 sans clé) et rate-limitée par la
    même dépendance que les autres `/v1/*` ;
  - un modèle qui ne déclare pas `embeddings` est refusé AVANT tout
    chargement de VRAM, avec une enveloppe d'erreur OpenAI ;
  - le corps est proxifié tel quel vers `/v1/embeddings` du backend, l'ID
    public remplace l'identifiant interne, et UNE ligne d'usage terminale est
    journalisée avec les tokens prompt et le `request_id` ;
  - `stream: true` ne pousse pas la requête sur le chemin SSE ;
  - `GET /v1/models/{id}` répond pour un id activé et 404 sinon ;
  - le dashboard n'envoie aucune capability absente de `MODEL_CAPABILITIES`.

Technique : `httpx.MockTransport` injecté dans le client partagé via
`proxy.set_http_client(...)` → aucun llama-server réel. Le `model_manager` de
`main` est remplacé par un double qui expose un registre minimal et compte les
chargements, pour prouver que le refus de capability n'en déclenche aucun.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import main
import proxy
from auth import get_current_user
from model_registry import MODEL_CAPABILITIES
from rate_limiter import check_rate_limit

DASHBOARD = Path(__file__).resolve().parents[1] / "static" / "dashboard.html"


# ── Doubles de test ───────────────────────────────────────────────────────────

class FakeModel:
    def __init__(
        self,
        model_id: str,
        capabilities: tuple[str, ...] = ("text_generation",),
        *,
        enabled: bool = True,
        description: str = "",
    ) -> None:
        self.id = model_id
        self.capabilities = list(capabilities)
        self.enabled = enabled
        self.description = description


class FakeRegistry:
    def __init__(self, models: list[FakeModel]) -> None:
        self._models = models

    def get(self, model_id: str) -> FakeModel | None:
        return next((m for m in self._models if m.id == model_id), None)

    def list_enabled(self) -> list[FakeModel]:
        return [m for m in self._models if m.enabled]

    def first_enabled_id(self) -> str | None:
        enabled = self.list_enabled()
        return enabled[0].id if enabled else None


class FakeServerManager:
    """ServerManager minimal : compte pin/unpin pour vérifier l'équilibre."""

    def __init__(self, model: FakeModel, port: int = 8081) -> None:
        self.model = model
        self.port = port
        self.pin_calls = 0
        self.unpin_calls = 0

    def pin(self) -> None:
        self.pin_calls += 1

    def unpin(self) -> None:
        self.unpin_calls += 1

    def llama_url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-internal"}


class FakeModelManager:
    """ModelManager minimal : registre + comptage des chargements."""

    def __init__(self, models: list[FakeModel]) -> None:
        self.registry = FakeRegistry(models)
        self.load_calls: list[str] = []
        self.managers: dict[str, FakeServerManager] = {}

    async def ensure_model_loaded(self, model_id: str) -> FakeServerManager:
        self.load_calls.append(model_id)
        model = self.registry.get(model_id)
        if model is None:
            raise LookupError(f"Modèle '{model_id}' introuvable.")
        return self.managers.setdefault(model_id, FakeServerManager(model))


async def _fake_user() -> dict:
    return {"user_id": 7, "key_id": 3, "username": "test", "rpm_limit": 0}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def usage_rows(monkeypatch) -> list[dict]:
    """
    Capture les lignes d'usage sans DB ni ordonnanceur.

    `db.log_usage` enregistre ses kwargs à l'APPEL (donc de façon synchrone et
    déterministe, même sous TestClient) et renvoie une coroutine inerte que le
    `fire_and_forget` neutralisé referme aussitôt.
    """
    rows: list[dict] = []

    def _record(**kwargs):
        rows.append(dict(kwargs))

        async def _noop() -> None:
            return None

        return _noop()

    def _swallow(coro, name=None):
        if asyncio.iscoroutine(coro):
            coro.close()
        return None

    monkeypatch.setattr(proxy.db, "log_usage", _record)
    monkeypatch.setattr(proxy, "fire_and_forget", _swallow)
    return rows


@pytest.fixture
def restore_http_client():
    """Restaure le client partagé après injection d'un MockTransport."""
    saved = proxy._http_client
    proxy.set_http_client(None)
    yield
    proxy.set_http_client(saved)


@pytest.fixture
def authenticated_client(monkeypatch):
    """
    TestClient avec l'authentification court-circuitée.

    `check_rate_limit` ET `get_current_user` sont surchargés : les routes
    d'inférence dépendent de la première, `/v1/models*` de la seconde.
    """
    def _build(manager: FakeModelManager) -> TestClient:
        monkeypatch.setattr(main, "model_manager", manager)
        main.app.dependency_overrides[check_rate_limit] = _fake_user
        main.app.dependency_overrides[get_current_user] = _fake_user
        return TestClient(main.app)

    yield _build
    main.app.dependency_overrides.clear()


def _capture_backend(response_json: dict, status_code: int = 200) -> list[httpx.Request]:
    """Injecte un backend factice et retourne la liste des requêtes reçues."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_code, json=response_json)

    proxy.set_http_client(
        httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    )
    return seen


def _embedding_payload(model_id: str = "embed-model") -> dict:
    return {
        "object": "list",
        "model": "interne-ne-doit-pas-fuiter",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
        "usage": {"prompt_tokens": 11, "total_tokens": 11},
    }


# ── 1. Présence et protection de la route ─────────────────────────────────────

def _iter_app_routes(router):
    """
    Énumère `(méthode, chemin)` récursivement.

    Depuis FastAPI 0.141, `include_router()` dépose un `_IncludedRouter` dans
    `app.routes` et les routes incluses ne sont atteignables que via
    `original_router` : une lecture directe de `app.routes` n'en verrait
    aucune (cf. AGENTS.md).
    """
    for route in getattr(router, "routes", []) or []:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _iter_app_routes(original)
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        for method in getattr(route, "methods", None) or ():
            yield (method, path)


def test_les_routes_embeddings_et_retrieve_model_existent():
    available = set(_iter_app_routes(main.app))

    assert ("POST", "/v1/embeddings") in available
    assert ("GET", "/v1/models/{model_id}") in available
    # Contrôle positif : le parcours voit aussi un routeur inclus, donc il ne
    # peut pas conclure « présent » sur un parcours devenu aveugle.
    assert ("GET", "/admin/status") in available


def test_embeddings_est_rate_limitee_comme_les_autres_routes_v1():
    """
    La route doit dépendre de `check_rate_limit`, pas du simple
    `get_current_user` : sinon elle serait authentifiée mais sans débit borné,
    et un client pourrait saturer le GPU en contournant la limite.
    """
    route = next(
        r for r in _iter_app_routes_objects(main.app)
        if getattr(r, "path", None) == "/v1/embeddings"
    )
    dependencies = {
        dep.call for dep in route.dependant.dependencies
    } | {
        sub.call
        for dep in route.dependant.dependencies
        for sub in dep.dependencies
    }

    assert check_rate_limit in dependencies
    # Contrôle positif : la même introspection voit bien une dépendance sur une
    # route dont on sait qu'elle n'utilise QUE get_current_user.
    models_route = next(
        r for r in _iter_app_routes_objects(main.app)
        if getattr(r, "path", None) == "/v1/models"
    )
    assert get_current_user in {
        dep.call for dep in models_route.dependant.dependencies
    }


def _iter_app_routes_objects(router):
    """Variante de `_iter_app_routes` qui rend les objets route eux-mêmes."""
    for route in getattr(router, "routes", []) or []:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _iter_app_routes_objects(original)
            continue
        yield route


def test_embeddings_refuse_sans_cle_api():
    client = TestClient(main.app)
    response = client.post("/v1/embeddings", json={"model": "x", "input": "coucou"})

    assert response.status_code == 401


def test_retrieve_model_refuse_sans_cle_api():
    client = TestClient(main.app)
    response = client.get("/v1/models/x")

    assert response.status_code == 401


# ── 2. Garde de capability ────────────────────────────────────────────────────

def test_embeddings_refuse_un_modele_sans_la_capability(
    authenticated_client, restore_http_client, usage_rows,
):
    """
    Refus AVANT chargement : charger plusieurs Go de VRAM pour répondre 400
    offrirait un déni de service GPU à coût nul.
    """
    manager = FakeModelManager([FakeModel("chat-model", ("text_generation",))])
    seen = _capture_backend(_embedding_payload())
    client = authenticated_client(manager)

    response = client.post(
        "/v1/embeddings", json={"model": "chat-model", "input": "coucou"}
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "400"
    assert "embeddings" in body["error"]["message"]
    assert manager.load_calls == [], "le modèle ne doit pas être chargé pour un refus"
    assert seen == [], "aucune requête ne doit atteindre le backend"
    assert usage_rows == []


def test_embeddings_accepte_un_modele_qui_declare_la_capability(
    authenticated_client, restore_http_client, usage_rows,
):
    manager = FakeModelManager([
        FakeModel("embed-model", ("text_generation", "embeddings")),
    ])
    seen = _capture_backend(_embedding_payload())
    client = authenticated_client(manager)

    response = client.post(
        "/v1/embeddings", json={"model": "embed-model", "input": ["a", "b"]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["embedding"] == [0.1, 0.2, 0.3]
    # L'identifiant interne du backend est remplacé par l'ID public.
    assert body["model"] == "embed-model"

    # Proxy : bon chemin, corps relayé tel quel.
    assert len(seen) == 1
    assert seen[0].url.path == "/v1/embeddings"
    assert json.loads(seen[0].content) == {"model": "embed-model", "input": ["a", "b"]}

    # Pin/unpin équilibrés autour de la requête.
    server = manager.managers["embed-model"]
    assert server.pin_calls == server.unpin_calls == 1


def test_embeddings_journalise_une_seule_ligne_dusage(
    authenticated_client, restore_http_client, usage_rows,
):
    """
    Une réponse d'embeddings ne porte pas de `completion_tokens` : la ligne
    d'usage doit retenir les tokens prompt et poser 0 en complétion, sans
    jamais écrire deux résultats terminaux pour une requête.
    """
    manager = FakeModelManager([FakeModel("embed-model", ("embeddings",))])
    _capture_backend(_embedding_payload())
    client = authenticated_client(manager)

    response = client.post(
        "/v1/embeddings", json={"model": "embed-model", "input": "coucou"}
    )

    assert response.status_code == 200
    assert len(usage_rows) == 1
    row = usage_rows[0]
    assert row["model"] == "embed-model"
    assert row["prompt_tokens"] == 11
    assert row["completion_tokens"] == 0
    assert row["status_code"] == 200
    assert row["user_id"] == 7
    assert row["key_id"] == 3
    assert row["request_id"]


def test_embeddings_ignore_stream_et_ne_le_forwarde_pas(
    authenticated_client, restore_http_client, usage_rows,
):
    """
    `stream: true` ne doit ni produire un flux SSE (le backend n'en émet pas
    sur cette route), ni partir vers llama-server.
    """
    manager = FakeModelManager([FakeModel("embed-model", ("embeddings",))])
    seen = _capture_backend(_embedding_payload())
    client = authenticated_client(manager)

    response = client.post(
        "/v1/embeddings",
        json={"model": "embed-model", "input": "coucou", "stream": True},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    forwarded = json.loads(seen[0].content)
    assert "stream" not in forwarded
    assert "stream_options" not in forwarded


def test_embeddings_relaie_une_erreur_backend_en_enveloppe_openai(
    authenticated_client, restore_http_client, usage_rows,
):
    manager = FakeModelManager([FakeModel("embed-model", ("embeddings",))])
    _capture_backend({"error": {"message": "pooling non configuré"}}, status_code=500)
    client = authenticated_client(manager)

    response = client.post(
        "/v1/embeddings", json={"model": "embed-model", "input": "coucou"}
    )

    assert response.status_code == 500
    assert response.json()["error"]["message"] == "pooling non configuré"
    assert len(usage_rows) == 1
    assert usage_rows[0]["status_code"] == 500


def test_embeddings_modele_inconnu_reste_un_404_model_not_found(
    authenticated_client, restore_http_client, usage_rows,
):
    """
    La garde de capability ne tranche pas un id absent du registre : le 404
    `model_not_found` doit rester identique à celui des autres routes.
    """
    manager = FakeModelManager([FakeModel("embed-model", ("embeddings",))])
    _capture_backend(_embedding_payload())
    client = authenticated_client(manager)

    response = client.post(
        "/v1/embeddings", json={"model": "inconnu", "input": "coucou"}
    )

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "model_not_found"


# ── 3. GET /v1/models/{model_id} ──────────────────────────────────────────────

def test_retrieve_model_retourne_un_modele_active(authenticated_client):
    manager = FakeModelManager([
        FakeModel("embed-model", ("embeddings",), description="Embeddings maison"),
    ])
    client = authenticated_client(manager)

    response = client.get("/v1/models/embed-model")

    assert response.status_code == 200
    assert response.json() == {
        "id": "embed-model",
        "object": "model",
        "created": 1704067200,
        "owned_by": "local-uppa",
        "description": "Embeddings maison",
    }


def test_retrieve_model_est_coherent_avec_la_liste(authenticated_client):
    """L'entrée unitaire doit être exactement celle que renvoie `/v1/models`."""
    manager = FakeModelManager([FakeModel("embed-model", ("embeddings",))])
    client = authenticated_client(manager)

    listed = client.get("/v1/models").json()["data"]
    single = client.get("/v1/models/embed-model").json()

    assert single in listed


def test_retrieve_model_404_sur_identifiant_inconnu(authenticated_client):
    manager = FakeModelManager([FakeModel("embed-model", ("embeddings",))])
    client = authenticated_client(manager)

    response = client.get("/v1/models/inexistant")

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "model_not_found"


def test_retrieve_model_404_sur_modele_desactive(authenticated_client):
    """`/v1/models` n'annonce pas les modèles désactivés ; l'unitaire non plus."""
    manager = FakeModelManager([
        FakeModel("embed-model", ("embeddings",), enabled=False),
    ])
    client = authenticated_client(manager)

    response = client.get("/v1/models/embed-model")

    assert response.status_code == 404


# ── 4. Cohérence du dashboard avec MODEL_CAPABILITIES ─────────────────────────

def _dashboard_form_capabilities() -> set[str]:
    """Valeurs des cases à cocher du formulaire « Register New Model »."""
    source = DASHBOARD.read_text(encoding="utf-8")
    group = re.search(
        r'id="caps-group".*?</div>', source, re.DOTALL
    )
    assert group is not None, "bloc caps-group introuvable — sélecteur à mettre à jour"
    return set(re.findall(r'value="([^"]+)"', group.group(0)))


def _dashboard_cap_label_keys() -> set[str]:
    """Clés de `CAP_LABELS`, qui libellent les capabilities dans la modale Info."""
    source = DASHBOARD.read_text(encoding="utf-8")
    block = re.search(r"const CAP_LABELS = \{(.*?)\};", source, re.DOTALL)
    assert block is not None, "CAP_LABELS introuvable — sélecteur à mettre à jour"
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", block.group(1), re.M))


def test_le_dashboard_nenvoie_que_des_capabilities_connues():
    """
    Le formulaire poste ses valeurs telles quelles à l'API admin, dont le
    schéma refuse toute capability inconnue : une valeur orpheline ici (c'était
    `embedding` au singulier) fait partir le formulaire en 422.
    """
    form = _dashboard_form_capabilities()

    # Contrôle positif : l'extraction voit réellement des valeurs, et une
    # valeur dont on sait qu'elle est valide.
    assert form, "aucune capability extraite — le test serait inerte"
    assert "text_generation" in form

    assert form <= MODEL_CAPABILITIES, (
        f"capabilities inconnues dans le formulaire : "
        f"{sorted(form - MODEL_CAPABILITIES)}"
    )


def test_le_dashboard_libelle_toutes_les_capabilities_connues():
    """
    `CAP_LABELS` sert de libellé dans la modale Info. Une clé mal orthographiée
    laissait un modèle correctement déclaré en YAML s'afficher sans nom.
    """
    labels = _dashboard_cap_label_keys()

    assert labels, "aucune clé extraite — le test serait inerte"
    assert "text_generation" in labels

    assert MODEL_CAPABILITIES <= labels, (
        f"capabilities sans libellé : {sorted(MODEL_CAPABILITIES - labels)}"
    )
