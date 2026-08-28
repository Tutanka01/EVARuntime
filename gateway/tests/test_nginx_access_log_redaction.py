"""
SEC-016 — le journal d'accès nginx livré ne doit contenir aucun nom d'utilisateur.

Pourquoi un moteur et pas un `grep` : la rédaction repose sur deux `map` et un
`log_format` déclarés dans les confs nginx livrées — `gateway/deploy/nginx.conf`
(Linux, référence) ET `gateway/deploy-macos/nginx.conf.macOS`, dont la version
initiale journalisait `$request` brut faute de `log_format` déclaré. Vérifier
que la chaîne « <redacted> » est présente dans un fichier ne prouve rien — ni
que les regex attrapent les bonnes routes, ni que le format s'en sert, ni qu'il
ne réintroduit pas l'URI brute par ailleurs. Ces tests LISENT les règles
livrées, les évaluent sur des requêtes concrètes construites depuis les routes
réelles de FastAPI, et rendent la ligne de journal qui en résulte. Le moteur
tourne sur LES DEUX confs : c'est le garde qui empêche la copie macOS de
divérer à nouveau (la parité sémantique des deux arbres est aussi vue par
`tests/test_deploy_trees_parity.py`, PAIRES_SEMANTIQUES).

Le moteur ci-dessous est délibérément petit et couvre le sous-ensemble de nginx
réellement employé par l'artefact : `map` (clé `default`, clé exacte, clé regex
`~`/`~*`), interpolation de variables, `log_format`. Il ne remplace pas
`nginx -t`, qui est exécuté pour de vrai par le dernier test quand un binaire
nginx est disponible (`skip` propre sinon).

Ce qu'aucun test de ce fichier ne prouve, et comment cela a été vérifié À LA MAIN
au moment d'écrire SEC-016 — à refaire si les `map` changent, aucun runner de ce
dépôt n'ayant nginx :

    python -m pip install crossplane          # parseur officiel de nginx Inc.
    python -c "import crossplane; print(crossplane.parse('nginx.conf'))"

`crossplane` a confirmé deux choses que le moteur ci-dessous ne peut pas dire :
(1) il tokenise les `map` livrés EXACTEMENT comme `parse_maps` ci-dessous — mêmes
clés, mêmes valeurs, mêmes guillemets retirés ; (2) en mode `strict`, avec les
corps de `map` vidés, tout le reste de la conf passe la table de directives de
nginx (nom, contexte, arité) — dont `log_format` au niveau `http`,
`access_log … eva_redacted` au niveau `server` et `error_log … crit` dans une
`location`. Le mode `strict` ne sait pas lire un corps de `map` : il rejette
aussi l'exemple canonique de la documentation nginx, ce n'est donc pas un signal.
Reste non vérifié sans binaire nginx : la sémantique PCRE exacte des regex de
`map` (les constructions employées sont communes à PCRE et à `re`).

Règle AGENTS.md appliquée : chaque test qui assère une ABSENCE porte un contrôle
positif. Ici ils sont de deux natures — un contrôle sur le format d'origine
(`combined` fuit le canari, donc le détecteur voit quelque chose) et un contrôle
sur la conf mutée (retirer la règle de rédaction fait rougir le test).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
# Les DEUX confs nginx livrées : l'arbre Linux (référence) et la copie macOS,
# appariées par rôle (PAIRES_SEMANTIQUES dans test_deploy_trees_parity.py).
CONF_LINUX = REPO_ROOT / "gateway" / "deploy" / "nginx.conf"
CONF_MACOS = REPO_ROOT / "gateway" / "deploy-macos" / "nginx.conf.macOS"
CONFS_NGINX = [
    pytest.param(CONF_LINUX, id="linux"),
    pytest.param(CONF_MACOS, id="macos"),
]
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "deployment.md"

# Nom témoin : il doit être introuvable dans toute ligne de journal rendue.
CANARI = "CANARI-Jean-Dupont"

# Format `combined` de nginx, tel que livré par la distribution. Sert UNIQUEMENT
# de contrôle positif : c'est le format que la conf remplace, et il doit fuir.
COMBINED = (
    '$remote_addr - $remote_user [$time_local] "$request" '
    '$status $body_bytes_sent "$http_referer" "$http_user_agent"'
)

# Variables qui recopient l'URI brute ou le corps de la requête. Aucune ne doit
# apparaître dans le format livré (SEC-016) : `$request` porte la ligne de
# requête entière, `$request_body` porte `email` et `notes`, `$http_referer` peut
# recopier l'URI d'une page admin, `$remote_user` serait un identifiant.
VARIABLES_INTERDITES = (
    "$request", "$request_uri", "$request_body", "$http_referer", "$remote_user",
)


# ── Moteur nginx minimal ──────────────────────────────────────────────────────

_MOT = re.compile(r'"([^"]*)"|\'([^\']*)\'|([^\s;{}]+)')
_VARIABLE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _sans_commentaires(text: str) -> str:
    return re.sub(r"#[^\n]*", "", text)


def _mots(fragment: str) -> list[str]:
    """Découpe un fragment de configuration en mots, guillemets retirés."""
    mots: list[str] = []
    for m in _MOT.finditer(fragment):
        mots.append(next(g for g in m.groups() if g is not None))
    return mots


class Map:
    """Un bloc `map $source $cible { … }` de la configuration livrée."""

    def __init__(self, source: str, cible: str, entrees: list[tuple[str, str]]):
        self.source = source
        self.cible = cible
        self.entrees = entrees  # ordre du fichier, `default` inclus

    @property
    def defaut(self) -> str | None:
        for cle, valeur in self.entrees:
            if cle == "default":
                return valeur
        return None

    def resoudre(self, valeur_source: str) -> str:
        """
        Reproduit la sélection nginx : clés exactes d'abord (table de hachage),
        puis les regex DANS L'ORDRE du fichier, puis `default`.
        """
        for cle, valeur in self.entrees:
            if cle == "default" or cle.startswith("~"):
                continue
            if cle == valeur_source:
                return valeur
        for cle, valeur in self.entrees:
            if not cle.startswith("~"):
                continue
            insensible = cle.startswith("~*")
            motif = cle[2:] if insensible else cle[1:]
            if re.search(motif, valeur_source, re.IGNORECASE if insensible else 0):
                return valeur
        defaut = self.defaut
        if defaut is None:
            return ""
        return defaut


def parse_maps(text: str) -> dict[str, Map]:
    """Tous les blocs `map` du fichier, indexés par la variable qu'ils produisent."""
    nettoye = _sans_commentaires(text)
    maps: dict[str, Map] = {}
    for entete in re.finditer(r"map\s+(\$\w+)\s+(\$\w+)\s*\{", nettoye):
        debut = entete.end()
        fin = nettoye.index("}", debut)
        entrees: list[tuple[str, str]] = []
        for ligne in nettoye[debut:fin].split(";"):
            mots = _mots(ligne)
            if len(mots) == 2:
                entrees.append((mots[0], mots[1]))
            elif mots:
                raise AssertionError(f"entrée de map illisible : {ligne!r}")
        cible = entete.group(2).lstrip("$")
        maps[cible] = Map(entete.group(1), cible, entrees)
    return maps


