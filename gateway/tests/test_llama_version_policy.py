"""
SEC-009, volet a — politique fail-closed unifiée de `LLAMA_SERVER_MIN_BUILD`.

Deux défauts fermés ici.

1. `enforce_llama_min_build()` autorisait le démarrage sur une version illisible
   **alors qu'un plancher était exigé**, là où `doctor.check_llama_server_version`
   refusait. Une gateway (ou un node-agent) démarré sans passer par `doctor`
   pouvait donc servir sur un binaire inattestable — exactement ce que le
   plancher est censé empêcher (GHSA-8947-pfff-2f3c).

2. `_VERSION_RE` cherchait `version|build` **n'importe où** dans la sortie
   combinée stdout+stderr et prenait le premier résultat. Un build CUDA émet des
   lignes d'initialisation de backend avant la ligne de build : il suffit qu'une
   d'elles contienne « version » suivi d'un nombre pour que la sonde rende un
   numéro parasite. L'extraction est désormais ancrée en début de ligne, et une
   sortie qui annonce plusieurs numéros différents est refusée explicitement.
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import doctor as doctor_mod
import llama_version
from llama_version import (
    LlamaVersion,
    enforce_llama_min_build,
    parse_llama_build,
    parse_llama_version,
)

BINARY = Path("/usr/local/bin/llama-server")

# Sortie réelle d'un `llama-server --version` compilé avec CUDA : la ligne de
# build est précédée des traces du registre de backends ggml.
CUDA_PREAMBLE = (
    "ggml_cuda_init: GGML_CUDA_FORCE_MMQ:    no\n"
    "ggml_cuda_init: found 1 CUDA devices, driver version 12040\n"
    "  Device 0: NVIDIA L40S, compute capability 8.9, VMM: yes\n"
    "load_backend: loaded CUDA backend from /opt/llama/libggml-cuda.so\n"
    "version: 6120 (a1b2c3d)\n"
    "built with cc (Ubuntu 11.4.0-1ubuntu1~22.04) 11.4.0 for x86_64-linux-gnu\n"
)


# ── Extraction robuste ────────────────────────────────────────────────────────

def test_backend_init_line_does_not_masquerade_as_the_build_line() -> None:
    """
    Le motif parasite `driver version 12040` précède la vraie ligne de build.

    Avant SEC-009, la sonde rendait 12040 : un numéro largement au-dessus de tout
    plancher réaliste, donc une attestation mensongère plutôt qu'un faux refus.
    """
    assert parse_llama_version(CUDA_PREAMBLE) == 6120


def test_commit_is_read_from_the_canonical_build_line() -> None:
    assert parse_llama_build(CUDA_PREAMBLE) == (6120, "a1b2c3d")


def test_commit_is_normalised_to_lowercase() -> None:
    assert parse_llama_build("version: 6120 (A1B2C3D)")[1] == "a1b2c3d"


def test_ambiguous_output_is_refused_rather_than_guessed() -> None:
    """
    Deux lignes candidates contradictoires : la sonde ne départage pas au hasard.

    Le fail-closed en aval transforme ce None en refus. Choisir la première
    aurait rendu `1` — la signature d'un clone superficiel — sur un binaire sain,
    ou l'inverse.
    """
    assert parse_llama_version("version: 1\nversion: 6120 (aaaaaaa)") is None


def test_absence_of_a_build_line_is_still_none() -> None:
    """Contrôle positif de l'absence : le parseur sait rendre autre chose que None."""
    assert parse_llama_version("aucune ligne de build ici") is None
    assert parse_llama_version("version: 6120 (a1b2c3d)") == 6120


@pytest.mark.parametrize(
    "output, expected",
    [
        ("version: 4567 (abc1234)", 4567),
        ("build: 4567 (abc1234)", 4567),
        ("VERSION 999", 999),
        ("build = 4567", 4567),
    ],
)
def test_known_formats_survive_the_anchoring(output: str, expected: int) -> None:
    assert parse_llama_version(output) == expected


def test_le_format_semantique_de_llama_cpp_rend_le_vrai_build() -> None:
    """
    Sortie réelle du binaire officiel b12020. L'ancien motif lisait « version: 0 »,
    donc un build 0 sous tout plancher : la gateway refusait de démarrer sur le
    dernier llama.cpp, en l'accusant d'être potentiellement vulnérable.
    """
    sortie = (
        "version: 0.8.0-dev (build 12020, commit 0ec4027a2)\n"
        "built with GNU 14.2.0 for Linux aarch64\n"
    )
    assert parse_llama_build(sortie) == (12020, "0ec4027a2")
    assert parse_llama_build(CUDA_PREAMBLE.replace(
        "version: 6120 (a1b2c3d)", "version: 0.9.1 (build 6120, commit A1B2C3D)",
    )) == (6120, "a1b2c3d")


@pytest.mark.parametrize("sortie", [
    "version: 0.8.0-dev",
    "version: 0.8.0",
    "version: 10.2",
    "version 0.8.0",
    "version: 0.8.0 (commit 0ec4027a2)",
])
def test_une_version_semantique_sans_build_n_atteste_rien(sortie: str) -> None:
    """Le plancher porte sur le build : mieux vaut None qu'un numéro de version mineur."""
    assert parse_llama_version(sortie) is None


