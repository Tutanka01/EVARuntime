"""REG-001/REG-002 — contrat strict du registre et des modèles vision."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from model_registry import ModelRegistry
from schemas import ModelEntryCreate


def _registry() -> ModelRegistry:
    """Construit un registre sans toucher au fichier de production."""
    registry = ModelRegistry.__new__(ModelRegistry)
    registry._allowed_dirs = []
    return registry


def _entry(**overrides) -> dict:
    entry = {
        "id": "model-test",
        "path": "/models/model-test.gguf",
        "description": "Modèle de test",
        "vram_gb": 5.0,
        "enabled": True,
        "capabilities": ["text_generation"],
        "llama_params": {},
    }
    entry.update(overrides)
    return entry


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", 42),
        ("path", 42),
        ("description", 42),
        ("vram_gb", "5.0"),
        ("vram_gb", True),
        ("vram_gb", float("nan")),
        ("enabled", "false"),
        ("capabilities", "text_generation"),
        ("capabilities", ["text_generation", 1]),
        ("llama_params", "defaults"),
        ("llama_params", {"ctx_size": "4096"}),
        ("load_timeout_seconds", "60"),
        ("sha256", 42),
        ("speculative", "mtp"),
    ],
)
def test_yaml_rejects_ambiguous_or_wrong_scalar_types(field, value):
    with pytest.raises(ValueError):
        _registry()._parse_entry(_entry(**{field: value}))


def test_yaml_rejects_unknown_model_key():
    with pytest.raises(ValueError, match="inconnues"):
        _registry()._parse_entry(_entry(typo_llama_param=1))


@pytest.mark.parametrize("capabilities", [[], ["text_generation", "vison"]])
def test_yaml_rejects_empty_or_unknown_capabilities(capabilities):
    with pytest.raises(ValueError, match="capabilities"):
        _registry()._parse_entry(_entry(capabilities=capabilities))


def test_yaml_accepts_admin_shaped_entry_and_round_trips():
    model = _registry()._parse_entry(_entry())
    reparsed = _registry()._parse_entry(model.to_dict())

    assert reparsed.id == model.id
    assert reparsed.path == model.path
    assert reparsed.description == model.description
    assert reparsed.vram_gb == model.vram_gb
    assert reparsed.enabled is True
    assert reparsed.capabilities == ["text_generation"]


@pytest.mark.parametrize(
    "payload",
    [
        {"enabled": "false"},
        {"vram_gb": "5.0"},
        {"capabilities": "vision"},
        {"llama_params": {"ctx_size": "4096"}},
        {"unknown": 1},
        {"capabilities": []},
        {"capabilities": ["text_generation", "vison"]},
    ],
)
def test_admin_contract_is_strict_for_the_same_fields(payload):
    body = {"id": "model-test", "path": "/models/model-test.gguf", "vram_gb": 5.0}
    body.update(payload)
    with pytest.raises(ValidationError):
        ModelEntryCreate.model_validate(body)


def test_admin_contract_carries_optional_registry_artifacts():
    body = ModelEntryCreate.model_validate(
        {
            "id": "model-test",
            "path": "/models/model-test.gguf",
            "vram_gb": 5.0,
            "capabilities": ["text_generation", "vision"],
            "mmproj_path": "/models/model-test-mmproj.gguf",
            "mmproj_sha256": "A" * 64,
            "sha256": "B" * 64,
            "load_timeout_seconds": 60,
            "speculative": {"type": "mtp", "draft_max": 8},
        }
    )

    assert body.mmproj_sha256 == "a" * 64
    assert body.sha256 == "b" * 64
    assert body.load_timeout_seconds == 60
    assert body.speculative is not None
    assert body.speculative.draft_max == 8


def test_vision_requires_projector_path_and_digest():
    with pytest.raises(ValueError, match="projecteur multimodal"):
        _registry()._parse_entry(_entry(capabilities=["text_generation", "vision"]))

    with pytest.raises(ValueError, match="projecteur multimodal"):
        _registry()._parse_entry(
            _entry(
                capabilities=["text_generation", "vision"],
                mmproj_path="/models/model-test-mmproj.gguf",
            )
        )


def test_vision_projector_digest_survives_round_trip():
    projector_digest = "A" * 64
    model = _registry()._parse_entry(
        _entry(
            capabilities=["text_generation", "vision"],
            mmproj_path="/models/model-test-mmproj.gguf",
            mmproj_sha256=projector_digest,
        )
    )

    assert model.mmproj_sha256 == "a" * 64
    serialized = model.to_dict()
    assert serialized["mmproj_path"] == "/models/model-test-mmproj.gguf"
    assert serialized["mmproj_sha256"] == "a" * 64
    reparsed = _registry()._parse_entry(serialized)
    assert reparsed.mmproj_path == model.mmproj_path
    assert reparsed.mmproj_sha256 == model.mmproj_sha256


@pytest.mark.parametrize(
    "field,value",
    [
        ("mmproj_path", 42),
        ("mmproj_path", "relative/projector.gguf"),
        ("mmproj_path", "/models/projector.bin"),
        ("mmproj_sha256", 42),
        ("mmproj_sha256", "bad"),
    ],
)
def test_vision_projector_fields_are_strict(field, value):
    entry = _entry(
        capabilities=["text_generation", "vision"],
        mmproj_path="/models/model-test-mmproj.gguf",
        mmproj_sha256="a" * 64,
    )
    entry[field] = value
    with pytest.raises(ValueError):
        _registry()._parse_entry(entry)


def test_projector_digest_without_projector_path_is_rejected():
    with pytest.raises(ValueError, match="mmproj_sha256"):
        _registry()._parse_entry(_entry(mmproj_sha256="a" * 64))
