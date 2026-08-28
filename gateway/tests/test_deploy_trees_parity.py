"""Garde de parité entre `gateway/deploy/` (Linux) et `gateway/deploy-macos/` (macOS).

Pourquoi ce test existe (issue #29) : les deux arbres dupliquent huit scripts
d'installation/exploitation dont une partie du contenu est commune. Rien ne
garantissait que les parties communes restent alignées, et la dérive a déjà eu
lieu (dc7f257 corrigeait uniquement la copie macOS d'un script ; le smoke test
exécuté par `deploy-macos/update.sh` était la copie **Linux**).

**Arbre de référence : `gateway/deploy/`.** Pour toute partie commune, la copie
Linux fait foi ; la copie macOS doit être répliquée à l'identique, modulo les
substitutions de chemins normalisées ci-dessous.

Le mécanisme : un manifeste déclare, pour chaque fichier commun, l'une des
trois politiques :

- **miroir** (`MIROIRS`) : les deux copies doivent être identiques après
  normalisation des chemins plateformes. Toute autre divergence échoue ;
- **fonctions partagées** (`FONCTIONS_PARTAGEES`) : le fichier diverge par
  nature (système de service, outils GNU/BSD), mais les fonctions listées
  doivent rester identiques après normalisation ;
- **divergence déclarée** (`DIVERGENCES_PAR_NATURE`) : divergence par nature,
  avec une justification écrite. Aucun fichier commun ne peut être ajouté aux
  deux arbres sans être déclaré ici : le manifeste doit couvrir exactement
  l'intersection des deux arborescences.

En cas d'échec, deux issues possibles : porter le changement sur l'autre copie
(règle par défaut), ou — si la divergence est devenue légitime — la déclarer
dans le manifeste avec sa justification. Ce test porte un contrôle positif
(test_le_garde_detecte_une_mutation_sur_une_copie) : il prouve que le
comparateur voit réellement les divergences, et qu'il ne rend pas un verdict
d'absence par inertie.

Les paires sémantiques de noms différents — `nginx.conf` (Linux) vs
`nginx.conf.macOS` — échappent au mécanisme par nom (intersection des noms) :
elles sont déclarées à part dans `PAIRES_SEMANTIQUES` et gardées par
INVARIANTS SÉMANTIQUES (même rédaction de journal SEC-016, mêmes timeouts
dérivés du registre, même body size, pas de « Connection "upgrade" », pas de
`http2` actif), pas par diff texte — les deux confs divergent légitimement
(TLS actif côté Linux et recette commentée côté macOS, allowlist admin
localhost, chemins de logs Homebrew).

`tests/test_nginx_http2_lib.py` ne compare PAS les deux arbres : il exerce le
rendu conditionnel HTTP/2 de `deploy/nginx-lib.sh` et ne lit que
`deploy/nginx.conf`.
"""

from __future__ import annotations

import difflib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_GATEWAY = Path(__file__).resolve().parents[1]
ARBRE_LINUX = REPO_GATEWAY / "deploy"
ARBRE_MACOS = REPO_GATEWAY / "deploy-macos"


# ── Normalisation des chemins plateformes ─────────────────────────────────────


@dataclass(frozen=True)
class Normalisation:
    """Substitution vers une forme canonique, appliquée côté par côté.

    `pattern_linux` et `pattern_macos` sont des regex (None = règle sans
    effet sur ce côté). Chaque règle est justifiée : elle encode une
    correspondance de la table « Architecture macOS vs Linux » de
    docs/deployment.md, pas une commodité ad hoc.
    """

    canonique: str
    pattern_linux: str | None
    pattern_macos: str | None
    justification: str


# L'ordre du tuple est SIGNIFICATIF : <INSTALL_DIR> macOS est un préfixe de
# <STATE_DIR> macOS (…/evaruntime[/gateway]) et la règle par fichier
# <STATE_DIR>/data/ suppose <STATE_DIR> déjà appliquée. Ne pas réordonner sans
# relire les justifications.
NORMALISATIONS: tuple[Normalisation, ...] = (
    Normalisation(
        canonique="<INSTALL_DIR>",
        pattern_linux=r"/opt/llm-gateway",
        pattern_macos=r"\$HOME/Library/Application Support/evaruntime/gateway",
        justification="répertoire d'installation (table Architecture, docs/deployment.md)",
    ),
    Normalisation(
        canonique="<CONFIG_DIR>",
        pattern_linux=r"/etc/llm-gateway",
        pattern_macos=r"(\$HOME|~)/\.config/evaruntime",
        justification="répertoire de configuration (table Architecture, docs/deployment.md)",
    ),
    Normalisation(
        canonique="<STATE_DIR>",
        pattern_linux=r"/var/lib/llm-gateway",
        pattern_macos=r"\$HOME/Library/Application Support/evaruntime",
        justification="répertoire de données et logs (table Architecture, docs/deployment.md)",
    ),
    Normalisation(
        canonique="deploy",
        pattern_linux=None,
        pattern_macos=r"deploy-macos",
        justification="nom de l'arbre source : chaque copie désigne son propre arbre",
    ),
    Normalisation(
        canonique="<PKG_SQLITE>",
        pattern_linux=r"apt install sqlite3",
        pattern_macos=r"brew install sqlite",
        justification="hint d'installation du paquet sqlite3 (apt vs brew)",
    ),
)

# Règles supplémentaires propres à un fichier : le layout macOS place la base
# sous `<racine>/data/`, là où Linux la met directement sous /var/lib/llm-gateway.
NORMALISATIONS_PAR_FICHIER: dict[str, tuple[Normalisation, ...]] = {
    "llm-gateway-backup.sh": (
        Normalisation(
            canonique="<STATE_DIR>/",
            pattern_linux=None,
            pattern_macos=r"<STATE_DIR>/data/",
            justification="sous-répertoire data/ du layout macOS (table Architecture)",
        ),
    ),
}

