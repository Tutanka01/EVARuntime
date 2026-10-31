"""
AUT-004 — tests de l'adaptateur LLMfit.

STATUT DES SORTIES ENREGISTRÉES
-------------------------------
Les fichiers de `tests/fixtures/llmfit/` sont **SYNTHÉTIQUES**, tous préfixés
`synthetic-`. Aucun n'est une capture d'un LLMfit réel : la forme exacte de
`llmfit recommend --json` n'est pas documentée publiquement (le dépôt et
`docs/cli.md` annoncent « top picks as JSON » sans fixer les champs — vérifié le
31 juillet 2026). Leur forme est **dérivée de la description du plan
d'amorçage**, pas observée. Elles doivent être remplacées par de vraies
captures avant mise en production ; voir `tests/fixtures/llmfit/README.md`.

Aucun test de ce fichier n'exécute LLMfit : le binaire est absent de la machine
de développement et de la CI, ce qui est précisément le cas nominal couvert par
`test_binaire_absent_produit_un_skip`. Les cas d'exécution passent par un
exécuteur injecté ou par un faux binaire fabriqué dans un `tmp_path`.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import subprocess
from pathlib import Path

import pytest

from bootstrap import llmfit, schema

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llmfit"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _fake_binary(tmp_path: Path, content: bytes = b"#!/bin/sh\nexit 0\n") -> tuple[Path, str]:
    """Fabrique un exécutable factice et retourne son chemin et son SHA-256 réel."""
    path = tmp_path / "llmfit"
    path.write_bytes(content)
    path.chmod(0o755)
    return path, hashlib.sha256(content).hexdigest()


def _runner(stdout: bytes = b"", *, returncode: int = 0, stderr: bytes = b"", record: list | None = None):
    """Exécuteur injectable — enregistre l'argv reçu si `record` est fourni."""
    def run(argv, timeout):
        if record is not None:
            record.append((list(argv), timeout))
        return llmfit._Completed(returncode=returncode, stdout=stdout, stderr=stderr)
    return run


def _no_binary(_name: str) -> str | None:
    return None


# ── 1. Absence de LLMfit : jamais un échec ────────────────────────────────────

def test_binaire_absent_produit_un_skip():
    """
    Le cas nominal en CI et sur une machine de développement : pas de LLMfit.

    C'est le test le plus important du fichier. Un conseiller optionnel qui manque
    ne doit ni faire échouer la collecte, ni empêcher un plan d'exister.
    """
    result = llmfit.run_llmfit(llmfit.LLMfitConfig(), which=_no_binary)

    assert result.status == "skip"
    assert result.source == "none"
    assert result.recommendation is None
    codes = [f.code for f in result.findings]
    assert "llmfit_absent" in codes
    assert all(f.level != "fail" for f in result.findings)


def test_section_skip_reste_assemblable_dans_un_plan():
    """Un plan contenant la section `skip` reste valide, applicable et exportable."""
    section = llmfit.to_plan_section(llmfit.run_llmfit(llmfit.LLMfitConfig(), which=_no_binary))
    plan = schema.BootstrapPlan(generated_at="2026-07-31T09:00:00Z", mode="test", sections=(section,))

    assert schema.validate_plan_dict(plan.to_dict()) == ()
    assert plan.applicable is True
    assert plan.exit_code() == schema.EXIT_OK
    assert "recommendation" in schema.render_json(plan)


def test_binaire_present_mais_non_executable_est_traite_comme_absent(tmp_path: Path):
    path = tmp_path / "llmfit"
    path.write_bytes(b"pas un binaire executable")
    path.chmod(0o644)

    result = llmfit.run_llmfit(llmfit.LLMfitConfig(binary_path=path))

    assert result.status == "skip"
    assert [f.code for f in result.findings] == ["llmfit_absent"]


def test_desactivation_explicite_produit_un_skip():
    result = llmfit.run_llmfit(llmfit.LLMfitConfig(enabled=False))

    assert result.status == "skip"
    assert [f.code for f in result.findings] == ["llmfit_disabled"]


# ── 2. Épinglage : version et SHA-256 figés ───────────────────────────────────

def test_binaire_non_epingle_nest_pas_execute(tmp_path: Path):
    path, _ = _fake_binary(tmp_path)
    appels: list = []

    result = llmfit.run_llmfit(
        llmfit.LLMfitConfig(binary_path=path, pin=None),
        runner=_runner(record=appels),
    )

    assert result.status == "skip"
    assert [f.code for f in result.findings] == ["llmfit_pin_absent"]
    assert result.findings[0].level == "warn"
    assert appels == [], "un binaire non épinglé ne doit pas être exécuté"


