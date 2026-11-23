"""
AUT-015 — régressions de l'applicateur (`bootstrap/applier.py`) et de sa CLI.

L'applicateur est le seul module de M2 qui peut modifier une machine sans qu'un
exécuteur le lui ait demandé : c'est lui qui décide de partir. Quatre familles
d'invariants sont donc verrouillées ici :

1. **le pré-vol** — une action sans exécuteur arrête tout AVANT le départ. Un
   plan entamé à moitié laisse l'hôte dans un état que personne n'a décrit ;
2. **la chaîne des preuves** — une preuve ne peut jamais être présumée. Elle
   n'existe que si l'étape qui devait la produire a eu lieu, a réussi, et a
   publié un document complet. Une clé manquante ne devient jamais un défaut ;
3. **la délibération** — la simulation est le défaut, l'application se demande.
   Une simulation ne produit aucune preuve et n'active donc rien ;
4. **la non-divulgation** — rien de ce que la commande publie, y compris par un
   message d'erreur, ne contient de secret.

Deux règles de forme, apprises en vague 5 et appliquées ici :

- tout test d'ABSENCE porte un contrôle positif ; sans lui, il passe au vert le
  jour où le détecteur devient inerte ;
- les options de la CLI sont lues sur l'objet Click, jamais cherchées dans une
  sortie rendue : `rich` colorise dès qu'il détecte GitHub Actions et fragmente
  les noms d'options par des séquences ANSI. Toute sortie inspectée est nettoyée
  avant qu'on y cherche — ou qu'on y vérifie une absence.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from typer.main import get_command
from typer.testing import CliRunner

import cli as cli_module
import doctor
import model_registry
from bootstrap import applier as ap
from bootstrap import calibration as cal
from bootstrap import execution as ex
from bootstrap import first_token as ft
from bootstrap import registry_writer as rw
from bootstrap import schema as sc

T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

MODEL_ID = "llama-3.1-8b-instruct"

# Construit à l'exécution : un littéral ressemblant à un vrai jeton n'a rien à
# faire dans un dépôt, même en fixture.
FAUX_TOKEN = "hf_" + "B" * 24

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _propre(texte: str) -> str:
    """
    Nettoie une sortie CLI de tout balisage ANSI avant qu'on y cherche quoi que ce soit.

    `rich` colorise dès qu'il détecte GitHub Actions : une recherche de
    sous-chaîne sur la sortie brute échoue alors en CI et nulle part ailleurs —
    et, pire, une vérification d'ABSENCE y passe au vert par accident.
    """
    return _ANSI.sub("", texte)


def _iso(moment: datetime) -> str:
    return moment.strftime(rw.PROOF_TIMESTAMP_FORMAT)


@pytest.mark.anyio
async def test_mutation_to_thread_termine_avant_de_propager_l_annulation() -> None:
    """Le thread d'écriture ne doit jamais survivre au début du rollback."""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def mutation() -> str:
        started.set()
        release.wait(timeout=5)
        finished.set()
        return "published"

    task = asyncio.create_task(ap._to_thread_completed(mutation))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not finished.is_set()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


# ── Fabriques de plan ─────────────────────────────────────────────────────────

def _section() -> sc.PlanSection:
    return sc.PlanSection(
        name=sc.SECTION_HARDWARE,
        version=1,
        status="ok",
        summary="hôte inventorié",
        data={"probe": "ok"},
    )


def _step(order: int, action: str, target: str) -> sc.PlanStep:
    return sc.PlanStep(
        order=order,
        action=action,
        target=target,
        detail=f"détail de l'étape {order}",
        requires_root=False,
        reversible=True,
    )


def _plan_document(actions: list[tuple[str, str]]) -> dict:
    plan = sc.BootstrapPlan(
        generated_at="2026-08-01T09:00:00Z",
        mode="local",
        sections=(_section(),),
        steps=tuple(
            _step(index + 1, action, target)
            for index, (action, target) in enumerate(actions)
        ),
        decisions=(sc.Decision(
            topic="runtime", choice="pinned", rationale="artefact officiel épinglé"
        ),),
    )
    return plan.to_dict()


def _plan_file(tmp_path: Path, actions: list[tuple[str, str]], name: str = "plan.json") -> Path:
    chemin = tmp_path / name
    chemin.write_text(
        json.dumps(_plan_document(actions), ensure_ascii=False), encoding="utf-8"
    )
    return chemin


# ── Fabriques de preuve ───────────────────────────────────────────────────────

LLAMA_PARAMS = {
    "n_gpu_layers": 999, "ctx_size": 32768, "parallel": 8, "batch_size": 2048,
    "ubatch_size": 512, "cache_type_k": "q8_0", "cache_type_v": "q8_0",
    "flash_attn": True, "threads": 4, "threads_http": 2,
}


def _calibration_block(**surcharges) -> dict:
    """Le bloc `calibration` tel qu'AUT-008 le publie, enrichi de ses clés §9."""
    base = {
        "model_id": MODEL_ID,
        "runtime_version": "b6042",
        "hardware_fingerprint": "L40S-48G-driver-570.86",
        "params_fingerprint": rw.params_fingerprint(LLAMA_PARAMS),
        "peak_vram_gb": 4.0,
        "peak_ram_gb": 0.9,
        "load_seconds": 3.2,
        "measured_at": _iso(T0 - timedelta(minutes=5)),
        # Clés propres à §9, que le consommateur ne connaît pas : la projection
        # doit les écarter sans se plaindre.
        "idle_vram_gb": 0.2,
        "measured_vram_gb": 4.0,
        "safety_margin": 0.10,
        "proposed_vram_gb": 4.4,
        "applied": False,
        "report_path": "/var/lib/eva/calibration.json",
    }
    base.update(surcharges)
    return base


def _tronquer(document: dict, cle: str) -> dict:
    document.pop(cle)
    return document


def _smoke_proof(**surcharges) -> dict:
    """La preuve telle qu'AUT-009 la publie."""
    base = {
        "kind": ft.PROOF_KIND,
        "version": ft.PROOF_VERSION,
        "verdict": ft.PROOF_SERVED,
        "reason": ft.REASON_OK,
        "model_id": MODEL_ID,
        "base_url": "https://eva.example",
        "endpoint": ft.GENERATION_PATH,
        "measured_at": _iso(T0 - timedelta(minutes=2)),
        "dry_run": False,
        "http_status": 200,
        "ttft_ms": 412,
        "completion_tokens": 16,
        "prompt_tokens": 11,
        "usage_logged": True,
        "usage_entries": 1,
    }
    base.update(surcharges)
    return base


