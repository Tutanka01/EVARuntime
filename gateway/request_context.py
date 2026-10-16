"""
Identifiant de requête propagé de bout en bout (OPS-004).

Un seul identifiant par requête HTTP, visible aux trois endroits où un
opérateur en a besoin pendant un incident :

  - le client le reçoit dans l'en-tête ``X-Request-Id`` de sa réponse ;
  - le journal d'accès de la gateway l'écrit sur la ligne de la requête ;
  - `usage_log.request_id` le conserve en base.

Jusqu'ici l'identifiant était tiré dans `proxy.py` et n'existait qu'en base :
un utilisateur qui signalait « ma requête a échoué » ne pouvait rien fournir
d'autre qu'une heure approximative.

Le transport se fait par `contextvars`, pas par un paramètre ajouté à chaque
signature : l'identifiant doit aussi être disponible dans le journal d'accès du
middleware et dans les routes admin, qui ne passent pas par `proxy_request`.
Un `ContextVar` est copié dans chaque tâche asyncio fille, donc le générateur
SSE voit la même valeur que la requête qui l'a créé.
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

# Nom canonique de l'en-tête. `X-Request-Id` plutôt que `X-Correlation-Id` ou
# `traceparent` : c'est la convention que suivent déjà nginx, les SDK OpenAI et
# la plupart des reverse proxies, et elle n'engage pas la gateway à implémenter
# le contexte W3C Trace, qui réclamerait un vrai tracing distribué.
REQUEST_ID_HEADER = "X-Request-Id"

# Longueur maximale retenue. Assez large pour un UUID (36), un ULID (26) ou un
# identifiant applicatif composite, assez courte pour borner la colonne SQLite
# et les lignes de journal. Un client qui envoie plus long voit son identifiant
# remplacé, pas tronqué : un identifiant coupé ressemblerait à un identifiant
# valide tout en ne correspondant plus à rien côté appelant.
REQUEST_ID_MAX_LENGTH = 64

# Jeu de caractères admis. La valeur arrive dans les journaux et en base : un
# retour chariot permettrait de forger une fausse ligne de journal, un octet de
# contrôle casserait journald, et un caractère non ASCII n'apporte rien à un
# identifiant de corrélation. On n'accepte donc que ce qu'un identifiant
# technique utilise réellement.
_REQUEST_ID_RE = re.compile(r"\A[A-Za-z0-9._:-]+\Z")

_request_id: ContextVar[str] = ContextVar("eva_request_id", default="")


def new_request_id() -> str:
    """Identifiant neuf, lorsque le client n'en fournit pas d'exploitable."""
    return str(uuid.uuid4())


def sanitize_request_id(raw: str | None) -> str | None:
    """
    Valide un `X-Request-Id` entrant.

    Retourne la valeur telle quelle si elle est exploitable, `None` sinon —
    l'appelant génère alors un identifiant neuf. Le refus est silencieux et
    non bloquant : un en-tête malformé est un défaut du client, pas une raison
    de refuser une requête d'inférence par ailleurs valide.

    La valeur n'est jamais nettoyée « en partie » (caractères retirés,
    troncature) : un identifiant modifié ne correspondrait plus à celui que
    l'appelant conserve de son côté, ce qui est pire qu'un identifiant
    franchement différent.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > REQUEST_ID_MAX_LENGTH:
        return None
    if not _REQUEST_ID_RE.match(candidate):
        return None
    return candidate


def resolve_request_id(raw: str | None) -> str:
    """En-tête entrant s'il est exploitable, sinon un identifiant neuf."""
    return sanitize_request_id(raw) or new_request_id()


def set_request_id(request_id: str) -> None:
    """Fixe l'identifiant pour le contexte courant."""
    _request_id.set(request_id)


def get_request_id() -> str:
    """
    Identifiant du contexte courant, ou chaîne vide hors contexte HTTP.

    Les appelants qui ont besoin d'une valeur certaine (journalisation d'usage)
    utilisent `current_or_new_request_id()`.
    """
    return _request_id.get()


def current_or_new_request_id() -> str:
    """
    Identifiant courant, ou un identifiant neuf s'il n'y en a pas.

    Le repli couvre les appels directs au proxy dans les tests unitaires et
    toute tâche de fond qui journaliserait un usage hors requête HTTP : mieux
    vaut un identifiant orphelin qu'une ligne d'usage sans corrélation.
    """
    return _request_id.get() or new_request_id()