def test_sha256_different_de_lepinglage_refuse_lexecution(tmp_path: Path):
    path, digest = _fake_binary(tmp_path, b"#!/bin/sh\necho binaire-substitue\n")
    pin = llmfit.LLMfitPin(version="0.6.1", sha256="0" * 64)
    appels: list = []

    result = llmfit.run_llmfit(
        llmfit.LLMfitConfig(binary_path=path, pin=pin),
        runner=_runner(record=appels),
    )

    assert result.status == "fail"
    assert [f.code for f in result.findings] == ["llmfit_sha256_mismatch"]
    assert result.findings[0].level == "fail"
    assert appels == [], "un binaire dont l'empreinte diffère ne doit JAMAIS être exécuté"
    # Le message doit être actionnable : les deux empreintes y figurent.
    assert digest in result.findings[0].message
    assert "0" * 64 in result.findings[0].message


def test_sha256_conforme_autorise_lexecution(tmp_path: Path):
    path, digest = _fake_binary(tmp_path)
    pin = llmfit.LLMfitPin(version="0.6.1", sha256=digest)
    appels: list = []

    result = llmfit.run_llmfit(
        llmfit.LLMfitConfig(binary_path=path, pin=pin),
        runner=_runner(_fixture("synthetic-nominal.json"), record=appels),
    )

    assert result.status == "ok"
    assert result.pin_verified is True
    assert result.binary_sha256 == digest
    assert len(appels) == 1


def test_version_differente_de_lepinglage_produit_un_warn(tmp_path: Path):
    path, digest = _fake_binary(tmp_path)
    pin = llmfit.LLMfitPin(version="0.5.0", sha256=digest)

    result = llmfit.run_llmfit(
        llmfit.LLMfitConfig(binary_path=path, pin=pin),
        runner=_runner(_fixture("synthetic-nominal.json")),
    )

    assert result.status == "warn"
    finding = next(f for f in result.findings if f.code == "llmfit_version_mismatch")
    assert finding.level == "warn"
    assert "0.6.1" in finding.message and "0.5.0" in finding.message
    # La recommandation reste exploitable : une version décalée n'invalide pas le conseil.
    assert result.recommendation is not None


def test_version_absente_de_la_sortie_produit_un_warn(tmp_path: Path):
    path, digest = _fake_binary(tmp_path)
    pin = llmfit.LLMfitPin(version="0.6.1", sha256=digest)

    result = llmfit.run_llmfit(
        llmfit.LLMfitConfig(binary_path=path, pin=pin),
        runner=_runner(b'{"recommendations": []}'),
    )

    assert "llmfit_version_unknown" in [f.code for f in result.findings]


@pytest.mark.parametrize("sha", ["", "abc", "0" * 63, "0" * 65, "A" * 64, "z" * 64])
def test_epinglage_mal_saisi_est_refuse_a_la_construction(sha: str):
    """Un épinglage invalide échoue tout de suite, pas au moment de protéger."""
    with pytest.raises(llmfit.LLMfitError):
        llmfit.LLMfitPin(version="0.6.1", sha256=sha)


def test_epinglage_valide_est_accepte():
    """Contrôle positif du test précédent : la validation n'est pas systématiquement rouge."""
    pin = llmfit.LLMfitPin(version="0.6.1", sha256="a" * 64)
    assert pin.sha256 == "a" * 64


def test_epinglage_sans_version_est_refuse():
    with pytest.raises(llmfit.LLMfitError):
        llmfit.LLMfitPin(version="  ", sha256="a" * 64)


def test_binaire_trop_gros_nest_pas_hache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path, _ = _fake_binary(tmp_path)
    monkeypatch.setattr(llmfit, "MAX_BINARY_BYTES", 1)
    appels: list = []

    result = llmfit.run_llmfit(
        llmfit.LLMfitConfig(binary_path=path, pin=llmfit.LLMfitPin(version="0.6.1", sha256="a" * 64)),
        runner=_runner(record=appels),
    )

    assert result.status == "fail"
    assert [f.code for f in result.findings] == ["llmfit_digest_unreadable"]
    assert appels == []


# ── 3. Validation du schéma JSON à la frontière ───────────────────────────────

