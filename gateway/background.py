"""
Tâches d'arrière-plan fire-and-forget.

asyncio ne garde qu'une référence faible vers les tâches : un
`asyncio.create_task(...)` sans référence forte peut être ramassé par le GC
avant d'avoir tourné, et ses exceptions sont perdues silencieusement.
Ce module garde une référence forte jusqu'à complétion et journalise les échecs.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

log = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


def fire_and_forget(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """Lance une coroutine hors du critical path, sans en attendre le résultat."""
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_on_done)
    return task


def _on_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("Tâche d'arrière-plan '%s' échouée : %s", task.get_name(), exc)
