"""
Authentification par Bearer token.

Flux :
1. Client envoie : Authorization: Bearer llmgw-<token>
2. On hash SHA-256 le token
3. On cherche le hash dans api_keys (+ user actif + clé active + non expirée)
4. On retourne le dict user+key ou on lève une 401

On ne stocke jamais la clé brute — seulement son hash.
La mise à jour de last_used est faite en fire-and-forget (hors critical path),
au plus une fois par clé et par minute.
"""
from __future__ import annotations

import logging
import secrets
import time
from collections import OrderedDict

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import JSONResponse

import database as db
from background import fire_and_forget

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

    # Mise à jour last_used en arrière-plan — non bloquant, et au plus une
    # écriture par clé et par minute (voir `_should_touch_last_used`).
    if _should_touch_last_used(user["key_id"]):
        fire_and_forget(db.touch_key_last_used(user["key_id"]), name="touch_key_last_used")

    return user


# `last_used` est une information d'administration (« cette clé sert-elle
# encore ? »), pas un journal : l'écrire à CHAQUE requête coûtait une
# transaction SQLite par appel authentifié, en concurrence avec `log_usage`
# pour l'unique verrou d'écriture. Une écriture par clé et par minute suffit ;
# la précision affichée par l'admin et la CLI devient ~1 min. L'historique
# exact des appels reste dans `usage_log`.
LAST_USED_DEBOUNCE_SECONDS = 60.0
# Borne de la table en mémoire. Ordonnée par date d'écriture (la plus
# ancienne en tête) : l'éviction retire la tête, en O(1) amorti. Évincer une
# clé encore récente coûte au pire une écriture `last_used` de plus.
_LAST_USED_MAX_ENTRIES = 10_000
_last_used_written: OrderedDict[int, float] = OrderedDict()
# Horloge injectable : les tests la remplacent sans toucher à `time.monotonic`
# du processus (que la boucle asyncio utilise aussi).
_clock = time.monotonic


def _should_touch_last_used(key_id: int) -> bool:
    """
    True si `last_used` de cette clé doit être réécrit maintenant.

    État par processus : un redémarrage provoque au pire une écriture de plus
    par clé. Aucun cache d'authentification n'est introduit — `lookup_key`
    interroge toujours la base, une révocation reste effective immédiatement.
    """
    now = _clock()
    last = _last_used_written.get(key_id)
    if last is not None and now - last < LAST_USED_DEBOUNCE_SECONDS:
        return False
    _last_used_written[key_id] = now
    _last_used_written.move_to_end(key_id)
    cutoff = now - LAST_USED_DEBOUNCE_SECONDS
    while _last_used_written:
        oldest_key = next(iter(_last_used_written))
        if (
            _last_used_written[oldest_key] >= cutoff
            and len(_last_used_written) <= _LAST_USED_MAX_ENTRIES
        ):
            break
        del _last_used_written[oldest_key]
    return True


async def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    """
    Dependency pour les routes /admin.
    Vérifie que le header Authorization: Bearer <ADMIN_SECRET> est correct.
    En production, ces routes sont également filtrées par IP dans nginx.

    Fail-closed : si ADMIN_SECRET est vide, laissé à sa valeur d'exemple ou
    trop court (< SECRET_MIN_LENGTH), les routes admin sont désactivées plutôt
    que protégées par un secret prévisible.
    """
    from config import SECRET_MIN_LENGTH, settings  # import local pour éviter la circularité

    if settings.admin_secret_is_weak():
        log.critical(
            "Tentative d'accès admin alors qu'ADMIN_SECRET est absent, "
            "placeholder ou plus court que %d caractères. Routes admin "
            "désactivées (fail-closed).",
            SECRET_MIN_LENGTH,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Administration désactivée : ADMIN_SECRET non configuré ou "
                "trop faible sur le serveur."
            ),
        )

    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=403, detail="Accès refusé.")

    # Comparaison constant-time — évite les attaques par timing sur le secret
    if not secrets.compare_digest(
        credentials.credentials.encode(), settings.admin_secret.encode()
    ):
        raise HTTPException(status_code=403, detail="Secret admin incorrect.")