def test_sortie_nominale_est_validee_et_bornee():
    reco = llmfit.parse_llmfit_json(_fixture("synthetic-nominal.json"))

    assert reco.llmfit_version == "0.6.1"
    assert len(reco.candidates) == 2
    first = reco.candidates[0]
    assert first.candidate == "qwen2.5-7b-instruct"
    assert first.quantization == "Q4_K_M"
    assert first.estimated_vram_mb == pytest.approx(5804.5)
    assert first.gpu_layers == 29
    assert first.fit == "fits"


def test_champs_inconnus_sont_ignores_et_signales():
    """Tolérance en avant : un champ non consommé ne fait pas échouer, mais est listé."""
    reco = llmfit.parse_llmfit_json(_fixture("synthetic-moe-multigpu.json"))

    assert reco.llmfit_version == "0.6.1"
    assert reco.candidates[0].candidate == "mixtral-8x7b-instruct"
    assert reco.candidates[0].fit == "cpu_offload"
    assert "hardware" in reco.ignored_fields
    assert "notes" in reco.ignored_fields


def test_liste_vide_est_valide():
    reco = llmfit.parse_llmfit_json(_fixture("synthetic-empty.json"))
    assert reco.candidates == ()


@pytest.mark.parametrize(
    ("nom", "attendu"),
    [
        ("synthetic-invalid-empty.json", "vide"),
        ("synthetic-invalid-truncated.json", "tronqu"),
        ("synthetic-invalid-missing-recommendations.json", "recommendations"),
        ("synthetic-invalid-negative-vram.json", "estimated_vram_mb"),
        ("synthetic-invalid-vram-string.json", "estimated_vram_mb"),
        ("synthetic-invalid-unknown-fit.json", "fit"),
    ],
)
def test_sorties_invalides_sont_refusees_avec_un_message_actionnable(nom: str, attendu: str):
    with pytest.raises(llmfit.LLMfitSchemaError) as exc:
        llmfit.parse_llmfit_json(_fixture(nom))
    assert attendu in str(exc.value)


@pytest.mark.parametrize(
    "payload",
    [
        "[]",                                              # racine liste
        "null",                                            # racine nulle
        "42",                                              # racine scalaire
        '{"recommendations": {}}',                         # mauvais type
        '{"recommendations": [42]}',                       # entrée scalaire
        '{"recommendations": [{}]}',                       # modèle manquant
        '{"recommendations": [{"model": ""}]}',            # modèle vide
        '{"recommendations": [{"model": 7}]}',             # modèle non textuel
        '{"recommendations": [{"model": "a", "gpu_layers": true}]}',        # bool ≠ entier
        '{"recommendations": [{"model": "a", "gpu_layers": 99999}]}',       # hors bornes
        '{"recommendations": [{"model": "a", "gpu_layers": 3.5}]}',         # non entier
        '{"recommendations": [{"model": "a", "context_length": 0}]}',       # borne basse
        '{"recommendations": [{"model": "a", "score": 1.5}]}',              # hors [0,1]
        '{"recommendations": [{"model": "a", "quantization": "Q4 K M"}]}',  # charset
        '{"recommendations": [{"model": "a", "quantization": 4}]}',         # mauvais type
        '{"llmfit_version": 6, "recommendations": []}',                     # version non textuelle
        '{"llmfit_version": "", "recommendations": []}',                    # version vide
    ],
)
def test_formes_refusees(payload: str):
    with pytest.raises(llmfit.LLMfitSchemaError):
        llmfit.parse_llmfit_json(payload)


def test_forme_minimale_acceptee():
    """Contrôle positif : le validateur n'est pas rouge sur tout ce qu'on lui donne."""
    reco = llmfit.parse_llmfit_json('{"recommendations": [{"model": "a"}]}')
    assert reco.candidates[0].candidate == "a"
    assert reco.candidates[0].estimated_vram_mb is None


def test_valeurs_non_finies_sont_refusees():
    """`json` accepte `NaN`/`Infinity` par défaut : le validateur, non."""
    for literal in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(llmfit.LLMfitSchemaError):
            llmfit.parse_llmfit_json('{"recommendations": [{"model": "a", "score": %s}]}' % literal)


def test_liste_trop_longue_est_refusee():
    entries = [{"model": f"m{i}"} for i in range(llmfit.MAX_RECOMMENDATIONS + 1)]
    with pytest.raises(llmfit.LLMfitSchemaError) as exc:
        llmfit.parse_llmfit_json(json.dumps({"recommendations": entries}))
    assert str(llmfit.MAX_RECOMMENDATIONS) in str(exc.value)


