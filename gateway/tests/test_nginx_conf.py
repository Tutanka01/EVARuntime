"""
Tests de l'artefact nginx réellement livré (`gateway/deploy/nginx.conf`).

nginx n'étant pas installé sur les runners, on ne peut pas s'appuyer sur
`nginx -t` : ces tests parsent le fichier livré et vérifient les invariants qui,
sur un hôte réel, produiraient un warning au reload (OPS-009) ou un 504 côté
client (COR-009).

Règle AGENTS.md appliquée partout ici : tout test qui assère une ABSENCE porte un
contrôle positif prouvant qu'il voit encore quelque chose — sinon il devient
inerte le jour où le parsing casse, sans jamais échouer.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_NGINX_CONF = REPO_ROOT / "gateway" / "deploy" / "nginx.conf"
SHIPPED_NGINX_CONF_MACOS = REPO_ROOT / "gateway" / "deploy-macos" / "nginx.conf.macOS"
SHIPPED_MODELS_YAML = REPO_ROOT / "gateway" / "models.yaml"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "deployment.md"
API_DOC = REPO_ROOT / "docs" / "api.md"

# Hôte public utilisé par tous les exemples de `docs/api.md`.
PUBLIC_HOST = "https://llm.eva.univ-pau.fr"


# ── Socle nginx supporté ──────────────────────────────────────────────────────
# `docs/deployment.md` §1 : nginx 1.18+ (Ubuntu 22.04 LTS → 1.18, Ubuntu 24.04
# LTS → 1.24). La conf livrée doit donc démarrer sur 1.18 ET ne produire aucun
# warning de dépréciation sur les versions récentes (Debian 13 → 1.26+).

# Paramètres de `listen` dépréciés ou retirés, avec la version concernée.
DEPRECATED_LISTEN_PARAMS = {
    "http2": "déprécié depuis nginx 1.25.1 — utiliser la directive « http2 on; »",
    "spdy": "retiré depuis nginx 1.9.5",
}

# Directives interdites dans l'artefact livré, avec la raison.
FORBIDDEN_DIRECTIVES = {
    # Dépréciées / retirées : warning ou erreur sur les nginx récents.
    "ssl": "déprécié depuis nginx 1.15.0, retiré en 1.25.1 — utiliser « listen … ssl »",
    "http2_push": "déprécié depuis nginx 1.25.1",
    "http2_push_preload": "déprécié depuis nginx 1.25.1",
    "http2_max_field_size": "déprécié depuis nginx 1.19.7",
    "http2_max_header_size": "déprécié depuis nginx 1.19.7",
    "http2_max_requests": "déprécié depuis nginx 1.19.7",
    "http2_recv_timeout": "déprécié depuis nginx 1.19.7",
    # Trop RÉCENTE : « unknown directive » sur tout le socle supporté (< 1.25.1),
    # donc refus de démarrer sur Ubuntu 22.04 et 24.04. Elle est livrée
    # commentée, à décommenter par l'opérateur (docs/deployment.md §8).
    "http2": "n'existe qu'à partir de nginx 1.25.1 — casse le socle supporté "
             "(Ubuntu 22.04 → 1.18, Ubuntu 24.04 → 1.24)",
}


def parse_statements(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """
    Statements nginx `(directive, arguments)`, commentaires retirés.

    Volontairement plus grossier que `doctor.parse_nginx_servers` : ici on veut
    voir CHAQUE occurrence, y compris les deux `listen` d'un même bloc, que
    l'aplatissement « dernière gagnante » de doctor masquerait.
    """
    without_comments = re.sub(r"#[^\n]*", "", text)
    statements: list[tuple[str, tuple[str, ...]]] = []
    for chunk in re.split(r"[;{}]", without_comments):
        words = chunk.split()
        if words:
            statements.append((words[0], tuple(words[1:])))
    return statements


def find_deprecated(text: str) -> list[str]:
    """Liste lisible des directives dépréciées/incompatibles trouvées."""
    problems: list[str] = []
    for directive, args in parse_statements(text):
        if directive in FORBIDDEN_DIRECTIVES:
            problems.append(f"{directive} — {FORBIDDEN_DIRECTIVES[directive]}")
        if directive == "listen":
            for arg in args:
                if arg in DEPRECATED_LISTEN_PARAMS:
                    problems.append(
                        f"listen … {arg} — {DEPRECATED_LISTEN_PARAMS[arg]}"
                    )
    return problems


@pytest.fixture(scope="module")
def conf_text() -> str:
    return SHIPPED_NGINX_CONF.read_text(encoding="utf-8")


# ── OPS-009 : aucune directive dépréciée ──────────────────────────────────────

def test_le_parsing_voit_reellement_la_configuration(conf_text):
    """
    Contrôle positif des tests ci-dessous.

    Sans lui, un `parse_statements` cassé rendrait « aucune directive dépréciée »
    trivialement vrai et le garde-fou deviendrait inerte sans jamais échouer.
    """
    statements = parse_statements(conf_text)
    assert len(statements) > 50, "parsing muet : la conf livrée n'est pas vide"

    directives = {name for name, _ in statements}
    assert {"listen", "server_name", "proxy_pass", "ssl_certificate"} <= directives

    listens = [args for name, args in statements if name == "listen"]
    # 443, [::]:443, 80, [::]:80 — les deux `listen` du bloc HTTPS doivent être
    # vus SÉPARÉMENT, c'est tout l'intérêt de ce parseur-ci.
    assert len(listens) == 4, listens


def test_la_conf_livree_n_emploie_aucune_directive_depreciee(conf_text):
    """OPS-009 : `nginx -t` ne doit produire aucun warning de dépréciation."""
    assert find_deprecated(conf_text) == []


def test_le_detecteur_de_depreciation_voit_le_defaut_dorigine(conf_text):
    """
    Contrôle positif du détecteur lui-même : on réintroduit le défaut OPS-009
    (`listen … ssl http2`) et il doit être signalé. Un détecteur qui ne détecte
    rien passerait le test précédent en silence.
    """
    regressed = conf_text.replace("listen 443 ssl;", "listen 443 ssl http2;")
    assert regressed != conf_text
    problems = find_deprecated(regressed)
    assert any("http2" in p for p in problems), problems

    # Et l'inverse : la directive moderne casse le socle < 1.25.1.
    too_recent = conf_text.replace("# http2 on;", "http2 on;")
    assert too_recent != conf_text
    assert any(p.startswith("http2 —") for p in find_deprecated(too_recent))


def test_http2_reste_disponible_en_une_ligne_a_decommenter(conf_text):
    """
    Le compromis OPS-009 sacrifie HTTP/2 par défaut : il doit rester activable
    sans réécrire la conf, et le mode d'emploi doit être documenté.
    """
    assert re.search(r"^\s*#\s*http2 on;\s*$", conf_text, re.MULTILINE)
    doc = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    assert "http2 on;" in doc
    assert "1.25.1" in doc


def test_la_conf_macos_n_emploie_aucune_directive_depreciee():
    """
    OPS-009 vaut aussi pour l'arbre macOS : `nginx.conf.macOS` est copié tel quel
    par `deploy-macos/update.sh` et doit démarrer sans warning sur les nginx
    récents. Détecteur partagé avec le conf Linux — son contrôle positif
    (`test_le_detecteur_de_depreciation_voit_le_defaut_dorigine`) prouve déjà
    qu'il voit. Les AUTRES invariants communs des deux confs (timeouts ≥ registre,
    body size 10m, rédaction SEC-016, statuts 429, Connection "") ne sont pas
    redécrits ici : ils sont gardés une seule fois par
    `tests/test_deploy_trees_parity.py` (PAIRES_SEMANTIQUES).
    """
    assert find_deprecated(SHIPPED_NGINX_CONF_MACOS.read_text(encoding="utf-8")) == []


# ── COR-009 : timeouts dérivés du registre ────────────────────────────────────

def _https_server(text):
    """Le bloc `server` qui porte le TLS — celui qui proxifie vers la gateway."""
    import doctor  # noqa: PLC0415 - import tardif : conftest prépare l'environnement

    servers = doctor.parse_nginx_servers(text)
    return next(s for s in servers if s.directives.get("ssl_certificate"))


def _required_timeout_seconds():
    """
    Attente maximale qu'un client peut subir sur un chargement, DÉRIVÉE du
    registre livré et non d'une constante figée : le jour où quelqu'un ajoute un
    modèle plus lent, cette valeur monte et le test échoue tant que nginx n'a pas
    suivi.

    Différence assumée avec `doctor`, qui ne regarde que les modèles ACTIVÉS :
    ici on prend `list_all()`. Un modèle désactivé se réactive d'un
    `enabled: true` (`minimax-m2.7`, 600 s), et la conf LIVRÉE doit couvrir ce
    cas sans qu'on ait à retoucher nginx dans la foulée.
    """
    import doctor  # noqa: PLC0415
    import model_registry  # noqa: PLC0415
    from config import settings  # noqa: PLC0415

    registry = model_registry.ModelRegistry(SHIPPED_MODELS_YAML)
    models = registry.list_all()
    return doctor.required_load_timeout_seconds(settings, models), models


def _doctor_enabled_models():
    """Scénario explicite : l'opérateur a activé les entrées livrées."""
    import model_registry  # noqa: PLC0415

    models = model_registry.ModelRegistry(SHIPPED_MODELS_YAML).list_all()
    assert models, "contrôle positif : le registre livré serait vide"
    return [replace(model, enabled=True) for model in models]