def test_le_format_semantique_ambigu_reste_refuse() -> None:
    assert parse_llama_version(
        "version: 0.8.0 (build 12020, commit 0ec4027a2)\nversion: 6120 (a1b2c3d)"
    ) is None


# ── Politique fail-closed ─────────────────────────────────────────────────────

def _probe(monkeypatch, version: LlamaVersion) -> None:
    async def fake_probe(_binary: Path) -> LlamaVersion:
        return version

    monkeypatch.setattr(llama_version, "probe_llama_version", fake_probe)


@pytest.mark.anyio
async def test_unreadable_version_refuses_startup_when_a_floor_is_required(
    monkeypatch, caplog
) -> None:
    """Le cœur de SEC-009 : plancher exigé + version illisible = refus."""
    _probe(monkeypatch, LlamaVersion(build=None, raw="<timeout de la sonde --version>"))
    with caplog.at_level(logging.CRITICAL, logger="llama_version"):
        assert await enforce_llama_min_build(BINARY, 6000) is False
    assert "fail-closed" in "\n".join(r.getMessage() for r in caplog.records)


@pytest.mark.anyio
async def test_unreadable_version_is_tolerated_when_no_floor_is_required(
    monkeypatch,
) -> None:
    """
    Contrôle positif : sans plancher, rien n'est exigé et rien n'est refusé.

    Sans ce test, un `return False` inconditionnel passerait le test précédent.
    """
    _probe(monkeypatch, LlamaVersion(build=None, raw="<binaire injoignable>"))
    assert await enforce_llama_min_build(BINARY, 0) is True


@pytest.mark.anyio
async def test_build_below_floor_still_refuses(monkeypatch) -> None:
    _probe(monkeypatch, LlamaVersion(build=5000, raw="version: 5000 (abc1234)"))
    assert await enforce_llama_min_build(BINARY, 6000) is False


@pytest.mark.anyio
async def test_build_at_or_above_floor_is_accepted(monkeypatch) -> None:
    _probe(monkeypatch, LlamaVersion(build=6000, raw="version: 6000 (abc1234)"))
    assert await enforce_llama_min_build(BINARY, 6000) is True


@pytest.mark.anyio
async def test_parasitic_build_number_no_longer_satisfies_a_floor(monkeypatch) -> None:
    """
    Bout à bout : la sortie CUDA parasite ne doit pas satisfaire un plancher à
    elle seule. Avec l'ancien motif, `driver version 12040` faisait passer le
    binaire pour un build 12040 et satisfaisait n'importe quel plancher.
    """
    build, _ = parse_llama_build(CUDA_PREAMBLE)
    _probe(monkeypatch, LlamaVersion(build=build, raw=CUDA_PREAMBLE))
    assert await enforce_llama_min_build(BINARY, 6500) is False


# ── Convergence des politiques ────────────────────────────────────────────────

def _doctor_config(tmp_path: Path, min_build: int) -> SimpleNamespace:
    """Faux binaire exécutable : `doctor` refuse de sonder ce qu'il ne peut pas lancer."""
    binaire = tmp_path / "llama-server"
    binaire.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binaire.chmod(0o755)
    return SimpleNamespace(
        llama_server_bin=str(binaire),
        llama_server_min_build=min_build,
        cluster_mode="local",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("build", [None, 5000])
async def test_doctor_and_runtime_guard_agree_on_refusal(
    monkeypatch, tmp_path, build
) -> None:
    """
    `doctor` et le garde-fou de démarrage doivent rendre le même verdict.

    C'était la divergence de SEC-009 : sur `build=None` avec plancher, `doctor`
    refusait et la gateway démarrait. Le test recoupe les deux implémentations
    sur la même sortie de sonde plutôt que de verrouiller deux comportements
    indépendamment — une divergence future casse le test.
    """
    version = LlamaVersion(build=build, raw="<sortie de test>")

    async def fake_probe(_binary: Path) -> LlamaVersion:
        return version

    monkeypatch.setattr(llama_version, "probe_llama_version", fake_probe)
    monkeypatch.setattr(doctor_mod, "probe_llama_version", fake_probe)

    verdict = await doctor_mod.check_llama_server_version(_doctor_config(tmp_path, 6000))

    assert verdict.status == "fail"
    assert await enforce_llama_min_build(BINARY, 6000) is False


@pytest.mark.anyio
async def test_doctor_and_runtime_guard_agree_on_acceptance(
    monkeypatch, tmp_path
) -> None:
    """Contrôle positif du test précédent : les deux savent aussi dire « oui »."""
    version = LlamaVersion(build=6120, raw="version: 6120 (a1b2c3d)")

    async def fake_probe(_binary: Path) -> LlamaVersion:
        return version

    monkeypatch.setattr(llama_version, "probe_llama_version", fake_probe)
    monkeypatch.setattr(doctor_mod, "probe_llama_version", fake_probe)

    verdict = await doctor_mod.check_llama_server_version(_doctor_config(tmp_path, 6000))

    assert verdict.status == "pass"
    assert await enforce_llama_min_build(BINARY, 6000) is True
