from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import main


@pytest.mark.anyio
async def test_cluster_startup_delegates_binary_and_gguf_validation(monkeypatch):
    """Un orchestrateur sans GPU ne doit exiger ni binaire ni GGUF locaux."""
    monkeypatch.setattr(main.settings, "cluster_mode", "cluster")
    enforce = AsyncMock(return_value=False)
    monkeypatch.setattr(main, "enforce_llama_min_build", enforce)
    model = SimpleNamespace(
        id="remote-model",
        sha256="a" * 64,
        verify_integrity=Mock(side_effect=AssertionError("GGUF local lu en cluster")),
    )

    await main._validate_inference_runtime([model])

    enforce.assert_not_awaited()
    model.verify_integrity.assert_not_called()


@pytest.mark.anyio
async def test_local_startup_keeps_binary_and_gguf_guards(monkeypatch):
    """Le parcours mono-nœud conserve tous les garde-fous historiques."""
    monkeypatch.setattr(main.settings, "cluster_mode", "local")
    enforce = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "enforce_llama_min_build", enforce)
    model = SimpleNamespace(
        id="local-model",
        sha256="b" * 64,
        verify_integrity=Mock(return_value=True),
    )

    await main._validate_inference_runtime([model])

    enforce.assert_awaited_once_with(
        main.settings.llama_server_bin,
        main.settings.llama_server_min_build,
    )
    model.verify_integrity.assert_called_once_with()


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
