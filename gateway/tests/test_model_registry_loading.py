"""
Tests du chargement/validation du registre de modèles (model_registry.py).

Couvre les défenses anti path-traversal / SSRF sur les fichiers GGUF :
  - fichier YAML absent / malformé (clé 'models' manquante, top-level liste) ;
  - IDs dupliqués, IDs invalides (regex stricte) ;
  - chemins relatifs, extension non-.gguf ;
  - allowed_model_dirs : chemin hors périmètre rejeté, chemin sous périmètre accepté ;
  - contournement par symlink (le .resolve() du registre doit le défaire) ;
  - add()/remove() : persistance atomique et erreurs sur ID dupliqué/inconnu ;
  - vram_gb <= 0 rejeté.
"""
from __future__ import annotations

import os

import pytest
import yaml

from model_registry import ModelRegistry


# ── Helpers ──────────────────────────────────────────────────────────────────

def _base_entry(**overrides) -> dict:
    """Entrée de modèle minimale valide (path absolu .gguf, sans allowed_dirs)."""
    entry = {
        "id": "llama-3.3-70b",
        "path": "/models/llama-3.3-70b.gguf",
        "vram_gb": 5.0,
    }
    entry.update(overrides)
    return entry


def _write_registry(tmp_path, models: list[dict]) -> ModelRegistry:
    """Écrit un models.yaml minimal et instancie un ModelRegistry dessus."""
    cfg = tmp_path / "models.yaml"
    cfg.write_text(yaml.safe_dump({"models": models}), encoding="utf-8")
    return ModelRegistry(config_path=cfg)


# ── 1. Fichier YAML absent ──────────────────────────────────────────────────

def test_missing_yaml_file_raises_file_not_found(tmp_path):
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(FileNotFoundError):
        ModelRegistry(config_path=missing)


# ── 2. YAML sans clé 'models' ────────────────────────────────────────────────

def test_yaml_without_models_key_raises(tmp_path):
    cfg = tmp_path / "models.yaml"
    cfg.write_text(yaml.safe_dump({}), encoding="utf-8")
    with pytest.raises(ValueError, match="models"):
        ModelRegistry(config_path=cfg)


