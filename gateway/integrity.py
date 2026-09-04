"""
Attestation d'intégrité des artefacts GGUF — SEC-ART-001 / CLU-002.

Le SHA-256 déclaré dans ``models.yaml`` n'était vérifié qu'au démarrage :
un GGUF remplacé après coup était chargé sans nouvelle attestation. Ce module
fournit l'attestation unique utilisée :

- au démarrage (``main._validate_inference_runtime``) ;
- à chaque transition vers LOADING (``ServerManager._load_and_signal``),
  fail-closed juste avant le lancement du sous-processus llama-server ;
- dans le node agent, avant réservation de port (CLU-002).

Propriétés :

- hachage hors event loop (``asyncio.to_thread``) : ``/health``, unload et
  heartbeat du node agent restent réactifs pendant le hash d'un GGUF de
  plusieurs Go ;
- cache attesté clé sur ``(chemin résolu, empreinte déclarée)`` confronté à
  l'identité fichier ``(st_dev, st_ino, st_size, st_mtime_ns)`` : un fichier
  inchangé n'est pas re-haché, un fichier modifié — ou une empreinte déclarée
  différente (édition YAML + reload) — est toujours re-vérifié ;
- single-flight : un seul hachage en vol par clé, les appelants concurrents
  partagent le même résultat ;
- re-stat après hachage : un fichier substitué pendant le hachage est refusé.
  Fenêtre TOCTOU résiduelle documentée : réécriture avec même taille, même
  inode et même ``mtime_ns`` (hors modèle de menace actuel).

Les messages d'erreur mentionnent le chemin : ils sont destinés aux journaux
et à l'orchestrateur (client de confiance de l'agent). Côté gateway, le point
d'appel ``ServerManager`` sanitise le message avant qu'il n'atteigne un client
OpenAI (cf. ``tests/test_load_error_sanitization.py``).
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

from model_registry import IntegrityError

# Taille de bloc pour le hachage incrémental (alignée sur model_registry).
_HASH_CHUNK_SIZE = 1024 * 1024

# Borne du cache : une entrée par (fichier, empreinte déclarée). Les entrées
# deviennent obsolètes dès que l'identité fichier change ; on évacue alors les
# plus anciennes pour borner la mémoire sur des hôtes très volatils.
_CACHE_MAX_ENTRIES = 512


@dataclass(frozen=True)
class _FileIdentity:
    """Identité matérielle d'un fichier — détecte toute substitution."""

    dev: int
    ino: int
    size: int
    mtime_ns: int


def _identity(st: object) -> _FileIdentity:
    return _FileIdentity(dev=st.st_dev, ino=st.st_ino, size=st.st_size, mtime_ns=st.st_mtime_ns)


def _hash_file(path: Path) -> str:
    """Hachage SHA-256 par blocs de 1 Mo (thread worker — bloquant assumé)."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_and_restat(path: Path) -> tuple[str, _FileIdentity]:
    """
    Hache le fichier par blocs puis revérifie son identité (thread worker).

    Lève OSError si le fichier disparaît ou devient illisible, IntegrityError
    s'il mute entre les deux stats — attestation refusée dans les deux cas.
    """
    before = _identity(path.stat())
    digest = _hash_file(path)
    after = _identity(path.stat())
    if after != before:
        raise IntegrityError(
            f"Fichier GGUF muté pendant le hachage : {path} — attestation refusée."
        )
    return digest, after


# Cache attesté : {(chemin résolu, empreinte déclarée): identité attestée}.
_attested: dict[tuple[str, str], _FileIdentity] = {}
# Hachages en vol : {(chemin, empreinte): (id(loop), future)}. L'id de boucle
# évite d'attendre une future attachée à un autre event loop (tests séquentiels,
# TestClient) — on repart alors sur un hachage propre à la boucle courante.
_inflight: dict[tuple[str, str], tuple[int, asyncio.Future]] = {}
_guard = threading.Lock()


def reset_integrity_cache() -> None:
    """Vide cache et vols en cours — réservé aux tests."""
    with _guard:
        _attested.clear()
        _inflight.clear()


async def attest_gguf(model) -> str:
    """
    Vérifie fail-closed l'empreinte SHA-256 du GGUF d'un modèle.

    No-op (retourne "") si le modèle ne déclare pas d'empreinte. Sinon :

    1. stat hors event loop → identité fichier courante ;
    2. cache attesté conforme → attestation déjà valable, aucun I/O lourd ;
    3. sinon hachage hors event loop, single-flight par clé, re-stat après
       hachage, puis mise en cache uniquement si l'empreinte correspond.

    Lève IntegrityError si le fichier est absent, illisible, mute pendant le
    hachage ou ne correspond pas à l'empreinte déclarée.
    """
    declared = getattr(model, "sha256", None)
    if not declared:
        return ""
    declared = str(declared).lower()
    model_id = getattr(model, "id", "?")
    path = Path(model.path).resolve()
    key = (str(path), declared)
    loop = asyncio.get_running_loop()

    try:
        current = await asyncio.to_thread(path.stat)
    except OSError as exc:
        raise IntegrityError(
            f"[{model_id}] Fichier GGUF introuvable ou illisible pour "
            f"vérification d'intégrité : {path}"
        ) from exc
    identity = _identity(current)

    with _guard:
        cached = _attested.get(key)
    if cached == identity:
        return declared

    # Single-flight : un seul hachage par clé et par boucle.
    with _guard:
        entry = _inflight.get(key)
        if entry is not None and entry[0] == id(loop):
            fut = entry[1]
            owner = False
        else:
            fut = loop.create_future()
            _inflight[key] = (id(loop), fut)
            owner = True

    if not owner:
        # shield : l'annulation d'un appelant ne doit pas annuler le hachage
        # partagé dont dépendent les autres appelants.
        return await asyncio.shield(fut)

    def _deliver_error(error: Exception) -> None:
        """Propage l'échec aux co-attendants sans « never retrieved » au GC.

        set_exception + lecture immédiate : les appelants qui ont déjà admis
        la future lèveront l'exception normalement ; sans co-attendant, asyncio
        ne logue pas l'exception comme jamais récupérée.
        """
        if fut.done():
            return
        fut.set_exception(error)
        fut.exception()

    try:
        digest, attested = await asyncio.to_thread(_hash_and_restat, path)
    except OSError as exc:
        error = IntegrityError(
            f"[{model_id}] Fichier GGUF introuvable ou illisible pour "
            f"vérification d'intégrité : {path}"
        )
        with _guard:
            _deliver_error(error)
        raise error from exc
    except BaseException as exc:
        # Annulation du porteur : le thread de hachage continue mais son
        # résultat ne sert plus ; les co-attendants reçoivent l'annulation et
        # repartiront d'un cache vide — toujours fail-closed.
        with _guard:
            if not fut.done():
                if isinstance(exc, Exception):
                    _deliver_error(exc)
                else:
                    fut.cancel()
        raise
    else:
        if digest != declared:
            error = IntegrityError(
                f"[{model_id}] Empreinte SHA-256 non conforme pour {path} : "
                f"attendu {declared}, obtenu {digest}. Fichier GGUF "
                "potentiellement corrompu ou substitué (SEC-ART-001)."
            )
            with _guard:
                _deliver_error(error)
            raise error
        with _guard:
            _attested[key] = attested
            if len(_attested) > _CACHE_MAX_ENTRIES:
                _attested.pop(next(iter(_attested)))
            if not fut.done():
                fut.set_result(declared)
        return declared
    finally:
        with _guard:
            live = _inflight.get(key)
            if live is not None and live[1] is fut:
                _inflight.pop(key, None)
