"""
Contrat OpenAPI versionné de la gateway.

La gateway démarre avec `openapi_url=None` : le schéma n'est pas servi en
ligne, pour ne pas publier la forme des routes admin à tout porteur de clé.
Le contrat existe quand même, et c'est lui que promet le projet — « API
compatible OpenAI ». Sans fichier de référence, une modification de route, de
champ ou de code d'erreur passait sans qu'aucun test ne s'en aperçoive.

Le schéma est donc exporté dans `docs/openapi.json`, versionné, et comparé en
CI par `tests/test_openapi_contract.py`. Un diff n'est pas un échec en soi :
c'est une demande de régénérer le fichier dans le même commit, pour que la
revue voie le changement de contrat à côté du changement de code.

Invariant à préserver : **le schéma ne doit dépendre d'aucune variable
d'environnement**. Un défaut de modèle Pydantic lu dans `settings` rendrait le
fichier différent d'une machine à l'autre et la garde inexploitable. C'est
pourquoi `gateway/schemas.py` ne référence pas `settings`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# `gateway/` → racine du dépôt. Le fichier vit dans `docs/` : c'est un
# artefact de documentation, lu par les générateurs de clients, pas un module
# importable.
SPEC_PATH = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def build_spec() -> dict[str, Any]:
    """
    Construit le schéma depuis l'application réelle.

    Import local : `main` monte les routeurs et configure le logging au niveau
    module. Le faire au chargement de ce module imposerait ce coût — et cet
    ordre d'import — à tout appelant, y compris au script d'export.
    """
    from main import app

    # `app.openapi()` mémorise son résultat dans `app.openapi_schema`. On
    # repart de zéro pour que deux appels successifs dans un même process
    # (test puis export) ne se renvoient pas un schéma construit avant une
    # modification de route.
    app.openapi_schema = None
    return app.openapi()


def serialize_spec(spec: dict[str, Any]) -> str:
    """
    Sérialisation canonique : clés triées, indentation fixe, retour final.

    Le but est qu'un `git diff` sur ce fichier montre le changement de contrat
    et rien d'autre. `sort_keys` neutralise l'ordre d'insertion, qui dépend de
    l'ordre de déclaration des routes et bougerait à chaque réorganisation de
    `main.py`.
    """
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_current_spec() -> str:
    """Schéma de l'application courante, sous sa forme sérialisée canonique."""
    return serialize_spec(build_spec())


def read_committed_spec() -> str | None:
    """Contenu de `docs/openapi.json`, ou `None` s'il n'existe pas encore."""
    if not SPEC_PATH.is_file():
        return None
    return SPEC_PATH.read_text(encoding="utf-8")


def write_spec(content: str) -> None:
    """Écrit le contrat sur disque."""
    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPEC_PATH.write_text(content, encoding="utf-8")
