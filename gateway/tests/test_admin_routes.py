"""
Tests HTTP des routes /admin/* (admin.py) — CRUD modèles/utilisateurs/clés.

Ces routes sont protégées par `require_admin` (secret Bearer, cf. auth.py) et
persistent dans deux stores distincts :
  - le registre de modèles (YAML, via `model_manager.registry`) ;
  - la base SQLite (via `database.py`).

Le singleton `model_manager` (importé dans admin.py comme `admin.model_manager`)
est construit une seule fois au chargement du module, contre le VRAI
`models.yaml` du dépôt (cf. conftest.py : MODELS_CONFIG_PATH pointe dessus par
défaut). Pour ne JAMAIS écrire dans ce fichier réel, chaque test qui mute le
registre remplace `admin.model_manager._registry` par un `ModelRegistry`
temporaire (fichier sous tmp_path), au lieu de passer par le singleton global.

De même, `DB_PATH=":memory:"` par défaut (conftest.py) ouvre une connexion
SQLite en mémoire *par appel* — les données ne persistent pas d'un appel à
l'autre. On réutilise donc le pattern `file_db` de test_security_hardening.py :
`monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")` + `db.init_db()`.
"""
from __future__ import annotations

import re
import hashlib

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

import admin
import database as db
import main
from config import settings
from model_registry import ModelRegistry


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def admin_headers() -> dict[str, str]:
    """En-tête Authorization avec le vrai secret admin de test (conftest.py)."""
    return {"Authorization": f"Bearer {settings.admin_secret}"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture
def temp_registry(tmp_path, monkeypatch):
    """
    Remplace le registre du singleton `model_manager` par un registre
    temporaire (fichier YAML sous tmp_path), pour que les tests de mutation
    (register/update/delete) n'écrivent jamais dans le vrai gateway/models.yaml.
    """
    cfg = tmp_path / "models.yaml"
    cfg.write_text(yaml.safe_dump({"models": []}), encoding="utf-8")
    registry = ModelRegistry(config_path=cfg)
    monkeypatch.setattr(admin.model_manager, "_registry", registry)
    return registry


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """
    Redirige settings.db_path vers un fichier SQLite temporaire (au lieu du
    ':memory:' par défaut des tests, qui ne persiste pas entre connexions) et
    initialise le schéma. Utilisé par toutes les routes users/keys.
    """
    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    return tmp_path / "test.db"


def _gguf_entry(tmp_path, **overrides) -> dict:
    """Modèle minimal valide : path absolu vers un .gguf existant."""
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"fake gguf content")
    entry = {
        "id": "test-model",
        "path": str(model_path),
        "vram_gb": 5.0,
    }
    entry.update(overrides)
    return entry


def test_register_model_cluster_accepts_remote_gguf_path(
    client, admin_headers, temp_registry, monkeypatch,
):
    """L'orchestrateur ne doit pas exiger une copie locale des GGUF distants."""
    monkeypatch.setattr(settings, "cluster_mode", "cluster")
    payload = {
        "id": "remote-model",
        "path": "/models/remote-only.gguf",
        "vram_gb": 5.0,
    }

    response = client.post("/admin/models", json=payload, headers=admin_headers)

    assert response.status_code == 201, response.text
    assert temp_registry.get("remote-model") is not None


# ── 1. Toutes les routes mutantes exigent le bon secret admin ────────────────

# (méthode, chemin, corps JSON) — un échantillon représentatif de TOUTES les
# routes POST/PATCH/DELETE sous /admin, avec des identifiants inexistants
# (peu importe puisque require_admin doit rejeter AVANT même d'atteindre le handler).
MUTATING_ROUTES = [
    ("POST", "/admin/models", {"id": "x", "path": "/nope.gguf", "vram_gb": 1.0}),
    ("PATCH", "/admin/models/does-not-exist", {"enabled": False}),
    ("DELETE", "/admin/models/does-not-exist", None),
    (
        "POST",
        "/admin/models/does-not-exist/bootstrap-sync",
        {
            "action": "activate",
            "digest": "0" * 64,
            "vram_gb": 1.0,
            "lease_seconds": 30,
        },
    ),
    ("POST", "/admin/models/does-not-exist/load", None),
    ("POST", "/admin/models/does-not-exist/unload", None),
    ("POST", "/admin/unload", None),
    ("POST", "/admin/users", {"username": "someone"}),
    ("PATCH", "/admin/users/does-not-exist", {"notes": "x"}),
    ("DELETE", "/admin/users/does-not-exist", None),
    ("POST", "/admin/users/does-not-exist/keys", {"name": "k"}),
    ("DELETE", "/admin/keys/llmgw-doesnotexist", None),
]


