"""
Environnement minimal des processus llama-server.

Module sans dépendance : le pool de production (`server_manager`) et la
calibration du bootstrap (`bootstrap.production`) lancent tous deux un binaire
qui parse des GGUF, sans partager la configuration de la gateway.

LD_LIBRARY_PATH est conservé : une compilation llama.cpp liée à des bibliothèques
hors /usr/lib échouerait au chargement sans lui. TMPDIR et HOME aussi, le binaire
y écrivant ses fichiers temporaires et son cache.
"""
from __future__ import annotations

import os

CHILD_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "TZ",
    "LD_LIBRARY_PATH",
)


def inherited_child_env(*extra: str) -> dict[str, str]:
    """Variables de l'environnement courant autorisées pour un llama-server."""
    return {
        key: os.environ[key]
        for key in (*CHILD_ENV_ALLOWLIST, *extra)
        if key in os.environ
    }
