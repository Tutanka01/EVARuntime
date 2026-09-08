"""
Tests du module d'attestation d'intégrité GGUF (SEC-ART-001 / CLU-002) :

- no-op sans empreinte déclarée (avec contrôle positif) ;
- cache attesté : fichier inchangé non re-haché, invalidation sur mutation
  de contenu, d'identité (mtime) ou d'empreinte déclarée ;
- échecs fail-closed : fichier absent, empreinte divergente, mutation pendant
  le hachage (TOCTOU) ;
- single-flight : un seul hachage partagé entre appelants concurrents ;
- annulation du porteur sans double hachage ni blocage des appels suivants ;
- robustesse multi-boucles (asyncio.run séquentiels) et borne du cache.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from types import SimpleNamespace

import pytest

import integrity
from integrity import attest_gguf, attest_model_artifacts, reset_integrity_cache
from model_registry import IntegrityError

# Tous les tests async de ce module tournent sur asyncio (backend forcé en conftest).
pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _clean_cache():
    """Le cache est un état module — on repart de zéro entre chaque test."""
    reset_integrity_cache()
    yield
    reset_integrity_cache()


def _make_model(tmp_path, content: bytes = b"fake gguf bytes", sha256: str | None = "auto"):
    """GGUF factice + modèle duck-typed (id/path/sha256)."""
    path = tmp_path / "m.gguf"
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest() if sha256 == "auto" else sha256
    model = type("FakeModel", (), {"id": "m", "path": path, "sha256": digest})
    return path, model, digest


class _HashCounter:
    """Compteur d'appels autour de integrity._hash_file, avec gate optionnel."""

    def __init__(self, gate: threading.Event | None = None):
        self.calls = 0
        self.gate = gate

    def __call__(self, path):
        self.calls += 1
        if self.gate is not None:
            assert self.gate.wait(timeout=5), "gate jamais libérée pendant le hachage"
        return hashlib.sha256(path.read_bytes()).hexdigest()


async def _until(predicate, timeout: float = 2.0):
    """Attend (boucle event) qu'une condition devienne vraie — évite les sleeps."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition jamais remplie (délai dépassé)")
        await asyncio.sleep(0.005)


def _observe_inflight_waiter(monkeypatch):
    """Signale qu'un appelant a rejoint la future single-flight partagée."""
    joined = asyncio.Event()
    original_shield = integrity.asyncio.shield
    shield_calls = 0

    def _shield_and_signal(fut):
        nonlocal shield_calls
        shield_calls += 1
        if shield_calls >= 2:
            joined.set()
        return original_shield(fut)

    monkeypatch.setattr(integrity.asyncio, "shield", _shield_and_signal)
    return joined


# ── No-op et chemin nominal ──────────────────────────────────────────────────

async def test_noop_without_declared_sha256(tmp_path):
    _path, model, _digest = _make_model(tmp_path, sha256=None)

    counter = _HashCounter()
    original = integrity._hash_file
    integrity._hash_file = counter
    try:
        assert await attest_gguf(model) == ""
        assert counter.calls == 0
        # Contrôle positif : le compteur VOIT les appels dès qu'une empreinte
        # est déclarée — le zéro observé ci-dessus est donc significatif.
        model.sha256 = "0" * 64
        with pytest.raises(IntegrityError):
            await attest_gguf(model)
        assert counter.calls == 1
    finally:
        integrity._hash_file = original


async def test_attest_returns_declared_digest(tmp_path):
    _path, model, digest = _make_model(tmp_path)
    assert await attest_gguf(model) == digest


async def test_declared_uppercase_is_normalized(tmp_path):
    _path, model, digest = _make_model(tmp_path)
    model.sha256 = digest.upper()
    assert await attest_gguf(model) == digest.lower()


# ── Cache attesté ────────────────────────────────────────────────────────────

