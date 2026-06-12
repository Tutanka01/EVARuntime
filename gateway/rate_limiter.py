"""
Rate limiting par fenêtre glissante en mémoire.

Algorithme : sliding window log
- On conserve une deque de timestamps de requêtes par user_id
- À chaque requête, on purge les entrées hors de la fenêtre d'1 minute
- Si le nombre restant >= rpm_limit, on refuse

Pas de Redis nécessaire à l'échelle universitaire (centaines d'users).
L'état est en mémoire : redémarrer la gateway remet les compteurs à zéro.
C'est acceptable — les limites sont par minute, pas par heure.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque

from fastapi import Depends, HTTPException

import database as db
from auth import get_current_user


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        # user_id → deque de timestamps (float, monotonic)
        self._windows: dict[int, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def is_allowed(self, user_id: int, rpm_limit: int) -> bool:
        """
        Retourne True si la requête est autorisée.
        Enregistre le timestamp si autorisée.
        """
        now = time.monotonic()
        window_start = now - 60.0

        async with self._lock:
            if user_id not in self._windows:
                self._windows[user_id] = deque()

            window = self._windows[user_id]

            # Purger les entrées expirées (oldest-first dans deque)
            while window and window[0] < window_start:
                window.popleft()

            if len(window) >= rpm_limit:
                return False

            window.append(now)
            return True

    def current_count(self, user_id: int) -> int:
        """Nombre de requêtes dans la fenêtre courante (lecture non-lockée, approximatif)."""
        now = time.monotonic()
        window_start = now - 60.0
        window = self._windows.get(user_id, deque())
        return sum(1 for t in window if t >= window_start)

    async def cleanup_stale(self) -> None:
        """Purge les users inactifs depuis plus de 5 minutes (évite les fuites mémoire)."""
        cutoff = time.monotonic() - 300.0
        async with self._lock:
            stale = [
                uid for uid, window in self._windows.items()
                if not window or window[-1] < cutoff
            ]
            for uid in stale:
                del self._windows[uid]


# Instance unique pour toute l'application
_limiter = SlidingWindowRateLimiter()


async def check_rate_limit(
    user: dict = Depends(get_current_user),
) -> dict:
    """
    Dependency FastAPI combinant auth + rate limiting + quota mensuel.
    Injecter cette dependency dans toutes les routes /v1/*.
    """
    rpm_limit = user.get("rpm_limit", 20)

    if rpm_limit > 0 and not await _limiter.is_allowed(user["user_id"], rpm_limit):
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "message": f"Limite de débit dépassée. Maximum {rpm_limit} requêtes/minute.",
                    "type": "rate_limit_error",
                    "code": "429",
                }
            },
            headers={
                "Retry-After": "60",
                "X-RateLimit-Limit": str(rpm_limit),
                "X-RateLimit-Reset": "60",
            },
        )

    # Quota mensuel de tokens — fenêtre glissante de 30 jours, cohérente avec
    # le dashboard (tokens_30d). 0 = illimité.
    monthly_limit = int(user.get("monthly_token_limit") or 0)
    if monthly_limit > 0:
        used = await db.tokens_used_last_30_days(user["user_id"])
        if used >= monthly_limit:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": {
                        "message": (
                            f"Quota mensuel de tokens atteint "
                            f"({used:,}/{monthly_limit:,} sur 30 jours glissants)."
                        ),
                        "type": "rate_limit_error",
                        "code": "429",
                    }
                },
                headers={"Retry-After": "86400"},
            )

    return user