# Doc d'en-tête plateforme (lancement launchd vs systemd) admise comme prologue
# hors de la comparaison : la comparaison du code commence au marqueur.
PROLOGUES: dict[str, str] = {
    "llm-gateway-backup.sh": "set -euo pipefail",
}


# ── Manifeste de parité ───────────────────────────────────────────────────────

#: Fichiers qui doivent être identiques après normalisation.
MIROIRS: frozenset[str] = frozenset(
    {
        "smoke_test.sh",
        "llm-gateway-backup.sh",
    }
)

#: Fonctions devant rester identiques (après normalisation) dans un fichier
#: qui diverge par nature sur le reste.
FONCTIONS_PARTAGEES: dict[str, tuple[str, ...]] = {
    "code-layout-lib.sh": (
        "_deploy_code_safe_roots",
        "deploy_sync_gateway_code",
        "deploy_sync_gateway_operational_files",
    ),
    "env-template-lib.sh": (
        "deploy_model_dirs_from_registry",
        "deploy_allowed_model_dirs",
    ),
    "gpu-preflight-lib.sh": ("deploy_gpu_waiver_declared",),
}

#: Justifications des divergences par nature. Tout fichier commun non miroir
#: doit y figurer, avec une justification substantive.
DIVERGENCES_PAR_NATURE: dict[str, str] = {
    "code-layout-lib.sh": (
        "Les listes _DEPLOY_OPERATIONAL_FILES diffèrent par nature : l'installation "
        "Linux copie aussi les libs (sourcees depuis /opt), tandis que macOS ne "
        "copie que les scripts exécutables (les libs restent sourcées depuis le "
        "repo). Les fonctions exclusives (snapshot/restore Linux vs permissions/"
        "static macOS) reflètent le modèle systemd vs utilisateur courant. Les "
        "trois fonctions communes de copie restent gardées par FONCTIONS_PARTAGEES."
    ),
    "deploy-mode-lib.sh": (
        "Divergence par nature : deploy_validate_mode refuse le cluster sur macOS, "
        "deploy_select_mode n'existe que côté Linux, et l'écriture du fichier "
        "d'environnement repose sur des outils GNU (chmod/chown --reference, "
        "mktemp+mv atomique) sans équivalent BSD direct. Les cinq fonctions "
        "communes doivent rester définies des deux côtés : invariant testé."
    ),
    "env-template-lib.sh": (
        "Les commentaires pédagogiques SEC-002, le calcul du budget mémoire "
        "(valeurs fixées pour la cible Linux vs sysctl hw.memsize sur mémoire "
        "unifiée Apple Silicon), le chemin du binaire llama-server et CUDA_"
        "VISIBLE_DEVICES diffèrent par nature. Les fonctions de découverte des "
        "répertoires de modèles et les trois clés de durcissement restent gardées."
    ),
    "gpu-preflight-lib.sh": (
        "Verdicts incompatibles par nature : Linux sonde nvidia-smi et peut refuser "
        "(waiver explicite), macOS retourne toujours metal-detected. Le contrat "
        "transverse (clé de waiver, grammaire deploy_gpu_waiver_declared, absence "
        "de résidu de chemin Linux) reste gardé."
    ),
    "install.sh": (
        "Modèles d'exécution opposés : root + systemd + useradd + llmservice + "
        "waiver GPU + timer de backup + journald côté Linux ; utilisateur courant "
        "+ launchd + Homebrew côté macOS. Toute évolution du contrat produit "
        "commun (Python 3.11+, env généré sans écrasement, models.yaml initial, "
        "DB initialisée) doit être répliquée à la main des deux côtés."
    ),
    "update.sh": (
        "La copie Linux est transactionnelle (snapshot de code, venv staged avec "
        "pip check, attente /ready, rollback, gate smoke test 0/4/5/6, doctor) ; "
        "la copie macOS est un pipeline simple (backup venv/env, sync, pip in-place, "
        "bootout/bootstrap). Les invariants transverses (smoke test de son propre "
        "arbre, signal SEC-002) sont testés ; toute correction apportée à l'une "
        "doit être évaluée pour l'autre."
    ),
}

#: Paires de fichiers appariés par RÔLE et non par nom : clé = nom canonique
#: (côté Linux), tuple = (nom Linux, nom macOS). Ces noms ne figurent PAS dans
#: `fichiers_communs()` (intersection par nom) : le mécanisme miroir ne les voit
#: donc jamais, et c'est précisément ce trou qui a laissé `nginx.conf.macOS`
#: dériver en silence (access_log commenté + coquille « eva_rigged » → usernames
#: en clair dans le journal ; /admin/ au timeout par défaut 60 s → 504 à chaque
#: chargement de modèle). Leur parité est sémantique, pas textuelle : les deux
#: confs nginx divergent légitimement (TLS actif vs recette commentée, allowlist
#: admin localhost, chemins de logs Homebrew) — voir `divergences_semantiques`.
PAIRES_SEMANTIQUES: dict[str, tuple[str, str]] = {
    "nginx.conf": ("nginx.conf", "nginx.conf.macOS"),
}


# ── Mécanique du garde ────────────────────────────────────────────────────────


def normalise(nom_fichier: str, texte: str) -> str:
    """Applique les règles communes puis les règles propres au fichier."""
    regles = NORMALISATIONS + NORMALISATIONS_PAR_FICHIER.get(nom_fichier, ())
    for regle in regles:
        if regle.pattern_linux:
            texte = re.sub(regle.pattern_linux, regle.canonique, texte)
        if regle.pattern_macos:
            texte = re.sub(regle.pattern_macos, regle.canonique, texte)
    return texte


def corps_comparable(nom_fichier: str, texte: str) -> str:
    """Retire le prologue documentaire plateforme, s'il en est déclaré un."""
    marqueur = PROLOGUES.get(nom_fichier)
    if marqueur is None:
        return texte
    try:
        return texte[texte.index(marqueur):]
    except ValueError:
        raise AssertionError(
            f"{nom_fichier}: marqueur de prologue '{marqueur}' introuvable — le "
            f"fichier a été reformaté, mets à jour PROLOGUES dans ce test."
        ) from None