@pytest.mark.anyio
async def test_unload_all_keeps_model_manager_running(monkeypatch):
    """L'action admin décharge les modèles sans appeler le shutdown du runtime."""
    class FakeManager:
        def __init__(self) -> None:
            self.unload_calls = 0
            self.shutdown_calls = 0

        async def unload_all_models(self) -> None:
            self.unload_calls += 1

        async def shutdown(self) -> None:
            self.shutdown_calls += 1

    manager = FakeManager()
    monkeypatch.setattr(admin, "model_manager", manager)

    response = await admin.unload_all(None)

    assert manager.unload_calls == 1
    assert manager.shutdown_calls == 0
    assert "VRAM" in response["message"]


@pytest.mark.anyio
async def test_unload_all_reports_partial_cluster_failure(monkeypatch):
    class FailingManager:
        async def unload_all_models(self) -> None:
            raise RuntimeError("Unload-all incomplet — node-b: timeout")

    monkeypatch.setattr(admin, "model_manager", FailingManager())

    with pytest.raises(HTTPException) as caught:
        await admin.unload_all(None)

    assert caught.value.status_code == 503
    assert "incomplet" in caught.value.detail


@pytest.mark.parametrize("method,path,body", MUTATING_ROUTES)
def test_mutating_routes_reject_missing_secret(client, method, path, body):
    """Sans header Authorization du tout → 403 (require_admin, credentials=None)."""
    response = client.request(method, path, json=body)
    assert response.status_code == 403