def test_le_registre_livre_est_lisible_et_dimensionnant():
    """Contrôle positif du test suivant : sans registre lu, l'exigence est vide."""
    required, models = _required_timeout_seconds()
    assert len(models) >= 3, "registre livré vide ou illisible"
    # Au moins un modèle surcharge le timeout par défaut : c'est ce qui rend
    # l'exigence supérieure aux 190 s de MODEL_LOAD_TIMEOUT_SECONDS + 10.
    assert any(getattr(m, "load_timeout_seconds", None) for m in models)
    assert required >= 610, required


# Remarque : la copie macOS (`deploy-macos/nginx.conf.macOS`) est soumise à la
# MÊME exigence de timeouts — mais elle n'est pas redécrite ici, pour n'avoir
# qu'un seul garde : `tests/test_deploy_trees_parity.py` (PAIRES_SEMANTIQUES)
# réutilise `_required_timeout_seconds` et aligne les mêmes routes des deux confs.

@pytest.mark.parametrize("path", [
    "/admin/models/llama-3.3-70b-instruct/load",
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/completion",
    "/completion",
    "/v1/tokenize",
])
def test_les_timeouts_livres_couvrent_le_registre(conf_text, path):
    """
    COR-009 : chaque route capable de déclencher un chargement de modèle doit
    laisser au proxy plus de temps que `load_timeout_seconds + 10` du pire modèle
    du registre. Sinon le client reçoit 504 alors que le chargement RÉUSSIT.
    """
    import doctor  # noqa: PLC0415

    required, _ = _required_timeout_seconds()
    https = _https_server(conf_text)

    location = doctor.match_location(https.locations, path)
    assert location is not None, f"{path} ne matche aucun location"
    assert "proxy_pass" in location.directives, (
        f"{path} tombe dans un bloc non proxifiant (404) — route injoignable"
    )

    for directive in ("proxy_read_timeout", "proxy_send_timeout"):
        raw = https.effective(location, directive)
        assert raw is not None, f"{directive} absent pour {path}"
        seconds = doctor.parse_nginx_time(raw)
        assert seconds is not None, f"{directive} illisible : {raw!r}"
        assert seconds >= required, (
            f"{directive}={raw} sur « {location.pattern} » alors que le registre "
            f"exige {required}s"
        )


