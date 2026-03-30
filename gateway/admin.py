"""
Endpoints d'administration — protégés par :
  1. Secret admin (Bearer <ADMIN_SECRET> dans le header Authorization)
  2. Filtrage IP nginx (réseau campus uniquement — configuré dans nginx.conf)

Ces routes ne sont PAS dans le préfixe /v1/ pour éviter toute confusion
avec l'API OpenAI.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

import database as db
from auth import require_admin
from schemas import (
    GatewayStatus,
    KeyCreate,
    KeyCreateResponse,
    KeyResponse,
    UsageEntry,
    UsageSummaryEntry,
    UserCreate,
    UserResponse,
    UserUpdate,
)
from server_manager import server_manager

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Statut système ────────────────────────────────────────────────────────────

@router.get("/status", response_model=GatewayStatus)
async def get_status(_: None = Depends(require_admin)) -> dict:
    """État du serveur : modèle chargé, PID, uptime, idle, params GPU."""
    return {"status": "ok", **server_manager.status()}


@router.post("/unload")
async def force_unload(_: None = Depends(require_admin)) -> dict:
    """Force le déchargement du modèle et la libération de la VRAM."""
    await server_manager.unload(reason="admin request")
    return {"message": "Modèle déchargé. GPU libéré."}


# ── Gestion utilisateurs ──────────────────────────────────────────────────────

@router.post("/users", response_model=UserResponse, status_code=201)
async def create_user(
    body: UserCreate,
    _: None = Depends(require_admin),
) -> dict:
    """Crée un nouvel utilisateur."""
    try:
        user = await db.create_user(
            username=body.username,
            email=body.email,
            rpm_limit=body.rpm_limit,
            monthly_token_limit=body.monthly_token_limit,
            notes=body.notes,
        )
    except Exception as exc:
        if "UNIQUE constraint" in str(exc):
            raise HTTPException(
                status_code=409,
                detail=f"Un utilisateur avec ce nom ou cet email existe déjà."
            )
        raise
    return user


@router.get("/users", response_model=list[UserResponse])
async def list_users(_: None = Depends(require_admin)) -> list[dict]:
    """Liste tous les utilisateurs."""
    return await db.list_users()


@router.get("/users/{username}", response_model=UserResponse)
async def get_user(
    username: str,
    _: None = Depends(require_admin),
) -> dict:
    user = await db.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"Utilisateur '{username}' introuvable.")
    return user


@router.patch("/users/{username}", response_model=UserResponse)
async def update_user(
    username: str,
    body: UserUpdate,
    _: None = Depends(require_admin),
) -> dict:
    """Modifie un utilisateur (activation/désactivation, RPM, quota, etc.)."""
    user = await db.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"Utilisateur '{username}' introuvable.")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="Aucun champ à mettre à jour.")

    updated = await db.update_user(user["id"], **updates)
    return updated


# ── Gestion des clés API ──────────────────────────────────────────────────────

@router.post("/users/{username}/keys", response_model=KeyCreateResponse, status_code=201)
async def create_key(
    username: str,
    body: KeyCreate,
    _: None = Depends(require_admin),
) -> dict:
    """
    Génère une nouvelle clé API pour l'utilisateur.
    La clé brute est retournée UNE SEULE FOIS — impossible de la récupérer ensuite.
    """
    user = await db.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"Utilisateur '{username}' introuvable.")

    raw_key, key_row = await db.create_api_key(
        user_id=user["id"],
        name=body.name,
        expires_at=body.expires_at,
    )

    log.info(
        "Nouvelle clé API créée pour '%s' (préfixe: %s)",
        username, key_row["key_prefix"],
    )

    return {
        "api_key": raw_key,
        "key_prefix": key_row["key_prefix"],
        "name": key_row["name"],
        "created_at": key_row["created_at"],
        "expires_at": key_row["expires_at"],
    }


@router.get("/users/{username}/keys", response_model=list[KeyResponse])
async def list_keys(
    username: str,
    _: None = Depends(require_admin),
) -> list[dict]:
    """Liste les clés d'un utilisateur (sans la valeur brute)."""
    user = await db.get_user_by_username(username)
    if not user:
        raise HTTPException(status_code=404, detail=f"Utilisateur '{username}' introuvable.")

    return await db.list_keys_for_user(user["id"])


@router.delete("/keys/{key_prefix}", status_code=200)
async def revoke_key(
    key_prefix: str,
    _: None = Depends(require_admin),
) -> dict:
    """
    Révoque une clé API par son préfixe (ex: 'llmgw-abc12345').
    La révocation est immédiate — la prochaine requête avec cette clé recevra un 401.
    """
    revoked = await db.revoke_key(key_prefix)
    if not revoked:
        raise HTTPException(
            status_code=404,
            detail=f"Aucune clé active avec le préfixe '{key_prefix}'."
        )

    log.info("Clé révoquée : préfixe '%s'", key_prefix)
    return {"message": f"Clé '{key_prefix}' révoquée avec succès."}


# ── Rapports d'usage ──────────────────────────────────────────────────────────

@router.get("/usage", response_model=list[UsageEntry])
async def get_usage(
    username: Optional[str] = Query(None, description="Filtrer par utilisateur"),
    from_date: Optional[str] = Query(None, description="Date de début ISO 8601 (ex: 2025-01-01)"),
    to_date: Optional[str] = Query(None, description="Date de fin ISO 8601 (ex: 2025-01-31)"),
    limit: int = Query(1000, ge=1, le=10000),
    _: None = Depends(require_admin),
) -> list[dict]:
    """Journal d'usage détaillé (une ligne par requête)."""
    user_id: int | None = None
    if username:
        user = await db.get_user_by_username(username)
        if not user:
            raise HTTPException(status_code=404, detail=f"Utilisateur '{username}' introuvable.")
        user_id = user["id"]

    return await db.get_usage_report(
        user_id=user_id,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
    )


@router.get("/usage/summary", response_model=list[UsageSummaryEntry])
async def get_usage_summary(
    from_date: Optional[str] = Query(None, description="Date de début ISO 8601"),
    to_date: Optional[str] = Query(None, description="Date de fin ISO 8601"),
    _: None = Depends(require_admin),
) -> list[dict]:
    """Résumé agrégé par utilisateur — idéal pour le reporting mensuel."""
    return await db.get_usage_summary(from_date=from_date, to_date=to_date)
