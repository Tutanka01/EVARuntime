"""
Authentification par Bearer token.

Flux :
1. Client envoie : Authorization: Bearer llmgw-<token>
2. On hash SHA-256 le token
3. On cherche le hash dans api_keys (+ user actif + clé active + non expirée)
4. On retourne le dict user+key ou on lève une 401

On ne stocke jamais la clé brute — seulement son hash.
La mise à jour de last_used est faite en fire-and-forget (hors critical path).
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import JSONResponse

import database as db

log = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


def openai_error(status_code: int, message: str, error_type: str) -> JSONResponse:
    """
    Retourne une erreur au format exact OpenAI pour compatibilité
    avec les clients standards (openai-python, LiteLLM, etc.).
    """
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "code": str(status_code),
            }
        },
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> dict:
    """
    Dependency FastAPI : injecte le dict user+key dans les routes protégées.
    Lève HTTPException 401 si invalide.
    """
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "En-tête Authorization manquant ou invalide. "
                               "Format attendu : Bearer <votre_clé_api>",
                    "type": "authentication_error",
                    "code": "401",
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_key = credentials.credentials
    user = await db.lookup_key(raw_key)

    if user is None:
        log.warning("Tentative d'authentification avec une clé invalide ou révoquée.")
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "Clé API invalide, révoquée ou expirée.",
                    "type": "authentication_error",
                    "code": "401",
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Mise à jour last_used en arrière-plan — non bloquant
    asyncio.create_task(db.touch_key_last_used(user["key_id"]))

    return user


async def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    """
    Dependency pour les routes /admin.
    Vérifie que le header Authorization: Bearer <ADMIN_SECRET> est correct.
    En production, ces routes sont également filtrées par IP dans nginx.
    """
    from config import settings  # import local pour éviter la circularité

    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=403, detail="Accès refusé.")

    if credentials.credentials != settings.admin_secret:
        raise HTTPException(status_code=403, detail="Secret admin incorrect.")
