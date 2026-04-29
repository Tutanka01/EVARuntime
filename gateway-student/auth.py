from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import database as db

log = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


async def get_current_user(credentials: HTTPAuthorizationCredentials | None = Security(bearer)) -> dict:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise_auth("En-tete Authorization manquant ou invalide.")

    raw_key = credentials.credentials
    if not raw_key.startswith("llmstu-"):
        raise_auth("Cle API etudiante invalide.")

    user = await db.lookup_key(raw_key)
    if user is None:
        log.warning("Student auth failed with invalid/revoked/expired key")
        raise_auth("Cle API invalide, revoquee ou expiree.")

    asyncio.create_task(db.touch_key_last_used(user["key_id"]))
    return user


def raise_auth(message: str) -> None:
    raise HTTPException(
        status_code=401,
        detail={"error": {"message": message, "type": "authentication_error", "code": "401"}},
        headers={"WWW-Authenticate": "Bearer"},
    )

