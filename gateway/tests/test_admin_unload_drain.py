"""
COR-004 — les opérations admin de déchargement ne tuent plus les requêtes actives.

Invariant testé (AGENTS.md, « Model Lifecycle Invariants ») : un modèle qui
traite une requête active ne doit pas être évincé. En mode local, les routes
admin appelaient `ServerManager.unload()` sans consulter `is_pinned`, alors que
`ClusterManager.unload_model()` refuse explicitement dès `active_requests > 0`.

Couverture :
  1. Reproduction du défaut : le déchargement admin attend la fin des requêtes
     actives au lieu de les interrompre (`active_requests_at_unload == 0`).
  2. Contrat de conflit : 409 explicite quand le drain expire, sur les quatre
     chemins admin (`/unload`, `DELETE`, `PATCH enabled:false`,
     `PATCH llama_params`), sans mutation partielle du registre.
  3. Bornage du drain (jamais bloquant) et absence de fuite de l'état de
     quarantaine après un refus.
  4. Forçage explicite opt-in (`?force=true`).

Les doubles suivent les patterns de test_model_manager_robustness.py :
`ServerManager` est remplacé par un fake, mais le `LocalModelManager` et le
`ModelRegistry` sont les vrais (registre YAML temporaire — jamais le
gateway/models.yaml du dépôt).
"""
from __future__ import annotations

import asyncio
import time

import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

import admin
import main
import model_manager as model_manager_module
from config import settings
from model_manager import LocalModelManager, ModelBusyError, ModelDrainingError
from model_registry import ModelRegistry
from server_manager import ModelState


# ── Doubles ───────────────────────────────────────────────────────────────────

class FakeServerManager:
    """
    Double de ServerManager avec un compteur de requêtes actives instrumenté.

    release_after_checks=None → la requête active ne se termine jamais (le drain
                                doit expirer et l'opération être refusée).
    release_after_checks=n    → `is_pinned` renvoie True n fois puis la requête
                                active se termine (stream qui finit pendant le
                                drain : l'unload doit alors réussir).
    """

    def __init__(
        self, model, port, on_unload=None, on_capacity_change=None, *,
        ready: bool = False, active_requests: int = 0,
        release_after_checks: int | None = None,
    ):
        self._model = model
        self._port = port
        self._on_unload = on_unload
        self._on_capacity_change = on_capacity_change
        self._state = ModelState.READY if ready else ModelState.UNLOADED
        self._active_requests = active_requests
        self._release_after_checks = release_after_checks
        self._pin_checks = 0

        self.unload_calls = 0
        self.unload_reasons: list[str] = []
        # Nombre de requêtes encore actives AU MOMENT du déchargement : c'est la
        # mesure directe du défaut COR-004 (doit valoir 0, jamais 1).
        self.active_requests_at_unload: int | None = None

    # ── API consommée par LocalModelManager ──────────────────────────────────

    @property
    def state(self):
        return self._state

    @property
    def model(self):
        return self._model

    @property
    def active_requests(self) -> int:
        return self._active_requests

    @property
    def is_pinned(self) -> bool:
        if self._active_requests > 0 and self._release_after_checks is not None:
            self._pin_checks += 1
            if self._pin_checks > self._release_after_checks:
                self._active_requests = 0
        return self._active_requests > 0

    async def ensure_loaded(self) -> None:
        self._state = ModelState.READY

    async def unload(self, reason: str = "test") -> None:
        self.unload_calls += 1
        self.unload_reasons.append(reason)
        self.active_requests_at_unload = self._active_requests
        self._state = ModelState.UNLOADED
        if self._on_unload:
            self._on_unload(self._model.id, reason)

    def pin(self) -> None:
        self._active_requests += 1

    def unpin(self) -> None:
        self._active_requests = max(0, self._active_requests - 1)

    def status(self) -> dict:
        return {
            "id": self._model.id,
            "description": self._model.description,
            "enabled": self._model.enabled,
            "vram_gb": self._model.vram_gb,
            "capabilities": self._model.capabilities,
            "state": self._state.value,
            "path": str(self._model.path),
            "pid": 4242,
            "port": self._port,
            "uptime_seconds": 12.0,
            "idle_seconds": 0.0,
            "active_requests": self._active_requests,
            "llama_params": None,
        }


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.admin_secret}"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture
def drain_settings(monkeypatch):
    """Drain court et poll rapide : les tests ne doivent jamais durer 5s."""
    monkeypatch.setattr(settings, "admin_unload_drain_timeout_seconds", 0.3)
    monkeypatch.setattr(settings, "shutdown_drain_poll_seconds", 0.01)
    monkeypatch.setattr(settings, "total_vram_gb", 40.0)
    monkeypatch.setattr(settings, "vram_overhead_gb", 0.0)
    monkeypatch.setattr(settings, "vram_safety_margin", 0.0)
    monkeypatch.setattr(settings, "max_loaded_models", 2)
    monkeypatch.setattr(settings, "base_llama_port", 18191)


