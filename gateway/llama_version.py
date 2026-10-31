"""
Sonde de version du binaire llama-server — mitigation supply-chain.

Contexte menace : plusieurs CVEs 2025-2026 touchent llama-server (écriture OOB
non authentifiée via `n_discard`/context-shift — GHSA-8947-pfff-2f3c —, overflows
de parsing GGUF menant au RCE). Épingler un build minimal patché permet de refuser
de démarrer sur un binaire vulnérable connu.

Ce module est volontairement partagé entre la gateway et le node_agent (qui ajoute
gateway/ à son sys.path, donc `from llama_version import ...` fonctionne des deux
côtés). Il n'ajoute aucune dépendance : subprocess + re, stdlib uniquement.

Politique (SEC-009) : **fail-closed dès qu'un plancher est exigé**. Tant que
`llama_server_min_build == 0`, aucun enforcement n'est demandé et une version
illisible reste un simple avertissement. Mais dès que l'opérateur exige un build
minimal, une version illisible est un refus : on ne peut pas prouver que le binaire
est patché, donc on ne sert pas dessus.

Cette sémantique est celle de `doctor.check_llama_server_version` et de
`bootstrap.runtime_installer._verdict_version`. Elle l'est désormais aussi ici :
avant SEC-009, une gateway démarrée sans passer par `doctor` pouvait servir sur un
binaire inattestable, parce que ce module se contentait d'un `log.warning`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# llama.cpp écrit sur stderr une ligne de build canonique :
#   version: 4567 (abc1234)
# ou parfois « build: 4567 (abc1234) », suivie de « built with cc … ».
#
# SEC-009 : l'ancien motif cherchait `\b(?:version|build)…` n'importe où dans la
# sortie et prenait le PREMIER résultat. Sur un build CUDA, `--version` est
# précédé de lignes d'initialisation de backend (`ggml_cuda_init: …`,
# `load_backend: …`, `register_backend: …`) ; il suffisait qu'une seule d'entre
# elles contienne « version » ou « build » suivi d'un nombre pour que la sonde
# rende un numéro parasite — potentiellement sous le plancher (faux refus), ou
# au-dessus (attestation mensongère). Deux corrections :
#
#  1. les deux motifs sont **ancrés en début de ligne** : une ligne de trace de
#     backend ne commence jamais par « version » ou « build » ;
#  2. si plusieurs lignes candidates annoncent des numéros **différents**, la
#     sortie est ambiguë et la sonde REFUSE explicitement (retour None) plutôt
#     que de départager au hasard. Fail-closed en aval fait le reste.
#
# Depuis fin 2026, llama.cpp annonce une version sémantique et place le build
# entre parenthèses : `version: 0.8.0-dev (build 12020, commit 0ec4027a2)`. Les
# motifs historiques refusent donc un numéro suivi de `.<chiffre>` : sans cela, ce
# format se lisait « build 0 » et tout binaire récent était refusé au démarrage.
# Une version sémantique sans build ne rend rien : le plancher ne porte que sur le
# numéro de build.
_SEMVER_BUILD_LINE_RE = re.compile(
    r"^[ \t]*version[ \t]*:[ \t]*v?\d+(?:\.\d+)+\S*[ \t]*"
    r"\([ \t]*build[ \t]+(?P<build>\d+)"
    r"(?:[ \t]*,[ \t]*commit[ \t]+(?P<commit>[0-9a-fA-F]{7,40}))?[ \t]*\)",
    re.IGNORECASE | re.MULTILINE,
)

_BUILD_LINE_RE = re.compile(
    r"^[ \t]*(?:version|build)[ \t]*:[ \t]*(?P<build>\d+)(?!\d|\.\d)"
    r"(?:[ \t]*\((?P<commit>[0-9a-fA-F]{7,40})\))?",
    re.IGNORECASE | re.MULTILINE,
)

# Repli tolérant, toujours ancré : « VERSION 999 », « build = 4567 ».
_LOOSE_BUILD_RE = re.compile(
    r"^[ \t]*(?:version|build)[ \t]*[:=]?[ \t]*(?P<build>\d+)\b(?!\.\d)",
    re.IGNORECASE | re.MULTILINE,
)

# Timeout court : la sonde ne doit jamais bloquer le démarrage longtemps.
_PROBE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class LlamaVersion:
    """
    Résultat de la sonde. `build` est None si la version n'a pas pu être lue.

    `commit` porte le SHA court rendu entre parenthèses par la ligne canonique
    (`version: 4567 (abc1234)` ou `version: 0.8.0 (build 4567, commit abc1234)`).
    Il vaut None si la sortie ne le donne pas ; il
    permet de recouper un manifeste de provenance contre le binaire réel (SEC-009).
    """
    build: int | None
    raw: str  # sortie brute (tronquée) pour diagnostic
    commit: str | None = None


def parse_llama_build(output: str) -> tuple[int | None, str | None]:
    """
    Extrait `(build, commit)` d'une sortie `llama-server --version`.

    Défensif (ne lève jamais) — appelé sur une sortie non fiable de sous-processus.
    Retourne `(None, None)` si aucune ligne canonique n'est reconnue, ou si
    plusieurs lignes candidates se contredisent : mieux vaut ne rien affirmer que
    d'affirmer le mauvais numéro sur une attestation de sécurité.
    """
    if not output:
        return None, None

    canonical = [
        *_SEMVER_BUILD_LINE_RE.finditer(output),
        *_BUILD_LINE_RE.finditer(output),
    ]
    if canonical:
        builds = {m.group("build") for m in canonical}
        if len(builds) > 1:
            return None, None  # sortie ambiguë : refus explicite
        commit = next(
            (m.group("commit") for m in canonical if m.group("commit")), None
        )
        try:
            return int(canonical[0].group("build")), (commit.lower() if commit else None)
        except (ValueError, IndexError):
            return None, None

    loose = list(_LOOSE_BUILD_RE.finditer(output))
    if not loose:
        return None, None
    builds = {m.group("build") for m in loose}
    if len(builds) > 1:
        return None, None  # sortie ambiguë : refus explicite
    try:
        return int(loose[0].group("build")), None
    except (ValueError, IndexError):
        return None, None


def parse_llama_version(output: str) -> int | None:
    """
    Extrait le numéro de build entier d'une sortie `llama-server --version`.

    Tolère les formats inconnus : retourne None si aucun motif reconnu. Défensif
    (ne lève jamais) — appelé sur une sortie non fiable de sous-processus.
    """
    return parse_llama_build(output)[0]


async def probe_llama_version(binary: Path) -> LlamaVersion:
    """
    Exécute `<binary> --version` avec un timeout court et extrait le build.

    NON FATAL : attrape toute exception (FileNotFoundError, timeout, permission,
    etc.) et retourne LlamaVersion(build=None, ...). C'est à l'appelant de décider
    quoi logguer/faire selon la politique d'enforcement.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            str(binary),
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # llama.cpp écrit la version sur stderr
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return LlamaVersion(build=None, raw=f"<binaire injoignable : {exc}>")

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return LlamaVersion(build=None, raw="<timeout de la sonde --version>")
    except Exception as exc:  # défensif : jamais fatal
        return LlamaVersion(build=None, raw=f"<erreur de sonde : {exc}>")

    raw = (stdout or b"").decode("utf-8", errors="replace").strip()
    build, commit = parse_llama_build(raw)
    return LlamaVersion(build=build, raw=raw[:500], commit=commit)