def parse_log_format(text: str, nom: str) -> str:
    """Le `log_format <nom>` livré, ses fragments concaténés."""
    nettoye = _sans_commentaires(text)
    debut = re.search(rf"log_format\s+{re.escape(nom)}\b", nettoye)
    if debut is None:
        raise AssertionError(f"log_format {nom} absent de la conf livrée")
    fin = nettoye.index(";", debut.end())
    return "".join(_mots(nettoye[debut.end():fin]))


def interpoler(gabarit: str, variables: dict[str, str]) -> str:
    """Remplace `$nom` / `${nom}` par sa valeur. Une variable inconnue lève."""
    def _remplacer(m: re.Match) -> str:
        nom = m.group(1) or m.group(2)
        if nom not in variables:
            raise AssertionError(f"variable nginx inconnue du moteur : ${nom}")
        return variables[nom]
    return _VARIABLE.sub(_remplacer, gabarit)


def variables_de_requete(methode: str, cible: str, statut: int = 200) -> dict[str, str]:
    """
    Variables nginx pour une requête donnée. `cible` est la forme brute écrite sur
    la ligne de requête, query string comprise.

    `$uri` est pourcent-décodé et `$args` ne l'est pas — c'est le comportement de
    nginx, et c'est ce qui fait que `/admin/users/%4Aean-Dupont` tombe quand même
    dans la règle de rédaction du chemin.
    """
    chemin, _, args = cible.partition("?")
    return {
        "remote_addr": "10.1.2.3",
        "remote_user": "-",
        "time_local": "03/Aug/2026:11:22:33 +0200",
        "request": f"{methode} {cible} HTTP/1.1",
        "request_method": methode,
        "request_uri": cible,
        "request_body": "-",
        "uri": unquote(chemin),
        "args": args,
        "server_protocol": "HTTP/1.1",
        "status": str(statut),
        "body_bytes_sent": "1234",
        "request_time": "0.042",
        "http_referer": "-",
        "http_user_agent": "curl/8.4.0",
    }