def diff_miroir(
    nom_fichier: str, racine_linux: Path = ARBRE_LINUX, racine_macos: Path = ARBRE_MACOS
) -> str:
    """Diff unifié des deux copies après normalisation. Chaîne vide = parité."""
    linux = corps_comparable(
        nom_fichier, normalise(nom_fichier, (racine_linux / nom_fichier).read_text(encoding="utf-8"))
    )
    macos = corps_comparable(
        nom_fichier, normalise(nom_fichier, (racine_macos / nom_fichier).read_text(encoding="utf-8"))
    )
    return "".join(
        difflib.unified_diff(
            linux.splitlines(keepends=True),
            macos.splitlines(keepends=True),
            fromfile=f"deploy/{nom_fichier} (référence, normalisée)",
            tofile=f"deploy-macos/{nom_fichier} (normalisée)",
        )
    )


def extraire_fonction(texte: str, nom: str) -> str:
    """Corps d'une fonction bash `nom() {` ... `}` en colonne 0."""
    match = re.search(
        rf"^{re.escape(nom)}\(\) \{{$\n(.*?)^\}}$", texte, re.MULTILINE | re.DOTALL
    )
    if match is None:
        raise AssertionError(
            f"Fonction {nom}() introuvable (style attendu : '{nom}() {{' en colonne 0, "
            "fermeture '}' en colonne 0)"
        )
    return match.group(0)


def diff_fonction(
    nom_fichier: str,
    nom_fonction: str,
    racine_linux: Path = ARBRE_LINUX,
    racine_macos: Path = ARBRE_MACOS,
) -> str:
    """Diff unifié d'une fonction partagée après normalisation. Vide = parité."""
    linux = normalise(
        nom_fichier,
        extraire_fonction((racine_linux / nom_fichier).read_text(encoding="utf-8"), nom_fonction),
    )
    macos = normalise(
        nom_fichier,
        extraire_fonction((racine_macos / nom_fichier).read_text(encoding="utf-8"), nom_fonction),
    )
    return "".join(
        difflib.unified_diff(
            linux.splitlines(keepends=True),
            macos.splitlines(keepends=True),
            fromfile=f"deploy/{nom_fichier}::{nom_fonction} (référence, normalisée)",
            tofile=f"deploy-macos/{nom_fichier}::{nom_fonction} (normalisée)",
        )
    )


def fichiers_communs() -> set[str]:
    """Intersection exacte des deux arborescences (dotfiles exclus).

    Les dotfiles (.DS_Store déposé par Finder, etc.) ne sont pas des artefacts
    du dépôt : les compter ferait échouer le manifeste pour rien.
    """
    linux = {
        p.name for p in ARBRE_LINUX.iterdir() if p.is_file() and not p.name.startswith(".")
    }
    macos = {
        p.name for p in ARBRE_MACOS.iterdir() if p.is_file() and not p.name.startswith(".")
    }
    return linux & macos


def manifeste_complet() -> set[str]:
    return set(MIROIRS) | set(FONCTIONS_PARTAGEES) | set(DIVERGENCES_PAR_NATURE)


# ── Le garde ──────────────────────────────────────────────────────────────────


def test_tout_fichier_commun_est_declare_dans_le_manifeste_et_inversement() -> None:
    """Le manifeste couvre exactement l'intersection des deux arbres.

    Un nouveau fichier dupliqué non déclaré échoue ici : c'est la protection
    contre la dérive future. Un fichier supprimé d'un arbre doit sortir du
    manifeste.
    """
    communs = fichiers_communs()
    declares = manifeste_complet()
    non_declares = sorted(communs - declares)
    fantomes = sorted(declares - communs)
    assert not non_declares, (
        f"Fichiers présents dans deploy/ ET deploy-macos/ mais absents du manifeste "
        f"de test_deploy_trees_parity.py : {non_declares}. Déclare-les : miroir si "
        f"identifiables, fonctions partagées ou divergence justifiée sinon."
    )
    assert not fantomes, (
        f"Fichiers déclarés dans le manifeste mais qui ne sont plus communs aux deux "
        f"arbres : {fantomes}. Retire-les du manifeste."
    )


@pytest.mark.parametrize("nom_fichier", sorted(MIROIRS))
def test_les_fichiers_miroirs_sont_identiques_apres_normalisation(nom_fichier: str) -> None:
    """Un fichier miroir doit être identique des deux côtés, chemins normalisés."""
    diff = diff_miroir(nom_fichier)
    assert diff == "", (
        f"Les copies de {nom_fichier} ont divergé (issue #29). {ARBRE_LINUX.name}/ "
        f"fait foi : porte le changement sur l'autre copie, ou si la divergence est "
        f"devenue légitime, documente-la dans le manifeste.\n{diff}"
    )


@pytest.mark.parametrize(
    ("nom_fichier", "nom_fonction"),
    [(fichier, fct) for fichier, fcts in sorted(FONCTIONS_PARTAGEES.items()) for fct in fcts],
)
def test_les_fonctions_partagees_sont_identiques_apres_normalisation(
    nom_fichier: str, nom_fonction: str
) -> None:
    """Une fonction partagée doit garder le même corps des deux côtés."""
    diff = diff_fonction(nom_fichier, nom_fonction)
    assert diff == "", (
        f"{nom_fichier}::{nom_fonction}() a divergé (issue #29). {ARBRE_LINUX.name}/ "
        f"fait foi : porte le changement sur l'autre copie, ou retire la fonction "
        f"des fonctions partagées en justifiant la divergence.\n{diff}"
    )


@pytest.mark.parametrize("nom_fichier", sorted(DIVERGENCES_PAR_NATURE))
def test_les_divergences_declarees_sont_justifiees(nom_fichier: str) -> None:
    """Une exception sans justification substantielle n'est pas une exception."""
    justification = DIVERGENCES_PAR_NATURE[nom_fichier]
    assert len(justification.strip()) >= 80, (
        f"La justification de divergence de {nom_fichier} est trop courte pour "
        f"constituer une exception documentée."
    )