async def enforce_llama_min_build(binary: Path, min_build: int) -> bool:
    """
    Sonde le binaire et applique la politique d'épinglage de version.

    Retourne True si le démarrage peut continuer, False dès qu'un enforcement
    explicite (min_build > 0) ne peut pas être prouvé satisfait.

    Comportement (SEC-009) :
      - version illisible et min_build == 0 (défaut) → log.warning, retourne True.
      - version illisible et min_build > 0           → log.critical, retourne False.
      - build lu OK, min_build == 0 (défaut)         → log.info, retourne True.
      - build lu < min_build (min_build > 0)         → log.critical, retourne False.
      - build lu ≥ min_build                         → log.info, retourne True.

    Le cas « illisible + plancher exigé » était l'écart de SEC-009 : il valait
    `log.warning` puis démarrage, alors que `doctor` refusait déjà. Un binaire qui
    ne sait pas dire ce qu'il est ne peut pas être attesté patché, et la seule
    réponse sûre à un plancher qu'on ne peut pas vérifier est le refus.
    """
    version = await probe_llama_version(binary)

    if version.build is None:
        if min_build > 0:
            log.critical(
                "LLAMA_SERVER_MIN_BUILD=%d est exigé mais la version de %s est illisible (%s). "
                "Politique fail-closed : impossible de prouver que ce binaire est patché "
                "(cf. GHSA-8947-pfff-2f3c) — DÉMARRAGE REFUSÉ. Réinstallez un llama-server dont "
                "`--version` répond, ou retirez le plancher en connaissance de cause.",
                min_build, binary, version.raw,
            )
            return False
        log.warning(
            "Version de llama-server illisible (%s) — aucun plancher exigé "
            "(LLAMA_SERVER_MIN_BUILD=0), le garde-fou supply-chain reste inerte. "
            "Binaire : %s. Fixez LLAMA_SERVER_MIN_BUILD sur un build patché.",
            version.raw, binary,
        )
        return True

    if min_build > 0 and version.build < min_build:
        log.critical(
            "llama-server build %d < minimum requis %d (LLAMA_SERVER_MIN_BUILD). "
            "Binaire potentiellement vulnérable (cf. GHSA-8947-pfff-2f3c) — DÉMARRAGE REFUSÉ. "
            "Mettez à jour llama.cpp ou abaissez l'enforcement.",
            version.build, min_build,
        )
        return False

    log.info(
        "llama-server build %d détecté (%s). Minimum requis : %s.",
        version.build, binary, min_build if min_build > 0 else "aucun (enforcement désactivé)",
    )
    return True