def rendre(conf: str, methode: str, cible: str, statut: int = 200,
           gabarit: str | None = None) -> str:
    """Ligne de journal telle que nginx l'écrirait avec la conf fournie."""
    variables = variables_de_requete(methode, cible, statut)
    maps = parse_maps(conf)
    # Une seule passe suffit : aucun map livré ne dépend d'un autre map.
    for nom, bloc in maps.items():
        source = bloc.source.lstrip("$")
        variables[nom] = interpoler(bloc.resoudre(variables[source]), variables)
    if gabarit is None:
        gabarit = parse_log_format(conf, "eva_redacted")
    return interpoler(gabarit, variables)


def cible_redigee(conf: str, cible: str) -> str:
    """La seule cible « chemin[?requête] » rédigée, hors du reste de la ligne."""
    return rendre(conf, "GET", cible, gabarit="$eva_log_path$eva_log_args")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", params=CONFS_NGINX)
def conf_text(request) -> str:
    """La conf nginx analysée : chaque test du module tourne sur les DEUX arbres."""
    return request.param.read_text(encoding="utf-8")


def _routes_de_lapplication() -> set[tuple[str, str]]:
    """
    `(méthode, chemin)` de TOUTES les routes exposées, routeurs inclus compris.

    Parcours récursif obligatoire : depuis FastAPI 0.141, `app.routes` ne contient
    qu'un `_IncludedRouter` pour `/admin/*` (voir AGENTS.md § Testing).
    """
    import main  # noqa: PLC0415 - import tardif : conftest prépare l'environnement
    from tests.test_smoke_test_script import _iter_app_routes  # noqa: PLC0415

    return set(_iter_app_routes(main.app))


def _requetes_porteuses_de_nom() -> list[tuple[str, str]]:
    """
    Requêtes concrètes dont un segment de chemin ou un paramètre porte un nom.

    Les chemins sont DÉRIVÉS des routes FastAPI (tout `{username}`), pas d'une
    liste écrite à la main : une route ajoutée demain entre automatiquement dans
    le périmètre du test. Le seul paramètre de requête concerné, `username` de
    `GET /admin/usage`, est ajouté explicitement — nginx ne peut pas le déduire
    de la signature Python.
    """
    requetes = [
        (methode, chemin.replace("{username}", CANARI))
        for methode, chemin in sorted(_routes_de_lapplication())
        if "{username}" in chemin
    ]
    requetes += [
        ("GET", f"/admin/usage?username={CANARI}"),
        ("GET", f"/admin/usage?username={CANARI}&from_date=2026-01-01&limit=50"),
        # Forme pourcent-encodée : `$uri` étant décodé, elle doit être rédigée
        # comme la forme claire.
        ("GET", "/admin/users/%43ANARI-Jean-Dupont"),
        # Espace encodé — un `username` réel peut en contenir un.
        ("GET", "/admin/users/CANARI-Jean%20Dupont"),
    ]
    return requetes


