"""
Sonde de version du binaire llama-server — mitigation supply-chain.

Contexte menace : plusieurs CVEs 2025-2026 touchent llama-server (écriture OOB
non authentifiée via `n_discard`/context-shift — GHSA-8947-pfff-2f3c —, overflows
de parsing GGUF menant au RCE). Épingler un build minimal patché permet de refuser
de démarrer sur un binaire vulnérable connu.

Ce module est volontairement partagé entre la gateway et le node_agent (qui ajoute
gateway/ à son sys.path, donc `from llama_version import ...` fonctionne des deux
côtés). Il n'ajoute aucune dépendance : subprocess + re, stdlib uniquement.

Politique : NON FATAL par défaut. Si le binaire est injoignable ou sa version
illisible, on n'échoue jamais — c'est un simple avertissement. Le seul cas de refus
de démarrage est un enforcement EXPLICITE (llama_server_min_build > 0) combiné à une
version lue strictement inférieure.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# llama.cpp affiche typiquement, sur stderr, une ligne du style :
#   version: 4567 (abc1234)
# ou parfois « build: 4567 (abc1234) ». On extrait le numéro de build entier de
# façon défensive et on tolère tout format inconnu (retour None).
_VERSION_RE = re.compile(r"\b(?:version|build)\s*[:=]?\s*(\d+)\b", re.IGNORECASE)

# Timeout court : la sonde ne doit jamais bloquer le démarrage longtemps.
_PROBE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class LlamaVersion:
    """Résultat de la sonde. `build` est None si la version n'a pas pu être lue."""
    build: int | None
    raw: str  # sortie brute (tronquée) pour diagnostic


def parse_llama_version(output: str) -> int | None:
    """
    Extrait le numéro de build entier d'une sortie `llama-server --version`.

    Tolère les formats inconnus : retourne None si aucun motif reconnu. Défensif
    (ne lève jamais) — appelé sur une sortie non fiable de sous-processus.
    """
    if not output:
        return None
    match = _VERSION_RE.search(output)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (ValueError, IndexError):
        return None


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
    return LlamaVersion(build=parse_llama_version(raw), raw=raw[:500])


async def enforce_llama_min_build(binary: Path, min_build: int) -> bool:
    """
    Sonde le binaire et applique la politique d'épinglage de version.

    Retourne True si le démarrage peut continuer, False UNIQUEMENT en cas
    d'enforcement explicite violé (min_build > 0 et build lu < min_build).

    Comportement :
      - binaire injoignable / version illisible → log.warning, retourne True.
      - build lu OK, min_build == 0 (défaut)      → log.info, retourne True.
      - build lu < min_build (min_build > 0)       → log.critical, retourne False.
      - build lu ≥ min_build                       → log.info, retourne True.
    """
    version = await probe_llama_version(binary)

    if version.build is None:
        log.warning(
            "Version de llama-server illisible (%s) — sonde non fatale, on continue. "
            "Binaire : %s. Pensez à fixer LLAMA_SERVER_MIN_BUILD sur un build patché.",
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