def test_liste_a_la_borne_est_acceptee():
    """Contrôle positif de la borne : `MAX_RECOMMENDATIONS` entrées passent."""
    entries = [{"model": f"m{i}"} for i in range(llmfit.MAX_RECOMMENDATIONS)]
    reco = llmfit.parse_llmfit_json(json.dumps({"recommendations": entries}))
    assert len(reco.candidates) == llmfit.MAX_RECOMMENDATIONS


def test_sortie_trop_volumineuse_est_refusee_sans_etre_analysee():
    payload = b'{"recommendations": []}' + b" " * (llmfit.MAX_OUTPUT_BYTES + 1)
    with pytest.raises(llmfit.LLMfitSchemaError) as exc:
        llmfit.parse_llmfit_json(payload)
    assert str(llmfit.MAX_OUTPUT_BYTES) in str(exc.value)


def test_chaine_trop_longue_est_refusee():
    with pytest.raises(llmfit.LLMfitSchemaError):
        llmfit.parse_llmfit_json(json.dumps({"recommendations": [{"model": "x" * 300}]}))


def test_caractere_de_controle_est_refuse():
    """Une séquence ANSI dans un nom de modèle est une injection de terminal."""
    with pytest.raises(llmfit.LLMfitSchemaError) as exc:
        llmfit.parse_llmfit_json(json.dumps({"recommendations": [{"model": "a\x1b[31mrouge"}]}))
    assert "contrôle" in str(exc.value)


def test_sortie_non_utf8_est_refusee():
    with pytest.raises(llmfit.LLMfitSchemaError) as exc:
        llmfit.parse_llmfit_json(b'{"recommendations": [{"model": "\xff\xfe"}]}')
    assert "UTF-8" in str(exc.value)


def test_type_dentree_inattendu_est_refuse():
    with pytest.raises(llmfit.LLMfitSchemaError):
        llmfit.parse_llmfit_json(None)  # type: ignore[arg-type]


def test_sortie_invalide_devient_un_warn_et_non_une_exception(tmp_path: Path):
    """Aucune exception brute ne remonte de `run_llmfit`."""
    path, digest = _fake_binary(tmp_path)
    pin = llmfit.LLMfitPin(version="0.6.1", sha256=digest)

    result = llmfit.run_llmfit(
        llmfit.LLMfitConfig(binary_path=path, pin=pin),
        runner=_runner(_fixture("synthetic-invalid-truncated.json")),
    )

    assert result.status == "warn"
    assert [f.code for f in result.findings] == ["llmfit_schema_invalid"]
    assert result.recommendation is None


def test_code_de_retour_non_nul_devient_un_warn(tmp_path: Path):
    path, digest = _fake_binary(tmp_path)
    pin = llmfit.LLMfitPin(version="0.6.1", sha256=digest)

    result = llmfit.run_llmfit(
        llmfit.LLMfitConfig(binary_path=path, pin=pin),
        runner=_runner(b"", returncode=3, stderr=b"no supported backend detected\n"),
    )

    assert result.status == "warn"
    assert [f.code for f in result.findings] == ["llmfit_exit_nonzero"]
    assert "no supported backend" in result.findings[0].message


def test_echec_dexecution_systeme_devient_un_warn(tmp_path: Path):
    path, digest = _fake_binary(tmp_path)
    pin = llmfit.LLMfitPin(version="0.6.1", sha256=digest)

    def run(argv, timeout):
        raise OSError("Exec format error")

    result = llmfit.run_llmfit(llmfit.LLMfitConfig(binary_path=path, pin=pin), runner=run)

    assert result.status == "warn"
    assert [f.code for f in result.findings] == ["llmfit_exec_failed"]


# ── 4. Timeout ────────────────────────────────────────────────────────────────

def test_depassement_de_delai_devient_un_warn(tmp_path: Path):
    path, digest = _fake_binary(tmp_path)
    pin = llmfit.LLMfitPin(version="0.6.1", sha256=digest)

    def run(argv, timeout):
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout)

    result = llmfit.run_llmfit(
        llmfit.LLMfitConfig(binary_path=path, pin=pin, timeout_seconds=1.5), runner=run
    )

    assert result.status == "warn"
    assert [f.code for f in result.findings] == ["llmfit_timeout"]
    assert "1.5" in result.findings[0].message
    assert result.recommendation is None