# ── Invariants transverses (fichiers divergents par nature) ──────────────────


def test_chaque_update_execute_le_smoke_test_de_son_arbre() -> None:
    """La gate smoke test d'un update.sh exécute la copie de SON arbre.

    Défaut réel trouvé par le garde : deploy-macos/update.sh exécutait
    $SCRIPT_DIR/deploy/smoke_test.sh — la copie Linux — donc validait un autre
    artefact que celui déployé.
    """
    attendu = {(ARBRE_LINUX, "update.sh"): "deploy", (ARBRE_MACOS, "update.sh"): "deploy-macos"}
    for (arbre, nom_fichier), arbre_attendu in attendu.items():
        texte = (arbre / nom_fichier).read_text(encoding="utf-8")
        match = re.search(r'^SMOKE_TEST_SCRIPT="\$SCRIPT_DIR/(\S+)/smoke_test\.sh"$', texte, re.M)
        assert match, (
            f"{arbre.name}/{nom_fichier} ne définit plus SMOKE_TEST_SCRIPT au format attendu "
            f"('SMOKE_TEST_SCRIPT=\"$SCRIPT_DIR/<arbre>/smoke_test.sh\"')"
        )
        assert match.group(1) == arbre_attendu, (
            f"{arbre.name}/{nom_fichier} exécute le smoke test de l'arbre "
            f"'{match.group(1)}' au lieu de '{arbre_attendu}' : la gate validerait un "
            f"artefact différent de celui déployé."
        )


def test_le_signal_sec002_est_porte_par_les_deux_update_sh() -> None:
    """Les deux update.sh signalent les durcissements SEC-002 manquants.

    Sur un hôte installé avant SEC-002, le fichier d'environnement n'a jamais
    été régénéré (update ne régénère JAMAIS l'env) : les clés de durcissement
    peuvent manquer. La copie Linux signale, la copie macOS n'avait ni le
    tableau ni la boucle — écart fonctionnel corrigé et verrouillé ici.
    """
    for env_template in (ARBRE_LINUX / "env-template-lib.sh", ARBRE_MACOS / "env-template-lib.sh"):
        texte = env_template.read_text(encoding="utf-8")
        match = re.search(r"^DEPLOY_HARDENING_KEYS=\(([^)]*)\)$", texte, re.M)
        assert match, f"{env_template.name} (arbre {env_template.parent.name}) doit définir DEPLOY_HARDENING_KEYS"
    contenu_linux = re.search(
        r"^DEPLOY_HARDENING_KEYS=\(([^)]*)\)$", (ARBRE_LINUX / "env-template-lib.sh").read_text(encoding="utf-8"), re.M
    ).group(1)
    contenu_macos = re.search(
        r"^DEPLOY_HARDENING_KEYS=\(([^)]*)\)$", (ARBRE_MACOS / "env-template-lib.sh").read_text(encoding="utf-8"), re.M
    ).group(1)
    assert contenu_linux.split() == contenu_macos.split(), (
        "Les deux env-template-lib.sh doivent lister exactement les mêmes clés "
        "de durcissement."
    )
    for arbre in (ARBRE_LINUX, ARBRE_MACOS):
        texte = (arbre / "update.sh").read_text(encoding="utf-8")
        assert "DEPLOY_HARDENING_KEYS[@]" in texte, (
            f"{arbre.name}/update.sh n'utilise plus DEPLOY_HARDENING_KEYS : le "
            f"signal SEC-002 sur environnement antérieur a disparu."
        )
        arbre_attendu = "deploy-macos" if arbre is ARBRE_MACOS else "deploy"
        assert f'source "$SCRIPT_DIR/{arbre_attendu}/env-template-lib.sh"' in texte, (
            f"{arbre.name}/update.sh doit sourcer env-template-lib.sh de son arbre "
            f"(fournit DEPLOY_HARDENING_KEYS)."
        )


def test_les_cles_de_durcissement_sec002_sont_posees_par_les_deux_env_template() -> None:
    """Le heredoc des deux env-template-lib.sh pose les trois clés SEC-002.

    La présence ET la valeur sûre par défaut sont verrouillées : un défaut durci
    d'un seul côté (CORS ouvert, build plancher positif) ne doit jamais pouvoir
    diverger en silence entre les deux arbres.
    """
    valeurs_attendues = {
        "ALLOWED_MODEL_DIRS": r"^ALLOWED_MODEL_DIRS=\$\{allowed_dirs\}$",
        "CORS_ALLOW_ORIGINS": r"^CORS_ALLOW_ORIGINS=$",
        "LLAMA_SERVER_MIN_BUILD": r"^LLAMA_SERVER_MIN_BUILD=0$",
    }
    for cle, motif in valeurs_attendues.items():
        for arbre in (ARBRE_LINUX, ARBRE_MACOS):
            texte = (arbre / "env-template-lib.sh").read_text(encoding="utf-8")
            assert re.search(motif, texte, re.M), (
                f"{arbre.name}/env-template-lib.sh : la clé de durcissement {cle} "
                f"ne respecte plus la valeur attendue (motif : {motif}). Un "
                f"resserrement de défaut doit être porté des DEUX côtés."
            )


def test_la_cle_waiver_gpu_est_identique_des_deux_cotes() -> None:
    """Le contrat doctor/preflight sur le waiver GPU ne doit pas diverger."""
    ligne_attendue = 'GPU_WAIVER_ENV_KEY="ALLOW_NO_GPU"'
    for arbre in (ARBRE_LINUX, ARBRE_MACOS):
        texte = (arbre / "gpu-preflight-lib.sh").read_text(encoding="utf-8")
        assert ligne_attendue in texte, (
            f"{arbre.name}/gpu-preflight-lib.sh doit déclarer {ligne_attendue} "
            f"(lue EXACTEMENT par gateway/doctor.py)."
        )