def test_le_defaut_dorigine_du_timeout_admin_serait_detecte(conf_text):
    """
    Contrôle positif du test précédent : on réintroduit `proxy_read_timeout 30s`
    sur `/admin/` et le contrôle doit rougir. Sans ça, une comparaison qui ne
    compare rien passerait inaperçue.
    """
    import doctor  # noqa: PLC0415

    regressed = conf_text.replace(
        "        proxy_send_timeout     900s;\n        proxy_read_timeout     900s;\n    }\n\n"
        "    # Bloquer tout le reste",
        "        proxy_send_timeout     30s;\n        proxy_read_timeout     30s;\n    }\n\n"
        "    # Bloquer tout le reste",
    )
    assert regressed != conf_text, "le bloc /admin/ n'a pas été retrouvé"

    required, _ = _required_timeout_seconds()
    https = _https_server(regressed)
    admin = doctor.match_location(https.locations, "/admin/models/m1/load")
    assert doctor.parse_nginx_time(admin.directives["proxy_read_timeout"]) < required

    # Et doctor doit continuer à le signaler : son diagnostic reste juste.
    from config import settings  # noqa: PLC0415
    models = _doctor_enabled_models()
    result = doctor.check_nginx_timeouts(
        doctor.parse_nginx_servers(regressed), settings, models
    )
    assert result.status == "warn"
    assert result.code == "nginx_admin_timeout_too_short"


def test_doctor_ne_signale_rien_sur_la_conf_livree(conf_text):
    """Le correctif doit rendre le contrôle `nginx_timeouts` de doctor vert."""
    import doctor  # noqa: PLC0415
    from config import settings  # noqa: PLC0415

    models = _doctor_enabled_models()
    result = doctor.check_nginx_timeouts(
        doctor.parse_nginx_servers(conf_text), settings, models
    )
    assert result.status == "pass", result.message