def test_delai_est_transmis_a_lexecuteur(tmp_path: Path):
    path, digest = _fake_binary(tmp_path)
    appels: list = []

    llmfit.run_llmfit(
        llmfit.LLMfitConfig(
            binary_path=path,
            pin=llmfit.LLMfitPin(version="0.6.1", sha256=digest),
            timeout_seconds=7.0,
        ),
        runner=_runner(b'{"recommendations": []}', record=appels),
    )

    assert appels[0][1] == 7.0


def test_delai_par_defaut_est_borne():
    assert 0 < llmfit.DEFAULT_TIMEOUT_SECONDS <= llmfit.MAX_TIMEOUT_SECONDS
    assert llmfit.LLMfitConfig().timeout_seconds == llmfit.DEFAULT_TIMEOUT_SECONDS


@pytest.mark.parametrize("valeur", [0, -1, 10_000, "20", True, None])
def test_delai_invalide_est_refuse(valeur):
    with pytest.raises(llmfit.LLMfitError):
        llmfit.LLMfitConfig(timeout_seconds=valeur)


def test_delai_valide_est_accepte():
    """Contrôle positif du test précédent."""
    assert llmfit.LLMfitConfig(timeout_seconds=42.0).timeout_seconds == 42.0


def test_timeout_reel_dun_sous_processus_qui_pend(tmp_path: Path):
    """
    Bout en bout, avec un vrai `subprocess` : un binaire qui dort est interrompu.

    Couvre `_default_runner`, que l'injection d'exécuteur laisse sinon non testé.
    """
    script = tmp_path / "llmfit"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(0o755)
    digest = hashlib.sha256(script.read_bytes()).hexdigest()

    result = llmfit.run_llmfit(
        llmfit.LLMfitConfig(
            binary_path=script,
            pin=llmfit.LLMfitPin(version="0.6.1", sha256=digest),
            timeout_seconds=0.5,
        )
    )

    assert result.status == "warn"
    assert [f.code for f in result.findings] == ["llmfit_timeout"]


# ── 5. Exécution : argv fermé, environnement purgé ────────────────────────────

def test_argv_est_ferme(tmp_path: Path):
    """Aucun paramètre appelant n'entre dans `argv` : rien ne peut y transiter."""
    path, digest = _fake_binary(tmp_path)
    appels: list = []

    llmfit.run_llmfit(
        llmfit.LLMfitConfig(binary_path=path, pin=llmfit.LLMfitPin(version="0.6.1", sha256=digest)),
        runner=_runner(b'{"recommendations": []}', record=appels),
    )

    assert appels[0][0] == [str(path), "recommend", "--json"]


def test_environnement_enfant_est_purge_des_secrets():
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/eva",
        "HF_TOKEN": "hf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "ADMIN_SECRET": "s" * 40,
        "INTERNAL_API_KEY": "k" * 40,
        "OPENAI_API_KEY": "sk-" + "a" * 30,
        "GATEWAY_PASSWORD": "hunter2",
    }
    child = llmfit.child_environment(base)

    # Contrôle positif : les variables anodines sont conservées.
    assert child["PATH"] == "/usr/bin"
    assert child["HOME"] == "/home/eva"
    for key in ("HF_TOKEN", "ADMIN_SECRET", "INTERNAL_API_KEY", "OPENAI_API_KEY", "GATEWAY_PASSWORD"):
        assert key not in child