# ── Contrôles positifs du moteur ──────────────────────────────────────────────

def test_le_moteur_lit_reellement_les_regles_livrees(conf_text):
    """
    Sans ce contrôle, un `parse_maps` cassé rendrait « aucun nom dans le journal »
    trivialement vrai (tout serait vide) et tous les tests ci-dessous
    deviendraient inertes sans jamais échouer.
    """
    maps = parse_maps(conf_text)
    assert set(maps) == {"eva_log_path", "eva_log_args"}, sorted(maps)

    chemin = maps["eva_log_path"]
    assert chemin.source == "$uri", "le chemin doit être dérivé de $uri (décodé)"
    assert len(chemin.entrees) >= 3, chemin.entrees
    assert sum(1 for cle, _ in chemin.entrees if cle.startswith("~")) >= 2

    args = maps["eva_log_args"]
    assert args.source == "$args"
    assert args.defaut is not None and "<redacted>" in args.defaut, (
        "le défaut de la query string doit être fail-closed"
    )

    gabarit = parse_log_format(conf_text, "eva_redacted")
    assert len(gabarit) > 60, gabarit
    for attendue in ("$remote_addr", "$request_method", "$status", "$eva_log_path"):
        assert attendue in gabarit, gabarit


def test_le_canari_est_detectable_avec_le_format_dorigine(conf_text):
    """
    Contrôle positif du détecteur de fuite : le format `combined` que la conf
    remplace DOIT laisser passer le canari sur chacune des requêtes du tableau.
    Si celui-ci ne fuit pas, c'est le tableau ou le rendu qui est cassé, pas la
    rédaction qui est bonne — et le test suivant ne prouverait rien.
    """
    requetes = _requetes_porteuses_de_nom()
    assert len(requetes) >= 8, requetes
    for methode, cible in requetes:
        ligne = rendre(conf_text, methode, cible, gabarit=COMBINED)
        # « Dupont » et non « CANARI » : `combined` journalise `$request`, donc la
        # cible BRUTE, où le pourcent-encodage déguise une partie du nom. Le
        # patronyme, lui, traverse toutes les formes testées — c'est précisément
        # ce qu'un encodage ne suffit pas à protéger.
        assert "Dupont" in ligne, (methode, cible, ligne)


# ── SEC-016 : aucun nom dans le journal d'accès ───────────────────────────────

@pytest.mark.parametrize("methode,cible", _requetes_porteuses_de_nom())
def test_aucun_nom_dutilisateur_ne_survit_dans_le_journal(conf_text, methode, cible):
    """Le cœur de SEC-016, éprouvé sur la conf réellement livrée."""
    ligne = rendre(conf_text, methode, cible)
    assert "CANARI" not in ligne, ligne
    assert "Dupont" not in ligne, ligne
    assert "<redacted>" in ligne, ligne


def test_toute_route_a_segment_username_est_couverte(conf_text):
    """
    Ne pas se fier à une liste écrite à la main : on repart des routes FastAPI.
    Toute route portant `{username}` doit être rédigée, y compris une route
    ajoutée après ce test.
    """
    porteuses = {
        chemin for _, chemin in _routes_de_lapplication() if "{username}" in chemin
    }
    # Contrôle positif : le parcours doit voir les routeurs inclus.
    assert "/admin/users/{username}" in porteuses, "parcours de routes aveugle"
    assert len(porteuses) >= 2, porteuses

    for chemin in sorted(porteuses):
        ligne = rendre(conf_text, "GET", chemin.replace("{username}", CANARI))
        assert "CANARI" not in ligne, (chemin, ligne)


