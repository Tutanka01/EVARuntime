"""
Sanitisation des erreurs de chargement — aucune fuite d'infra au client.

Un échec de chargement llama-server embarquait dans la réponse 503 le tail
stderr brut (chemins GGUF absolus, erreurs CUDA, dump des flags --mmproj),
l'URL interne de santé, ou le corps HTTP des node-agents relayé par le
cluster (audit 2026-08-28, ISSUE 1). Le message client doit rester générique ;
le détail technique ne vit que dans les journaux serveur (corrélation OPS-004).

Conformément à la règle des tests d'absence (AGENTS.md), chaque assertion
d'absence est adossée à un contrôle positif : caplog prouve que le test SAIT
lire le détail technique au moment où il circule côté serveur — ce n'est pas
le test qui est aveugle, c'est la réponse client qui est saine.

Couverture en deux couches :
  1. La source (ServerManager._wait_for_health, ClusterManager) produit des
     messages sûrs — prouvé ici contre le VRAI code.
  2. proxy.py relaie str(exc) tel quel — prouvé en rejouant l'exception
     réellement produite par la couche 1 à travers proxy.proxy_request.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest
import telemetry

import integrity
from cluster.cluster_manager import ClusterManager
from cluster.node_client import LocalNodeAdapter, NodeProtocolError
from cluster.node_protocol import NodeHealth, NodeStatus, UnloadResponse
from model_manager import LocalModelManager
from proxy import proxy_request
from server_manager import LOAD_CAPACITY_MARKERS, ServerManager

# ── Doubles de test (locaux, inspirés de test_server_manager.py) ─────────────

CHEMIN_SECRET = "/models/secret/modele.gguf"

# Le tail stderr d'un vrai crash llama-server : chemin GGUF, erreur CUDA, flags.
STDERR_CRASH = [
    f"gguf_init_from_file: failed to load GGUF model file from {CHEMIN_SECRET}",
    "load_model: error: unable to allocate CUDA0 buffer of 53687091200 bytes",
    "srv    operator] params: --mmproj /models/secret/mmproj.bin --ctx-size 32768",
    "main: error: out of memory",
]

STDERR_CRASH_SANS_CAPACITE = [
    f"gguf_init_from_file: failed to load GGUF model file from {CHEMIN_SECRET}",
    "main: error: invalid magic bytes in model header",
]


class _FakeLlamaParams:
    n_gpu_layers = 999
    ctx_size = 2048
    parallel = 1
    flash_attn = False
    cache_type_k = "f16"
    cache_type_v = "f16"
    cpu_moe = False


class FakeModelDef:
    """Champs requis par ServerManager ET ClusterManager."""

    def __init__(
        self,
        mid: str = "m1",
        vram: float = 10.0,
        load_timeout: int = 5,
        sha256: str | None = None,
    ):
        self.id = mid
        self.vram_gb = vram
        self.enabled = True
        self.description = ""
        self.path = Path(CHEMIN_SECRET)
        self.sha256 = sha256
        self.capabilities = ["text_generation"]
        self.llama_params = _FakeLlamaParams()
        self.speculative = None
        self.load_timeout_seconds = load_timeout

    def to_dict(self) -> dict:
        return {"id": self.id, "path": str(self.path), "vram_gb": self.vram_gb}


class FakeProcess:
    def __init__(self, returncode: int | None = None, pid: int = 4242):
        self.pid = pid
        self.returncode = returncode


def make_real_manager(load_timeout: float = 5) -> ServerManager:
    """Vrai ServerManager ; seule _start_process est remplacée (pas de binaire)."""
    return ServerManager(
        FakeModelDef("m1", load_timeout=load_timeout),
        port=9001,
        idle_unload_enabled=False,
    )


def patch_start_process(monkeypatch, mgr: ServerManager, process: FakeProcess) -> None:
    async def fake_start():
        mgr._process = process

    async def fake_kill():
        mgr._process = None

    monkeypatch.setattr(mgr, "_start_process", fake_start)
    monkeypatch.setattr(mgr, "_kill_process", fake_kill)


def poser_processus_mort(mgr: ServerManager, returncode: int = 1) -> None:
    """Pose le faux processus directement : _wait_for_health est appelée nue."""
    mgr._process = FakeProcess(returncode=returncode)


def seed_stderr(mgr: ServerManager, lines: list[str]) -> None:
    for line in lines:
        mgr._stderr_tail.append(line)


@pytest.fixture(autouse=True)
def reset_telemetry():
    telemetry.reset_all()
    # Le cache attesté d'integrity.py est un état module — repartir de zéro.
    integrity.reset_integrity_cache()
    yield
    telemetry.reset_all()
    integrity.reset_integrity_cache()


# ── Couche 1a : mort du processus (mode local) ────────────────────────────────

@pytest.mark.anyio
async def test_crash_message_client_sans_stderr_ni_chemin(monkeypatch, caplog):
    """Le RuntimeError de _wait_for_health ne porte ni stderr, ni chemin, ni URL."""
    mgr = make_real_manager()
    poser_processus_mort(mgr)
    seed_stderr(mgr, STDERR_CRASH)

    with pytest.raises(RuntimeError) as exc_info:
        await mgr._wait_for_health()
    message = str(exc_info.value)

    # Éléments sûrs : identifiant de modèle + cause probable de capacité.
    assert "m1" in message
    assert "prématurément" in message
    assert "out of memory" in message  # marqueur de capacité conservé

    # Rien d'infrastructuralement sensible (test d'absence).
    assert CHEMIN_SECRET not in message
    assert "/models/" not in message
    assert "mmproj" not in message
    assert "CUDA" not in message
    assert "Stderr" not in message
    assert "http" not in message.lower()

    # Contrôle positif : le détail technique EST passé par le serveur (log).
    assert CHEMIN_SECRET in caplog.text


@pytest.mark.anyio
async def test_crash_sans_capacite_n_emet_pas_le_marqueur(monkeypatch, caplog):
    """Sans marqueur de capacité dans le stderr, le message reste sans hypothèse."""
    mgr = make_real_manager()
    poser_processus_mort(mgr)
    seed_stderr(mgr, STDERR_CRASH_SANS_CAPACITE)

    with pytest.raises(RuntimeError) as exc_info:
        await mgr._wait_for_health()
    message = str(exc_info.value)

    assert "out of memory" not in message.lower()
    assert CHEMIN_SECRET not in message
    # Contrôle positif : le vrai motif d'échec est au journal serveur.
    assert "invalid magic bytes" in caplog.text


@pytest.mark.anyio
async def test_timeout_message_sans_url_interne(monkeypatch, caplog):
    """Le TimeoutError ne divulgue ni l'URL de santé ni le port du backend."""
    # 0 est falsy : _wait_for_health retomberait sur le timeout par défaut.
    mgr = make_real_manager(load_timeout=0.05)
    poser_processus_mort(mgr, returncode=None)

    with pytest.raises(TimeoutError) as exc_info:
        await mgr._wait_for_health()
    message = str(exc_info.value)

    assert "m1" in message
    assert "http" not in message.lower()
    assert "9001" not in message
    assert "/health" not in message
    # Contrôle positif : l'URL interne reste au journal serveur.
    assert "/health" in caplog.text


