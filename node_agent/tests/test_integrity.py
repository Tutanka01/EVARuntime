"""
Tests d'intégration de l'attestation GGUF dans le cycle de vie de `_AgentState`
(CLU-002) : le hachage du GGUF ne doit plus se faire dans l'event loop de
l'agent (anciennement : `model.verify_integrity()` synchrone → /health, unload
et heartbeat bloqués des minutes sur un gros GGUF).

Reprend les conventions de test_main.py :
  - pas de plugin async dans ce venv → scénarios exécutés via asyncio.run(...)
    dans des fonctions de test synchrones normales ;
  - FakeServerManager (réimporté de test_main) monkeypatché sur
    `main.ServerManager` AVANT construction de `_AgentState` — aucun
    sous-processus llama-server n'est jamais lancé ;
  - `_validate_model_files` est neutralisé pour que la garde testée ici soit
    bien l'attestation d'intégrité elle-même (et non la simple vérification de
    présence du fichier).

Différence clé avec test_main : les tests créent de VRAIS fichiers GGUF
temporaires (le hachage lit réellement les octets). `allowed_model_dirs` reste
vide par défaut → pas de contrainte de répertoire (même hypothèse que
make_model_dict dans test_main).
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

# Imports top-level : conftest.py a déjà placé node_agent/ puis gateway/ en
# tête de sys.path — `main` (agent) et `integrity` (module gateway) résolvent
# sans ambiguïté.
import main
import integrity
from test_main import FakeServerManager


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_gguf(tmp_path: Path, name: str, content: bytes) -> tuple[Path, str]:
    """Écrit un GGUF factice réel et renvoie (chemin, empreinte SHA-256)."""
    path = tmp_path / name
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def make_model_dict(
    model_id: str, path: Path, sha256: str | None = None, vram_gb: float = 1.0
) -> dict:
    """Entrée YAML valide pour ModelRegistry._parse_entry, avec vrai chemin."""
    entry: dict = {"id": model_id, "path": str(path), "vram_gb": vram_gb}
    if sha256 is not None:
        entry["sha256"] = sha256
    return entry


class _HashProbe:
    """
    Remplaçant compteur de `integrity._hash_file` (thread worker).

    Enregistre le thread d'exécution de chaque hachage, signale la mise en vol
    via `started`, et peut retenir le hachage en vol via `gate` — permet de
    figer l'état « hash en cours » pendant qu'une sonde interroge l'agent.
    """

    def __init__(
        self,
        gate: threading.Event | None = None,
        started: threading.Event | None = None,
    ) -> None:
        self.calls = 0
        self.thread_idents: list[int] = []
        self.gate = gate
        self.started = started

    def __call__(self, path) -> str:
        self.calls += 1
        self.thread_idents.append(threading.get_ident())
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            assert self.gate.wait(timeout=10), "gate jamais libérée pendant le hachage"
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()


async def _until(predicate, timeout: float = 5.0) -> None:
    """Attend (boucle event) qu'une condition devienne vraie — évite les sleeps."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition jamais remplie (délai dépassé)")
        await asyncio.sleep(0.005)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_shared_state():
    """
    Le cache attesté (état module) et les registres de classe de
    FakeServerManager sont partagés — repartir de zéro avant/après chaque test.
    """
    integrity.reset_integrity_cache()
    FakeServerManager.FAIL_LOAD = set()
    FakeServerManager.LOAD_GATES = {}
    FakeServerManager.UNLOAD_GATES = {}
    FakeServerManager.ENSURE_CALLS = {}
    FakeServerManager.INSTANCES = []
    yield
    integrity.reset_integrity_cache()
    FakeServerManager.INSTANCES = []


@pytest.fixture
def fake_state(monkeypatch) -> "main._AgentState":
    """
    _AgentState fraîche, ServerManager remplacé AVANT construction (cf.
    test_main.fake_state). `_validate_model_files` est neutralisé : le fichier
    est ensuite « validé » par l'attestation d'intégrité, qui est le sujet ici.
    """
    monkeypatch.setattr(main, "ServerManager", FakeServerManager)
    monkeypatch.setattr(main, "_validate_model_files", lambda model: None)
    return main._AgentState()


# ── CLU-002 : hachage hors event loop ────────────────────────────────────────