# ── Exécuteurs doubles ────────────────────────────────────────────────────────

def _executeur(status: str, *, evidence: dict | None = None, error: str | None = None):
    async def executer(step, context):
        return ex.StepResult.for_step(
            step, status=status, summary=f"double pour {step.action}",
            evidence=evidence or {}, error=error,
        )
    return executer


def _registre(**executeurs) -> ex.ExecutorRegistry:
    registry = ex.ExecutorRegistry()
    for action, executor in executeurs.items():
        registry.register(action, executor)
    return registry


def _contexte(mode: ex.ExecutionMode, racines=()) -> ex.ExecutionContext:
    horloge = iter([f"2026-08-01T12:00:{s:02d}Z" for s in range(60)])
    return ex.ExecutionContext(
        mode, allowed_roots=tuple(racines), monotonic=lambda: 0.0,
        now=lambda: next(horloge),
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. Projection stricte : aucune valeur n'est inventée
# ══════════════════════════════════════════════════════════════════════════════

def test_la_projection_retient_exactement_les_cles_du_contrat_consommateur():
    projete = ap.project_proof(
        _calibration_block(), rw.CALIBRATION_PROOF_KEYS, "calibration"
    )
    assert set(projete) == set(rw.CALIBRATION_PROOF_KEYS)
    # Contrôle positif : les clés §9 étaient bien là et ont été écartées, pas
    # absentes du document de départ.
    assert "proposed_vram_gb" in _calibration_block()
    assert "proposed_vram_gb" not in projete


@pytest.mark.parametrize("cle", sorted(rw.CALIBRATION_PROOF_KEYS))
def test_une_cle_absente_du_producteur_fait_echouer_la_projection(cle):
    """
    Aucune traduction ne fabrique une preuve : une clé manquante est un refus.

    Le paramétrage porte sur TOUTES les clés du contrat, et pas sur une seule :
    une projection qui ne saurait exiger qu'un champ sur huit laisserait passer
    sept preuves incomplètes.
    """
    document = _calibration_block()
    del document[cle]
    with pytest.raises(rw.ProofRejected) as exc:
        ap.project_proof(document, rw.CALIBRATION_PROOF_KEYS, "calibration")
    assert cle in str(exc.value)


def test_la_projection_refuse_ce_qui_n_est_pas_un_objet():
    for valeur in (None, [], "calibration", 3):
        with pytest.raises(rw.ProofRejected):
            ap.project_proof(valeur, rw.CALIBRATION_PROOF_KEYS, "calibration")


def test_les_deux_producteurs_satisfont_le_contrat_consommateur():
    """
    Le test de la réconciliation, dans les deux sens.

    Il n'affirme pas que les noms se ressemblent : il CONSTRUIT les deux volets
    depuis ce que les producteurs publient réellement, et laisse le consommateur
    les valider. C'est le seul test qui tomberait si l'un des deux renommait un
    champ de son côté.
    """
    calibration = ap.calibration_proof_from_evidence({"calibration": _calibration_block()})
    smoke = ap.smoke_test_proof_from_evidence({"proof": _smoke_proof()})
    preuve = rw.ActivationProof(calibration=calibration, smoke_test=smoke)
    assert preuve.model_id == MODEL_ID
    assert preuve.smoke_test.endpoint.startswith(rw.PUBLIC_ENDPOINT_PREFIX)


def test_une_recette_non_servie_ne_donne_aucune_preuve():
    for surcharge in (
        {"verdict": ft.PROOF_NOT_SERVED},
        {"dry_run": True},
        {"usage_logged": False},
        {"reason": ft.REASON_NO_CONTENT},
    ):
        with pytest.raises(rw.ProofRejected):
            ap.smoke_test_proof_from_evidence({"proof": _smoke_proof(**surcharge)})
    # Contrôle positif : le document non altéré, lui, donne bien une preuve.
    assert ap.smoke_test_proof_from_evidence({"proof": _smoke_proof()}).ttft_ms == 412


# ══════════════════════════════════════════════════════════════════════════════
# 2. Le porteur d'état : une preuve ne se présume pas
# ══════════════════════════════════════════════════════════════════════════════

def test_un_registre_de_preuves_vide_n_autorise_rien():
    ledger = ap.ProofLedger()
    assert ledger.get(MODEL_ID) is None
    assert MODEL_ID not in ledger
    assert len(ledger) == 0
    assert ledger.missing_volets(MODEL_ID) != ()


def test_un_seul_volet_ne_fait_pas_une_preuve():
    ledger = ap.ProofLedger()
    ledger.record_calibration(
        ap.calibration_proof_from_evidence({"calibration": _calibration_block()})
    )
    assert ledger.get(MODEL_ID) is None
    assert "recette du premier token" in " ".join(ledger.missing_volets(MODEL_ID))
    # Contrôle positif : le second volet suffit à la rendre disponible.
    ledger.record_smoke_test(ap.smoke_test_proof_from_evidence({"proof": _smoke_proof()}))
    assert isinstance(ledger.get(MODEL_ID), rw.ActivationProof)
    assert ledger.missing_volets(MODEL_ID) == ()


def test_une_preuve_fournie_est_disponible_mais_cede_a_celle_de_l_execution():
    fournie = rw.ActivationProof(
        calibration=ap.calibration_proof_from_evidence(
            {"calibration": _calibration_block(peak_vram_gb=9.0)}
        ),
        smoke_test=ap.smoke_test_proof_from_evidence({"proof": _smoke_proof(ttft_ms=999)}),
    )
    ledger = ap.ProofLedger({MODEL_ID: fournie})
    assert ledger[MODEL_ID].smoke_test.ttft_ms == 999

    ledger.record_calibration(
        ap.calibration_proof_from_evidence({"calibration": _calibration_block()})
    )
    ledger.record_smoke_test(ap.smoke_test_proof_from_evidence({"proof": _smoke_proof()}))
    assert ledger[MODEL_ID].smoke_test.ttft_ms == 412


def test_une_preuve_fournie_qui_n_en_est_pas_une_est_refusee_a_la_construction():
    for valeur in (True, {"calibration": {}}, "preuve"):
        with pytest.raises(ap.ApplierUsageError):
            ap.ProofLedger({MODEL_ID: valeur})


# ══════════════════════════════════════════════════════════════════════════════
# 3. Pré-vol : un plan sans exécuteur n'est pas entamé
# ══════════════════════════════════════════════════════════════════════════════

def test_un_plan_dont_une_action_n_a_pas_d_executeur_n_est_pas_entame(tmp_path):
    chemin = _plan_file(tmp_path, [
        (sc.ACTION_WRITE_REGISTRY, f"models.yaml → {MODEL_ID}"),
        (sc.ACTION_CALIBRATE_MODEL, MODEL_ID),
    ])
    temoin: list[str] = []

    with pytest.raises(ap.ApplierUsageError) as exc:
        asyncio.run(ap.apply_plan_file(
            chemin, ap.ApplierConfig(), mode=ex.ExecutionMode.APPLY,
            allowed_roots=[tmp_path], log=temoin.append,
        ))

    message = str(exc.value)
    assert "write_registry" in message and "calibrate_model" in message
    assert "AUT-007" in message and "AUT-008" in message
    # Rien n'a été tenté : le lanceur journalise une ligne par étape exécutée.
    assert temoin == []


def test_apply_loaded_plan_garde_l_instantane_si_le_fichier_change(tmp_path):
    chemin = _plan_file(tmp_path, [(sc.ACTION_WARMUP_MODEL, MODEL_ID)])
    instantane = ex.load_plan_file(chemin)

    # Le chemin n'est plus la source de vérité une fois l'objet relu. Cette
    # mutation reproduit la fenêtre TOCTOU entre le câblage et l'exécution.
    chemin.write_text(
        json.dumps(
            _plan_document([(sc.ACTION_WARMUP_MODEL, "modele-remplace")]),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config = ap.ApplierConfig(warmup=ap.WarmupWiring(
        settings=_warmup_settings(), client=object(), admin_secret="secret-de-test",
    ))

    outcome = asyncio.run(ap.apply_loaded_plan(
        instantane,
        config,
        mode=ex.ExecutionMode.DRY_RUN,
        allowed_roots=[tmp_path],
    ))

    assert ex.load_plan_file(chemin).steps[0].target == "modele-remplace"
    assert outcome.plan is instantane
    assert outcome.report.plan_fingerprint == instantane.fingerprint
    assert [result.target for result in outcome.report.results] == [MODEL_ID]


def test_apply_plan_file_reste_un_adaptateur_qui_delegue(monkeypatch, tmp_path):
    chemin = _plan_file(tmp_path, [(sc.ACTION_WARMUP_MODEL, MODEL_ID)])
    instantane = ex.load_plan_file(chemin)
    attendu = object()
    appels = []

    def load_once(path):
        appels.append(("load", Path(path)))
        return instantane

    async def apply_loaded(plan, config, **kwargs):
        appels.append(("apply", plan, config, kwargs))
        return attendu

    config = ap.ApplierConfig()
    monkeypatch.setattr(ap.execution, "load_plan_file", load_once)
    monkeypatch.setattr(ap, "apply_loaded_plan", apply_loaded)

    obtenu = asyncio.run(ap.apply_plan_file(
        chemin,
        config,
        mode=ex.ExecutionMode.DRY_RUN,
        allowed_roots=[tmp_path],
    ))

    assert obtenu is attendu
    assert appels[0] == ("load", chemin)
    assert appels[1][0:3] == ("apply", instantane, config)
    assert appels[1][3]["mode"] is ex.ExecutionMode.DRY_RUN
    assert appels[1][3]["allowed_roots"] == [tmp_path]


def test_le_pre_vol_nomme_uniquement_ce_qui_manque(tmp_path):
    """Contrôle positif du test précédent : ce qui EST câblé n'est pas signalé."""
    registry = _registre(**{sc.ACTION_WARMUP_MODEL: _executeur(ex.STEP_DONE)})
    plan = ex.load_plan_document(json.dumps(_plan_document([
        (sc.ACTION_WARMUP_MODEL, MODEL_ID),
        (sc.ACTION_SMOKE_TEST, "nginx → gateway"),
    ])))
    raisons = ap.missing_executor_reasons(plan, registry)
    assert len(raisons) == 1
    assert "smoke_test" in raisons[0]
    assert "warmup_model" not in raisons[0]


def test_le_cablage_fourni_enregistre_exactement_ce_qu_il_couvre():
    registry = ap.build_registry(ap.ApplierConfig(), ap.ProofLedger())
    assert registry.registered_actions() == ()

    config = ap.ApplierConfig(warmup=ap.WarmupWiring(
        settings=_warmup_settings(), client=object(), admin_secret="s",
    ))
    registry = ap.build_registry(config, ap.ProofLedger())
    assert registry.registered_actions() == (sc.ACTION_WARMUP_MODEL,)


def test_le_warmup_multi_modele_fabrique_une_sonde_pour_la_cible_de_l_etape():
    appels: list[str] = []

    def factory(model_id: str):
        appels.append(model_id)

        async def probe():
            return None

        return probe

    wiring = ap.WarmupWiring(
        settings=_warmup_settings(),
        client=object(),
        admin_secret="secret-de-test",
        generation_probe_factory=factory,
    )
    executor = ap._targeted_warmup_executor(wiring)
    result = asyncio.run(executor(
        _step(1, sc.ACTION_WARMUP_MODEL, "autre-modele"),
        _contexte(ex.ExecutionMode.DRY_RUN),
    ))

    assert result.status == ex.STEP_WOULD_APPLY
    assert appels == ["autre-modele"]


def test_le_warmup_refuse_deux_sources_de_sonde_concurrentes():
    with pytest.raises(ap.ApplierUsageError):
        ap.WarmupWiring(
            settings=_warmup_settings(),
            client=object(),
            admin_secret="secret-de-test",
            generation_probe=lambda: None,
            generation_probe_factory=lambda _model_id: None,
        )


def _warmup_settings():
    from bootstrap import warmup as wu
    return wu.WarmupSettings(
        admin_url="http://127.0.0.1:8000", model_id=MODEL_ID, timeout_seconds=300
    )


# ══════════════════════════════════════════════════════════════════════════════
# 4. La chaîne des preuves, de bout en bout
# ══════════════════════════════════════════════════════════════════════════════

REGISTRE_DESACTIVE = """# Registre des modèles — EVA Inference Gateway
models:
  # Réglé à la main par l'exploitant.
  - id: "llama-3.1-8b-instruct"
    path: "{models_dir}/Llama-3.1-8B-Instruct-Q4_K_M.gguf"
    description: "Llama 3.1 8B"
    vram_gb: 5.5
    enabled: false
    capabilities:
      - text_generation
      - streaming
    llama_params:
      n_gpu_layers: 999
      ctx_size: 32768
      parallel: 8
      batch_size: 2048
      ubatch_size: 512
      cache_type_k: "q8_0"
      cache_type_v: "q8_0"
      flash_attn: true
      threads: 4
      threads_http: 2
"""


@pytest.fixture
def hote(tmp_path: Path) -> dict:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "Llama-3.1-8B-Instruct-Q4_K_M.gguf").write_bytes(b"GGUF")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    registre = tmp_path / "registry" / "models.yaml"
    registre.parent.mkdir()
    registre.write_text(REGISTRE_DESACTIVE.format(models_dir=models_dir), encoding="utf-8")
    return {"tmp_path": tmp_path, "models_dir": models_dir,
            "scratch": scratch, "registry": registre}


def _writer_config(hote: dict) -> rw.WriterConfig:
    return rw.WriterConfig(
        registry_path=hote["registry"],
        models_dir=hote["models_dir"],
        allowed_model_dirs=(hote["models_dir"],),
        runtime_version="b6042",
        hardware_fingerprint="L40S-48G-driver-570.86",
        vram_budget_gb=43.6,
        now=lambda: T0,
        scratch_dir=hote["scratch"],
    )


def _registry_sync(calls: list[tuple] | None = None) -> ap.RegistrySyncWiring:
    journal = calls if calls is not None else []

    async def activate(model_id: str, vram_gb: float, digest: str) -> None:
        journal.append(("activate", model_id, vram_gb, digest))

    async def rollback(model_id: str, digest: str) -> None:
        journal.append(("rollback", model_id, digest))

    async def confirm(model_id: str, digest: str) -> None:
        journal.append(("confirm", model_id, digest))

    return ap.RegistrySyncWiring(activate=activate, rollback=rollback, confirm=confirm)


def _config_chainee(hote: dict, **surcharges) -> ap.ApplierConfig:
    """Câblage complet à base de doubles : la calibration et la recette réussissent."""
    base = dict(
        writer=_writer_config(hote),
        registry_sync=_registry_sync(),
        calibration=None,
        first_token=None,
    )
    base.update(surcharges)
    return ap.ApplierConfig(**base)


def _registre_chaine(hote: dict, ledger: ap.ProofLedger, *, calibration_evidence=None,
                     smoke_evidence=None, smoke_status=ex.STEP_DONE,
                     registry_sync=None) -> ex.ExecutorRegistry:
    """
    Construit le registre réel, puis y ajoute les deux producteurs sous forme de doubles.

    Les doubles ne remplacent pas l'applicateur : ils remplacent le GPU et le
    réseau. Tout le reste — capture, registre de preuves, garde d'activation,
    écriture de `models.yaml` — est le code de production.
    """
    config = _config_chainee(
        hote,
        **({"registry_sync": registry_sync} if registry_sync is not None else {}),
    )
    registry = ap.build_registry(config, ledger)
    registry.register(
        sc.ACTION_CALIBRATE_MODEL,
        ap._capturing_calibration(
            _executeur(ex.STEP_DONE, evidence=calibration_evidence
                       if calibration_evidence is not None
                       else {"calibration": _calibration_block()}),
            ledger,
        ),
    )
    registry.register(
        sc.ACTION_SMOKE_TEST,
        ap._capturing_smoke_test(
            _executeur(
                smoke_status,
                evidence=smoke_evidence
                if smoke_evidence is not None else {"proof": _smoke_proof()},
                error="recette simulée échouée"
                if smoke_status == ex.STEP_FAILED else None,
            ),
            ledger,
            _writer_config(hote),
            config.registry_sync,
        ),
    )
    return registry


ETAPES_CHAINE = [
    (sc.ACTION_CALIBRATE_MODEL, MODEL_ID),
    (sc.ACTION_ENABLE_MODEL, MODEL_ID),
    (sc.ACTION_SMOKE_TEST, MODEL_ID),
]


def _executer(hote: dict, etapes, mode, ledger=None, **kwargs) -> ex.ExecutionReport:
    ledger = ledger if ledger is not None else ap.ProofLedger()
    plan = ex.load_plan_document(json.dumps(_plan_document(etapes)))
    registry = _registre_chaine(hote, ledger, **kwargs)
    return asyncio.run(ex.execute_plan(
        plan, registry, _contexte(mode, racines=(hote["tmp_path"],))
    ))


def test_la_chaine_complete_active_reellement_le_modele(hote):
    sync_calls: list[tuple] = []
    rapport = _executer(
        hote,
        ETAPES_CHAINE,
        ex.ExecutionMode.APPLY,
        registry_sync=_registry_sync(sync_calls),
    )

    assert rapport.verdict() == ex.VERDICT_OK, rapport.failures()
    registre = model_registry.ModelRegistry(
        hote["registry"], allowed_model_dirs=[str(hote["models_dir"])]
    )
    entree = next(m for m in registre.list_all() if m.id == MODEL_ID)
    assert entree.enabled is True
    # La capacité a été RELEVÉE par la mesure, jamais abaissée.
    assert entree.vram_gb >= 5.5
    assert rapport.result(2).evidence["provisional"] is True
    assert rapport.result(2).evidence["written"] is False
    assert "activation_confirmation" in rapport.result(3).evidence
    assert [call[0] for call in sync_calls] == ["activate", "confirm"]


def test_un_echec_de_recette_annule_immediatement_l_activation_provisoire(hote):
    sync_calls: list[tuple] = []
    rapport = _executer(
        hote,
        ETAPES_CHAINE,
        ex.ExecutionMode.APPLY,
        smoke_status=ex.STEP_FAILED,
        registry_sync=_registry_sync(sync_calls),
    )

    assert rapport.result(2).status == ex.STEP_DONE
    recette = rapport.result(3)
    assert recette.status == ex.STEP_FAILED
    assert recette.evidence["rollback"]["enabled"] is False
    document = yaml.safe_load(hote["registry"].read_text(encoding="utf-8"))
    assert document["models"][0]["enabled"] is False
    assert [call[0] for call in sync_calls] == ["activate", "rollback"]


def test_une_preuve_complete_refusee_apres_recette_declenche_le_rollback(hote):
    rapport = _executer(
        hote,
        ETAPES_CHAINE,
        ex.ExecutionMode.APPLY,
        smoke_evidence={"proof": _smoke_proof(measured_at=_iso(T0 + timedelta(hours=1)))},
    )

    recette = rapport.result(3)
    assert recette.status == ex.STEP_FAILED
    assert "refus" in recette.summary
    assert recette.evidence["rollback"]["enabled"] is False
    document = yaml.safe_load(hote["registry"].read_text(encoding="utf-8"))
    assert document["models"][0]["enabled"] is False


def test_reappliquer_sur_un_modele_deja_actif_renouvelle_la_preuve_sans_rollback(hote):
    config = _writer_config(hote)
    preuve = rw.ActivationProof(
        calibration=ap.calibration_proof_from_evidence(
            {"calibration": _calibration_block()}
        ),
        smoke_test=ap.smoke_test_proof_from_evidence({"proof": _smoke_proof()}),
    )
    assert rw.enable_model_entry(
        config, MODEL_ID, preuve, mode=ex.ExecutionMode.APPLY
    ).status == ex.STEP_DONE

    rapport = _executer(hote, ETAPES_CHAINE, ex.ExecutionMode.APPLY)

    assert rapport.verdict() == ex.VERDICT_OK
    assert rapport.result(2).status == ex.STEP_ALREADY_SATISFIED
    assert rapport.result(2).evidence["preexisting_enabled"] is True
    assert "activation_confirmation" in rapport.result(3).evidence
    document = yaml.safe_load(hote["registry"].read_text(encoding="utf-8"))
    assert document["models"][0]["enabled"] is True


def test_echec_de_recette_ne_desactive_pas_un_modele_actif_avant_ce_run(hote):
    config = _writer_config(hote)
    preuve = rw.ActivationProof(
        calibration=ap.calibration_proof_from_evidence(
            {"calibration": _calibration_block()}
        ),
        smoke_test=ap.smoke_test_proof_from_evidence({"proof": _smoke_proof()}),
    )
    rw.enable_model_entry(config, MODEL_ID, preuve, mode=ex.ExecutionMode.APPLY)

    rapport = _executer(
        hote, ETAPES_CHAINE, ex.ExecutionMode.APPLY,
        smoke_status=ex.STEP_FAILED,
    )

    assert rapport.result(3).status == ex.STEP_FAILED
    assert "rollback" not in rapport.result(3).evidence
    document = yaml.safe_load(hote["registry"].read_text(encoding="utf-8"))
    assert document["models"][0]["enabled"] is True


def test_sans_l_etape_de_recette_le_prevol_refuse_la_chaine_non_compensable(hote):
    """
    L'invariant central : une preuve absente n'est jamais présumée.

    Le plan retire l'étape qui produit le second volet. Rien d'autre ne change —
    la calibration réussit, l'entrée existe, le budget tient.
    """
    etapes = [
        (sc.ACTION_CALIBRATE_MODEL, MODEL_ID),
        (sc.ACTION_ENABLE_MODEL, MODEL_ID),
        (sc.ACTION_WARMUP_MODEL, MODEL_ID),
    ]
    ledger = ap.ProofLedger()
    plan = ex.load_plan_document(json.dumps(_plan_document(etapes)))
    constats = ap.proof_chain_findings(plan, ledger)
    assert [f.code for f in constats] == ["chaine_activation_non_compensable"]

    # Et l'entrée est restée désactivée sur le disque.
    document = yaml.safe_load(hote["registry"].read_text(encoding="utf-8"))
    assert document["models"][0]["enabled"] is False


def test_une_calibration_dont_la_preuve_est_inexploitable_echoue_a_son_etape(hote):
    """
    Le défaut est signalé là où il est, pas deux étapes plus loin.

    Sans cette garde, la calibration serait rapportée « faite » et l'opérateur
    chercherait la cause à l'activation.
    """
    tronquee = _calibration_block()
    del tronquee["load_seconds"]
    rapport = _executer(
        hote, ETAPES_CHAINE, ex.ExecutionMode.APPLY,
        calibration_evidence={"calibration": tronquee},
    )
    premiere = rapport.result(1)
    assert premiere.status == ex.STEP_FAILED
    assert "load_seconds" in premiere.error
    assert rapport.result(2).status == ex.STEP_NOT_ATTEMPTED


def test_une_recette_dont_la_preuve_est_inexploitable_echoue_et_rollback(hote):
    """
    Un HTTP 200 ne suffit pas : sans document de preuve complet l'activation
    provisoire doit être compensée et l'entrée rester désactivée.
    """
    etapes = ETAPES_CHAINE
    rapport = _executer(
        hote, etapes, ex.ExecutionMode.APPLY,
        smoke_evidence={"proof": _tronquer(_smoke_proof(), "ttft_ms")},
    )
    resultat = rapport.result(3)
    assert resultat.status == ex.STEP_FAILED
    assert any(f.code == "preuve_recette_inexploitable" for f in resultat.findings)
    assert resultat.evidence["rollback"]["enabled"] is False
    document = yaml.safe_load(hote["registry"].read_text(encoding="utf-8"))
    assert document["models"][0]["enabled"] is False


def test_une_simulation_ne_produit_aucune_preuve_et_saute_l_activation(hote):
    """
    Une simulation ne peut rien prouver — et ne doit pas prétendre qu'elle activerait.

    « Sauté » plutôt qu'« échoué » : le préalable n'est pas réuni, ce n'est pas
    l'hôte qui est en défaut. Le verdict reste partiel, jamais `ok`.
    """
    ledger = ap.ProofLedger()
    plan = ex.load_plan_document(json.dumps(_plan_document(ETAPES_CHAINE)))
    registry = ap.build_registry(_config_chainee(hote), ledger)
    registry.register(
        sc.ACTION_CALIBRATE_MODEL,
        ap._capturing_calibration(_executeur(ex.STEP_WOULD_APPLY), ledger),
    )
    registry.register(
        sc.ACTION_SMOKE_TEST,
        ap._capturing_smoke_test(_executeur(ex.STEP_WOULD_APPLY), ledger),
    )
    rapport = asyncio.run(ex.execute_plan(
        plan, registry, _contexte(ex.ExecutionMode.DRY_RUN, racines=(hote["tmp_path"],))
    ))

    assert rapport.result(2).status == ex.STEP_SKIPPED
    assert len(ledger) == 0
    assert rapport.verdict() == ex.VERDICT_PARTIAL
    assert rapport.exit_code() == ex.EXIT_PARTIAL
    # Contrôle positif : le registre sur disque n'a pas bougé d'un octet.
    assert "enabled: false" in hote["registry"].read_text(encoding="utf-8")


def test_une_preuve_fournie_permet_l_activation_sans_etape_productrice(hote):
    """
    Le seul chemin praticable tant que l'ordonnancement n'est pas tranché.

    Ce n'est pas une porte dérobée : la preuve fournie passe par `_check_proof`,
    qui recoupe empreintes, runtime, matériel et fraîcheur.
    """
    preuve = rw.ActivationProof(
        calibration=ap.calibration_proof_from_evidence({"calibration": _calibration_block()}),
        smoke_test=ap.smoke_test_proof_from_evidence({"proof": _smoke_proof()}),
    )
    ledger = ap.ProofLedger({MODEL_ID: preuve})
    plan = ex.load_plan_document(json.dumps(_plan_document(
        [(sc.ACTION_ENABLE_MODEL, MODEL_ID)]
    )))
    registry = ap.build_registry(_config_chainee(hote), ledger)
    rapport = asyncio.run(ex.execute_plan(
        plan, registry, _contexte(ex.ExecutionMode.APPLY, racines=(hote["tmp_path"],))
    ))
    assert rapport.result(1).status == ex.STEP_DONE, rapport.result(1).error


def test_une_preuve_d_un_autre_materiel_ne_passe_pas_la_garde_de_l_ecrivain(hote):
    """Contrôle positif du test précédent : la preuve est bien recoupée, pas crue."""
    preuve = rw.ActivationProof(
        calibration=ap.calibration_proof_from_evidence(
            {"calibration": _calibration_block(hardware_fingerprint="A100-80G")}
        ),
        smoke_test=ap.smoke_test_proof_from_evidence({"proof": _smoke_proof()}),
    )
    ledger = ap.ProofLedger({MODEL_ID: preuve})
    plan = ex.load_plan_document(json.dumps(_plan_document(
        [(sc.ACTION_ENABLE_MODEL, MODEL_ID)]
    )))
    registry = ap.build_registry(_config_chainee(hote), ledger)
    rapport = asyncio.run(ex.execute_plan(
        plan, registry, _contexte(ex.ExecutionMode.APPLY, racines=(hote["tmp_path"],))
    ))
    assert rapport.result(1).status == ex.STEP_FAILED


# ══════════════════════════════════════════════════════════════════════════════
# 5. La chaîne compensable est imposée au pré-vol
# ══════════════════════════════════════════════════════════════════════════════

def test_un_ancien_ordre_non_compensable_est_refuse():
    """
    Un plan vague 5 ne doit plus pouvoir ouvrir un modèle puis exécuter autre
    chose avant la recette responsable de son rollback.
    """
    plan = ex.load_plan_document(json.dumps(_plan_document([
        (sc.ACTION_CALIBRATE_MODEL, MODEL_ID),
        (sc.ACTION_ENABLE_MODEL, MODEL_ID),
        (sc.ACTION_WARMUP_MODEL, MODEL_ID),
        (sc.ACTION_SMOKE_TEST, "nginx → gateway"),
    ])))
    constats = ap.proof_chain_findings(plan, ap.ProofLedger())
    assert [f.code for f in constats] == ["chaine_activation_non_compensable"]
    assert constats[0].level == "fail"
    assert "Rien ne sera exécuté" in constats[0].message


def test_le_triplet_dec_010_ne_produit_aucun_constat():
    """Contrôle positif : la séquence calibrate → enable → smoke est admise."""
    plan = ex.load_plan_document(json.dumps(_plan_document(ETAPES_CHAINE)))
    assert ap.proof_chain_findings(plan, ap.ProofLedger()) == ()


def test_apply_refuse_une_chaine_non_compensable_avant_toute_mutation(tmp_path, hote):
    """La garantie de pré-vol porte sur le point d'entrée réel, pas seulement son helper."""
    chemin = _plan_file(tmp_path, [
        (sc.ACTION_CALIBRATE_MODEL, MODEL_ID),
        (sc.ACTION_ENABLE_MODEL, MODEL_ID),
        (sc.ACTION_SMOKE_TEST, "autre-modele"),
    ])
    avant = hote["registry"].read_bytes()
    journal: list[str] = []
    config = ap.ApplierConfig(
        writer=_writer_config(hote),
        registry_sync=_registry_sync(),
        calibration=cal.CalibrationOptions(
            probes=object(),  # jamais appelé : le pré-vol doit arrêter avant
            runtime_version="b6042",
            hardware_fingerprint="sha256:" + "a" * 64,
            report_dir=tmp_path,
        ),
        first_token=ap.FirstTokenWiring(
            settings=ft.FirstTokenSettings(
                base_url="https://eva.example", admin_url="http://127.0.0.1:8000"
            ),
            client=object(),
            admin_secret="secret-de-test",
        ),
    )

    with pytest.raises(ap.ApplierUsageError, match="rien n'a été tenté"):
        asyncio.run(ap.apply_plan_file(
            chemin,
            config,
            mode=ex.ExecutionMode.APPLY,
            allowed_roots=[hote["tmp_path"]],
            log=journal.append,
        ))

    assert journal == []
    assert hote["registry"].read_bytes() == avant


def test_une_preuve_deja_disponible_eteint_le_constat():
    preuve = rw.ActivationProof(
        calibration=ap.calibration_proof_from_evidence({"calibration": _calibration_block()}),
        smoke_test=ap.smoke_test_proof_from_evidence({"proof": _smoke_proof()}),
    )
    plan = ex.load_plan_document(json.dumps(_plan_document([
        (sc.ACTION_ENABLE_MODEL, MODEL_ID),
    ])))
    assert ap.proof_chain_findings(plan, ap.ProofLedger({MODEL_ID: preuve})) == ()


def test_apply_reel_refuse_une_activation_sans_synchronisation_live(hote):
    preuve = rw.ActivationProof(
        calibration=ap.calibration_proof_from_evidence(
            {"calibration": _calibration_block()}
        ),
        smoke_test=ap.smoke_test_proof_from_evidence({"proof": _smoke_proof()}),
    )
    plan = ex.load_plan_document(json.dumps(_plan_document([
        (sc.ACTION_ENABLE_MODEL, MODEL_ID),
    ])))

    with pytest.raises(ap.ApplierUsageError, match="registry_sync est absent"):
        asyncio.run(ap.apply_loaded_plan(
            plan,
            ap.ApplierConfig(
                writer=_writer_config(hote),
                supplied_proofs={MODEL_ID: preuve},
            ),
            mode=ex.ExecutionMode.APPLY,
            allowed_roots=[hote["tmp_path"]],
        ))

    document = yaml.safe_load(hote["registry"].read_text(encoding="utf-8"))
    assert document["models"][0]["enabled"] is False


# ══════════════════════════════════════════════════════════════════════════════
# 6. `verify_artifact`, action à deux domaines
# ══════════════════════════════════════════════════════════════════════════════

def test_une_cible_de_verification_non_rattachable_echoue_au_lieu_de_deviner(hote):
    """
    Le plan émet `verify_artifact` pour l'archive du runtime ET pour les GGUF.

    Sans câblage capable de rattacher la cible à l'un des deux domaines,
    l'applicateur refuse : choisir au hasard reviendrait à publier un contrôle
    d'intégrité qui n'a pas eu lieu.
    """
    config = ap.ApplierConfig(runtime=None, download=None)
    dispatcher = ap._verify_dispatcher(config, None)
    step = _step(1, sc.ACTION_VERIFY_ARTIFACT, "llama-server b6042 (cuda)")
    resultat = asyncio.run(dispatcher(step, _contexte(ex.ExecutionMode.APPLY)))
    assert resultat.status == ex.STEP_FAILED
    assert "ne correspond ni" in resultat.error


# ══════════════════════════════════════════════════════════════════════════════
# 7. La CLI — son CONTRAT, jamais son rendu
# ══════════════════════════════════════════════════════════════════════════════

def _commande():
    return get_command(cli_module.app).commands["bootstrap-apply"]


def _options() -> set[str]:
    """
    Lit les options sur l'objet Click, pas dans une sortie rendue.

    En vague 5, un test cherchait un nom d'option par sous-chaîne dans `--help` :
    `rich` colorise dès qu'il détecte GitHub Actions et fragmentait le nom par
    des séquences ANSI. L'option était parfaitement déclarée et introuvable — et
    l'échec n'existait qu'en CI.
    """
    return {opt for p in _commande().params for opt in getattr(p, "opts", [])}


def test_la_commande_declare_les_options_attendues():
    attendues = {
        "--apply", "--json", "--allowed-root", "--catalog", "--models-dir",
        "--registry", "--runtime-version", "--hardware-fingerprint", "--vram-budget-gb",
        "--runtime-root", "--llama-server-bin", "--calibration-report-dir",
        "--calibration-port", "--calibration-load-timeout", "--base-url",
        "--admin-url", "--admin-secret-file", "--env-file", "--accept-license",
        "--license-reference", "--ttft-threshold-ms", "--ttft-gate",
    }
    assert attendues <= _options()
    assert "--admin-secret" not in _options()
    # Contrôle positif : la lecture n'invente pas d'options.
    assert "--option-qui-n-existe-pas" not in _options()


def test_l_application_reelle_n_est_pas_le_defaut():
    """
    `ExecutionMode` n'a pas de défaut ; la CLI ne doit pas en réintroduire un.

    Le drapeau est lu sur l'objet Click : sa valeur par défaut doit être fausse,
    et il ne doit exister aucune forme négative qui rendrait l'application
    atteignable par omission.
    """
    param = next(p for p in _commande().params if "--apply" in getattr(p, "opts", []))
    assert param.default is False
    assert param.secondary_opts == []


@pytest.fixture
def allowlist_service(monkeypatch, hote):
    """`ALLOWED_MODEL_DIRS` de la configuration lue par la CLI (sans --env-file)."""
    dirs = [str(hote["models_dir"])]
    monkeypatch.setattr(cli_module.settings, "allowed_model_dirs", dirs)
    return dirs


def test_une_simulation_complete_ne_sort_jamais_en_zero(tmp_path, hote, allowlist_service):
    resultat = CliRunner().invoke(cli_module.app, [
        "bootstrap-apply", str(_plan_file(tmp_path, ETAPES_CHAINE)),
        "--allowed-root", str(hote["tmp_path"]),
        "--registry", str(hote["registry"]),
        "--models-dir", str(hote["models_dir"]),
        "--runtime-version", "b6042",
        "--hardware-fingerprint", "L40S-48G-driver-570.86",
        "--vram-budget-gb", "43.6",
    ])
    # Le plan porte `calibrate_model` et `smoke_test`, qu'aucune option ne câble.
    assert resultat.exit_code == sc.EXIT_USAGE
    sortie = _propre(resultat.output)
    assert "calibrate_model" in sortie and "smoke_test" in sortie


def test_sans_racine_autorisee_la_commande_refuse_en_usage(tmp_path):
    resultat = CliRunner().invoke(cli_module.app, [
        "bootstrap-apply", str(_plan_file(tmp_path, ETAPES_CHAINE)),
    ])
    assert resultat.exit_code == sc.EXIT_USAGE
    assert "allowed-root" in _propre(resultat.output)


def test_un_plan_illisible_sort_en_bloque_pas_en_erreur_interne(tmp_path):
    """
    Grille de sortie : « l'hôte est bloqué » (1) et « l'outil a cassé » (4) ne se confondent pas.

    Un plan qu'on refuse de lire n'est pas une panne de l'applicateur.
    """
    mauvais = tmp_path / "plan.json"
    mauvais.write_text("{ pas du json", encoding="utf-8")
    resultat = CliRunner().invoke(cli_module.app, [
        "bootstrap-apply", str(mauvais), "--allowed-root", str(tmp_path),
    ])
    assert resultat.exit_code == sc.EXIT_BLOCKED
    assert "Plan refusé" in _propre(resultat.output)


def test_un_plan_bloque_n_est_pas_applique(tmp_path):
    document = _plan_document(ETAPES_CHAINE)
    document["sections"][0]["status"] = "fail"
    document["sections"][0]["findings"] = [{
        "code": "gpu_absent", "level": "fail",
        "message": "Aucun GPU exposé sur cet hôte.",
    }]
    chemin = tmp_path / "bloque.json"
    chemin.write_text(json.dumps(document), encoding="utf-8")
    resultat = CliRunner().invoke(cli_module.app, [
        "bootstrap-apply", str(chemin), "--allowed-root", str(tmp_path),
    ])
    assert resultat.exit_code == sc.EXIT_BLOCKED


def test_aucun_secret_ne_sort_de_la_commande(tmp_path):
    """
    Le chemin du plan porte un faux jeton : il ressortirait dans un message d'erreur.

    Le contrôle positif est indispensable — sans lui, ce test passerait au vert
    le jour où la commande n'écrirait plus rien du tout.
    """
    piege = tmp_path / f"plan-{FAUX_TOKEN}.json"
    piege.write_text("{}", encoding="utf-8")
    resultat = CliRunner().invoke(cli_module.app, [
        "bootstrap-apply", str(piege), "--allowed-root", str(tmp_path),
    ])
    sortie = _propre(resultat.output)
    assert sortie.strip()                                          # contrôle positif
    assert sc.find_secret_leaks({"m": FAUX_TOKEN}) != ()           # contrôle positif
    assert FAUX_TOKEN not in sortie


def test_une_erreur_environmentfile_expurge_la_valeur_reelle_du_secret(
    tmp_path, monkeypatch
):
    """La rédaction doit connaître les secrets du fichier, même sans motif reconnaissable."""
    secret = "secret-ordinaire-avec-une-entropie-faible-1234"
    env_file = tmp_path / "service.env"
    env_file.write_text(f"ADMIN_SECRET={secret}\n", encoding="utf-8")
    env_file.chmod(0o640)

    def refuse_settings(_path):
        raise ValueError(f"diagnostic synthétique contenant {secret}")

    monkeypatch.setattr(doctor, "load_settings_from_env_file", refuse_settings)
    resultat = CliRunner().invoke(cli_module.app, [
        "bootstrap-apply", str(_plan_file(tmp_path, ETAPES_CHAINE)),
        "--allowed-root", str(tmp_path),
        "--env-file", str(env_file),
    ])

    sortie = _propre(resultat.output)
    assert resultat.exit_code == sc.EXIT_USAGE
    assert "EnvironmentFile invalide" in sortie  # contrôle positif
    assert "***" in sortie                       # contrôle positif de rédaction
    assert secret not in sortie


def test_le_rendu_json_est_du_json_pur(tmp_path, hote, allowlist_service):
    """
    Le rapport JSON ne doit porter aucun balisage : un script le relit.

    Le plan est réduit à ce que la CLI sait câbler pour que la commande aille
    jusqu'au rendu.
    """
    catalogue_id = "smollm2-360m-instruct-q8_0"
    (hote["models_dir"] / f"{catalogue_id}.gguf").write_bytes(b"GGUF")
    chemin = _plan_file(
        tmp_path, [(sc.ACTION_WRITE_REGISTRY, f"models.yaml → {catalogue_id}")]
    )
    resultat = CliRunner().invoke(cli_module.app, [
        "bootstrap-apply", str(chemin), "--json",
        "--allowed-root", str(hote["tmp_path"]),
        "--registry", str(hote["registry"]),
        "--models-dir", str(hote["models_dir"]),
        "--runtime-version", "b6042",
        "--hardware-fingerprint", "L40S-48G-driver-570.86",
        "--vram-budget-gb", "43.6",
    ])
    assert resultat.exit_code == sc.EXIT_WARNINGS, resultat.output
    document = json.loads(resultat.stdout)
    assert document["execution"]["mode"] == ex.ExecutionMode.DRY_RUN.value
    assert document["execution"]["applied"] is False


# ── Allowlist de modèles : celle du service, pas `(--models-dir,)` ───────────
# Régression : la CLI validait `models.yaml` contre `(--models-dir,)`. Sur le
# registre livré (entrées sous `/models` ET `/data/models`), la commande
# documentée `--models-dir /models` échouait dès la simulation ; à l'inverse,
# un `--models-dir` hors `ALLOWED_MODEL_DIRS` du service écrivait une entrée
# que la gateway refusait au démarrage suivant.

_SHIPPED_REGISTRY = Path(__file__).resolve().parent.parent / "models.yaml"


def _apply_write_registry(tmp_path, hote, *extra):
    catalogue_id = "smollm2-360m-instruct-q8_0"
    (hote["models_dir"] / f"{catalogue_id}.gguf").write_bytes(b"GGUF")
    chemin = _plan_file(
        tmp_path, [(sc.ACTION_WRITE_REGISTRY, f"models.yaml → {catalogue_id}")]
    )
    return CliRunner().invoke(cli_module.app, [
        "bootstrap-apply", str(chemin), "--json",
        "--allowed-root", str(hote["tmp_path"]),
        "--registry", str(hote["registry"]),
        "--runtime-version", "b6042",
        "--hardware-fingerprint", "L40S-48G-driver-570.86",
        "--vram-budget-gb", "43.6",
        *extra,
    ])


def test_registre_livre_sur_deux_repertoires_valide_avec_l_allowlist_du_service(
    tmp_path, hote, monkeypatch
):
    hote["registry"].write_text(_SHIPPED_REGISTRY.read_text(encoding="utf-8"), encoding="utf-8")
    chemins = [
        Path(m["path"])
        for m in yaml.safe_load(hote["registry"].read_text(encoding="utf-8"))["models"]
    ]
    # Contrôle positif : le matériau porte bien les deux répertoires.
    assert any(p.is_relative_to("/data/models") for p in chemins)
    assert any(p.parent == Path("/models") for p in chemins)
    monkeypatch.setattr(
        cli_module.settings, "allowed_model_dirs",
        [str(hote["models_dir"]), "/models", "/data/models"],
    )

    resultat = _apply_write_registry(tmp_path, hote, "--models-dir", str(hote["models_dir"]))

    assert resultat.exit_code == sc.EXIT_WARNINGS, resultat.output  # simulation réussie
    document = json.loads(resultat.stdout)
    assert document["execution"]["applied"] is False


def test_registry_sans_allowed_model_dirs_du_service_est_refuse(tmp_path, hote, monkeypatch):
    monkeypatch.setattr(cli_module.settings, "allowed_model_dirs", [])

    resultat = _apply_write_registry(tmp_path, hote, "--models-dir", str(hote["models_dir"]))

    assert resultat.exit_code == sc.EXIT_USAGE
    assert "ALLOWED_MODEL_DIRS" in _propre(resultat.output)


def test_models_dir_hors_allowlist_du_service_est_refuse_au_cablage(
    tmp_path, hote, monkeypatch
):
    autorise = tmp_path / "service-models"
    autorise.mkdir()
    monkeypatch.setattr(cli_module.settings, "allowed_model_dirs", [str(autorise)])

    resultat = _apply_write_registry(tmp_path, hote, "--models-dir", str(hote["models_dir"]))

    sortie = _propre(resultat.output)
    assert resultat.exit_code == sc.EXIT_USAGE
    assert "Câblage refusé" in sortie and "ALLOWED_MODEL_DIRS" in sortie
    assert hote["registry"].read_text(encoding="utf-8") == REGISTRE_DESACTIVE.format(
        models_dir=hote["models_dir"]
    )


def test_allowed_model_dirs_relatif_est_refuse_au_cablage(tmp_path, hote, monkeypatch):
    """
    La CLI résoudrait une entrée relative depuis son répertoire courant, la
    gateway depuis son WorkingDirectory : la validation ne prouverait plus rien
    sur ce que le service acceptera. Refus explicite, rien n'est écrit.
    """
    monkeypatch.setattr(
        cli_module.settings, "allowed_model_dirs", [str(hote["models_dir"]), "models"]
    )
    avant = hote["registry"].read_bytes()

    resultat = _apply_write_registry(tmp_path, hote, "--models-dir", str(hote["models_dir"]))

    sortie = _propre(resultat.output)
    assert resultat.exit_code == sc.EXIT_USAGE
    assert "relatifs" in sortie and "absolus" in sortie
    assert hote["registry"].read_bytes() == avant
