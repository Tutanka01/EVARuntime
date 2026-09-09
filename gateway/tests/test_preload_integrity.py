"""
Attestation GGUF à chaque transition vers LOADING (SEC-ART-001) — vrai ServerManager.

Historiquement le SHA-256 n'était vérifié qu'au démarrage : un GGUF remplacé
après coup était chargé sans nouvelle attestation. Désormais
``ServerManager._load_and_signal`` atteste l'artefact fail-closed AVANT
``_start_process`` : aucun sous-processus llama-server n'est lancé sur un
fichier qui ne correspond plus à l'empreinte déclarée.

Style de tests/test_server_manager.py : le VRAI ServerManager, seuls
_start_process / _wait_for_health / _kill_process sont remplacés. Le hachage
passe par le vrai ``integrity.attest_gguf`` (cache attesté + single-flight),
seul ``integrity._hash_file`` est remplacé par un compteur pour rendre le
coût observable.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import integrity
import main
import telemetry
from integrity import reset_integrity_cache
from server_manager import LOAD_CAPACITY_MARKERS, ModelState, ServerManager

# Tous les tests async de ce module tournent sur asyncio (backend forcé en conftest).
pytestmark = pytest.mark.anyio


# ── Doubles de test (inspirés de test_server_manager.py) ──────────────────────

class _FakeLlamaParams:
    n_gpu_layers = 999
    ctx_size = 2048
    parallel = 1
    flash_attn = False
    cache_type_k = "f16"
    cache_type_v = "f16"
    cpu_moe = False


class FakeModelDef:
    """Définition minimale ; path pointe un VRAI fichier GGUF factice."""

    def __init__(
        self,
        mid: str,
        path: Path,
        sha256: str | None = None,
        vram: float = 10.0,
        *,
        capabilities: list[str] | None = None,
        mmproj_path: Path | None = None,
        mmproj_sha256: str | None = None,
    ):
        self.id = mid
        self.vram_gb = vram
        self.enabled = True
        self.description = ""
        self.path = path
        self.sha256 = sha256
        self.capabilities = capabilities or ["text_generation"]
        self.mmproj_path = mmproj_path
        self.mmproj_sha256 = mmproj_sha256
        self.llama_params = _FakeLlamaParams()
        self.speculative = None
        self.load_timeout_seconds = 5


class FakeProcess:
    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.returncode: int | None = None


def make_gguf(tmp_path: Path, content: bytes = b"gguf factice SEC-ART-001"):
    """Écrit un GGUF factice et retourne (chemin, empreinte réelle)."""
    path = tmp_path / "modele.gguf"
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def make_manager(model: FakeModelDef, monkeypatch, calls: dict) -> ServerManager:
    """Vrai ServerManager ; _start_process compte ses appels dans ``calls``."""
    mgr = ServerManager(model, port=9001, idle_unload_enabled=False)

    async def fake_start():
        calls["start"] += 1
        mgr._process = FakeProcess()

    async def fake_health():
        return None

    async def fake_kill():
        mgr._process = None  # idempotent, comme le vrai

    monkeypatch.setattr(mgr, "_start_process", fake_start)
    monkeypatch.setattr(mgr, "_wait_for_health", fake_health)
    monkeypatch.setattr(mgr, "_kill_process", fake_kill)
    return mgr


class _HashCounter:
    """Remplace integrity._hash_file en comptant les appels (contrôle positif)."""

    def __init__(self):
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _clean_state():
    """Cache attesté et télémétrie sont des états module — repartir de zéro."""
    reset_integrity_cache()
    telemetry.reset_all()
    yield
    reset_integrity_cache()
    telemetry.reset_all()


# ── Refus fail-closed d'un GGUF substitué ─────────────────────────────────────

@pytest.mark.anyio
async def test_tampered_gguf_refused_after_first_load(monkeypatch, tmp_path):
    """
    Un GGUF conforme charge une première fois ; substitué ensuite, tout
    rechargement est refusé avant le lancement du sous-processus.
    """
    path, digest = make_gguf(tmp_path)
    model = FakeModelDef("m1", path, sha256=digest)
    calls = {"start": 0}
    mgr = make_manager(model, monkeypatch, calls)

    await mgr.ensure_loaded()
    assert mgr.state == ModelState.READY
    assert calls["start"] == 1
    await mgr.unload()

    # Substitution APRÈS le premier chargement (le cas visé par SEC-ART-001).
    path.write_bytes(b"contenu substitue - artefact compromis")

    loader = asyncio.create_task(mgr.ensure_loaded())
    waiter = asyncio.create_task(mgr.ensure_loaded())  # coalescé sur le même event
    with pytest.raises(RuntimeError) as exc_info:
        await asyncio.wait_for(loader, timeout=2.0)
    # Le waiter coalescé échoue aussi, sans hang.
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(waiter, timeout=2.0)

    message = str(exc_info.value)
    assert "m1" in message
    # Sanitisation : ni chemin, ni empreinte, ni marqueur de capacité.
    assert str(path) not in message
    assert digest not in message
    for marker in LOAD_CAPACITY_MARKERS:
        assert marker not in message.lower()

    # Fail-closed : jamais de sous-processus sur un artefact compromis.
    assert calls["start"] == 1, "_start_process ne doit pas être rappelé"
    assert mgr.state == ModelState.UNLOADED
    assert isinstance(mgr._load_error, RuntimeError)
    assert mgr._ready_event is not None and mgr._ready_event.is_set()


@pytest.mark.anyio
async def test_integrity_refusal_detail_lands_in_logs_only(monkeypatch, tmp_path, caplog):
    """
    Contrôle positif de sanitisation : le détail complet de l'IntegrityError
    (chemin, empreintes) circule au journal — et JAMAIS dans le message client.
    """
    path, digest = make_gguf(tmp_path, content=b"contenu legitime")
    model = FakeModelDef("m1", path, sha256=digest)
    mgr = make_manager(model, monkeypatch, {"start": 0})

    path.write_bytes(b"contenu falsifie")  # falsifié avant le premier chargement
    with caplog.at_level(logging.INFO, logger="server_manager"):
        with pytest.raises(RuntimeError) as exc_info:
            await mgr.ensure_loaded()

    assert str(path) in caplog.text
    assert "SEC-ART-001" in caplog.text
    assert digest in caplog.text  # empreintes attendue/obtenue au journal
    assert str(path) not in str(exc_info.value)
    assert digest not in str(exc_info.value)


# ── No-op sans empreinte déclarée ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_no_sha256_model_loads_without_hashing(monkeypatch, tmp_path):
    """Sans sha256 déclaré, l'attestation est un no-op : le chargement normal."""
    content = b"gguf sans empreinte declaree"
    path, digest = make_gguf(tmp_path, content=content)
    model = FakeModelDef("m1", path, sha256=None)
    calls = {"start": 0}
    mgr = make_manager(model, monkeypatch, calls)

    counter = _HashCounter()
    monkeypatch.setattr(integrity, "_hash_file", counter)
    await mgr.ensure_loaded()

    assert mgr.state == ModelState.READY
    assert calls["start"] == 1
    assert counter.calls == 0, "sans empreinte déclarée, aucun hachage"

    # Contrôle positif : le même compteur VOIT le hachage dès qu'une empreinte
    # est déclarée — le zéro observé ci-dessus est donc significatif.
    await mgr.unload()
    stamped = FakeModelDef("m2", path, sha256=digest)
    mgr2 = make_manager(stamped, monkeypatch, calls)
    await mgr2.ensure_loaded()
    assert counter.calls == 1
    assert calls["start"] == 2
    await mgr2.unload()