class TestHashOffEventLoop:
    def test_hash_does_not_block_loop_and_health_stays_responsive(
        self, fake_state, monkeypatch, tmp_path
    ):
        """
        CLU-002 — régression : pendant l'empreinte SHA-256 d'un GGUF, l'event
        loop de l'agent doit rester réactive.

        Mécanisme : le hachage est retenu par une gate dans le thread worker.
        Tant qu'il est en vol, une sonde `state.health()` doit aboutir dans un
        délai court — impossible si la boucle restait bloquée — et le thread
        enregistré du hachage doit différer du thread de la boucle.
        """
        gguf, digest = make_gguf(tmp_path, "gros-modele.gguf", b"contenu gguf de test")
        model_dict = make_model_dict("gros-modele", gguf, sha256=digest)

        gate = threading.Event()
        started = threading.Event()
        probe = _HashProbe(gate=gate, started=started)
        monkeypatch.setattr(integrity, "_hash_file", probe)

        async def scenario():
            # Le scénario EST la coroutine de la boucle : son thread est celui
            # de l'event loop.
            loop_ident = threading.get_ident()
            load_task = asyncio.create_task(fake_state.load(model_dict))

            # Attendre que le hachage soit réellement en vol (gate fermée).
            await _until(lambda: probe.calls >= 1 and started.is_set())
            assert not gate.is_set()

            # Sonde de réactivité : /health doit répondre PENDANT le hachage.
            async def health_probe():
                return fake_state.health()

            health = await asyncio.wait_for(health_probe(), timeout=2.0)
            assert health.status == "ok"
            assert not gate.is_set(), "le hachage doit toujours être en vol pendant la sonde"

            gate.set()
            resp = await asyncio.wait_for(load_task, timeout=10.0)
            return loop_ident, resp

        loop_ident, resp = asyncio.run(scenario())

        assert resp.already_loaded is False
        # Contrôle positif du mécanisme : le substitut a réellement tourné
        # (calls == 1, ident enregistré) — la comparaison d'idents est
        # donc significative.
        assert probe.calls == 1
        assert probe.thread_idents
        assert probe.thread_idents[0] != loop_ident, (
            "le hachage doit s'exécuter hors thread de l'event loop (asyncio.to_thread)"
        )


# ── Échecs fail-closed avant réservation de port ─────────────────────────────

class TestIntegrityFailClosed:
    def test_wrong_declared_sha256_is_422_before_port_reservation(self, fake_state, tmp_path):
        """
        Empreinte déclarée ≠ contenu réel → 422, aucun manager créé, aucun port
        consommé (la garde précède la réservation).
        """
        gguf, _digest = make_gguf(tmp_path, "substitue.gguf", b"vrai contenu")
        model_dict = make_model_dict("substitue", gguf, sha256="0" * 64)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(fake_state.load(model_dict))

        exc = exc_info.value
        assert exc.status_code == 422
        assert "Vérification d'intégrité échouée" in exc.detail
        assert FakeServerManager.INSTANCES == []
        assert fake_state._allocated_ports == {}
        assert len(fake_state._port_pool) == main.settings.max_loaded_models

    def test_missing_gguf_with_declared_sha256_is_422(self, fake_state, tmp_path):
        """
        Fichier absent + empreinte déclarée → 422 fail-closed. (`_validate_model_files`
        étant neutralisé par la fixture, c'est bien l'attestation qui refuse.)
        """
        model_dict = make_model_dict("absent", tmp_path / "absent.gguf", sha256="0" * 64)

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(fake_state.load(model_dict))

        exc = exc_info.value
        assert exc.status_code == 422
        assert "Vérification d'intégrité échouée" in exc.detail
        assert "introuvable" in exc.detail
        assert FakeServerManager.INSTANCES == []
        assert len(fake_state._port_pool) == main.settings.max_loaded_models


# ── Cache attesté ────────────────────────────────────────────────────────────