def test_aucun_chemin_linux_ne_reside_dans_l_arbre_macos() -> None:
    """Résidu de copie interdit : l'arbre macOS ne référence pas les chemins Linux.

    Contrôle positif direct : la liste des scripts est exigée non vide avant la
    boucle, pour qu'un arbre renommé ou un checkout partiel échoue ici plutôt
    que de passer en silence.
    """
    residus: list[str] = []
    scripts = sorted(ARBRE_MACOS.glob("*.sh"))
    assert scripts, "Aucun script dans deploy-macos/ : le contrôle ne verrait rien."
    for script in scripts:
        for motif in ("/etc/llm-gateway", "/var/lib/llm-gateway", "/opt/llm-gateway"):
            if motif in script.read_text(encoding="utf-8"):
                residus.append(f"{script.name}: {motif}")
    assert not residus, (
        f"Chemins Linux résiduels dans deploy-macos/ (résidus de copie) : {residus}"
    )


def test_les_fonctions_de_mode_existent_des_deux_cotes_avec_leur_contrat() -> None:
    """deploy-mode-lib.sh : mêmes fonctions, contrats plateformes opposés."""
    fonctions_attendues = (
        "deploy_env_value",
        "deploy_validate_mode",
        "deploy_set_env_value",
        "deploy_secret_is_missing",
        "deploy_apply_mode",
    )
    for nom in fonctions_attendues:
        for arbre in (ARBRE_LINUX, ARBRE_MACOS):
            texte = (arbre / "deploy-mode-lib.sh").read_text(encoding="utf-8")
            assert re.search(rf"^{nom}\(\) \{{", texte, re.M), (
                f"{arbre.name}/deploy-mode-lib.sh doit définir {nom}()."
            )
    macos = (ARBRE_MACOS / "deploy-mode-lib.sh").read_text(encoding="utf-8")
    assert "Mode cluster non supporté sur macOS" in macos, (
        "deploy-mode-lib.sh macOS doit refuser le mode cluster (contrat macOS)."
    )
    linux_set_env = extraire_fonction(
        (ARBRE_LINUX / "deploy-mode-lib.sh").read_text(encoding="utf-8"), "deploy_set_env_value"
    )
    assert "mktemp" in linux_set_env and "mv -f" in linux_set_env, (
        "deploy_set_env_value Linux doit rester atomique (mktemp + mv -f) : "
        "une écriture in-place exposerait un env partiellement écrit."
    )


def test_les_scripts_des_deux_arbres_sont_syntaxiquement_valides() -> None:
    """bash -n sur chaque script des deux arbres.

    Limites : `bash -n` attrape la syntaxe, pas la sémantique de version. Les
    runners CI tournent sous bash 5.x alors que l'arbre macOS cible `/bin/bash`
    3.2 : les constructions bash-4+ (`mapfile`, `declare -A`, `${var,,}`) ne
    sont donc pas tolérées dans deploy-macos/, même si ce test les accepterait.
    Aucune aujourd'hui ; si le besoin arrive, passer par l'arbre de référence
    Linux ou documenter un équivalent bash 3.2.

    Redondant avec ci.yml
    volontairement : le garde reste vrai même hors CI.
    """
    echecs: list[str] = []
    scripts = sorted(ARBRE_LINUX.glob("*.sh")) + sorted(ARBRE_MACOS.glob("*.sh"))
    assert scripts, "Aucun script trouvé : les deux arbres doivent exister pour que ce test voie quoi que ce soit."
    for script in scripts:
        proc = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            echecs.append(f"{script}: {proc.stderr.strip()}")
    assert not echecs, "Erreurs de syntaxe bash :\n" + "\n".join(echecs)


# ── Paires sémantiques : invariants communs aux fichiers de noms différents ───

# Routes qui peuvent déclencher un chargement de modèle (COR-009) : le proxy
# doit leur accorder au moins `load_timeout_seconds + 10` du pire modèle du
# registre. /v1/models et /ready ne chargent rien : elles doivent juste rester
# proxifiées (30 s leur suffit, des deux côtés).
ROUTES_DE_CHARGEMENT = (
    "/admin/models/llama-3.3-70b-instruct/load",
    "/v1/chat/completions",
    "/v1/completions",
    "/completion",
)
ROUTES_SANS_CHARGEMENT = ("/v1/models", "/ready")


def _sans_commentaires_nginx(texte: str) -> str:
    """Retire les commentaires `# …` (pleine ligne ET fin de ligne).

    Indispensable à toute assertion d'absence sur ces fichiers (règle AGENTS.md)
    : le conf Linux explique justement, en commentaires, pourquoi « listen …
    ssl http2 » est interdit — chercher la chaîne brute rendrait le garde
    faussement rouge.
    """
    return re.sub(r"#[^\n]*", "", texte)


def _statements_nginx(texte: str) -> list[tuple[str, tuple[str, ...]]]:
    """Statements nginx `(directive, arguments)`, commentaires retirés.

    Copie locale de `parse_statements` (tests/test_nginx_conf.py) : dupliquée
    volontairement pour ne pas coupler les deux suites de test.
    """
    statements: list[tuple[str, tuple[str, ...]]] = []
    for chunk in re.split(r"[;{}]", _sans_commentaires_nginx(texte)):
        words = chunk.split()
        if words:
            statements.append((words[0], tuple(words[1:])))
    return statements


def _normaliser_chemins_logs(texte: str) -> str:
    """Chemins de logs vers une forme canonique (divergence Homebrew admise).

    `/opt/homebrew/var/log/nginx` CONTIENT `/var/log/nginx` : remplacer le
    préfixe le plus long d'abord.
    """
    return texte.replace("/opt/homebrew/var/log/nginx", "<LOG_DIR>").replace(
        "/var/log/nginx", "<LOG_DIR>"
    )


def _normaliser_espace(texte: str) -> str:
    return " ".join(texte.split())


