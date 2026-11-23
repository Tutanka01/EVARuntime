"""
`POST /admin/models/{id}/load` ne doit pas resservir l'échec récent mémorisé par
le mode cluster : l'opérateur vient peut-être de corriger l'artefact sur le nœud.

Le cache négatif vit dans `ClusterManager` (`forget_load_failures`) ; le mode
local n'en a pas. La route doit donc l'oublier quand le gestionnaire l'expose,
AVANT de demander le chargement, et rester inchangée quand il ne l'expose pas.
"""
from __future__ import annotations

import asyncio

import admin


class _ManagerWithFailureCache:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def forget_load_failures(self, model_id: str) -> None:
        self.calls.append(("forget", model_id))

    async def ensure_model_loaded(self, model_id: str) -> None:
        self.calls.append(("ensure", model_id))


class _ManagerWithoutFailureCache:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def ensure_model_loaded(self, model_id: str) -> None:
        self.calls.append(("ensure", model_id))


def test_admin_load_forgets_recent_failures_before_loading(monkeypatch):
    manager = _ManagerWithFailureCache()
    monkeypatch.setattr(admin, "model_manager", manager)

    result = asyncio.run(admin.load_model("m1", None))

    assert manager.calls == [("forget", "m1"), ("ensure", "m1")]
    assert "m1" in result["message"]


def test_admin_load_works_without_failure_cache(monkeypatch):
    """Mode local : le gestionnaire n'a pas de cache d'échec, rien ne change."""
    manager = _ManagerWithoutFailureCache()
    monkeypatch.setattr(admin, "model_manager", manager)

    result = asyncio.run(admin.load_model("m1", None))

    assert manager.calls == [("ensure", "m1")]
    assert "m1" in result["message"]
