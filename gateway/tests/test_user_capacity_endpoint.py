from __future__ import annotations

from fastapi.testclient import TestClient

import main
from auth import get_current_user
from config import settings


class FakeManager:
    def __init__(self, queue: dict | None):
        self._queue = queue

    def status(self) -> dict:
        status = {
            "vram_budget": {},
            "models": [],
        }
        if self._queue is not None:
            status["capacity_queue"] = self._queue
        return status


async def fake_user() -> dict:
    return {"user_id": 1, "key_id": 1, "username": "test"}


def test_capacity_status_exposes_minimal_authenticated_queue_state(monkeypatch):
    monkeypatch.setattr(
        main,
        "model_manager",
        FakeManager({
            "enabled": True,
            "waiters": 2,
            "max_waiters": 100,
            "timeout_seconds": 120,
        }),
    )
    monkeypatch.setattr(settings, "capacity_queue_retry_after_seconds", 10)
    main.app.dependency_overrides[get_current_user] = fake_user
    try:
        client = TestClient(main.app)
        response = client.get("/v1/capacity", headers={"Authorization": "Bearer test"})
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "object": "capacity_queue",
        "mode": settings.cluster_mode,
        "available": True,
        "enabled": True,
        "status": "waiting",
        "waiters": 2,
        "max_waiters": 100,
        "timeout_seconds": 120,
        "retry_after_seconds": 10,
    }


def test_capacity_status_requires_user_auth():
    client = TestClient(main.app)
    response = client.get("/v1/capacity")

    assert response.status_code == 401


def test_capacity_status_handles_missing_queue(monkeypatch):
    monkeypatch.setattr(main, "model_manager", FakeManager(None))
    monkeypatch.setattr(settings, "capacity_queue_retry_after_seconds", 10)
    main.app.dependency_overrides[get_current_user] = fake_user
    try:
        client = TestClient(main.app)
        response = client.get("/v1/capacity", headers={"Authorization": "Bearer test"})
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["status"] == "unavailable"
    assert body["waiters"] == 0