# ── COR-009 : alignement routes FastAPI ↔ nginx ↔ docs/api.md ─────────────────

def _app_route_paths() -> set[str]:
    """
    Chemins exposés par l'application, `{param}` remplacés par une valeur
    concrète pour pouvoir les confronter aux `location` nginx.

    Réutilise le parcours récursif de `test_smoke_test_script` : depuis FastAPI
    0.141, `include_router()` dépose un `_IncludedRouter` dans `app.routes` et
    une lecture directe ne verrait AUCUNE route `/admin/*`.
    """
    import main  # noqa: PLC0415
    from tests.test_smoke_test_script import _iter_app_routes  # noqa: PLC0415

    return {
        re.sub(r"\{[^}]+\}", "x", path) for _, path in _iter_app_routes(main.app)
    }


def _documented_public_paths() -> set[str]:
    """
    Chemins que `docs/api.md` annonce à l'utilisateur. Extraits du texte, deux
    formes : les URL complètes des exemples `curl`/httpx, et les endpoints cités
    en `` `MÉTHODE /chemin` `` (tableau récapitulatif §7, titres de section). Le
    test suit donc automatiquement la doc au lieu de figer une liste.
    """
    text = API_DOC.read_text(encoding="utf-8")
    paths = set()
    for match in re.finditer(re.escape(PUBLIC_HOST) + r"(/[^\s\"'`),]*)", text):
        paths.add(match.group(1).rstrip(".,"))
    for match in re.finditer(r"`(?:GET|POST|PATCH|PUT|DELETE) (/[^`\s]*)`", text):
        paths.add(match.group(1))
    # `…/v1` est une base_url de client OpenAI, pas une route.
    paths.discard("/v1")
    return paths


def test_les_routes_documentees_sont_proxifiees():
    """
    Critère COR-009 : « chaque route documentée est exposée ou supprimée de la
    documentation ». Une route documentée qui retombe dans le `location /` de
    repli renvoie 404 — c'était le cas de `/completion` et de `/ready`.
    """
    import doctor  # noqa: PLC0415

    documented = _documented_public_paths()
    # Contrôle positif : l'extraction doit voir la doc, pas un ensemble vide.
    assert len(documented) >= 6, documented
    assert "/v1/chat/completions" in documented

    https = _https_server(SHIPPED_NGINX_CONF.read_text(encoding="utf-8"))
    app_paths = _app_route_paths()
    for path in sorted(documented):
        assert path in app_paths, f"{path} est documenté mais n'existe pas côté FastAPI"
        location = doctor.match_location(https.locations, path)
        assert location is not None and "proxy_pass" in location.directives, (
            f"{path} est documenté mais nginx renvoie 404 dessus"
        )


def test_toute_route_fastapi_est_joignable_a_travers_nginx():
    """
    Sens inverse : aucune route de l'application ne doit tomber dans le repli
    404. `/admin/*` est joignable mais restreint au réseau campus — c'est le
    contrôle d'accès qui protège, pas un 404 accidentel.
    """
    import doctor  # noqa: PLC0415

    app_paths = _app_route_paths()
    # Contrôle positif : le parcours doit voir les routeurs inclus.
    assert "/admin/status" in app_paths, "parcours de routes aveugle aux routeurs inclus"
    assert len(app_paths) >= 30, len(app_paths)

    https = _https_server(SHIPPED_NGINX_CONF.read_text(encoding="utf-8"))
    unreachable = []
    for path in sorted(app_paths):
        location = doctor.match_location(https.locations, path)
        if location is None or "proxy_pass" not in location.directives:
            unreachable.append(path)
    assert unreachable == [], f"routes exposées par FastAPI mais 404 via nginx : {unreachable}"


def test_admin_reste_restreint_au_reseau_campus(conf_text):
    """Aucune protection relâchée par COR-009 : `/admin/` garde son filtrage IP."""
    import doctor  # noqa: PLC0415

    https = _https_server(conf_text)
    admin = doctor.match_location(https.locations, "/admin/status")
    assert admin is not None and admin.pattern == "/admin/"
    assert admin.directives.get("deny") == "all"

    allows = [
        args[0] for name, args in parse_statements(conf_text)
        if name == "allow" and args
    ]
    assert len(allows) >= 4, allows
    assert "127.0.0.1" in allows

    # Le repli reste un 404 sec : nginx ne devient pas un proxy ouvert.
    fallback = doctor.match_location(https.locations, "/n-importe-quoi")
    assert fallback is not None and fallback.pattern == "/"
    assert fallback.directives.get("return") == "404"
    assert "proxy_pass" not in fallback.directives