def test_retirer_la_regle_de_redaction_fait_fuir_le_journal(conf_text):
    """
    Contrôle positif du correctif lui-même : on retire du `map` la règle générique
    qui rédige `/admin/users/<nom>`, et la fuite doit réapparaître. Sans lui, une
    regex qui ne matcherait jamais passerait le test précédent en silence — la
    ligne serait rédigée pour une autre raison, ou pas du tout comparée.
    """
    ampute = conf_text.replace('    "~^/admin/users/[^/]+"        '
                               '"/admin/users/<redacted>";\n', "")
    assert ampute != conf_text, "la règle de rédaction du chemin n'a pas été retrouvée"
    ligne = rendre(ampute, "GET", f"/admin/users/{CANARI}")
    assert "CANARI" in ligne, ligne

    # Et la query string : on autorise `username`, la fuite doit revenir aussi.
    permissif = conf_text.replace("(from_date|to_date|limit|force|period)",
                                  "(from_date|to_date|limit|force|period|username)")
    assert permissif != conf_text
    ligne = rendre(permissif, "GET", f"/admin/usage?username={CANARI}")
    assert "CANARI" in ligne, ligne


# ── L'arbitrage : le journal doit rester exploitable ──────────────────────────

@pytest.mark.parametrize("methode,cible,statut", [
    ("GET", f"/admin/users/{CANARI}", 200),
    ("GET", f"/admin/usage?username={CANARI}", 404),
    ("POST", "/v1/chat/completions", 502),
])
def test_le_journal_conserve_ce_qui_sert_en_incident(conf_text, methode, cible, statut):
    """
    `access_log off` fermerait la fuite et rendrait le diagnostic impossible : ce
    n'est PAS la réponse retenue. Chaque ligne doit encore porter l'adresse
    cliente, la méthode, le protocole, le statut, le volume et la durée.
    """
    ligne = rendre(conf_text, methode, cible, statut)
    assert "10.1.2.3" in ligne, ligne          # adresse cliente
    assert methode in ligne, ligne             # méthode
    assert "HTTP/1.1" in ligne, ligne          # protocole
    assert f" {statut} " in ligne, ligne       # statut
    assert "1234" in ligne, ligne              # volume
    assert "0.042" in ligne, ligne             # durée
    assert "curl/8.4.0" in ligne, ligne        # user-agent


@pytest.mark.parametrize("cible,attendu", [
    # Routes sans nom : le chemin reste ENTIER, sinon la rédaction serait un
    # `access_log off` déguisé.
    ("/v1/chat/completions", "/v1/chat/completions"),
    ("/admin/models/llama-3.3-70b-instruct/load", "/admin/models/llama-3.3-70b-instruct/load"),
    ("/admin/users", "/admin/users"),
    ("/admin/keys/llmgw-abc12345", "/admin/keys/llmgw-abc12345"),
    # Sous-ressource conservée : on sait encore qu'il s'agissait des clés.
    (f"/admin/users/{CANARI}/keys", "/admin/users/<redacted>/keys"),
    # Paramètres structurels : lisibles, ce sont eux qui rendent le journal utile.
    ("/admin/usage?from_date=2026-01-01&to_date=2026-01-31&limit=100",
     "/admin/usage?from_date=2026-01-01&to_date=2026-01-31&limit=100"),
    ("/admin/metrics/overview?period=24h", "/admin/metrics/overview?period=24h"),
    ("/admin/models/m1?force=true", "/admin/models/m1?force=true"),
])
def test_la_forme_de_la_route_reste_lisible(conf_text, cible, attendu):
    ligne = rendre(conf_text, "GET", cible)
    assert f'"GET {attendu} HTTP/1.1"' in ligne, ligne


# ── Alignement avec la politique uvicorn (SEC-010) ────────────────────────────

def _params_autorises_par_nginx(conf_text: str) -> set[str]:
    """Les noms de paramètres que la regex `eva_log_args` laisse lisibles."""
    args = parse_maps(conf_text)["eva_log_args"]
    noms: set[str] = set()
    for cle, _valeur in args.entrees:
        if not cle.startswith("~"):
            continue
        for groupe in re.findall(r"\(([a-z_|]+)\)", cle):
            noms.update(groupe.split("|"))
    return noms


def test_la_liste_dautorisation_nginx_est_celle_duvicorn(conf_text):
    """
    Une seule politique, pas deux. Si SEC-010 élargit ou restreint
    `_LOGGABLE_QUERY_PARAMS` côté Python sans toucher nginx, ce test rougit.
    """
    import main  # noqa: PLC0415

    uvicorn = set(main._LOGGABLE_QUERY_PARAMS)
    # Contrôle positif : la liste Python doit être non vide et contenir un
    # paramètre réellement employé par une route.
    assert "from_date" in uvicorn, uvicorn
    assert _params_autorises_par_nginx(conf_text) == uvicorn


