from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from fastapi import HTTPException

import database as db
from config import settings


class SlidingWindowRateLimiter:
    """Compteur de requêtes sur une fenêtre glissante, par utilisateur.

    Paramètres :
      window_seconds  — durée de la fenêtre (ex. 60 pour RPM, 10 pour burst)
      label           — nom lisible pour les messages d'erreur et les headers
    """

    def __init__(self, window_seconds: float = 60.0, label: str = "rate") -> None:
        self._window = window_seconds
        self._label = label
        self._windows: dict[int, deque[float]] = defaultdict(deque)
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def check(self, user: dict, limit: int | None = None, limit_key: str = "rpm_limit") -> None:
        """Vérifie la limite.

        - Si `limit` est fourni, il est utilisé directement (burst global).
        - Sinon, on lit `user[limit_key]` (limite per-user en base).
        """
        effective_limit = limit if limit is not None else int(user.get(limit_key) or 0)
        if effective_limit <= 0:
            return

        user_id = user["user_id"]
        now = time.monotonic()
        async with self._locks[user_id]:
            window = self._windows[user_id]
            cutoff = now - self._window
            while window and window[0] < cutoff:
                window.popleft()

            count = len(window)
            remaining = max(0, effective_limit - count)

            if remaining == 0:
                oldest_ts = window[0]
                retry_after = max(1, int(oldest_ts + self._window - now) + 1)
                reset_epoch = int(time.time()) + retry_after
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": {
                            "message": f"Limite de débit dépassée ({self._label}).",
                            "type": "rate_limit_error",
                            "code": "429",
                        }
                    },
                    headers={
                        "Retry-After": str(retry_after),
                        "X-RateLimit-Limit": str(effective_limit),
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": str(reset_epoch),
                        "X-RateLimit-Window": str(int(self._window)),
                    },
                )
            window.append(now)


class PerUserConcurrency:
    def __init__(self) -> None:
        self._active: dict[int, int] = defaultdict(int)
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def acquire(self, user: dict) -> None:
        limit = int(user.get("concurrent_stream_limit") or 1)
        user_id = user["user_id"]
        async with self._locks[user_id]:
            if self._active[user_id] >= limit:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": {
                            "message": f"Trop de requêtes concurrentes (max {limit}).",
                            "type": "rate_limit_error",
                            "code": "429",
                        }
                    },
                    headers={"Retry-After": "10"},
                )
            self._active[user_id] += 1

    async def release(self, user: dict) -> None:
        user_id = user["user_id"]
        async with self._locks[user_id]:
            self._active[user_id] = max(0, self._active[user_id] - 1)


# Instances partagées
rpm_limiter = SlidingWindowRateLimiter(window_seconds=60.0, label="RPM")
burst_limiter = SlidingWindowRateLimiter(
    window_seconds=float(settings.burst_window_seconds),
    label=f"burst/{settings.burst_window_seconds}s",
)
concurrency = PerUserConcurrency()


async def check_daily_tokens(user: dict) -> None:
    limit = int(user.get("daily_token_limit") or 0)
    if limit <= 0:
        return
    used = await db.tokens_used_today(user["user_id"])
    if used >= limit:
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "message": f"Quota quotidien de tokens atteint ({used:,}/{limit:,}).",
                    "type": "rate_limit_error",
                    "code": "429",
                }
            },
            headers={"Retry-After": str(_seconds_until_midnight())},
        )


async def check_hourly_tokens(user: dict) -> None:
    limit = int(user.get("hourly_token_limit") or 0)
    if limit <= 0:
        return
    used = await db.tokens_used_last_minutes(user["user_id"], 60)
    if used >= limit:
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "message": f"Quota horaire de tokens atteint ({used:,}/{limit:,}).",
                    "type": "rate_limit_error",
                    "code": "429",
                }
            },
            headers={"Retry-After": "3600"},
        )


def _seconds_until_midnight() -> int:
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    midnight = (now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((midnight - now).total_seconds()))