@pytest.mark.anyio
async def test_classification_capacite_reste_operationnelle(monkeypatch):
    """
    La retry OOM de LocalModelManager (_is_load_capacity_error) continue de
    fonctionner sur le message assaini : sinon le correctif de sécurité casserait
    silencieusement le réessai après éviction réelle.
    """
    mgr = make_real_manager()
    poser_processus_mort(mgr)
    seed_stderr(mgr, STDERR_CRASH)

    with pytest.raises(RuntimeError) as exc_info:
        await mgr._wait_for_health()

    assert LocalModelManager._is_load_capacity_error(exc_info.value) is True


# ── Couche 1b : échec sur tous les nœuds (mode cluster) ───────────────────────

class _BackendQuiRefuse:
    """Backend minimal : health ok, load refuse toujours avec un détail sensible."""

    def __init__(self):
        self.load_calls: list[dict] = []

    async def health(self) -> NodeHealth:
        return NodeHealth(
            status="ok",
            total_vram_gb=80.0,
            used_vram_gb=0.0,
            available_vram_gb=80.0,
            loaded_model_ids=[],
            free_ports=2,
        )

    async def status(self) -> NodeStatus:
        return NodeStatus(node_id="a", health=await self.health(), models=[])

    async def load_model(self, model_dict: dict):
        self.load_calls.append(model_dict)
        # Forme exacte d'un échec agent relayé par node_client : le corps HTTP
        # de l'agent (stderr llama-server, chemins) finit dans l'exception.
        raise NodeProtocolError(
            "Nœud 'a' a renvoyé 500 sur /internal/models/load : "
            f"Échec du chargement : llama-server a quitté — stderr : "
            f"gguf_init_from_file failed for {CHEMIN_SECRET}"
        )

    async def unload_model(self, model_id: str) -> UnloadResponse:
        return UnloadResponse(model_id=model_id, unloaded=True, freed_vram_gb=0.0)

    async def unload_all(self) -> None:
        return None


class _RegistryUneEntree:
    """Registry minimal : une entrée activée, suffisante pour ClusterManager."""

    def __init__(self):
        self._model = FakeModelDef("m1", vram=20.0)

    def get(self, model_id: str):
        return self._model if model_id == self._model.id else None

    def list_all(self):
        return [self._model]

    def list_enabled(self):
        return [self._model]

    def first_enabled_id(self):
        return self._model.id


@pytest.mark.anyio
async def test_cluster_echec_tous_noeuds_detail_au_journal_seulement(caplog):
    """
    Le détail des échecs (corps HTTP des agents : stderr, chemins) part au
    journal ; le RuntimeError client reste générique.
    """
    mgr = ClusterManager(
        registry=_RegistryUneEntree(),
        nodes=[LocalNodeAdapter("a", _BackendQuiRefuse())],
        health_interval=1,
        health_failures_to_offline=3,
    )
    await mgr.start_health_monitor()
    try:
        with pytest.raises(RuntimeError) as exc_info:
            await mgr.ensure_model_loaded("m1")
    finally:
        await mgr.shutdown()

    message = str(exc_info.value)
    assert "m1" in message
    assert CHEMIN_SECRET not in message
    assert "/models/" not in message
    # Contrôle positif : le détail complet (corps de l'agent) est au journal.
    assert CHEMIN_SECRET in caplog.text