def _cible_redigee_par_uvicorn(cible: str) -> str:
    """La même cible, passée au filtre `uvicorn.access` livré par SEC-010."""
    import main  # noqa: PLC0415

    chemin, sep, args = cible.partition("?")
    # uvicorn lit `scope["path"]`, déjà pourcent-décodé, et `query_string` brute.
    return main._redact_target(unquote(chemin) + sep + args)


def test_nginx_ne_revele_jamais_plus_quuvicorn(conf_text):
    """
    Le sens de la comparaison est celui-ci et pas l'inverse : nginx a le droit
    d'être PLUS conservateur (il rédige la query string entière dès qu'un
    paramètre sort de la liste, là où uvicorn rédige valeur par valeur), jamais
    plus permissif. On vérifie donc que tout fragment conservé par nginx est
    aussi conservé par uvicorn.
    """
    cibles = [cible for _methode, cible in _requetes_porteuses_de_nom()]
    cibles += [
        "/admin/usage?from_date=2026-01-01&limit=100",
        "/admin/users",
        "/admin/keys/llmgw-abc12345",
        "/v1/chat/completions",
    ]
    for cible in cibles:
        cote_nginx = cible_redigee(conf_text, cible)
        cote_uvicorn = _cible_redigee_par_uvicorn(cible)
        # Contrôle positif : les deux côtés doivent produire quelque chose.
        assert cote_nginx and cote_uvicorn, (cible, cote_nginx, cote_uvicorn)
        for fragment in re.split(r"[/?&=]", cote_nginx):
            if fragment and "redacted" not in fragment:
                assert fragment in cote_uvicorn, (
                    f"nginx conserve « {fragment} » qu'uvicorn cache — "
                    f"divergence dans le MAUVAIS sens : {cote_nginx!r} vs "
                    f"{cote_uvicorn!r}"
                )


def test_la_divergence_de_granularite_est_celle_annoncee(conf_text):
    """
    Épingle la seule divergence assumée, pour qu'elle reste un choix documenté et
    non une dérive : sur `?username=…&limit=50`, uvicorn garde `limit=50`, nginx
    rédige la requête entière. Si un jour nginx devenait aussi fin, ce test
    rougirait et le commentaire de la conf devrait être mis à jour.
    """
    cible = f"/admin/usage?username={CANARI}&limit=50"
    assert cible_redigee(conf_text, cible) == "/admin/usage?<redacted>"
    assert _cible_redigee_par_uvicorn(cible) == (
        "/admin/usage?username=<redacted>&limit=50"
    )


def test_les_deux_politiques_sont_citees_lune_par_lautre(conf_text):
    """
    L'alignement n'est utile que s'il est trouvable : la conf nginx doit renvoyer
    au filtre uvicorn, sinon la prochaine modification n'en touchera qu'un côté.
    """
    assert "_LOGGABLE_QUERY_PARAMS" in conf_text
    assert "SEC-010" in conf_text
    assert "main.py" in conf_text


# ── Le format livré ne réintroduit pas l'URI brute ────────────────────────────

def test_le_format_livre_nemploie_aucune_variable_interdite(conf_text):
    gabarit = parse_log_format(conf_text, "eva_redacted")
    trouvees = [v for v in VARIABLES_INTERDITES
                if re.search(re.escape(v) + r"\b", gabarit)]
    assert trouvees == [], trouvees


def test_le_detecteur_de_variable_interdite_voit_le_format_dorigine():
    """Contrôle positif du test précédent : `combined` doit être rejeté."""
    trouvees = [v for v in VARIABLES_INTERDITES
                if re.search(re.escape(v) + r"\b", COMBINED)]
    assert "$request" in trouvees and "$http_referer" in trouvees, trouvees


# ── Portée : les deux serveurs, et les choix déjà pris ────────────────────────