@pytest.fixture
def local_manager(tmp_path, monkeypatch):
    """
    Vrai LocalModelManager + vrai ModelRegistry (YAML temporaire), injecté dans
    admin.py à la place du singleton. `ServerManager` est fake : aucun
    sous-processus llama-server n'est lancé.
    """
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"fake gguf")
    cfg = tmp_path / "models.yaml"
    cfg.write_text(yaml.safe_dump({"models": [{
        "id": "m1",
        "path": str(gguf),
        "description": "modele de test",
        "vram_gb": 5.0,
        "enabled": True,
        "capabilities": ["text_generation"],
        "llama_params": {"ctx_size": 4096, "parallel": 2},
    }]}), encoding="utf-8")

    monkeypatch.setattr(model_manager_module, "ServerManager", FakeServerManager)
    manager = LocalModelManager(ModelRegistry(config_path=cfg))
    monkeypatch.setattr(admin, "model_manager", manager)
    return manager


def add_loaded(manager, model_id="m1", *, active_requests=0, release_after_checks=None):
    """Simule un modèle déjà chargé (READY) dans le pool, comme après un /load."""
    model = manager.registry.get(model_id)
    port = manager._port_pool.pop(0)
    manager._allocated_ports[model_id] = port
    server = FakeServerManager(
        model, port,
        on_unload=manager._on_model_unloaded,
        on_capacity_change=manager._notify_capacity_changed,
        ready=True,
        active_requests=active_requests,
        release_after_checks=release_after_checks,
    )
    manager._managers[model_id] = server
    return server


# ── 1. Reproduction du défaut (sans dépendre du nouveau réglage) ──────────────

def test_unload_route_waits_for_active_request_instead_of_killing_it(
    client, admin_headers, local_manager,
):
    """
    Reproduction COR-004 : avant correctif, la route déchargeait immédiatement
    un modèle pinné — la requête active était tuée en plein vol
    (active_requests_at_unload == 1).
    """
    server = add_loaded(local_manager, active_requests=1, release_after_checks=2)

    response = client.post("/admin/models/m1/unload", headers=admin_headers)

    assert response.status_code == 200, response.text
    assert server.unload_calls == 1
    assert server.active_requests_at_unload == 0, (
        "le modèle a été déchargé alors qu'une requête était encore active"
    )


def test_delete_route_waits_for_active_request_instead_of_killing_it(
    client, admin_headers, local_manager,
):
    """Même invariant sur DELETE /admin/models/{id} (décharge avant suppression)."""
    server = add_loaded(local_manager, active_requests=1, release_after_checks=2)

    response = client.delete("/admin/models/m1", headers=admin_headers)

    assert response.status_code == 200, response.text
    assert server.active_requests_at_unload == 0
    assert local_manager.registry.get("m1") is None