def _bloc_map(texte: str, source: str, cible: str) -> str | None:
    """Corps d'un bloc `map <source> <cible> { … }`, commentaires retirés."""
    match = re.search(
        rf"map\s+{re.escape(source)}\s+{re.escape(cible)}\s*\{{([^}}]*)\}}",
        _sans_commentaires_nginx(texte),
    )
    return match.group(1) if match else None


def _log_format_redige(texte: str) -> str | None:
    """Le `log_format eva_redacted` livré, normalisé (espace, chemins de logs)."""
    match = re.search(
        r"log_format\s+eva_redacted\b(.*?);", _sans_commentaires_nginx(texte), re.DOTALL
    )
    if match is None:
        return None
    return _normaliser_espace(_normaliser_chemins_logs(match.group(1)))


def _serveur_proxifiant(texte: str):
    """L'unique bloc `server` qui proxifie vers la gateway, ou None.

    Le conf Linux porte deux blocs (443 + redirection port 80) ; seul le 443
    proxifie. Le conf macOS n'en porte qu'un, qui fait les deux. On ne peut pas
    s'appuyer sur `ssl_certificate` comme le fait test_nginx_conf.py : la copie
    macOS n'a pas de TLS actif par défaut.
    """
    import doctor  # import tardif : conftest prépare l'environnement

    proxifiants = [
        serveur
        for serveur in doctor.parse_nginx_servers(texte)
        if any("proxy_pass" in location.directives for location in serveur.locations)
        or "proxy_pass" in serveur.directives
    ]
    return proxifiants[0] if len(proxifiants) == 1 else None


def _timeout_requis() -> int:
    """Exigence du registre livré (COR-009), dérivée comme pour le conf Linux.

    Réutilise le calcul de `tests/test_nginx_conf.py` : une seule définition de
    la formule `load_timeout_seconds + 10` du pire modèle, deux consommateurs.
    """
    from tests.test_nginx_conf import _required_timeout_seconds

    requis, _modeles = _required_timeout_seconds()
    return requis


def divergences_semantiques(
    texte_linux: str, texte_macos: str, timeout_requis: int
) -> list[str]:
    """Invariants communs aux deux confs nginx ; retourne la liste des manquements.

    Pas un diff texte : les deux fichiers divergent légitimement (bloc TLS,
    allowlist admin, chemins Homebrew). On compare ce qui doit être IDENTIQUE
    des deux côtés (rédaction SEC-016, body size, statuts 429, keepalive
    upstream) et ce qui doit être COUVERT DES DEUX CÔTÉS (timeouts dérivés du
    registre, error_log crit sur /admin/, aucun http2 actif).
    """
    import doctor  # import tardif : conftest prépare l'environnement

    problemes: list[str] = []
    cotes = (("Linux", texte_linux), ("macOS", texte_macos))

    # ── Body size : prompts longs, même valeur des deux côtés ─────────────────
    for etiquette, texte in cotes:
        valeurs = {
            args[0]
            for nom, args in _statements_nginx(texte)
            if nom == "client_max_body_size" and args
        }
        if not valeurs:
            problemes.append(f"{etiquette}: client_max_body_size absent")
        elif valeurs != {"10m"}:
            problemes.append(
                f"{etiquette}: client_max_body_size={sorted(valeurs)} != 10m "
                "(prompts longs)"
            )

    # ── Timeouts dérivés du registre (COR-009) ────────────────────────────────
    for etiquette, texte in cotes:
        serveur = _serveur_proxifiant(texte)
        if serveur is None:
            problemes.append(
                f"{etiquette}: zéro ou plusieurs blocs server proxifiants — le "
                "contrôle ne saurait quel bloc aligner"
            )
            continue
        for chemin in ROUTES_DE_CHARGEMENT:
            location = doctor.match_location(serveur.locations, chemin)
            if location is None or "proxy_pass" not in location.directives:
                problemes.append(f"{etiquette}: {chemin} non proxifié (repli 404 ?)")
                continue
            for directive in ("proxy_read_timeout", "proxy_send_timeout"):
                brut = serveur.effective(location, directive)
                secondes = doctor.parse_nginx_time(brut) if brut else None
                if secondes is None or secondes < timeout_requis:
                    problemes.append(
                        f"{etiquette}: {directive}={brut!r} sur {chemin} < "
                        f"{timeout_requis}s exigés par le registre (COR-009)"
                    )
        for chemin in ROUTES_SANS_CHARGEMENT:
            location = doctor.match_location(serveur.locations, chemin)
            if location is None or "proxy_pass" not in location.directives:
                problemes.append(f"{etiquette}: {chemin} non proxifié (repli 404 ?)")

    # ── Rédaction du journal d'accès (SEC-016) ────────────────────────────────
    for etiquette, texte in cotes:
        for source, cible in (("$uri", "$eva_log_path"), ("$args", "$eva_log_args")):
            if _bloc_map(texte, source, cible) is None:
                problemes.append(f"{etiquette}: map {source} → {cible} absent (SEC-016)")
        gabarit = _log_format_redige(texte)
        if gabarit is None:
            problemes.append(f"{etiquette}: log_format eva_redacted absent (SEC-016)")
        elif "$eva_log_path" not in gabarit or "$eva_log_args" not in gabarit:
            problemes.append(
                f"{etiquette}: log_format eva_redacted n'emploie pas les maps de "
                "rédaction — l'URI brute serait journalisée"
            )
    format_linux = _log_format_redige(texte_linux)
    format_macos = _log_format_redige(texte_macos)
    if format_linux is not None and format_macos is not None and format_linux != format_macos:
        problemes.append(
            f"log_format eva_redacted divergent : Linux={format_linux!r} "
            f"macOS={format_macos!r} (une seule politique de rédaction, pas deux)"
        )
    for source, cible in (("$uri", "$eva_log_path"), ("$args", "$eva_log_args")):
        corps_linux = _bloc_map(texte_linux, source, cible)
        corps_macos = _bloc_map(texte_macos, source, cible)
        if corps_linux is not None and corps_macos is not None:
            if _normaliser_espace(corps_linux) != _normaliser_espace(corps_macos):
                problemes.append(
                    f"map {source} → {cible} divergente entre les deux arbres : "
                    "la liste d'autorisation des paramètres doit rester identique"
                )

    # ── Statuts de rate-limiting : 429 et pas le défaut 503 ───────────────────
    for etiquette, texte in cotes:
        for directive in ("limit_req_status", "limit_conn_status"):
            valeurs = {
                args[0]
                for nom, args in _statements_nginx(texte)
                if nom == directive and args
            }
            if valeurs != {"429"}:
                problemes.append(
                    f"{etiquette}: {directive}={sorted(valeurs) or 'absent'} — "
                    "attendu 429 uniquement (503 ne distinguerait pas la saturation "
                    "d'une erreur serveur)"
                )

    # ── error_log crit sur /admin/ (SEC-016) ──────────────────────────────────
    for etiquette, texte in cotes:
        admin = None
        for serveur in doctor.parse_nginx_servers(texte):
            location = doctor.match_location(serveur.locations, "/admin/status")
            if location is not None and location.pattern == "/admin/":
                admin = location
                break
        if admin is None:
            problemes.append(f"{etiquette}: location /admin/ introuvable")
            continue
        if admin.directives.get("deny") != "all":
            problemes.append(f"{etiquette}: /admin/ sans « deny all »")
        error_log = admin.directives.get("error_log")
        if error_log is None or error_log.split()[-1] != "crit":
            problemes.append(
                f"{etiquette}: /admin/ sans « error_log … crit » — le journal "
                f"d'erreur nginx recopie l'URI brute (SEC-016) : {error_log!r}"
            )

    # ── Keepalive upstream : Connection "" requis, jamais « upgrade » ─────────
    for etiquette, texte in cotes:
        headers = [
            args
            for nom, args in _statements_nginx(texte)
            if nom == "proxy_set_header" and len(args) >= 2
        ]
        if any(
            args[0] == "Connection" and "upgrade" in args[1].lower() for args in headers
        ):
            problemes.append(
                f'{etiquette}: proxy_set_header Connection "upgrade" — casse le '
                "keepalive upstream (aucun websocket dans cette API)"
            )
        if any(args[0] == "Upgrade" for args in headers):
            problemes.append(
                f"{etiquette}: proxy_set_header Upgrade — aucun websocket dans "
                "cette API (résidu d'une recette copiée)"
            )
        if not any(args[0] == "Connection" and args[1] == '""' for args in headers):
            problemes.append(
                f'{etiquette}: aucun proxy_set_header Connection "" — keepalive '
                "upstream mort (keepalive 32 inutilisé)"
            )

    # ── Aucun http2 actif (OPS-009) ───────────────────────────────────────────
    # Le texte est lu SANS ses lignes commentées : la recette HTTPS désactivée
    # (macOS) et le commentaire OPS-009 du conf Linux mentionnent tous deux
    # « http2 » sans l'activer.
    for etiquette, texte in cotes:
        for directive, args in _statements_nginx(texte):
            if directive == "http2":
                problemes.append(
                    f"{etiquette}: directive « http2 » active — inexistante avant "
                    "nginx 1.25.1, le service refuserait de démarrer (OPS-009)"
                )
            if directive == "listen" and "http2" in args:
                problemes.append(
                    f"{etiquette}: listen … http2 — déprécié depuis nginx 1.25.1 "
                    "(OPS-009)"
                )
    return problemes