def _blocs_server(conf_text: str) -> list[str]:
    """Corps de chaque bloc `server { … }` de premier niveau."""
    nettoye = _sans_commentaires(conf_text)
    blocs: list[str] = []
    for entete in re.finditer(r"\bserver\s*\{", nettoye):
        profondeur, i = 1, entete.end()
        while profondeur:
            if nettoye[i] == "{":
                profondeur += 1
            elif nettoye[i] == "}":
                profondeur -= 1
            i += 1
        blocs.append(nettoye[entete.end():i - 1])
    return blocs


@pytest.mark.parametrize(
    ("chemin", "nb_serveurs"),
    [pytest.param(CONF_LINUX, 2, id="linux"), pytest.param(CONF_MACOS, 1, id="macos")],
)
def test_les_serveurs_journalisent_avec_le_format_redige(chemin: Path, nb_serveurs: int):
    """
    Tout bloc `server` ACTIF doit journaliser avec le format rédigé.

    Linux : le 443 ET la redirection port 80 — une redirection journalise l'URI
    demandée exactement comme une requête servie, et l'oublier laissait
    `GET /admin/users/<nom>` en clair avant même de rediriger. macOS : un seul
    bloc actif, qui proxifie ET redirige conditionnellement (la recette HTTPS
    est commentée) ; sa première version avait précisément ce `access_log`
    commenté, et donc le format `combined` par défaut.
    """
    blocs = _blocs_server(chemin.read_text(encoding="utf-8"))
    assert len(blocs) == nb_serveurs, f"{len(blocs)} blocs server trouvés"
    for bloc in blocs:
        assert re.search(r"access_log\s+\S+\s+eva_redacted\s*;", bloc), bloc


def test_les_sondes_gardent_leur_access_log_off(conf_text):
    """
    `/health` et `/ready` coupent leur journal pour ne pas noyer le fichier sous
    les sondes (choix antérieur, COR-009). SEC-016 ne le défait pas : un
    `access_log` au niveau `server` est hérité, pas prioritaire. Le `=` est
    optionnel dans le motif : macOS expose les deux sondes en location exacte.
    """
    nettoye = _sans_commentaires(conf_text)
    for motif in (r"location\s+=?\s*/health\s*\{[^}]*access_log\s+off\s*;",
                  r"location\s+=?\s*/ready\s*\{[^}]*access_log\s+off\s*;"):
        assert re.search(motif, nettoye, re.DOTALL), motif


def test_le_journal_derreur_est_releve_sur_admin(conf_text):
    """
    Le format du journal d'erreur nginx n'est pas configurable et recopie l'URI
    brute (« request: "GET /admin/users/<nom> HTTP/1.1" »). Seule parade native :
    relever le seuil sur la location concernée.
    """
    nettoye = _sans_commentaires(conf_text)
    admin = re.search(r"location\s+/admin/\s*\{(.*?)\n    \}", nettoye, re.DOTALL)
    assert admin is not None, "bloc /admin/ introuvable"
    assert re.search(r"error_log\s+\S+\s+crit\s*;", admin.group(1)), admin.group(1)

    # Contrôle positif ET limite explicite du choix : /v1/ garde son journal
    # d'erreur complet, ses URI ne portant aucun nom.
    v1 = re.search(r"location\s+/v1/\s*\{(.*?)\n    \}", nettoye, re.DOTALL)
    assert v1 is not None
    assert "error_log" not in v1.group(1)


def test_la_redaction_est_documentee():
    doc = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    assert "SEC-016" in doc
    assert "eva_redacted" in doc
    assert "llm-gateway-access.log" in doc


# ── Validation par nginx lui-même ─────────────────────────────────────────────

