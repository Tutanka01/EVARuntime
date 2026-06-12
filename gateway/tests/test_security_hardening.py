"""
Tests des durcissements sécurité :
  - require_admin : fail-closed sur secret placeholder, comparaison correcte
  - quota mensuel de tokens (monthly_token_limit) appliqué à la requête
  - lookup_key : expiration parsée en datetime (formats ISO variés, fail-closed)
  - validation du body JSON dans proxy_request
  - le moniteur d'inactivité ne décharge jamais un modèle pinné
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import database as db
import main
import rate_limiter
from config import settings
from rate_limiter import check_rate_limit
from server_manager import ModelState, ServerManager


# ── require_admin ─────────────────────────────────────────────────────────────

def test_admin_route_rejects_wrong_secret():
    client = TestClient(main.app)
    response = client.get(
        "/admin/cluster", headers={"Authorization": "Bearer mauvais-secret"}
    )
    assert response.status_code == 403


def test_admin_route_accepts_correct_secret():
    client = TestClient(main.app)
    response = client.get(
        "/admin/cluster",
        headers={"Authorization": f"Bearer {settings.admin_secret}"},
    )
    assert response.status_code == 200


def test_admin_routes_disabled_with_placeholder_secret(monkeypatch):
    """Fail-closed : un ADMIN_SECRET laissé à sa valeur d'exemple désactive /admin."""
    monkeypatch.setattr(settings, "admin_secret", "CHANGE_ME_ADMIN_SECRET")
    client = TestClient(main.app)
    # Même en présentant la valeur placeholder « correcte », l'accès est refusé.
    response = client.get(
        "/admin/cluster",
        headers={"Authorization": "Bearer CHANGE_ME_ADMIN_SECRET"},
    )
    assert response.status_code == 503


def test_admin_routes_disabled_with_empty_secret(monkeypatch):
    monkeypatch.setattr(settings, "admin_secret", "")
    client = TestClient(main.app)
    response = client.get("/admin/cluster", headers={"Authorization": "Bearer "})
    assert response.status_code in (403, 503)


# ── Quota mensuel ─────────────────────────────────────────────────────────────

def _user(monthly_limit: int) -> dict:
    return {
        "user_id": 42,
        "key_id": 1,
        "username": "test",
        "rpm_limit": 1000,
        "monthly_token_limit": monthly_limit,
    }


@pytest.mark.anyio
async def test_monthly_quota_blocks_when_exhausted(monkeypatch):
    async def fake_usage(user_id: int) -> int:
        return 500_000

    monkeypatch.setattr(rate_limiter.db, "tokens_used_last_30_days", fake_usage)

    with pytest.raises(HTTPException) as exc_info:
        await check_rate_limit(user=_user(monthly_limit=100_000))

    assert exc_info.value.status_code == 429
    assert "Quota mensuel" in exc_info.value.detail["error"]["message"]


@pytest.mark.anyio
async def test_monthly_quota_allows_under_limit(monkeypatch):
    async def fake_usage(user_id: int) -> int:
        return 50_000

    monkeypatch.setattr(rate_limiter.db, "tokens_used_last_30_days", fake_usage)

    user = await check_rate_limit(user=_user(monthly_limit=100_000))
    assert user["user_id"] == 42


@pytest.mark.anyio
async def test_monthly_quota_zero_means_unlimited(monkeypatch):
    async def fail_if_called(user_id: int) -> int:
        raise AssertionError("ne doit pas interroger la DB quand le quota est illimité")

    monkeypatch.setattr(rate_limiter.db, "tokens_used_last_30_days", fail_if_called)

    user = await check_rate_limit(user=_user(monthly_limit=0))
    assert user["user_id"] == 42


# ── lookup_key : expiration ───────────────────────────────────────────────────

@pytest.fixture
def file_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")


async def _create_user_with_key(expires_at: str | None) -> str:
    await db.init_db()
    user = await db.create_user(username=f"u-{time.monotonic_ns()}")
    raw_key, _ = await db.create_api_key(user["id"], name="t", expires_at=expires_at)
    return raw_key


@pytest.mark.anyio
async def test_lookup_key_accepts_future_expiry(file_db):
    raw = await _create_user_with_key("2099-01-01T00:00:00+00:00")
    assert await db.lookup_key(raw) is not None


@pytest.mark.anyio
async def test_lookup_key_rejects_past_expiry_date_only_format(file_db):
    """Format « date seule » (2020-01-01) — la comparaison lexicale échouait ici."""
    raw = await _create_user_with_key("2020-01-01")
    assert await db.lookup_key(raw) is None


@pytest.mark.anyio
async def test_lookup_key_rejects_invalid_expiry_format(file_db):
    """Fail-closed : format invalide → clé considérée expirée."""
    raw = await _create_user_with_key("pas-une-date")
    assert await db.lookup_key(raw) is None


@pytest.mark.anyio
async def test_lookup_key_no_expiry_is_valid(file_db):
    raw = await _create_user_with_key(None)
    assert await db.lookup_key(raw) is not None


# ── proxy_request : validation du body ────────────────────────────────────────

async def _fake_user() -> dict:
    return {"user_id": 1, "key_id": 1, "username": "test", "rpm_limit": 0,
            "monthly_token_limit": 0}


def test_proxy_rejects_non_object_body():
    main.app.dependency_overrides[check_rate_limit] = _fake_user
    try:
        client = TestClient(main.app)
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer x", "Content-Type": "application/json"},
            content=b'["pas", "un", "objet"]',
        )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_proxy_rejects_non_string_model_field():
    main.app.dependency_overrides[check_rate_limit] = _fake_user
    try:
        client = TestClient(main.app)
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer x", "Content-Type": "application/json"},
            content=b'{"model": {"id": "x"}, "messages": []}',
        )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 400


# ── Moniteur d'inactivité vs pin ──────────────────────────────────────────────

class _FakeModel:
    id = "m"
    description = ""
    enabled = True
    vram_gb = 1.0
    capabilities = ["text_generation"]
    path = "/models/m.gguf"
    load_timeout_seconds = None
    speculative = None


@pytest.mark.anyio
async def test_idle_monitor_never_unloads_pinned_model(monkeypatch):
    monkeypatch.setattr(settings, "idle_check_interval_seconds", 0)
    monkeypatch.setattr(settings, "idle_timeout_seconds", 0)

    manager = ServerManager(model=_FakeModel(), port=18099)
    manager._state = ModelState.READY
    manager._last_request_time = time.monotonic() - 3600  # largement idle

    unload_calls = []

    async def fake_unload(reason: str = "") -> None:
        unload_calls.append(reason)
        manager._state = ModelState.UNLOADED

    monkeypatch.setattr(manager, "unload", fake_unload)

    manager.pin()
    monitor = asyncio.create_task(manager._idle_monitor())
    await asyncio.sleep(0.05)
    assert unload_calls == [], "un modèle pinné ne doit jamais être déchargé pour inactivité"

    # Fin de la requête : unpin rafraîchit la fenêtre idle. On simule à nouveau
    # une longue inactivité pour vérifier que le déchargement reprend.
    manager.unpin()
    manager._last_request_time = time.monotonic() - 3600
    await asyncio.sleep(0.05)
    assert len(unload_calls) == 1

    monitor.cancel()
    try:
        await monitor
    except asyncio.CancelledError:
        pass


@pytest.mark.anyio
async def test_unpin_refreshes_idle_window():
    manager = ServerManager(model=_FakeModel(), port=18098)
    manager._state = ModelState.READY
    manager._last_request_time = time.monotonic() - 3600

    manager.pin()
    manager.unpin()

    assert manager.idle_seconds < 1.0
