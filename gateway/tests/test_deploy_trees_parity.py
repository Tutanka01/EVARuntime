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

Limite connue : le manifeste apparie les fichiers par **nom**. Les paires
sémantiques de noms différents — aujourd'hui `nginx.conf` (Linux) vs
`nginx.conf.macOS` — ne sont donc pas comparées ; leurs invariants communs
(SSE sans buffer, TLS) relèvent de `tests/test_nginx_http2_lib.py` et de la
revue humaine, pas de ce garde.
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