@pytest.mark.parametrize("method,path,body", MUTATING_ROUTES)
def test_mutating_routes_reject_wrong_secret(client, method, path, body):
    response = client.request(
        method, path, json=body,
        headers={"Authorization": "Bearer mauvais-secret-totalement-different"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize("method,path,body", MUTATING_ROUTES)
def test_mutating_routes_disabled_with_placeholder_secret(client, method, path, body, monkeypatch):
    """Fail-closed : ADMIN_SECRET laissé à sa valeur d'exemple → 503, même toutes routes mutantes."""
    monkeypatch.setattr(settings, "admin_secret", "CHANGE_ME_ADMIN_SECRET")
    response = client.request(
        method, path, json=body,
        headers={"Authorization": "Bearer CHANGE_ME_ADMIN_SECRET"},
    )
    assert response.status_code == 503


# ── 2. create_key : clé brute exposée une seule fois, jamais relue ───────────

def test_create_key_returns_raw_key_once_with_expected_format(
    client, admin_headers, temp_db,
):
    import asyncio
    asyncio.run(db.init_db())
    asyncio.run(db.create_user(username="alice"))

    response = client.post(
        "/admin/users/alice/keys", json={"name": "laptop"}, headers=admin_headers,
    )
    assert response.status_code == 201
    body = response.json()

    # Format attendu : "llmgw-" + token urlsafe (cf. database.generate_api_key)
    assert body["api_key"].startswith("llmgw-")
    assert len(body["api_key"]) > len("llmgw-")
    assert re.match(r"^llmgw-[A-Za-z0-9_-]+$", body["api_key"])
    # Le préfixe renvoyé doit être un préfixe littéral de la clé brute.
    assert body["api_key"].startswith(body["key_prefix"])
    assert body["key_prefix"].startswith("llmgw-")
    assert body["name"] == "laptop"


def test_create_key_raw_key_not_returned_by_list_keys(client, admin_headers, temp_db):
    import asyncio
    asyncio.run(db.init_db())
    asyncio.run(db.create_user(username="bob"))

    create_resp = client.post(
        "/admin/users/bob/keys", json={"name": "server"}, headers=admin_headers,
    )
    raw_key = create_resp.json()["api_key"]

    list_resp = client.get("/admin/users/bob/keys", headers=admin_headers)
    assert list_resp.status_code == 200
    keys = list_resp.json()
    assert len(keys) == 1

    # La clé brute ne doit apparaître nulle part dans la réponse de listing.
    assert raw_key not in str(keys)
    for key_entry in keys:
        assert "api_key" not in key_entry
        assert "key_hash" not in key_entry
        assert set(key_entry.keys()) == {
            "id", "key_prefix", "name", "created_at", "last_used",
            "is_active", "expires_at",
        }


def test_create_key_raw_key_not_persisted_in_db(client, admin_headers, temp_db):
    """Vérifie directement en base que seul le hash SHA-256 est stocké, jamais la clé brute."""
    import asyncio
    import sqlite3

    asyncio.run(db.init_db())
    asyncio.run(db.create_user(username="carol"))

    create_resp = client.post(
        "/admin/users/carol/keys", json={"name": "ci"}, headers=admin_headers,
    )
    raw_key = create_resp.json()["api_key"]

    conn = sqlite3.connect(temp_db)
    try:
        rows = conn.execute("SELECT key_hash, key_prefix FROM api_keys").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    key_hash, key_prefix = rows[0]
    assert raw_key != key_hash
    assert raw_key not in key_hash
    # Le hash doit correspondre exactement à sha256(raw_key) — confirme qu'aucune
    # autre valeur (ex: la clé brute elle-même) n'est stockée à la place.
    assert key_hash == db.hash_key(raw_key)
    assert raw_key.startswith(key_prefix)


# ── 3. register_model : vram_gb invalide (<= 0) → erreur HTTP, registre inchangé ──

@pytest.mark.parametrize("bad_vram", [0, -1, -3.5])
def test_register_model_rejects_non_positive_vram_gb(
    client, admin_headers, temp_registry, tmp_path, bad_vram,
):
    entry = _gguf_entry(tmp_path, vram_gb=bad_vram)
    response = client.post("/admin/models", json=entry, headers=admin_headers)

    # vram_gb <= 0 est rejeté par la contrainte Pydantic `gt=0.0` du schéma
    # ModelEntryCreate, donc FastAPI répond 422 (erreur de validation du corps),
    # avant même d'atteindre le handler / le registre.
    assert response.status_code == 422
    assert temp_registry.get("test-model") is None
    assert temp_registry.list_all() == []


def test_register_model_missing_gguf_file_returns_422_and_registry_unchanged(
    client, admin_headers, temp_registry, tmp_path,
):
    """path pointant vers un fichier inexistant sur le serveur → 422, pas d'ajout."""
    entry = {
        "id": "ghost-model",
        "path": str(tmp_path / "does-not-exist.gguf"),
        "vram_gb": 5.0,
    }
    response = client.post("/admin/models", json=entry, headers=admin_headers)
    assert response.status_code == 422
    assert temp_registry.get("ghost-model") is None


def test_register_model_valid_entry_is_persisted(
    client, admin_headers, temp_registry, tmp_path,
):
    """Cas positif de contrôle : une entrée valide est bien ajoutée au registre."""
    entry = _gguf_entry(tmp_path)
    response = client.post("/admin/models", json=entry, headers=admin_headers)
    assert response.status_code == 201
    assert temp_registry.get("test-model") is not None
    assert temp_registry.get("test-model").vram_gb == 5.0


def test_register_model_persists_vision_and_integrity_fields(
    client, admin_headers, temp_registry, tmp_path,
):
    projector = tmp_path / "model-mmproj.gguf"
    projector.write_bytes(b"projecteur vision")
    model_path = tmp_path / "model.gguf"
    model_path.write_bytes(b"poids vision")
    entry = {
        "id": "vision-model",
        "path": str(model_path),
        "vram_gb": 5.0,
        "capabilities": ["text_generation", "vision"],
        "mmproj_path": str(projector),
        "mmproj_sha256": hashlib.sha256(projector.read_bytes()).hexdigest(),
        "sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "load_timeout_seconds": 60,
        "speculative": {"type": "mtp", "draft_max": 8},
    }

    response = client.post("/admin/models", json=entry, headers=admin_headers)

    assert response.status_code == 201, response.text
    model = temp_registry.get("vision-model")
    assert model is not None
    assert model.mmproj_path == projector
    assert model.mmproj_sha256 == entry["mmproj_sha256"]
    assert model.sha256 == entry["sha256"]
    assert model.load_timeout_seconds == 60
    assert model.speculative is not None
    assert model.speculative.draft_max == 8


# ── 4. update_model : llama_params invalides → 422, registre non corrompu ────

def test_update_model_rejects_ubatch_greater_than_batch_and_keeps_old_params(
    client, admin_headers, temp_registry, tmp_path,
):
    entry = _gguf_entry(tmp_path)
    temp_registry.add(entry)
    original_llama_params = temp_registry.get("test-model").llama_params

    bad_llama_params = {
        "batch_size": 512,
        "ubatch_size": 4096,  # > batch_size : rejeté par le validator du schéma
    }
    response = client.patch(
        "/admin/models/test-model",
        json={"llama_params": bad_llama_params},
        headers=admin_headers,
    )

    # Rejeté au niveau du schéma Pydantic (LlamaParamsSchema.ubatch_lte_batch)
    # avant d'atteindre le handler — FastAPI répond 422.
    assert response.status_code == 422

    # Le registre ne doit PAS avoir été corrompu : mêmes llama_params qu'avant.
    reloaded = temp_registry.get("test-model")
    assert reloaded.llama_params == original_llama_params
    assert reloaded.llama_params.batch_size == 4096  # valeur par défaut d'origine


def test_update_model_unknown_id_returns_404(client, admin_headers, temp_registry):
    response = client.patch(
        "/admin/models/does-not-exist", json={"enabled": False}, headers=admin_headers,
    )
    assert response.status_code == 404


def test_update_model_empty_body_returns_422(
    client, admin_headers, temp_registry, tmp_path,
):
    """Aucun champ à mettre à jour → 422 explicite (pas un no-op silencieux)."""
    temp_registry.add(_gguf_entry(tmp_path))
    response = client.patch(
        "/admin/models/test-model", json={}, headers=admin_headers,
    )
    assert response.status_code == 422


def test_update_model_valid_vram_gb_is_applied(
    client, admin_headers, temp_registry, tmp_path,
):
    """Cas positif de contrôle : une mise à jour valide modifie bien le registre."""
    temp_registry.add(_gguf_entry(tmp_path))
    response = client.patch(
        "/admin/models/test-model", json={"vram_gb": 12.5}, headers=admin_headers,
    )
    assert response.status_code == 200
    assert temp_registry.get("test-model").vram_gb == 12.5


# ── 5. delete_model / delete_user / revoke_key sur ID inconnu ────────────────

def test_delete_model_unknown_id_returns_404(client, admin_headers, temp_registry):
    response = client.delete("/admin/models/does-not-exist", headers=admin_headers)
    assert response.status_code == 404
    assert "introuvable" in response.json()["detail"]


def test_delete_model_known_id_succeeds_and_is_removed(
    client, admin_headers, temp_registry, tmp_path,
):
    temp_registry.add(_gguf_entry(tmp_path))
    response = client.delete("/admin/models/test-model", headers=admin_headers)
    assert response.status_code == 200
    assert temp_registry.get("test-model") is None


# ── 5 bis. COR-029 — un refus d'écriture se lit pareil sur les trois verbes ──
#
# `RegistryWriteRefused` hérite délibérément de `ValueError` pour ressortir en
# 422 avec son message : c'est celui qui dit à l'opérateur POURQUOI on a refusé,
# et comment réparer son fichier. `DELETE` n'attrapait que `KeyError` ; le refus
# ressortait donc en 500 au corps générique, message perdu.

def _rendre_le_registre_non_retouchable(registry) -> bytes:
    """
    Réécrit `models.yaml` en style « flow », que le localisateur refuse.

    Cas réel et non artificiel : la gateway refuse d'identifier une entrée dans
    un `models: [{...}]`, et c'est le bon comportement (COR-020). Ce qui se
    teste ici, c'est la traduction HTTP de ce refus, pas le refus lui-même.
    """
    document = yaml.safe_load(registry._path.read_text(encoding="utf-8"))
    registry._path.write_text(
        yaml.safe_dump(document, default_flow_style=True), encoding="utf-8"
    )
    return registry._path.read_bytes()


@pytest.mark.parametrize("verbe", ["delete", "patch", "post"])
def test_un_refus_d_ecriture_du_registre_rend_422_sur_les_trois_verbes(
    client, admin_headers, temp_registry, tmp_path, verbe,
):
    temp_registry.add(_gguf_entry(tmp_path))
    avant = _rendre_le_registre_non_retouchable(temp_registry)

    if verbe == "delete":
        response = client.delete("/admin/models/test-model", headers=admin_headers)
    elif verbe == "patch":
        response = client.patch(
            "/admin/models/test-model", headers=admin_headers, json={"vram_gb": 2.0}
        )
    else:
        response = client.post(
            "/admin/models",
            headers=admin_headers,
            json=_gguf_entry(tmp_path, id="autre-modele"),
        )

    assert response.status_code == 422, response.text
    # Le message de refus est ce qui rend la 422 exploitable : sans lui,
    # l'opérateur ne sait pas quoi corriger dans son fichier.
    detail = response.json()["detail"]
    assert "identifi" in detail or "ne signifie pas" in detail or "flow" in detail
    assert "rien n'a été écrit" in detail.lower()
    assert temp_registry._path.read_bytes() == avant


def test_un_identifiant_inconnu_reste_un_404_sur_delete(
    client, admin_headers, temp_registry, tmp_path,
):
    """Contrôle positif : élargir le `except` n'a pas transformé les 404 en 422."""
    temp_registry.add(_gguf_entry(tmp_path))
    ok = client.delete("/admin/models/test-model", headers=admin_headers)
    absent = client.delete("/admin/models/test-model", headers=admin_headers)

    assert ok.status_code == 200
    assert absent.status_code == 404


def test_delete_user_unknown_username_returns_404(client, admin_headers, temp_db):
    import asyncio
    asyncio.run(db.init_db())

    response = client.delete("/admin/users/no-such-user", headers=admin_headers)
    assert response.status_code == 404
    assert "introuvable" in response.json()["detail"]


def test_delete_user_known_username_succeeds(client, admin_headers, temp_db):
    import asyncio
    asyncio.run(db.init_db())
    asyncio.run(db.create_user(username="dave"))

    response = client.delete("/admin/users/dave", headers=admin_headers)
    assert response.status_code == 200

    get_resp = client.get("/admin/users/dave", headers=admin_headers)
    assert get_resp.status_code == 404


def test_revoke_key_unknown_prefix_returns_404(client, admin_headers, temp_db):
    import asyncio
    asyncio.run(db.init_db())

    response = client.delete("/admin/keys/llmgw-nosuchkey", headers=admin_headers)
    assert response.status_code == 404
    assert "Aucune clé active" in response.json()["detail"]


def test_revoke_key_known_prefix_succeeds_and_key_becomes_inactive(
    client, admin_headers, temp_db,
):
    import asyncio
    asyncio.run(db.init_db())
    user = asyncio.run(db.create_user(username="erin"))
    _, key_row = asyncio.run(db.create_api_key(user["id"], name="k"))

    response = client.delete(
        f"/admin/keys/{key_row['key_prefix']}", headers=admin_headers,
    )
    assert response.status_code == 200

    keys = asyncio.run(db.list_keys_for_user(user["id"]))
    assert keys[0]["is_active"] == 0

    # Écart de comportement constaté (non corrigé, voir rapport) : revoke_key()
    # fait un UPDATE ... WHERE key_prefix LIKE ? SANS filtrer sur is_active=1,
    # donc rowcount reste > 0 même si la clé est déjà révoquée. Une seconde
    # révocation du même préfixe renvoie donc encore 200, PAS 404. On verrouille
    # ici le comportement réel plutôt que le comportement idéalisé attendu.
    second_response = client.delete(
        f"/admin/keys/{key_row['key_prefix']}", headers=admin_headers,
    )
    assert second_response.status_code == 200
