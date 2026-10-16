"""
Garde du contrat OpenAPI versionné (`docs/openapi.json`).

La gateway promet une API compatible OpenAI mais ne sert pas son schéma
(`openapi_url=None`). Sans fichier de référence, un changement de route, de
champ obligatoire ou de code d'erreur passait sans qu'aucun test ne le voie.

Ce module échoue dès que l'application et le fichier versionné divergent. Le
message dit quoi lancer : la régénération fait partie du commit qui modifie la
route, pour que la revue voie le changement de contrat.

Chaque assertion d'absence porte ici un contrôle positif (cf. AGENTS.md) :
sans lui, un schéma vide ou une lecture ratée rendrait les tests silencieux.
"""
from __future__ import annotations

import json

from openapi_contract import SPEC_PATH, read_committed_spec, render_current_spec

_REGENERATE = "python scripts/export_openapi.py"


def test_le_contrat_versionne_existe():
    assert read_committed_spec() is not None, (
        f"{SPEC_PATH} est absent. Lancez : {_REGENERATE}"
    )


def test_le_contrat_versionne_correspond_a_l_application():
    committed = read_committed_spec()
    assert committed is not None, f"{SPEC_PATH} est absent. Lancez : {_REGENERATE}"

    current = render_current_spec()
    if committed == current:
        return

    anciennes = set(json.loads(committed).get("paths", {}))
    nouvelles = set(json.loads(current).get("paths", {}))
    ajoutees = sorted(nouvelles - anciennes)
    retirees = sorted(anciennes - nouvelles)

    raise AssertionError(
        "Le contrat OpenAPI versionné ne correspond plus aux routes de la "
        f"gateway.\nRoutes ajoutées : {ajoutees or 'aucune'}\n"
        f"Routes retirées : {retirees or 'aucune'}\n"
        "Si le changement est voulu, régénérez le fichier dans le même "
        f"commit :\n  {_REGENERATE}"
    )


def test_le_contrat_decrit_bien_les_routes_attendues():
    """
    Contrôle positif du test de correspondance : il doit comparer un schéma
    réellement peuplé. Un `app.openapi()` qui ne verrait plus les routes
    incluses — le piège `_IncludedRouter` de FastAPI ≥ 0.141 décrit dans
    AGENTS.md — rendrait la comparaison verte et vide.
    """
    spec = json.loads(render_current_spec())
    paths = spec.get("paths", {})

    assert spec.get("openapi", "").startswith("3."), spec.get("openapi")
    assert "/v1/chat/completions" in paths
    assert "/v1/models" in paths
    # Route montée par `include_router` : la preuve que le schéma traverse
    # bien les routeurs inclus.
    assert "/admin/status" in paths
    assert len(paths) > 20, sorted(paths)


def test_le_contrat_ne_contient_aucun_secret_ni_chemin_hote():
    """
    Le fichier est versionné dans un dépôt public : il ne doit transporter ni
    valeur de secret ni chemin absolu de l'hôte qui l'a généré.
    """
    rendu = render_current_spec()

    # Contrôle positif : la recherche voit bien le contenu du schéma.
    assert "chat/completions" in rendu

    from config import settings

    interdits = [
        settings.internal_api_key,
        settings.admin_secret,
        settings.agent_secret,
        "/Users/",
        "/home/",
        "/var/log/",
    ]
    for interdit in interdits:
        assert interdit not in rendu, f"valeur sensible exposée : {interdit!r}"


def test_la_serialisation_est_stable():
    """
    Deux rendus successifs doivent être identiques, sinon la garde produirait
    des diffs fantômes à chaque exécution (ordre des clés, cache de schéma).
    """
    assert render_current_spec() == render_current_spec()