def test_environnement_enfant_lit_os_environ_par_defaut(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EVA_TEST_MARQUEUR", "present")
    monkeypatch.setenv("EVA_TEST_TOKEN", "secret")
    child = llmfit.child_environment()

    assert child["EVA_TEST_MARQUEUR"] == "present"  # contrôle positif
    assert "EVA_TEST_TOKEN" not in child


def test_sortie_derreur_est_desinfectee_avant_affichage(tmp_path: Path):
    """Une séquence ANSI sur stderr ne doit pas atteindre le terminal via un constat."""
    path, digest = _fake_binary(tmp_path)

    result = llmfit.run_llmfit(
        llmfit.LLMfitConfig(binary_path=path, pin=llmfit.LLMfitPin(version="0.6.1", sha256=digest)),
        runner=_runner(b"", returncode=1, stderr=b"\x1b[31mboom\x1b[0m\nligne 2\n"),
    )

    message = result.findings[0].message
    assert "boom" in message           # contrôle positif : le message utile passe
    assert "\x1b" not in message
    assert "ligne 2" not in message     # une seule ligne, bornée


# ── 6. Fallback manuel ────────────────────────────────────────────────────────

def test_profil_manuel_remplace_entierement_llmfit(tmp_path: Path):
    path, digest = _fake_binary(tmp_path)
    appels: list = []

    result = llmfit.run_llmfit(
        llmfit.LLMfitConfig(
            binary_path=path,
            pin=llmfit.LLMfitPin(version="0.6.1", sha256=digest),
            manual_profile_path=FIXTURES / "synthetic-manual-profile.json",
        ),
        runner=_runner(_fixture("synthetic-nominal.json"), record=appels),
    )

    assert result.status == "ok"
    assert result.source == "manual"
    assert appels == [], "un profil manuel doit court-circuiter l'exécution de LLMfit"
    assert result.recommendation is not None
    assert result.recommendation.candidates[0].candidate == "qwen2.5-3b-instruct"
    assert "manual_profile_used" in [f.code for f in result.findings]


@pytest.mark.parametrize(
    "nom",
    [
        "synthetic-invalid-truncated.json",
        "synthetic-invalid-negative-vram.json",
        "synthetic-invalid-unknown-fit.json",
    ],
)
def test_profil_manuel_subit_la_meme_validation(nom: str):
    """Une entrée d'opérateur n'est pas plus fiable qu'une sortie d'outil."""
    result = llmfit.run_llmfit(llmfit.LLMfitConfig(manual_profile_path=FIXTURES / nom))

    assert result.status == "fail"
    assert [f.code for f in result.findings] == ["manual_profile_unreadable"]


def test_profil_manuel_introuvable_est_un_echec_explicite(tmp_path: Path):
    """Déclaré puis ignoré en silence serait le pire des cas : l'opérateur y croirait."""
    result = llmfit.run_llmfit(llmfit.LLMfitConfig(manual_profile_path=tmp_path / "absent.json"))

    assert result.status == "fail"
    assert [f.code for f in result.findings] == ["manual_profile_unreadable"]
    assert "absent.json" in result.findings[0].message


def test_profil_manuel_trop_gros_est_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "profil.json"
    path.write_text('{"recommendations": []}')
    monkeypatch.setattr(llmfit, "MAX_OUTPUT_BYTES", 4)

    with pytest.raises(llmfit.LLMfitError):
        llmfit.load_manual_profile(path)


def test_profil_manuel_vide_est_un_warn_pas_un_ok(tmp_path: Path):
    path = tmp_path / "profil.json"
    path.write_text('{"recommendations": []}')

    result = llmfit.run_llmfit(llmfit.LLMfitConfig(manual_profile_path=path))

    assert result.status == "warn"
    assert "llmfit_no_recommendation" in [f.code for f in result.findings]


# ── 7. Conseiller, pas autorité ───────────────────────────────────────────────

def _module_ast() -> ast.Module:
    return ast.parse(inspect.getsource(llmfit))


def _identifiers(tree: ast.Module) -> set[str]:
    """Noms réellement référencés par le code — hors docstrings et commentaires."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
    return names


def _string_literals(tree: ast.Module) -> set[str]:
    """Chaînes littérales du code, docstrings exclues."""
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    }


def _imported_modules(tree: ast.Module) -> set[str]:
    """
    Modules importés, imports relatifs compris.

    TST-007 : la version d'origine ne gardait que `node.module`, or `from . import
    public_https` porte `module=None` et son nom vit dans les alias. Le test
    d'absence adossé à cette fonction était donc aveugle à la seule façon
    réaliste, dans ce paquet, de faire entrer le réseau par la bande.
    """
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module.split(".")[0])
            else:  # `from . import x` : c'est l'alias qui nomme le module
                modules.update(alias.name.split(".")[0] for alias in node.names)
    return modules


def test_le_module_ne_peut_pas_construire_une_etape_de_plan():
    """
    Garantie structurelle : sans `PlanStep` ni constante `ACTION_*`, ce module ne
    peut pas émettre `enable_model`, seule voie d'activation d'un plan.

    Vérifié sur l'AST — les noms réellement référencés — et non par recherche
    textuelle, qui confondrait le code avec ce que la documentation en dit.
    """
    tree = _module_ast()
    names = _identifiers(tree)

    assert "PlanSection" in names, "contrôle positif : le module projette bien vers une section"
    assert "PlanStep" not in names
    assert not [n for n in names if n.startswith("ACTION_")]
    assert not (_string_literals(tree) & set(schema.PLAN_ACTIONS))

    signature = inspect.signature(llmfit.to_plan_section)
    assert signature.return_annotation == "schema.PlanSection"


def test_la_section_ne_publie_aucun_identifiant_de_modele():
    """Rien de ce que produit ce module n'a la forme d'une entrée de registre."""
    section = llmfit.to_plan_section(
        llmfit.run_llmfit(
            llmfit.LLMfitConfig(manual_profile_path=FIXTURES / "synthetic-manual-profile.json")
        )
    )
    entry = section.data["candidates"][0]

    assert "candidate" in entry            # contrôle positif
    assert "model" not in entry
    assert "model_id" not in entry
    assert entry["catalog_approved"] is None


def test_la_section_porte_les_limites_de_llmfit():
    section = llmfit.to_plan_section(llmfit.run_llmfit(llmfit.LLMfitConfig(), which=_no_binary))

    assert section.data["advisory_only"] is True
    assert section.data["limitations"] == list(llmfit.LIMITATIONS)
    assert len(llmfit.LIMITATIONS) == 8
    assert "cpu_moe" in " ".join(llmfit.LIMITATIONS)
    assert "calibration" in section.data["activation_rule"]
    assert "conseil consultatif" in section.summary


def test_les_limites_apparaissent_dans_le_rendu_json():
    section = llmfit.to_plan_section(llmfit.run_llmfit(llmfit.LLMfitConfig(), which=_no_binary))
    plan = schema.BootstrapPlan(generated_at="2026-07-31T09:00:00Z", mode="test", sections=(section,))
    rendu = schema.render_json(plan)

    for limite in llmfit.LIMITATIONS:
        assert limite in rendu


def test_le_resume_de_la_section_est_toujours_imprime_en_rendu_humain():
    """
    `render_human()` n'imprime ni `data` ni les constats `info` : la mention
    consultative doit donc vivre dans le résumé, qui, lui, est toujours imprimé.
    """
    section = llmfit.to_plan_section(llmfit.run_llmfit(llmfit.LLMfitConfig(), which=_no_binary))
    plan = schema.BootstrapPlan(generated_at="2026-07-31T09:00:00Z", mode="test", sections=(section,))

    assert "conseil consultatif" in schema.render_human(plan)


def test_notice_consultative_est_rendable_pour_la_cli():
    notice = llmfit.render_advisory_notice()

    for limite in llmfit.LIMITATIONS:
        assert limite in notice
    assert llmfit.ACTIVATION_RULE in notice


# ── 8. Contrat de producteur et non-divulgation ───────────────────────────────

def test_contrat_de_producteur():
    assert llmfit.SECTION_NAME == schema.SECTION_RECOMMENDATION
    assert llmfit.SECTION_NAME in schema.SECTION_NAMES
    assert isinstance(llmfit.SECTION_VERSION, int) and llmfit.SECTION_VERSION >= 1


@pytest.mark.parametrize(
    "config",
    [
        llmfit.LLMfitConfig(),
        llmfit.LLMfitConfig(enabled=False),
        llmfit.LLMfitConfig(manual_profile_path=FIXTURES / "synthetic-nominal.json"),
        llmfit.LLMfitConfig(manual_profile_path=FIXTURES / "synthetic-moe-multigpu.json"),
        llmfit.LLMfitConfig(manual_profile_path=FIXTURES / "synthetic-invalid-truncated.json"),
    ],
)
def test_la_section_est_serialisable_et_sans_secret(config: llmfit.LLMfitConfig):
    section = llmfit.to_plan_section(llmfit.run_llmfit(config, which=_no_binary))
    document = section.to_dict()

    json.dumps(document)  # sérialisable
    assert schema.find_secret_leaks(document) == ()

    plan = schema.BootstrapPlan(generated_at="2026-07-31T09:00:00Z", mode="test", sections=(section,))
    assert schema.validate_plan_dict(plan.to_dict()) == ()


def test_le_detecteur_de_fuite_voit_bien_quelque_chose():
    """
    Contrôle positif du test précédent : `find_secret_leaks` n'est pas inerte sur
    la forme de document que produit ce module.
    """
    section = llmfit.to_plan_section(llmfit.run_llmfit(llmfit.LLMfitConfig(), which=_no_binary))
    document = section.to_dict()
    document["data"]["binary_path"] = "/opt/llmfit hf_aaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    assert schema.find_secret_leaks(document) != ()


def test_aucun_secret_de_lenvironnement_ne_transite_dans_la_section(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HF_TOKEN", "hf_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")
    section = llmfit.to_plan_section(llmfit.run_llmfit(llmfit.LLMfitConfig(), which=_no_binary))

    assert "hf_zzzz" not in json.dumps(section.to_dict())
    assert schema.find_secret_leaks(section.to_dict()) == ()


def test_aucun_acces_reseau_ni_installation_dans_le_module():
    """§7 : « éviter `curl | sh` ». Le module détecte, vérifie, exécute ou passe."""
    modules = _imported_modules(_module_ast())

    assert "subprocess" in modules  # contrôle positif : l'analyse voit bien les imports
    assert not (modules & {"urllib", "urllib3", "requests", "httpx", "socket", "http", "ftplib"})

    # L'argv est fermé et ne contient aucune sous-commande d'installation.
    assert llmfit.LLMFIT_ARGV == ("recommend", "--json")


def test_statuts_emis_appartiennent_au_contrat(tmp_path: Path):
    path, digest = _fake_binary(tmp_path)
    resultats = [
        llmfit.run_llmfit(llmfit.LLMfitConfig(), which=_no_binary),
        llmfit.run_llmfit(llmfit.LLMfitConfig(enabled=False)),
        llmfit.run_llmfit(llmfit.LLMfitConfig(binary_path=path), runner=_runner()),
        llmfit.run_llmfit(
            llmfit.LLMfitConfig(binary_path=path, pin=llmfit.LLMfitPin(version="0.6.1", sha256="0" * 64)),
            runner=_runner(),
        ),
        llmfit.run_llmfit(
            llmfit.LLMfitConfig(binary_path=path, pin=llmfit.LLMfitPin(version="0.6.1", sha256=digest)),
            runner=_runner(_fixture("synthetic-nominal.json")),
        ),
    ]

    assert [r.status for r in resultats] == ["skip", "skip", "skip", "fail", "ok"]
    for result in resultats:
        section = llmfit.to_plan_section(result)
        assert section.status in ("ok", "warn", "fail", "skip")
        assert section.summary
        assert schema.find_secret_leaks(section.to_dict()) == ()


def test_fixtures_sont_declarees_synthetiques():
    """
    Aucune fixture ne doit pouvoir passer pour une capture réelle. Si une vraie
    capture est ajoutée un jour, elle sera préfixée `real-` et ce test le dira.
    """
    fichiers = sorted(p.name for p in FIXTURES.glob("*.json"))

    assert fichiers, "contrôle positif : des fixtures existent bien"
    assert all(nom.startswith("synthetic-") for nom in fichiers), fichiers
    assert "SYNTHÉTIQUES" in (FIXTURES / "README.md").read_text(encoding="utf-8")


def test_toutes_les_fixtures_valides_traversent_le_parseur():
    """« Tests avec sorties enregistrées » : le parseur tourne sur chaque fixture."""
    valides = [p for p in sorted(FIXTURES.glob("*.json")) if "invalid" not in p.name]
    assert len(valides) >= 3

    for chemin in valides:
        reco = llmfit.parse_llmfit_json(chemin.read_bytes())
        assert isinstance(reco, llmfit.LLMfitRecommendation)

    invalides = [p for p in sorted(FIXTURES.glob("*invalid*.json"))]
    assert len(invalides) >= 5
    for chemin in invalides:
        with pytest.raises(llmfit.LLMfitSchemaError):
            llmfit.parse_llmfit_json(chemin.read_bytes())


def test_calcul_dempreinte_est_exact(tmp_path: Path):
    path = tmp_path / "artefact"
    path.write_bytes(b"contenu-de-test")

    assert llmfit.file_sha256(path) == hashlib.sha256(b"contenu-de-test").hexdigest()


def test_empreinte_dun_fichier_illisible_remonte_une_erreur_typee(tmp_path: Path):
    if os.geteuid() == 0:
        pytest.skip("root lit tout, le contrôle de permission ne prouverait rien")
    path = tmp_path / "artefact"
    path.write_bytes(b"x")
    path.chmod(0o000)

    with pytest.raises(OSError):
        llmfit.file_sha256(path)