async def test_unchanged_file_hashed_once(tmp_path):
    _path, model, _digest = _make_model(tmp_path)

    counter = _HashCounter()
    original = integrity._hash_file
    integrity._hash_file = counter
    try:
        assert await attest_gguf(model) == model.sha256
        assert await attest_gguf(model) == model.sha256
        assert await attest_gguf(model) == model.sha256
        assert counter.calls == 1, "le fichier inchangé ne doit pas être re-haché"
    finally:
        integrity._hash_file = original


async def test_content_change_forces_rehash(tmp_path):
    path, model, _digest = _make_model(tmp_path)

    counter = _HashCounter()
    original = integrity._hash_file
    integrity._hash_file = counter
    try:
        await attest_gguf(model)
        # Même longueur → la détection passe par l'identité (mtime_ns), pas la taille.
        path.write_bytes(b"tampered content!!!!!")
        with pytest.raises(IntegrityError, match="non conforme"):
            await attest_gguf(model)
        assert counter.calls == 2
    finally:
        integrity._hash_file = original


async def test_mtime_touch_forces_rehash(tmp_path):
    path, model, _digest = _make_model(tmp_path)

    counter = _HashCounter()
    original = integrity._hash_file
    integrity._hash_file = counter
    try:
        await attest_gguf(model)
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        await attest_gguf(model)
        assert counter.calls == 2, "un mtime modifié doit invalider l'attestation"
    finally:
        integrity._hash_file = original


async def test_declared_digest_change_forces_rehash(tmp_path):
    path, model, _digest = _make_model(tmp_path)

    counter = _HashCounter()
    original = integrity._hash_file
    integrity._hash_file = counter
    try:
        await attest_gguf(model)
        # Même fichier, autre empreinte déclarée (édition YAML + reload) → re-hachage.
        model.sha256 = "1" * 64
        with pytest.raises(IntegrityError, match="non conforme"):
            await attest_gguf(model)
        assert counter.calls == 2
    finally:
        integrity._hash_file = original


# ── Échecs fail-closed ───────────────────────────────────────────────────────

async def test_missing_file_raises(tmp_path):
    path, model, _digest = _make_model(tmp_path)
    path.unlink()
    with pytest.raises(IntegrityError, match="introuvable ou illisible"):
        await attest_gguf(model)


async def test_mutation_during_hash_refused(tmp_path):
    """TOCTOU : un fichier substitué pendant le hachage est refusé."""
    path, model, _digest = _make_model(tmp_path)

    def _substituting_hash(p):
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        p.write_bytes(b"swapped mid-hash")  # mute l'identité sous nos pieds
        return digest

    original = integrity._hash_file
    integrity._hash_file = _substituting_hash
    try:
        with pytest.raises(IntegrityError, match="muté pendant le hachage"):
            await attest_gguf(model)
        assert not integrity._attested, "aucune attestation ne doit être mémorisée"
    finally:
        integrity._hash_file = original


# ── Single-flight ────────────────────────────────────────────────────────────

async def test_concurrent_callers_share_one_hash(tmp_path, monkeypatch):
    _path, model, digest = _make_model(tmp_path)

    gate = threading.Event()
    counter = _HashCounter(gate=gate)
    original = integrity._hash_file
    integrity._hash_file = counter
    try:
        joined = _observe_inflight_waiter(monkeypatch)
        first = asyncio.create_task(attest_gguf(model))
        await _until(lambda: len(integrity._inflight) == 1)
        second = asyncio.create_task(attest_gguf(model))
        await asyncio.wait_for(joined.wait(), timeout=2.0)
        assert counter.calls <= 1
        gate.set()
        results = await asyncio.gather(first, second)
        assert results == [digest, digest]
        assert counter.calls == 1, "les appelants concurrents partagent le hachage"
    finally:
        gate.set()
        integrity._hash_file = original