# ── Couche 2 : le proxy relaie le message sain tel quel ──────────────────────

class _FakeRegistryProxy:
    def __init__(self):
        self._model = FakeModelDef("m1")

    def get(self, model_id: str):
        return self._model if model_id == self._model.id else None

    def get_enabled(self, model_id: str):
        model = self.get(model_id)
        return model if model and model.enabled else None

    def first_enabled_id(self):
        return self._model.id


class _FakeRequest:
    def __init__(self, body: bytes):
        self._body = body

    async def body(self):
        return self._body


@pytest.mark.anyio
async def test_proxy_relaye_le_message_sain_et_journalise(monkeypatch, caplog):
    """
    La réponse 503 contient exactement le message produit par la couche 1
    (prouvé sûr ci-dessus) — et le détail est journalisé côté proxy.
    """
    mgr = make_real_manager()
    poser_processus_mort(mgr)
    seed_stderr(mgr, STDERR_CRASH)

    with pytest.raises(RuntimeError) as exc_info:
        await mgr._wait_for_health()
    exception_reelle = exc_info.value

    class Manager:
        registry = _FakeRegistryProxy()

        async def ensure_model_loaded(self, model_id: str):
            raise exception_reelle

    response = await proxy_request(
        _FakeRequest(b'{"model":"m1","messages":[]}'),
        "/v1/chat/completions",
        {"user_id": 1, "key_id": 1},
        Manager(),
    )

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["error"]["message"] == str(exception_reelle)
    assert body["error"]["type"] == "server_error"
    assert body["error"]["code"] == "503"
    assert CHEMIN_SECRET not in response.body.decode()
    assert "/models/" not in response.body.decode()
    # Contrôle positif : le proxy a bien journalisé le message de chargement.
    assert "Chargement de 'm1' impossible" in caplog.text


# ── Couche 1c : refus d'intégrité GGUF (SEC-ART-001) ──────────────────────────

@pytest.mark.anyio
async def test_refus_integrite_sans_chemin_dans_la_reponse_proxy(monkeypatch, caplog, tmp_path):
    """
    SEC-ART-001 : un GGUF falsifié (empreinte déclarée ≠ fichier) refuse le
    chargement AVANT tout lancement de llama-server. Le RuntimeError suit le
    même contrat de sanitisation que _wait_for_health — ni chemin, ni
    empreinte, ni marqueur de capacité — et le proxy le relaie en 503 dans
    l'envelope d'erreur OpenAI.
    """
    gguf = tmp_path / "modele.gguf"
    gguf.write_bytes(b"contenu legitime")
    digest_attendu = hashlib.sha256(b"contenu falsifie").hexdigest()

    model = FakeModelDef("m1", sha256=digest_attendu)
    model.path = gguf
    mgr = ServerManager(model, port=9001, idle_unload_enabled=False)
    starts = {"count": 0}

    async def fake_start():
        starts["count"] += 1
        mgr._process = FakeProcess()

    async def fake_kill():
        mgr._process = None

    async def interdit():
        raise AssertionError(
            "_wait_for_health ne doit pas être atteint après un refus d'intégrité"
        )

    monkeypatch.setattr(mgr, "_start_process", fake_start)
    monkeypatch.setattr(mgr, "_wait_for_health", interdit)
    monkeypatch.setattr(mgr, "_kill_process", fake_kill)

    with caplog.at_level(logging.INFO, logger="server_manager"):
        with pytest.raises(RuntimeError) as exc_info:
            await mgr.ensure_loaded()
    message = str(exc_info.value)

    assert "m1" in message
    assert "intégrité" in message
    assert str(gguf) not in message
    assert digest_attendu not in message
    for marker in LOAD_CAPACITY_MARKERS:
        assert marker not in message.lower()
    assert starts["count"] == 0, "aucun sous-processus lancé sur un artefact refusé"

    # Contrôle positif : le détail complet (chemin, empreintes) est au journal.
    assert str(gguf) in caplog.text
    assert "SEC-ART-001" in caplog.text
    assert digest_attendu in caplog.text

    # Couche 2 : le proxy relaie le message sain tel quel (503, envelope OpenAI).
    class Manager:
        registry = _FakeRegistryProxy()

        async def ensure_model_loaded(self, model_id: str):
            raise exc_info.value

    response = await proxy_request(
        _FakeRequest(b'{"model":"m1","messages":[]}'),
        "/v1/chat/completions",
        {"user_id": 1, "key_id": 1},
        Manager(),
    )

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["error"]["message"] == message
    assert body["error"]["type"] == "server_error"
    assert body["error"]["code"] == "503"
    assert str(gguf) not in response.body.decode()
    assert "/models/" not in response.body.decode()