class TestAttestedCache:
    def test_happy_path_then_idempotent_reload_hashes_once(self, fake_state, monkeypatch, tmp_path):
        """
        Chemin nominal : charge OK, puis rechargement idempotent du modèle
        READY → un SEUL hachage au total (cache attesté, O(stat) ensuite).
        """
        gguf, digest = make_gguf(tmp_path, "stable.gguf", b"contenu stable")
        model_dict = make_model_dict("stable", gguf, sha256=digest)

        probe = _HashProbe()
        monkeypatch.setattr(integrity, "_hash_file", probe)

        async def scenario():
            first = await fake_state.load(model_dict)
            second = await fake_state.load(model_dict)
            return first, second

        first, second = asyncio.run(scenario())

        assert first.already_loaded is False
        assert second.already_loaded is True
        assert second.port == first.port
        assert FakeServerManager.ENSURE_CALLS["stable"] == 1
        assert probe.calls == 1, (
            "un rechargement idempotent d'un modèle READY ne doit pas re-hacher (cache attesté)"
        )

    def test_content_change_between_loads_forces_rehash(self, fake_state, monkeypatch, tmp_path):
        """
        GGUF muté entre deux loads : le cache attesté est invalidé. Contenu +
        empreinte cohérents → OK (re-hachage) ; empreinte déclarée obsolète
        → 422.
        """
        gguf, digest1 = make_gguf(tmp_path, "mute.gguf", b"contenu v1")
        model_dict = make_model_dict("mute", gguf, sha256=digest1)

        probe = _HashProbe()
        monkeypatch.setattr(integrity, "_hash_file", probe)

        async def scenario():
            await fake_state.load(model_dict)  # hachage n°1, charge OK

            # Contenu remplacé + empreinte déclarée mise à jour → re-hachage, OK.
            content2 = b"contenu v2 - plus long"
            gguf.write_bytes(content2)
            model_dict["sha256"] = hashlib.sha256(content2).hexdigest()
            reloaded = await fake_state.load(model_dict)  # hachage n°2

            # Contenu remplacé SANS mettre à jour l'empreinte déclarée → 422.
            gguf.write_bytes(b"contenu v3 - encore plus long, taille differente")
            with pytest.raises(HTTPException) as exc_info:
                await fake_state.load(model_dict)  # hachage n°3, empreinte obsolète
            return reloaded, exc_info.value

        reloaded, exc = asyncio.run(scenario())

        # Le modèle était READY → already_loaded=True ; l'attestation a malgré
        # tout re-couru (elle précède le court-circuit READY).
        assert reloaded.already_loaded is True
        assert probe.calls == 3, "toute identité fichier nouvelle doit être re-hachée"
        assert exc.status_code == 422
        assert "non conforme" in exc.detail


# ── Single-flight ────────────────────────────────────────────────────────────

class TestSingleFlight:
    def test_concurrent_loads_share_one_hash(self, fake_state, monkeypatch, tmp_path):
        """Deux load() concurrents du même modèle → un seul hachage en vol."""
        gguf, digest = make_gguf(tmp_path, "partage.gguf", b"contenu partage")
        model_dict = make_model_dict("partage", gguf, sha256=digest)

        gate = threading.Event()
        probe = _HashProbe(gate=gate)
        monkeypatch.setattr(integrity, "_hash_file", probe)

        async def scenario():
            first = asyncio.create_task(fake_state.load(model_dict))
            await _until(lambda: probe.calls == 1)  # hachage en vol, retenu par la gate
            second = asyncio.create_task(fake_state.load(model_dict))
            await asyncio.sleep(0.05)  # laisse le second appelant rejoindre le vol
            assert not first.done() and not second.done()
            gate.set()
            return await asyncio.gather(first, second)

        first, second = asyncio.run(scenario())

        assert probe.calls == 1, "les chargements concurrents partagent le même hachage"
        assert first.already_loaded is False
        assert second.already_loaded is True
        assert first.port == second.port
        assert FakeServerManager.ENSURE_CALLS["partage"] == 1
        assert len(fake_state._port_pool) == main.settings.max_loaded_models - 1


# ── No-op sans empreinte déclarée ────────────────────────────────────────────

class TestNoSha256Noop:
    def test_no_declared_sha256_never_invokes_hasher(self, fake_state, monkeypatch, tmp_path):
        """
        Sans empreinte déclarée, aucun hachage (no-op). Contrôle positif : le
        même compteur VOIT un appel dès qu'une empreinte (fausse) est déclarée
        — le zéro observé est donc significatif.
        """
        gguf, _digest = make_gguf(tmp_path, "non-signe.gguf", b"contenu non signe")
        model_dict = make_model_dict("non-signe", gguf)  # pas de sha256

        probe = _HashProbe()
        monkeypatch.setattr(integrity, "_hash_file", probe)

        async def scenario():
            resp = await fake_state.load(model_dict)
            noop_calls = probe.calls
            # Contrôle positif : empreinte déclarée (fausse) → l'attestation hache.
            model_dict["sha256"] = "0" * 64
            with pytest.raises(HTTPException) as exc_info:
                await fake_state.load(model_dict)
            return resp, noop_calls, exc_info.value

        resp, noop_calls, exc = asyncio.run(scenario())

        assert resp.already_loaded is False
        assert noop_calls == 0, "sans sha256 déclaré, aucun hachage ne doit avoir lieu"
        assert probe.calls == 1, (
            "contrôle positif : le compteur voit les appels dès qu'une empreinte existe"
        )
        assert exc.status_code == 422