@pytest.mark.parametrize("chemin_conf", CONFS_NGINX)
def test_nginx_valide_la_configuration_livree(tmp_path, chemin_conf):
    """
    La seule vérification qui prouve que la syntaxe est acceptée (regex de `map`
    comprises), sur les DEUX confs livrées. `skip` propre quand nginx est absent
    — c'est le cas de la machine de développement macOS de ce dépôt, mais **pas**
    des runners CI GitHub, qui livrent nginx et exécutent donc réellement ce
    test. Les tests structurels ci-dessus restent la garantie permanente ;
    celui-ci est la seule preuve que nginx accepte ce que nous écrivons.
    """
    binaire = shutil.which("nginx")
    if binaire is None:
        pytest.skip("nginx absent de la machine — validation syntaxique impossible")

    conf_text = chemin_conf.read_text(encoding="utf-8")
    racine = tmp_path / "racine"
    for sous in ("logs", "conf", "tmp"):
        (racine / sous).mkdir(parents=True)
    (racine / "conf" / "mime.types").write_text("types { }\n", encoding="utf-8")

    # macOS : le préfixe Homebrew CONTIENT "/var/log/nginx/" — remplacer le
    # préfixe le plus long d'abord, sinon la règle Linux tronque le chemin.
    site = (
        conf_text
        .replace("/opt/homebrew/var/log/nginx/", str(racine / "logs") + "/")
        .replace("/var/log/nginx/", str(racine / "logs") + "/")
        # Ports non privilégiés : `nginx -t` n'écoute pas, mais reste cohérent.
        .replace("listen 443 ssl;", "listen 8443 ssl;")
        .replace("listen [::]:443 ssl;", "listen [::]:8443 ssl;")
        .replace("listen 80;", "listen 8080;")
        .replace("listen [::]:80;", "listen [::]:8080;")
    )

    # Certificat auto-signé : `nginx -t` ouvre réellement les fichiers TLS. La
    # conf macOS n'a pas de TLS actif (recette commentée) : rien à générer.
    if re.search(r"^\s*ssl_certificate\s", site, re.MULTILINE):
        cert, cle = racine / "tls.crt", racine / "tls.key"
        genere = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(cle), "-out", str(cert), "-days", "1",
             "-subj", "/CN=llm.eva.univ-pau.fr"],
            capture_output=True, text=True,
        )
        if genere.returncode != 0:
            pytest.skip(f"openssl indisponible : {genere.stderr.strip()[:200]}")
        site = (
            site
            .replace("/etc/ssl/certs/llm-gateway.crt", str(cert))
            .replace("/etc/ssl/private/llm-gateway.key", str(cle))
        )

    (racine / "conf" / "site.conf").write_text(site, encoding="utf-8")
    # `pid` et les répertoires temporaires doivent être déportés sous la racine
    # jetable : `nginx -t` ouvre RÉELLEMENT le fichier de pid, et un utilisateur
    # non privilégié — le runner CI, par exemple — n'a pas accès à
    # `/run/nginx.pid`. Sans ces directives, le test échoue sur les droits de
    # l'hôte alors que nginx a déjà répondu « syntax is ok », c'est-à-dire sur
    # tout autre chose que ce qu'il prétend vérifier.
    (racine / "conf" / "nginx.conf").write_text(
        f"pid {racine / 'nginx.pid'};\n"
        "events { worker_connections 64; }\n"
        "http {\n"
        "    include mime.types;\n"
        f"    error_log {racine / 'logs' / 'error.log'} warn;\n"
        f"    client_body_temp_path {racine / 'tmp' / 'client'};\n"
        f"    proxy_temp_path {racine / 'tmp' / 'proxy'};\n"
        f"    fastcgi_temp_path {racine / 'tmp' / 'fastcgi'};\n"
        f"    uwsgi_temp_path {racine / 'tmp' / 'uwsgi'};\n"
        f"    scgi_temp_path {racine / 'tmp' / 'scgi'};\n"
        "    include site.conf;\n"
        "}\n",
        encoding="utf-8",
    )

    resultat = subprocess.run(
        [binaire, "-t", "-p", str(racine), "-c", "conf/nginx.conf",
         "-e", str(racine / "logs" / "error.log")],
        capture_output=True, text=True,
    )
    sortie = resultat.stdout + resultat.stderr
    assert resultat.returncode == 0, sortie
    # Aucun warning non plus : ils masquent le bruit utile au reload (OPS-009).
    assert "[warn]" not in sortie, sortie
