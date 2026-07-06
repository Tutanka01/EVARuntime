"""
Tests des garde-fous supply-chain :
  - parsing/validation du champ `sha256` (opt-in) dans le registre ;
  - vérification d'intégrité SHA-256 sur un GGUF factice ;
  - round-trip to_dict()/_parse_entry() préservant `sha256` ;
  - parseur de version llama-server (sans lancer de vrai binaire).
"""
from __future__ import annotations

import hashlib

import pytest

from llama_version import parse_llama_version
from model_registry import IntegrityError, ModelRegistry


def _bare_registry() -> ModelRegistry:
    """ModelRegistry sans _load() — juste pour appeler _parse_entry."""
    reg = ModelRegistry.__new__(ModelRegistry)
    reg._allowed_dirs = []
    return reg


def _entry(**extra) -> dict:
    return {"id": "m", "path": "/models/m.gguf", "vram_gb": 5.0, **extra}


# ── Parsing / validation du sha256 ──────────────────────────────────────────

def test_valid_sha256_parses_and_normalizes():
    reg = _bare_registry()
    good = "A" * 64  # 64 hex, majuscules → normalisé en minuscules
    model = reg._parse_entry(_entry(sha256=good))
    assert model.sha256 == "a" * 64


def test_absent_sha256_is_none():
    model = _bare_registry()._parse_entry(_entry())
    assert model.sha256 is None


@pytest.mark.parametrize(
    "bad",
    [
        "abc",                 # trop court
        "g" * 64,              # caractère non hexadécimal
        "a" * 63,              # 63 caractères
        "a" * 65,              # 65 caractères
        "",                    # vide
    ],
)
def test_malformed_sha256_rejected(bad):
    reg = _bare_registry()
    with pytest.raises(ValueError, match="sha256"):
        reg._parse_entry(_entry(sha256=bad))


# ── verify_integrity sur un GGUF factice ────────────────────────────────────

def _make_gguf(tmp_path, content: bytes = b"fake gguf bytes"):
    p = tmp_path / "m.gguf"
    p.write_bytes(content)
    return p, hashlib.sha256(content).hexdigest()


def test_verify_integrity_correct_hash_ok(tmp_path):
    path, digest = _make_gguf(tmp_path)
    model = _bare_registry()._parse_entry(
        {"id": "m", "path": str(path), "vram_gb": 5.0, "sha256": digest}
    )
    assert model.verify_integrity() is True


def test_verify_integrity_wrong_hash_raises(tmp_path):
    path, _ = _make_gguf(tmp_path)
    wrong = "0" * 64
    model = _bare_registry()._parse_entry(
        {"id": "m", "path": str(path), "vram_gb": 5.0, "sha256": wrong}
    )
    with pytest.raises(IntegrityError, match="non conforme"):
        model.verify_integrity()


def test_verify_integrity_missing_file_raises(tmp_path):
    path, digest = _make_gguf(tmp_path)
    path.unlink()
    model = _bare_registry()._parse_entry(
        {"id": "m", "path": str(path), "vram_gb": 5.0, "sha256": digest}
    )
    with pytest.raises(IntegrityError, match="introuvable"):
        model.verify_integrity()


def test_verify_integrity_noop_when_no_sha256(tmp_path):
    path, _ = _make_gguf(tmp_path)
    model = _bare_registry()._parse_entry(
        {"id": "m", "path": str(path), "vram_gb": 5.0}
    )
    # Aucun sha256 déclaré → no-op, pas d'I/O, retourne True.
    assert model.verify_integrity() is True


# ── Round-trip to_dict() / _parse_entry() ───────────────────────────────────

def test_sha256_survives_roundtrip():
    reg = _bare_registry()
    digest = "b" * 64
    model = reg._parse_entry(_entry(sha256=digest))
    d = model.to_dict()
    assert d["sha256"] == digest
    reparsed = reg._parse_entry(d)
    assert reparsed.sha256 == digest


def test_to_dict_omits_sha256_when_absent():
    assert "sha256" not in _bare_registry()._parse_entry(_entry()).to_dict()


# ── Parseur de version llama-server (sans binaire réel) ─────────────────────

@pytest.mark.parametrize(
    "output, expected",
    [
        ("version: 4567 (abc1234)", 4567),
        ("build: 4567 (abc1234)", 4567),
        ("some noise\nversion: 12345 (deadbee)\nmore", 12345),
        ("VERSION 999", 999),
    ],
)
def test_parse_llama_version_known_formats(output, expected):
    assert parse_llama_version(output) == expected


@pytest.mark.parametrize(
    "output",
    [
        "",
        "no version here",
        "llama-server: command not found",
        "<binaire injoignable>",
    ],
)
def test_parse_llama_version_unknown_returns_none(output):
    # Tolérance aux formats inconnus : ne lève jamais, retourne None.
    assert parse_llama_version(output) is None
