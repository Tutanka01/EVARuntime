"""
Tests du drain borné des tâches fire-and-forget (`background.drain_pending`).

ACC-002 : les lignes d'usage planifiées en fire-and-forget juste avant le
SIGTERM pouvaient être perdues au redémarrage — rien ne drainait `_tasks`.
Le lifespan attend maintenant (borné) leur achèvement après le déchargement
des modèles et avant la fermeture du client HTTP/DB :

  - tâches déjà terminées → retour 0 ;
  - tâche bloquée → attente bornée par le deadline, tâche NON annulée ;
  - deadline 0 ou registre vide → retour immédiat.

Le fichier porte aussi la validation du réglage
`SHUTDOWN_BACKGROUND_FLUSH_SECONDS` (même style que test_retention.py).
"""
from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

import background
from config import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolate_task_registry():
    """Isole le registre global `_tasks` entre les tests."""
    background._tasks.clear()
    yield
    background._tasks.clear()


@pytest.mark.anyio
async def test_drain_returns_zero_when_pending_tasks_complete():
    """Les tâches en vol s'achèvent dans le délai → drain retourne 0."""

    async def rapide() -> None:
        await asyncio.sleep(0)

    background.fire_and_forget(rapide(), name="usage-rapide")

    assert await background.drain_pending(1.0) == 0


@pytest.mark.anyio
async def test_drain_is_bounded_when_a_task_never_completes():
    """
    Une tâche bloquée au-delà du deadline borne l'attente : drain retourne 1
    (tâche toujours en cours, NON annulée), en nettement moins que
    deadline + 1 s — le flush ne doit jamais bloquer un arrêt.
    """
    event = asyncio.Event()  # jamais levé

    async def bloquee() -> None:
        await event.wait()

    task = background.fire_and_forget(bloquee(), name="usage-bloquee")
    try:
        start = time.monotonic()
        remaining = await background.drain_pending(0.2)
        elapsed = time.monotonic() - start

        assert remaining == 1
        assert elapsed < 0.2 + 1.0
        assert not task.done(), "le drain ne doit pas annuler les tâches restantes"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_drain_with_zero_deadline_returns_immediately():
    """`0` = pas d'attente : retour immédiat avec le compte des tâches en vol."""
    event = asyncio.Event()  # jamais levé

    async def bloquee() -> None:
        await event.wait()

    task = background.fire_and_forget(bloquee(), name="usage-bloquee")
    try:
        start = time.monotonic()
        remaining = await background.drain_pending(0)
        elapsed = time.monotonic() - start

        assert remaining == 1
        assert elapsed < 0.5
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_drain_empty_registry_returns_zero():
    """Aucune tâche en vol → 0, quel que soit le deadline."""
    assert await background.drain_pending(1.0) == 0


# ── Validation du réglage SHUTDOWN_BACKGROUND_FLUSH_SECONDS ──────────────────

def _settings(monkeypatch, **env: str) -> Settings:
    """Construit un Settings depuis de vraies variables d'environnement."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_shutdown_background_flush_negatif_refuse(monkeypatch):
    with pytest.raises(Exception, match="shutdown_background_flush_seconds"):
        _settings(monkeypatch, SHUTDOWN_BACKGROUND_FLUSH_SECONDS="-1")


def test_shutdown_background_flush_default_et_zero_legaux(monkeypatch):
    # Défaut 5.0 ; 0 = pas d'attente, valeur légale.
    assert Settings(_env_file=None).shutdown_background_flush_seconds == 5.0
    assert _settings(monkeypatch, SHUTDOWN_BACKGROUND_FLUSH_SECONDS="0") \
        .shutdown_background_flush_seconds == 0.0