def test_patch_disable_waits_for_active_request_instead_of_killing_it(
    client, admin_headers, local_manager,
):
    """Même invariant sur PATCH enabled:false (le dashboard passe par là)."""
    server = add_loaded(local_manager, active_requests=1, release_after_checks=2)

    response = client.patch(
        "/admin/models/m1", json={"enabled": False}, headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert server.active_requests_at_unload == 0
    assert local_manager.registry.get("m1").enabled is False


# ── 2. Contrat de conflit : 409 explicite quand le drain expire ───────────────

def test_unload_route_returns_409_when_request_never_finishes(
    client, admin_headers, local_manager, drain_settings,
):
    server = add_loaded(local_manager, active_requests=1)  # jamais relâché

    response = client.post("/admin/models/m1/unload", headers=admin_headers)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "m1" in detail
    assert "force" in detail  # l'échappatoire est indiquée à l'opérateur
    # Le pin est intact et le modèle est toujours chargé et utilisable.
    assert server.unload_calls == 0
    assert server.active_requests == 1
    assert server.state == ModelState.READY
    assert local_manager._managers.get("m1") is server


def test_delete_route_returns_409_and_keeps_registry_entry(
    client, admin_headers, local_manager, drain_settings,
):
    server = add_loaded(local_manager, active_requests=1)

    response = client.delete("/admin/models/m1", headers=admin_headers)

    assert response.status_code == 409, response.text
    assert server.unload_calls == 0
    # Aucune suppression partielle : le modèle est toujours dans le registre.
    assert local_manager.registry.get("m1") is not None


def test_patch_disable_returns_409_and_keeps_model_enabled(
    client, admin_headers, local_manager, drain_settings,
):
    """
    Refus AVANT mutation du registre : un `enabled: false` persisté sur un modèle
    encore en train de servir serait un état incohérent.
    """
    server = add_loaded(local_manager, active_requests=1)

    response = client.patch(
        "/admin/models/m1", json={"enabled": False}, headers=admin_headers,
    )

    assert response.status_code == 409, response.text
    assert server.unload_calls == 0
    assert local_manager.registry.get("m1").enabled is True


def test_patch_llama_params_returns_409_and_keeps_old_params(
    client, admin_headers, local_manager, drain_settings,
):
    server = add_loaded(local_manager, active_requests=1)
    before = local_manager.registry.get("m1").llama_params.ctx_size

    response = client.patch(
        "/admin/models/m1",
        json={"llama_params": {"ctx_size": 32768, "parallel": 2}},
        headers=admin_headers,
    )

    assert response.status_code == 409, response.text
    assert server.unload_calls == 0
    assert local_manager.registry.get("m1").llama_params.ctx_size == before


def test_unload_all_route_returns_409_when_a_model_is_busy(
    client, admin_headers, local_manager, drain_settings,
):
    """POST /admin/unload : même contrat qu'en cluster (409, rien de déchargé)."""
    server = add_loaded(local_manager, active_requests=1)

    response = client.post("/admin/unload", headers=admin_headers)

    assert response.status_code == 409, response.text
    assert server.unload_calls == 0


def test_patch_other_fields_never_unload_a_busy_model(
    client, admin_headers, local_manager, drain_settings,
):
    """Un PATCH sans effet sur le cycle de vie (description) ne drain pas."""
    server = add_loaded(local_manager, active_requests=1)

    response = client.patch(
        "/admin/models/m1", json={"description": "nouvelle desc"}, headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert server.unload_calls == 0
    assert server.active_requests == 1
    assert local_manager.registry.get("m1").description == "nouvelle desc"


# ── 3. Cas non pinné : comportement inchangé ─────────────────────────────────

def test_unload_route_unpinned_model_still_returns_200(
    client, admin_headers, local_manager, drain_settings,
):
    server = add_loaded(local_manager, active_requests=0)

    start = time.monotonic()
    response = client.post("/admin/models/m1/unload", headers=admin_headers)
    elapsed = time.monotonic() - start

    assert response.status_code == 200, response.text
    assert server.unload_calls == 1
    assert "m1" in response.json()["message"]
    # Aucun modèle pinné → aucune attente (le drain retourne immédiatement).
    assert elapsed < 0.2, f"le drain a attendu {elapsed:.2f}s sans requête active"


def test_unload_route_unknown_model_still_returns_404(client, admin_headers, local_manager):
    response = client.post("/admin/models/nope/unload", headers=admin_headers)
    assert response.status_code == 404


def test_unload_route_not_loaded_model_is_a_noop(client, admin_headers, local_manager):
    """Modèle du registre jamais chargé → 200, aucun drain, aucun effet."""
    response = client.post("/admin/models/m1/unload", headers=admin_headers)
    assert response.status_code == 200, response.text
    assert local_manager._draining == set()


# ── 4. Bornage du drain et non-fuite de l'état de quarantaine ────────────────

def test_drain_is_bounded_by_its_timeout(
    client, admin_headers, local_manager, drain_settings, monkeypatch,
):
    """Le drain ne dépasse pas son timeout : la route admin ne bloque jamais."""
    monkeypatch.setattr(settings, "admin_unload_drain_timeout_seconds", 0.4)
    add_loaded(local_manager, active_requests=1)

    start = time.monotonic()
    response = client.post("/admin/models/m1/unload", headers=admin_headers)
    elapsed = time.monotonic() - start

    assert response.status_code == 409
    assert 0.3 <= elapsed < 3.0, f"drain non borné : {elapsed:.2f}s pour un timeout de 0.4s"


def test_refused_unload_leaves_no_draining_state_and_model_stays_usable(
    client, admin_headers, local_manager, drain_settings,
):
    """Après un 409, aucun état de quarantaine ne fuit : le modèle reste servable."""
    server = add_loaded(local_manager, active_requests=1)

    assert client.post("/admin/models/m1/unload", headers=admin_headers).status_code == 409
    assert local_manager._draining == set(), "l'état de drain a fui après le refus"

    # Le modèle est immédiatement réutilisable pour de nouvelles requêtes.
    resumed = asyncio.run(local_manager.ensure_model_loaded("m1"))
    assert resumed is server
    assert server.state == ModelState.READY


@pytest.mark.anyio
async def test_draining_state_is_released_even_if_unload_raises(local_manager, drain_settings, monkeypatch):
    """Erreur inattendue pendant unload() → la quarantaine est quand même levée."""
    server = add_loaded(local_manager, active_requests=0)

    async def boom(reason: str = "x"):
        raise OSError("kill -TERM a échoué")

    monkeypatch.setattr(server, "unload", boom)

    with pytest.raises(OSError):
        await local_manager.unload_model("m1")

    assert local_manager._draining == set()


# ── 5. Quarantaine : aucune nouvelle requête admise pendant le drain ─────────

@pytest.mark.anyio
async def test_no_new_request_is_admitted_during_drain(local_manager, drain_settings, monkeypatch):
    """
    Sans quarantaine, un flux continu de nouvelles requêtes empêcherait le drain
    de converger. Pendant le drain, ensure_model_loaded doit refuser.
    """
    monkeypatch.setattr(settings, "admin_unload_drain_timeout_seconds", 1.0)
    server = add_loaded(local_manager, active_requests=1)

    task = asyncio.create_task(local_manager.unload_model("m1"))
    await asyncio.sleep(0.05)  # le drain est en cours

    assert local_manager._draining == {"m1"}
    with pytest.raises(ModelDrainingError):
        await local_manager.ensure_model_loaded("m1")

    with pytest.raises(ModelBusyError):
        await task

    # Quarantaine levée → l'admission redevient possible.
    assert local_manager._draining == set()
    assert await local_manager.ensure_model_loaded("m1") is server


# ── 6. Forçage explicite (opt-in, jamais par défaut) ─────────────────────────

def test_force_true_unloads_a_busy_model(
    client, admin_headers, local_manager, drain_settings,
):
    server = add_loaded(local_manager, active_requests=1)

    response = client.post(
        "/admin/models/m1/unload?force=true", headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert server.unload_calls == 1
    assert server.active_requests_at_unload == 1  # interruption assumée
    assert "forcé" in server.unload_reasons[0]


def test_force_false_is_the_default(client, admin_headers, local_manager, drain_settings):
    """Le forçage n'est jamais implicite : sans le paramètre, le refus s'applique."""
    add_loaded(local_manager, active_requests=1)
    assert client.post("/admin/models/m1/unload", headers=admin_headers).status_code == 409


def test_force_true_allows_delete_of_a_busy_model(
    client, admin_headers, local_manager, drain_settings,
):
    server = add_loaded(local_manager, active_requests=1)

    response = client.delete("/admin/models/m1?force=true", headers=admin_headers)

    assert response.status_code == 200, response.text
    assert server.unload_calls == 1
    assert local_manager.registry.get("m1") is None


def test_force_is_reported_as_unsupported_instead_of_being_ignored(
    client, admin_headers, local_manager, drain_settings, monkeypatch,
):
    """
    En mode cluster, le forçage n'existe pas (les agents refusent tout modèle
    avec des requêtes actives). Sur un modèle occupé, le refus doit le dire
    explicitement — jamais ignorer le paramètre en silence.
    """
    monkeypatch.setattr(local_manager, "supports_unload_force", False, raising=False)
    server = add_loaded(local_manager, active_requests=1)

    response = client.post("/admin/models/m1/unload?force=true", headers=admin_headers)

    assert response.status_code == 409
    assert "force=true n'est pas supporté" in response.json()["detail"]
    assert server.unload_calls == 0


def test_force_on_an_idle_model_is_not_rejected_without_support(
    client, admin_headers, local_manager, drain_settings, monkeypatch,
):
    """force=true n'a de conséquence que sur un modèle occupé : sinon, 200."""
    monkeypatch.setattr(local_manager, "supports_unload_force", False, raising=False)
    server = add_loaded(local_manager, active_requests=0)

    response = client.post("/admin/models/m1/unload?force=true", headers=admin_headers)

    assert response.status_code == 200, response.text
    assert server.unload_calls == 1


# ── 7. Contrat d'erreur identique en mode cluster ────────────────────────────

@pytest.mark.anyio
async def test_cluster_style_busy_error_maps_to_the_same_409(monkeypatch):
    """
    ClusterManager signale le conflit par un RuntimeError (message des agents),
    LocalModelManager par un ModelBusyError : les deux doivent produire le même
    409, pour que les clients admin n'aient qu'un seul contrat à gérer.
    """
    class ClusterLikeManager:  # pas de supports_unload_force → aucun forçage
        async def unload_model(self, model_id: str) -> None:
            raise RuntimeError(
                f"Le modèle '{model_id}' traite encore 2 requête(s) "
                f"et ne peut pas être déchargé."
            )

    monkeypatch.setattr(admin, "model_manager", ClusterLikeManager())

    with pytest.raises(HTTPException) as caught:
        await admin._unload_for_admin("m1")

    assert caught.value.status_code == 409
    assert "m1" in caught.value.detail


@pytest.mark.anyio
async def test_non_conflict_unload_failure_still_maps_to_503(monkeypatch):
    """Un échec technique (agent injoignable) reste un 503, pas un 409."""
    class FailingManager:
        async def unload_model(self, model_id: str) -> None:
            raise RuntimeError("Unload incomplet — node-b: timeout")

    monkeypatch.setattr(admin, "model_manager", FailingManager())

    with pytest.raises(HTTPException) as caught:
        await admin._unload_for_admin("m1")

    assert caught.value.status_code == 503


# ── 8. Le chemin de shutdown garde sa sémantique de forçage ──────────────────

@pytest.mark.anyio
async def test_shutdown_still_forces_after_drain_timeout(local_manager, drain_settings, monkeypatch):
    """
    Régression : le SIGTERM doit toujours finir par décharger, même si une
    requête ne se termine jamais (sinon systemd tue la gateway et laisse des
    llama-server orphelins).
    """
    monkeypatch.setattr(settings, "shutdown_drain_timeout_seconds", 0.2)
    server = add_loaded(local_manager, active_requests=1)

    await asyncio.wait_for(local_manager.shutdown(), timeout=3.0)

    assert server.unload_calls == 1