# ── Cache attesté partagé entre chargements ──────────────────────────────────

@pytest.mark.anyio
async def test_attested_cache_avoids_rehash_across_cycles(monkeypatch, tmp_path):
    """
    Fichier inchangé : un seul hachage sur deux cycles load→unload→load
    (O(stat) grâce au cache attesté). Fichier réécrit : re-hachage puis refus.
    """
    path, digest = make_gguf(tmp_path)
    model = FakeModelDef("m1", path, sha256=digest)
    calls = {"start": 0}
    counter = _HashCounter()
    monkeypatch.setattr(integrity, "_hash_file", counter)
    mgr = make_manager(model, monkeypatch, calls)

    await mgr.ensure_loaded()
    await mgr.unload()
    await mgr.ensure_loaded()  # cycle 2, fichier inchangé → cache attesté
    await mgr.unload()
    assert counter.calls == 1, "fichier inchangé : un seul hachage sur deux cycles"

    path.write_bytes(b"contenu different")
    with pytest.raises(RuntimeError, match="intégrité"):
        await mgr.ensure_loaded()
    assert counter.calls == 2, "fichier réécrit : re-hachage puis refus fail-closed"
    assert mgr.state == ModelState.UNLOADED


@pytest.mark.anyio
async def test_concurrent_ensure_loaded_hash_and_start_once(monkeypatch, tmp_path):
    """N appelants concurrents → un seul hachage, un seul _start_process."""
    path, digest = make_gguf(tmp_path)
    model = FakeModelDef("m1", path, sha256=digest)
    calls = {"start": 0}
    counter = _HashCounter()
    monkeypatch.setattr(integrity, "_hash_file", counter)
    mgr = make_manager(model, monkeypatch, calls)

    await asyncio.gather(*[mgr.ensure_loaded() for _ in range(5)])

    assert calls["start"] == 1
    assert counter.calls == 1
    assert mgr.state == ModelState.READY
    await mgr.unload()