def test_les_paires_semantiques_pointent_sur_des_fichiers_non_communs() -> None:
    """Le manifeste des paires doit rester disjoint de l'intersection par nom.

    Une paire dont les deux noms existent des deux côtés relève du mécanisme
    miroir/divergence, pas de `PAIRES_SEMANTIQUES` ; un nom inexistant ferait
    passer le garde pour une comparaison vide.
    """
    communs = fichiers_communs()
    for canonique, (nom_linux, nom_macos) in sorted(PAIRES_SEMANTIQUES.items()):
        assert (ARBRE_LINUX / nom_linux).is_file(), f"{canonique}: {nom_linux} absent"
        assert (ARBRE_MACOS / nom_macos).is_file(), f"{canonique}: {nom_macos} absent"
        assert nom_linux not in communs and nom_macos not in communs, (
            f"{canonique}: les deux noms existent des deux arbres — sort "
            f"{nom_linux}/{nom_macos} de PAIRES_SEMANTIQUES et déclare-les dans le "
            "manifeste par nom (miroir ou divergence justifiée)."
        )


@pytest.mark.parametrize("canonique", sorted(PAIRES_SEMANTIQUES))
def test_les_paires_semantiques_partagent_leurs_invariants(canonique: str) -> None:
    """Garde les invariants communs d'une paire de fichiers de noms différents.

    Le mécanisme par nom ne voit pas ces fichiers : sans ce test, la copie macOS
    pouvait dériver en silence — c'est arrivé (voir PAIRES_SEMANTIQUES).
    """
    nom_linux, nom_macos = PAIRES_SEMANTIQUES[canonique]
    texte_linux = (ARBRE_LINUX / nom_linux).read_text(encoding="utf-8")
    texte_macos = (ARBRE_MACOS / nom_macos).read_text(encoding="utf-8")
    problemes = divergences_semantiques(texte_linux, texte_macos, _timeout_requis())
    assert not problemes, (
        f"La paire sémantique {canonique} a dérivé ({ARBRE_LINUX.name}/{nom_linux} vs "
        f"{ARBRE_MACOS.name}/{nom_macos}). Porte l'invariant sur la copie fautive, ou "
        f"documente la divergence dans le manifeste :\n- " + "\n- ".join(problemes)
    )