def test_yaml_with_unrelated_key_raises(tmp_path):
    cfg = tmp_path / "models.yaml"
    cfg.write_text(yaml.safe_dump({"autre": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="models"):
        ModelRegistry(config_path=cfg)


# ── 3. Top-level malformé : une liste au lieu d'un dict ─────────────────────

def test_yaml_top_level_list_raises(tmp_path):
    cfg = tmp_path / "models.yaml"
    cfg.write_text(yaml.safe_dump([{"id": "m", "path": "/models/m.gguf", "vram_gb": 1.0}]), encoding="utf-8")
    with pytest.raises(ValueError):
        ModelRegistry(config_path=cfg)


# ── 4. IDs dupliqués ──────────────────────────────────────────────────────

def test_duplicate_model_id_raises(tmp_path):
    models = [
        _base_entry(id="dup-model", path="/models/a.gguf"),
        _base_entry(id="dup-model", path="/models/b.gguf"),
    ]
    with pytest.raises(ValueError, match="dupliqu"):
        _write_registry(tmp_path, models)


# ── 5. IDs invalides (path traversal / caractères interdits) ────────────────

@pytest.mark.parametrize(
    "bad_id",
    [
        "../../etc/passwd",
        "Foo",          # majuscule interdite
        "a/b",          # slash interdit
        "",             # vide
    ],
)
def test_invalid_model_id_raises(tmp_path, bad_id):
    models = [_base_entry(id=bad_id)]
    with pytest.raises(ValueError):
        _write_registry(tmp_path, models)


# ── 6. Chemin relatif ────────────────────────────────────────────────────────

def test_relative_path_raises(tmp_path):
    models = [_base_entry(path="models/relative.gguf")]
    with pytest.raises(ValueError, match="absolu"):
        _write_registry(tmp_path, models)


# ── 7. Extension non-.gguf ──────────────────────────────────────────────────

@pytest.mark.parametrize("bad_path", ["/models/m.bin", "/models/m.txt", "/models/m"])
def test_non_gguf_extension_raises(tmp_path, bad_path):
    models = [_base_entry(path=bad_path)]
    with pytest.raises(ValueError, match="gguf"):
        _write_registry(tmp_path, models)


# ── 8. allowed_model_dirs : hors périmètre rejeté, sous périmètre accepté ───

def test_path_outside_allowed_dirs_raises(tmp_path):
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        yaml.safe_dump({"models": [_base_entry(path=str(outside_dir / "m.gguf"))]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="autoris"):
        ModelRegistry(config_path=cfg, allowed_model_dirs=[str(allowed_dir)])


def test_path_under_allowed_dir_is_accepted(tmp_path):
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()

    cfg = tmp_path / "models.yaml"
    model_path = allowed_dir / "m.gguf"
    cfg.write_text(
        yaml.safe_dump({"models": [_base_entry(path=str(model_path))]}),
        encoding="utf-8",
    )
    reg = ModelRegistry(config_path=cfg, allowed_model_dirs=[str(allowed_dir)])
    model = reg.get("llama-3.3-70b")
    assert model is not None
    assert model.path == model_path


# ── 9. Contournement par symlink ────────────────────────────────────────────

def test_symlink_escaping_allowed_dir_is_rejected(tmp_path):
    """
    Un symlink DANS un répertoire autorisé qui pointe vers un .gguf HORS de ce
    répertoire doit être rejeté : ModelRegistry._validate_model_path résout le
    chemin via .resolve() avant de vérifier l'appartenance au périmètre.
    """
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    real_target = outside_dir / "secret.gguf"
    real_target.write_bytes(b"fake gguf content")

    symlink_path = allowed_dir / "innocuous.gguf"
    os.symlink(real_target, symlink_path)

    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        yaml.safe_dump({"models": [_base_entry(path=str(symlink_path))]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="autoris"):
        ModelRegistry(config_path=cfg, allowed_model_dirs=[str(allowed_dir)])


# ── 10. add() : persistance atomique + rejet doublon ────────────────────────

def test_add_persists_atomically_and_is_visible_after_reload(tmp_path):
    reg = _write_registry(tmp_path, [_base_entry()])
    new_entry = _base_entry(id="new-model", path="/models/new-model.gguf")
    reg.add(new_entry)

    # Relire depuis le disque via une toute nouvelle instance.
    cfg = tmp_path / "models.yaml"
    reloaded = ModelRegistry(config_path=cfg)
    assert reloaded.get("new-model") is not None
    assert reloaded.get("llama-3.3-70b") is not None


def test_add_duplicate_id_raises(tmp_path):
    reg = _write_registry(tmp_path, [_base_entry()])
    with pytest.raises(ValueError, match="existe déjà"):
        reg.add(_base_entry())


# ── 11. remove() ─────────────────────────────────────────────────────────────

def test_remove_unknown_id_raises_key_error(tmp_path):
    reg = _write_registry(tmp_path, [_base_entry()])
    with pytest.raises(KeyError):
        reg.remove("does-not-exist")


def test_remove_existing_id_disappears_and_rewrites_yaml(tmp_path):
    reg = _write_registry(tmp_path, [_base_entry()])
    reg.remove("llama-3.3-70b")
    assert reg.get("llama-3.3-70b") is None

    cfg = tmp_path / "models.yaml"
    on_disk = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    ids_on_disk = [m["id"] for m in on_disk["models"]]
    assert "llama-3.3-70b" not in ids_on_disk


# ── 12. vram_gb <= 0 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_vram", [0, -1, -0.5])
def test_vram_gb_not_positive_raises(tmp_path, bad_vram):
    models = [_base_entry(vram_gb=bad_vram)]
    with pytest.raises(ValueError, match="vram_gb"):
        _write_registry(tmp_path, models)