async def test_owner_cancellation_does_not_hang_waiters(tmp_path, monkeypatch):
    _path, model, digest = _make_model(tmp_path)

    gate = threading.Event()
    counter = _HashCounter(gate=gate)
    original = integrity._hash_file
    integrity._hash_file = counter
    try:
        joined = _observe_inflight_waiter(monkeypatch)
        owner = asyncio.create_task(attest_gguf(model))
        await _until(lambda: len(integrity._inflight) == 1)
        waiter = asyncio.create_task(attest_gguf(model))
        await asyncio.wait_for(joined.wait(), timeout=2.0)
        owner.cancel()
        # L'attente asyncio peut être annulée avant la fin du thread worker ;
        # on libère le hachage pour que ce worker termine proprement.
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.gather(owner)
        # Le worker partagé survit à l'annulation de son premier appelant ; le
        # waiter reçoit le résultat sans lancer un second hachage en parallèle.
        assert await asyncio.gather(waiter) == [digest]
        assert counter.calls == 1
        # L'entrée en vol est purgée et le résultat est en cache.
        assert not integrity._inflight
        assert await attest_gguf(model) == digest
    finally:
        gate.set()
        integrity._hash_file = original


# ── Robustesse multi-boucles / borne du cache ────────────────────────────────

def test_sequential_event_loops_are_independent(tmp_path):
    """asyncio.run successifs (pytest, TestClient) : pas de future orpheline."""
    _path, model, digest = _make_model(tmp_path)

    async def _scenario():
        return await attest_gguf(model)

    assert asyncio.run(_scenario()) == digest
    assert asyncio.run(_scenario()) == digest  # cache hit, nouvelle boucle
    assert not integrity._inflight


async def test_cache_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(integrity, "_CACHE_MAX_ENTRIES", 1)
    tmp_path_b = tmp_path / "b"
    tmp_path_b.mkdir()
    _p1, model_a, _d1 = _make_model(tmp_path, content=b"content a")
    _p2, model_b, _d2 = _make_model(tmp_path_b, content=b"content b, longer")

    await attest_gguf(model_a)
    await attest_gguf(model_b)
    assert len(integrity._attested) <= 1
    assert not integrity._inflight


# ── Artefacts vision (REG-002) ────────────────────────────────────────────────

def _make_vision_model(
    tmp_path,
    *,
    model_content: bytes = b"model",
    projector_content: bytes = b"projector",
    model_sha256: str | None = None,
    projector_sha256: str | None = None,
):
    model_path = tmp_path / "vision.gguf"
    projector_path = tmp_path / "vision-mmproj.gguf"
    model_path.write_bytes(model_content)
    projector_path.write_bytes(projector_content)
    return SimpleNamespace(
        id="vision",
        path=model_path,
        sha256=(
            hashlib.sha256(model_content).hexdigest()
            if model_sha256 == "auto"
            else model_sha256
        ),
        capabilities=["text_generation", "vision"],
        mmproj_path=projector_path,
        mmproj_sha256=(
            hashlib.sha256(projector_content).hexdigest()
            if projector_sha256 == "auto"
            else projector_sha256
        ),
    )


async def test_vision_attests_projector_even_without_main_gguf_digest(tmp_path):
    model = _make_vision_model(tmp_path, projector_sha256="auto")

    assert await attest_model_artifacts(model) is None
    assert (str(model.mmproj_path.resolve()), model.mmproj_sha256) in integrity._attested


async def test_vision_projector_digest_mismatch_is_fail_closed(tmp_path):
    model = _make_vision_model(tmp_path, projector_sha256="0" * 64)

    with pytest.raises(IntegrityError, match="projecteur multimodal"):
        await attest_model_artifacts(model)
    assert not any(key[0] == str(model.mmproj_path.resolve()) for key in integrity._attested)


async def test_vision_missing_projector_is_fail_closed(tmp_path):
    model = _make_vision_model(tmp_path, projector_sha256="auto")
    model.mmproj_path.unlink()

    with pytest.raises(IntegrityError, match="projecteur multimodal"):
        await attest_model_artifacts(model)