def test_le_garde_semantique_refuse_les_mutations_connues() -> None:
    """Contrôle positif du garde sémantique (règle AGENTS.md sur les absences).

    On réinjecte, un par un, les défauts qui ont motivé l'appariement — timeout
    court sur /admin/ (504 au chargement), « Connection "upgrade" » (keepalive
    upstream mort), `$request` brut dans le format (usernames en clair) — plus
    un body size rétréci. Chaque mutation doit produire une divergence NOMMÉE,
    et la copie conforme n'en produit aucune.
    """
    texte_linux = (ARBRE_LINUX / "nginx.conf").read_text(encoding="utf-8")
    texte_macos = (ARBRE_MACOS / "nginx.conf.macOS").read_text(encoding="utf-8")
    requis = _timeout_requis()

    # Faux positif : la copie conforme doit passer sans aucune divergence.
    assert divergences_semantiques(texte_linux, texte_macos, requis) == [], (
        "Faux positif : la copie conforme est rapportée divergente."
    )

    def bloc_admin() -> str:
        match = re.search(r"location\s+/admin/\s*\{.*?\n    \}", texte_macos, re.DOTALL)
        assert match, "bloc /admin/ macOS introuvable : le contrôle ne verrait rien"
        return match.group(0)

    # 1. Timeout court sur /admin/ — le défaut d'origine de la copie macOS.
    bloc = bloc_admin()
    timeout_mute = texte_macos.replace(bloc, bloc.replace("900s", "300s"))
    assert timeout_mute != texte_macos, "mutation du timeout non injectée"
    problemes = divergences_semantiques(texte_linux, timeout_mute, requis)
    assert any("proxy_read_timeout" in p and "300s" in p for p in problemes), problemes

    # 2. « Connection "upgrade" » sur /admin/ — casse le keepalive upstream.
    connection_mute = texte_macos.replace(
        bloc,
        bloc.replace(
            'proxy_set_header   Connection        "";',
            'proxy_set_header   Connection        "upgrade";',
        ),
    )
    assert connection_mute != texte_macos, "mutation Connection non injectée"
    problemes = divergences_semantiques(texte_linux, connection_mute, requis)
    assert any('"upgrade"' in p and "macOS" in p for p in problemes), problemes

    # 3. `$request` brut réintroduit dans le format — usernames en clair.
    request_mute = texte_macos.replace(
        '"$request_method $eva_log_path$eva_log_args $server_protocol" ',
        '"$request_method $request $server_protocol" ',
    )
    assert request_mute != texte_macos, "mutation du log_format non injectée"
    problemes = divergences_semantiques(texte_linux, request_mute, requis)
    assert any("log_format eva_redacted divergent" in p for p in problemes), problemes

    # 4. Body size rétréci d'un seul côté — prompts longs refusés sur macOS.
    body_mute = texte_macos.replace(
        "client_max_body_size 10m;", "client_max_body_size 1m;"
    )
    assert body_mute != texte_macos, "mutation du body size non injectée"
    problemes = divergences_semantiques(texte_linux, body_mute, requis)
    assert any(
        "client_max_body_size" in p and p.startswith("macOS") for p in problemes
    ), problemes


# ── Contrôle positif : le garde voit réellement les divergences ──────────────


def test_le_garde_detecte_une_mutation_sur_une_copie(tmp_path: Path) -> None:
    """Un test d'absence sans contrôle positif peut devenir inerte (AGENTS.md).

    On copie les deux arbres dans un tmp_path, on vérifie d'abord que la copie
    conforme ne déclenche rien (pas de faux positif), puis on injecte une
    mutation unilatérale dans la copie macOS : le garde doit la voir et la
    citer. Même protocole sur une fonction partagée.
    """
    faux_linux = tmp_path / "deploy"
    faux_macos = tmp_path / "deploy-macos"
    faux_linux.mkdir()
    faux_macos.mkdir()

    (faux_linux / "smoke_test.sh").write_text(
        (ARBRE_LINUX / "smoke_test.sh").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (faux_macos / "smoke_test.sh").write_text(
        (ARBRE_MACOS / "smoke_test.sh").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert diff_miroir("smoke_test.sh", faux_linux, faux_macos) == "", (
        "Faux positif : une copie conforme est rapportée divergente."
    )

    marque_mutation = "MUTATION_INJECTEE_PAR_LE_TEST_DE_PARITE=1"
    (faux_macos / "smoke_test.sh").write_text(
        (ARBRE_MACOS / "smoke_test.sh").read_text(encoding="utf-8") + f"\n{marque_mutation}\n"
    )
    diff = diff_miroir("smoke_test.sh", faux_linux, faux_macos)
    assert diff != "" and marque_mutation in diff, (
        "Le garde ne détecte pas une mutation unilatérale : il est inerte."
    )

    # Même protocole sur une fonction partagée.
    texte_linux = (ARBRE_LINUX / "code-layout-lib.sh").read_text(encoding="utf-8")
    texte_macos = (ARBRE_MACOS / "code-layout-lib.sh").read_text(encoding="utf-8")
    assert diff_fonction("code-layout-lib.sh", "_deploy_code_safe_roots").strip() == ""
    mutee = texte_macos.replace(
        'echo "$operation refusée : racines source/cible dangereuses ou identiques" >&2',
        'echo "MUTATION_INJECTEE_PAR_LE_TEST_DE_PARITE" >&2',
    )
    assert mutee != texte_macos, "La mutation n'a pas pu être injectée (ancre introuvable)."
    (faux_linux / "code-layout-lib.sh").write_text(texte_linux, encoding="utf-8")
    (faux_macos / "code-layout-lib.sh").write_text(mutee, encoding="utf-8")

    diff_fonction_mutee = diff_fonction(
        "code-layout-lib.sh", "_deploy_code_safe_roots", faux_linux, faux_macos
    )
    assert "MUTATION_INJECTEE_PAR_LE_TEST_DE_PARITE" in diff_fonction_mutee, (
        "Le comparateur de fonctions partagées ne voit pas une mutation du corps."
    )
