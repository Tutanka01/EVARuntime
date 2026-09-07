from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main
from model_registry import IntegrityError


@pytest.mark.anyio
async def test_cluster_startup_delegates_binary_and_gguf_validation(monkeypatch):
    """Un orchestrateur sans GPU ne doit exiger ni binaire ni GGUF locaux."""
    monkeypatch.setattr(main.settings, "cluster_mode", "cluster")
    enforce = AsyncMock(return_value=False)
    monkeypatch.setattr(main, "enforce_llama_min_build", enforce)
    attest = AsyncMock(return_value="ok")
    monkeypatch.setattr(main, "attest_gguf", attest)
    model = SimpleNamespace(id="remote-model", sha256="a" * 64)

    await main._validate_inference_runtime([model])

    enforce.assert_not_awaited()
    attest.assert_not_awaited()


@pytest.mark.anyio
async def test_local_startup_attests_every_model_with_declared_digest(monkeypatch):
    """
    Le parcours mono-nœud conserve le garde-fou d'intégrité, désormais via
    attest_gguf (hors event loop, cache attesté partagé avec le pré-chargement).
    Les modèles sans empreinte déclarée restent acceptés sans attestation.
    """
    monkeypatch.setattr(main.settings, "cluster_mode", "local")
    enforce = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "enforce_llama_min_build", enforce)
    attest = AsyncMock(return_value="ok")
    monkeypatch.setattr(main, "attest_gguf", attest)
    stamped = SimpleNamespace(id="local-model", sha256="b" * 64)
    unstamped = SimpleNamespace(id="no-digest", sha256=None)

    await main._validate_inference_runtime([stamped, unstamped])

    enforce.assert_awaited_once_with(
        main.settings.llama_server_bin,
        main.settings.llama_server_min_build,
    )
    attest.assert_awaited_once_with(stamped)


@pytest.mark.anyio
async def test_local_startup_refuses_outdated_llama_binary(monkeypatch):
    monkeypatch.setattr(main.settings, "cluster_mode", "local")
    monkeypatch.setattr(
        main,
        "enforce_llama_min_build",
        AsyncMock(return_value=False),
    )

    with pytest.raises(RuntimeError, match="LLAMA_SERVER_MIN_BUILD"):
        await main._validate_inference_runtime([])


@pytest.mark.anyio
async def test_local_startup_refuses_tampered_gguf(monkeypatch):
    """Un échec d'attestation au démarrage reste fail-closed (RuntimeError)."""
    monkeypatch.setattr(main.settings, "cluster_mode", "local")
    monkeypatch.setattr(main, "enforce_llama_min_build", AsyncMock(return_value=True))
    monkeypatch.setattr(
        main,
        "attest_gguf",
        AsyncMock(side_effect=IntegrityError("empreinte SHA-256 non conforme")),
    )
    model = SimpleNamespace(id="local-model", sha256="b" * 64)

    with pytest.raises(RuntimeError, match="démarrage refusé"):
        await main._validate_inference_runtime([model])
