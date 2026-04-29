"""Tests du rate limiter : RPM, burst, concurrence, quotas tokens."""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import HTTPException

from rate_limiter import SlidingWindowRateLimiter, PerUserConcurrency


USER_A = {"user_id": 1, "rpm_limit": 5, "concurrent_stream_limit": 2}
USER_B = {"user_id": 2, "rpm_limit": 5, "concurrent_stream_limit": 1}


# ---------------------------------------------------------------------------
# SlidingWindowRateLimiter — RPM
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_rpm_allows_within_limit() -> None:
    limiter = SlidingWindowRateLimiter(window_seconds=60.0)
    for _ in range(5):
        await limiter.check(USER_A, limit_key="rpm_limit")


@pytest.mark.anyio
async def test_rpm_blocks_on_limit_exceeded() -> None:
    limiter = SlidingWindowRateLimiter(window_seconds=60.0)
    for _ in range(5):
        await limiter.check(USER_A, limit_key="rpm_limit")
    with pytest.raises(HTTPException) as exc:
        await limiter.check(USER_A, limit_key="rpm_limit")
    assert exc.value.status_code == 429


@pytest.mark.anyio
async def test_rpm_429_has_retry_after_header() -> None:
    limiter = SlidingWindowRateLimiter(window_seconds=60.0)
    for _ in range(5):
        await limiter.check(USER_A, limit_key="rpm_limit")
    with pytest.raises(HTTPException) as exc:
        await limiter.check(USER_A, limit_key="rpm_limit")
    assert "Retry-After" in exc.value.headers
    assert "X-RateLimit-Limit" in exc.value.headers
    assert exc.value.headers["X-RateLimit-Limit"] == "5"
    assert exc.value.headers["X-RateLimit-Remaining"] == "0"


@pytest.mark.anyio
async def test_rpm_users_are_independent() -> None:
    """Les limites de user_A n'affectent pas user_B."""
    limiter = SlidingWindowRateLimiter(window_seconds=60.0)
    for _ in range(5):
        await limiter.check(USER_A, limit_key="rpm_limit")
    # user_B peut encore faire des requêtes
    await limiter.check(USER_B, limit_key="rpm_limit")


@pytest.mark.anyio
async def test_rpm_zero_limit_is_disabled() -> None:
    """Un limit=0 désactive le rate limiting."""
    limiter = SlidingWindowRateLimiter(window_seconds=60.0)
    user = {"user_id": 99, "rpm_limit": 0}
    for _ in range(100):
        await limiter.check(user, limit_key="rpm_limit")


@pytest.mark.anyio
async def test_rpm_window_expiry() -> None:
    """Les entrées hors de la fenêtre sont évincées."""
    limiter = SlidingWindowRateLimiter(window_seconds=0.1)  # fenêtre très courte
    user = {"user_id": 42, "rpm_limit": 3}
    for _ in range(3):
        await limiter.check(user, limit_key="rpm_limit")
    await asyncio.sleep(0.15)  # attendre l'expiration de la fenêtre
    # Doit être à nouveau autorisé
    await limiter.check(user, limit_key="rpm_limit")


# ---------------------------------------------------------------------------
# Burst limiter (fenêtre courte, limit fixe)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_burst_blocks_rapid_fire() -> None:
    limiter = SlidingWindowRateLimiter(window_seconds=10.0, label="burst/10s")
    user = {"user_id": 1}
    for _ in range(3):
        await limiter.check(user, limit=3)
    with pytest.raises(HTTPException) as exc:
        await limiter.check(user, limit=3)
    assert exc.value.status_code == 429


@pytest.mark.anyio
async def test_burst_independent_of_rpm() -> None:
    """Burst et RPM sont des instances séparées — ne se contaminent pas."""
    rpm = SlidingWindowRateLimiter(window_seconds=60.0, label="RPM")
    burst = SlidingWindowRateLimiter(window_seconds=10.0, label="burst")
    user = {"user_id": 7, "rpm_limit": 10}
    # 3 requêtes rapides → burst déclenche, RPM pas encore
    for _ in range(3):
        await burst.check(user, limit=3)
        await rpm.check(user, limit_key="rpm_limit")
    with pytest.raises(HTTPException):
        await burst.check(user, limit=3)
    # RPM n'est pas encore déclenché (3 < 10)
    await rpm.check(user, limit_key="rpm_limit")


# ---------------------------------------------------------------------------
# PerUserConcurrency
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_concurrency_allows_up_to_limit() -> None:
    conc = PerUserConcurrency()
    await conc.acquire(USER_A)  # limite = 2
    await conc.acquire(USER_A)


@pytest.mark.anyio
async def test_concurrency_blocks_over_limit() -> None:
    conc = PerUserConcurrency()
    await conc.acquire(USER_A)
    await conc.acquire(USER_A)
    with pytest.raises(HTTPException) as exc:
        await conc.acquire(USER_A)
    assert exc.value.status_code == 429


@pytest.mark.anyio
async def test_concurrency_release_allows_new_acquire() -> None:
    conc = PerUserConcurrency()
    await conc.acquire(USER_B)  # limite = 1
    with pytest.raises(HTTPException):
        await conc.acquire(USER_B)
    await conc.release(USER_B)
    await conc.acquire(USER_B)  # doit marcher après release


@pytest.mark.anyio
async def test_concurrency_users_independent() -> None:
    conc = PerUserConcurrency()
    await conc.acquire(USER_B)  # user_B limite=1 atteinte
    await conc.acquire(USER_A)  # user_A limite=2 non atteinte


@pytest.mark.anyio
async def test_concurrency_release_never_goes_negative() -> None:
    conc = PerUserConcurrency()
    user = {"user_id": 55, "concurrent_stream_limit": 2}
    await conc.release(user)  # release sans acquire préalable = no-op
    await conc.acquire(user)  # doit encore fonctionner
