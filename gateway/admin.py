"""
Endpoints d'administration — protégés par :
  1. Secret admin (Bearer <ADMIN_SECRET> dans le header Authorization)
  2. Filtrage IP nginx (réseau campus uniquement — configuré dans nginx.conf)

Ces routes ne sont PAS dans le préfixe /v1/ pour éviter toute confusion
avec l'API OpenAI.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

import database as db
from auth import require_admin
from config import settings
from model_manager import model_manager
from schemas import (
    GatewayStatus,
    KeyCreate,
    KeyCreateResponse,
    KeyResponse,
    ModelEntryCreate,
    ModelEntryUpdate,
    ModelStatusResponse,
    UsageEntry,
    UsageSummaryEntry,
    UserCreate,
    UserResponse,
    UserUpdate,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Statut système multi-modèles ──────────────────────────────────────────────

@router.get("/status", response_model=GatewayStatus)
async def get_status(_: None = Depends(require_admin)) -> dict:
    """
    État complet de la gateway : budget VRAM + état de chaque modèle.
    """
    return {"status": "ok", **model_manager.status()}


# ── Registre des modèles ──────────────────────────────────────────────────────

@router.get("/models", response_model=list[ModelStatusResponse])
async def list_models(_: None = Depends(require_admin)) -> list[dict]:
    """
    Liste tous les modèles du registre avec leur état live (chargé / déchargé).
    """
    return model_manager.status()["models"]


@router.post("/models", response_model=ModelStatusResponse, status_code=201)
async def register_model(
    body: ModelEntryCreate,
    _: None = Depends(require_admin),
) -> dict:
    """
    Enregistre un nouveau modèle dans le registre.

    Validations de sécurité :
    - path doit être absolu et pointer vers un fichier .gguf
    - path doit être sous un répertoire autorisé (si ALLOWED_MODEL_DIRS est configuré)
    - Le fichier .gguf doit exister sur le serveur
    - vram_gb doit être raisonnable (≤ budget VRAM net)
    - Le modèle n'est PAS chargé automatiquement après enregistrement
    """
    # Vérification du fichier (existence sur disque)
    model_path = Path(body.path)
    if not model_path.exists():
        raise HTTPException(
            status_code=422,
            detail=f"Fichier introuvable sur le serveur : {body.path}",
        )

    # Vérification budget VRAM (avertissement si vram_gb dépasse le budget net)
    budget = settings.effective_vram_budget_gb()
    if body.vram_gb > budget:
        raise HTTPException(
            status_code=422,
            detail=(
                f"vram_gb ({body.vram_gb:.1f} GB) dépasse le budget VRAM net disponible "
                f"({budget:.1f} GB). Ce modèle ne pourra jamais être chargé seul."
            ),
        )

    try:
        entry_dict = {
            "id": body.id,
            "path": body.path,
            "description": body.description,
            "vram_gb": body.vram_gb,
            "enabled": body.enabled,
            "capabilities": body.capabilities,
            "llama_params": body.llama_params.model_dump(),
        }
        model = model_manager.registry.add(entry_dict)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    log.info("Admin : nouveau modèle enregistré '%s'", body.id)
    return {
        "id": model.id,
        "description": model.description,
        "enabled": model.enabled,
        "vram_gb": model.vram_gb,
        "capabilities": model.capabilities,
        "state": "unloaded",
        "path": str(model.path),
        "pid": None,
        "port": None,
        "uptime_seconds": None,
        "idle_seconds": None,
        "llama_params": None,
    }


@router.patch("/models/{model_id}", response_model=ModelStatusResponse)
async def update_model(
    model_id: str,
    body: ModelEntryUpdate,
    _: None = Depends(require_admin),
) -> dict:
    """
    Met à jour les métadonnées d'un modèle (enabled, vram_gb, description).
    Si le modèle est actuellement chargé et qu'on le désactive (enabled=false),
    il est déchargé immédiatement.
    """
    if not model_manager.registry.get(model_id):
        raise HTTPException(status_code=404, detail=f"Modèle '{model_id}' introuvable dans le registre.")

    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=422, detail="Aucun champ à mettre à jour.")

    try:
        model = model_manager.registry.update(model_id, **updates)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Si désactivé, décharger immédiatement s'il est en mémoire
    if updates.get("enabled") is False:
        await model_manager.unload_model(model_id)
        log.info("Admin : modèle '%s' désactivé et déchargé", model_id)

    # Récupérer l'état live
    status_list = model_manager.status()["models"]
    entry = next((m for m in status_list if m["id"] == model_id), None)
    return entry or {"id": model_id, "state": "unloaded", **model.__dict__}


@router.delete("/models/{model_id}", status_code=200)
async def delete_model(
    model_id: str,
    _: None = Depends(require_admin),
) -> dict:
    """
    Supprime un modèle du registre.
    Le modèle doit être déchargé au préalable (ou sera déchargé automatiquement).
    """
    if not model_manager.registry.get(model_id):
        raise HTTPException(status_code=404, detail=f"Modèle '{model_id}' introuvable dans le registre.")

    # Décharger d'abord si chargé
    await model_manager.unload_model(model_id)

    try:
        model_manager.registry.remove(model_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    log.info("Admin : modèle '%s' supprimé du registre", model_id)
    return {"message": f"Modèle '{model_id}' supprimé du registre."}


@router.post("/models/{model_id}/load")
async def load_model(
    model_id: str,
    _: None = Depends(require_admin),
) -> dict:
    """
    Pré-charge un modèle en mémoire (warm-up).
    Utile pour éviter la latence de cold-start sur la première requête.
    Évinçe un modèle LRU si le budget VRAM est dépassé.
    """
    try:
        await model_manager.ensure_model_loaded(model_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (RuntimeError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    log.info("Admin : modèle '%s' pré-chargé", model_id)
    return {"message": f"Modèle '{model_id}' chargé et prêt."}


@router.post("/models/{model_id}/unload")
async def unload_model(
    model_id: str,
    _: None = Depends(require_admin),
) -> dict:
    """
    Décharge un modèle spécifique et libère sa VRAM.
    Sans effet si le modèle n'est pas chargé.
    """
    if not model_manager.registry.get(model_id):
        raise HTTPException(status_code=404, detail=f"Modèle '{model_id}' introuvable dans le registre.")

    await model_manager.unload_model(model_id)
    log.info("Admin : modèle '%s' déchargé", model_id)
    return {"message": f"Modèle '{model_id}' déchargé. VRAM libérée."}


@router.post("/unload")
async def unload_all(_: None = Depends(require_admin)) -> dict:
    """Décharge tous les modèles chargés et libère toute la VRAM."""
    await model_manager.shutdown()
    return {"message": "Tous les modèles déchargés. GPU entièrement libéré."}


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
                detail="Un utilisateur avec ce nom ou cet email existe déjà."
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
