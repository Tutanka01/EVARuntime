from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from model_registry import (
    LlamaParams,
    ModelRegistry,
    SpeculativeParams,
)


# ── SpeculativeParams : validation ──────────────────────────────────────────

def test_speculative_defaults():
    s = SpeculativeParams()
    assert s.type == "mtp"
    assert s.draft_max == 16
    assert s.draft_min == 0
    assert s.draft_p_min == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"type": "unknown"},      # type hors allowlist
        {"draft_max": 0},          # < 1
        {"draft_min": -1},         # < 0
        {"draft_min": 5, "draft_max": 4},  # min > max
        {"draft_p_min": 1.5},      # hors [0, 1]
        {"draft_p_min": -0.1},     # hors [0, 1]
    ],
)
def test_speculative_invalid(kwargs):
    with pytest.raises(ValueError):
        SpeculativeParams(**kwargs)


# ── build_llama_cmd : émission des flags ────────────────────────────────────

def _model(**extra):
    """Construit une ModelDefinition minimale via _parse_entry (validation incluse)."""
    reg = ModelRegistry.__new__(ModelRegistry)  # pas de _load()
    reg._allowed_dirs = []
    entry = {
        "id": "m",
        "path": "/models/m.gguf",
        "vram_gb": 5.0,
        **extra,
    }
    return reg._parse_entry(entry)


def _cmd(model):
    return model.build_llama_cmd(
        binary=Path("/usr/bin/llama-server"),
        host="127.0.0.1",
        port=8081,
        log_path=Path("/tmp/x.log"),
    )


def test_cmd_never_contains_api_key():
    """La clé interne passe par l'env (LLAMA_API_KEY), jamais par argv (visible via ps)."""
    cmd = _cmd(_model())
    assert "--api-key" not in cmd


def test_no_speculative_block_emits_no_spec_flags():
    """Rétrocompat : sans bloc speculative, aucun flag --spec-* ."""
    cmd = _cmd(_model())
    assert not any(arg.startswith("--spec") for arg in cmd)


def test_mtp_emits_spec_type_and_n_max():
    cmd = _cmd(_model(speculative={"type": "mtp", "draft_max": 8}))
    assert "--spec-type" in cmd
    assert cmd[cmd.index("--spec-type") + 1] == "draft-mtp"
    assert "--spec-draft-n-max" in cmd
    assert cmd[cmd.index("--spec-draft-n-max") + 1] == "8"
    # n-min et p-min restent aux défauts (0) → non émis
    assert "--spec-draft-n-min" not in cmd
    assert "--spec-draft-p-min" not in cmd


def test_mtp_emits_optional_flags_when_set():
    cmd = _cmd(_model(speculative={"draft_max": 10, "draft_min": 2, "draft_p_min": 0.75}))
    assert cmd[cmd.index("--spec-draft-n-min") + 1] == "2"
    assert cmd[cmd.index("--spec-draft-p-min") + 1] == "0.75"


# ── Round-trip to_dict() → _parse_entry() ───────────────────────────────────

def test_speculative_survives_roundtrip():
    model = _model(speculative={"type": "mtp", "draft_max": 12, "draft_min": 1, "draft_p_min": 0.5})
    d = model.to_dict()
    assert d["speculative"] == {
        "type": "mtp",
        "draft_max": 12,
        "draft_min": 1,
        "draft_p_min": 0.5,
    }
    reg = ModelRegistry.__new__(ModelRegistry)
    reg._allowed_dirs = []
    reparsed = reg._parse_entry(d)
    assert reparsed.speculative == SpeculativeParams(
        type="mtp", draft_max=12, draft_min=1, draft_p_min=0.5
    )


def test_to_dict_omits_speculative_when_absent():
    assert "speculative" not in _model().to_dict()


def test_invalid_speculative_in_yaml_raises():
    reg = ModelRegistry.__new__(ModelRegistry)
    reg._allowed_dirs = []
    with pytest.raises(ValueError, match="speculative"):
        reg._parse_entry(
            {"id": "m", "path": "/models/m.gguf", "vram_gb": 5.0,
             "speculative": {"type": "bogus"}}
        )


# ── update() / set_enabled() préservent le bloc speculative ─────────────────

def _registry_with_one_model(tmp_path: Path) -> ModelRegistry:
    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {"models": [{
                "id": "m",
                "path": "/models/m.gguf",
                "vram_gb": 5.0,
                "enabled": True,
                "load_timeout_seconds": 120,
                "speculative": {"type": "mtp", "draft_max": 7},
            }]}
        ),
        encoding="utf-8",
    )
    return ModelRegistry(config_path=cfg)


def test_update_preserves_speculative_and_timeout(tmp_path):
    reg = _registry_with_one_model(tmp_path)
    updated = reg.update("m", vram_gb=6.0)
    assert updated.vram_gb == 6.0
    assert updated.speculative == SpeculativeParams(type="mtp", draft_max=7)
    assert updated.load_timeout_seconds == 120


def test_set_enabled_preserves_speculative_and_timeout(tmp_path):
    reg = _registry_with_one_model(tmp_path)
    updated = reg.set_enabled("m", False)
    assert updated.enabled is False
    assert updated.speculative == SpeculativeParams(type="mtp", draft_max=7)
    assert updated.load_timeout_seconds == 120