@pytest.mark.anyio
async def test_startup_attestation_populates_shared_cache(monkeypatch, tmp_path):
    """
    Le hachage du démarrage alimente le cache attesté : le premier chargement
    ne re-hache pas un GGUF inchangé — un hachage par processus, pas par load.
    """
    path, digest = make_gguf(tmp_path)
    model = FakeModelDef("m1", path, sha256=digest)
    calls = {"start": 0}
    counter = _HashCounter()
    monkeypatch.setattr(integrity, "_hash_file", counter)

    monkeypatch.setattr(main.settings, "cluster_mode", "local")
    monkeypatch.setattr(main, "enforce_llama_min_build", AsyncMock(return_value=True))
    await main._validate_inference_runtime([model])
    assert counter.calls == 1, "le démarrage hache le GGUF déclaré"

    mgr = make_manager(model, monkeypatch, calls)
    await mgr.ensure_loaded()
    assert counter.calls == 1, "le pré-chargement réutilise l'attestation du démarrage"
    assert calls["start"] == 1
    await mgr.unload()


@pytest.mark.anyio
async def test_vision_projector_is_attested_before_local_load(monkeypatch, tmp_path):
    """Un projecteur modifié après un cycle est refusé avant le sous-processus."""
    path, _model_digest = make_gguf(tmp_path, content=b"poids vision")
    projector = tmp_path / "modele-mmproj.gguf"
    projector.write_bytes(b"projecteur conforme")
    projector_digest = hashlib.sha256(projector.read_bytes()).hexdigest()
    model = FakeModelDef(
        "vision",
        path,
        capabilities=["text_generation", "vision"],
        mmproj_path=projector,
        mmproj_sha256=projector_digest,
    )
    calls = {"start": 0}
    mgr = make_manager(model, monkeypatch, calls)

    await mgr.ensure_loaded()
    assert mgr.state == ModelState.READY
    await mgr.unload()

    projector.write_bytes(b"projecteur substitue")
    with pytest.raises(RuntimeError, match="intégrité"):
        await mgr.ensure_loaded()

    assert calls["start"] == 1
    assert mgr.state == ModelState.UNLOADED
