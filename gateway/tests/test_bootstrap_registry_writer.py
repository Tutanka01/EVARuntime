"""
AUT-007 — régressions du générateur de `models.yaml` (`bootstrap/registry_writer.py`).

Ce module écrit de la **configuration de production**. Deux classes de défaut le
guettent, et le dépôt les a déjà payées toutes les deux :

- COR-014 — de la configuration livrée, conforme à sa documentation, et
  **impossible à charger**. La parade est de ne jamais comparer du texte : chaque
  fichier produit ici est soumis à `model_registry.ModelRegistry`, le validateur
  réel de la gateway ;
- §0.9 — des estimations VRAM optimistes. La parade est que la calibration ne
  peut que **relever** la capacité, et qu'un modèle hors budget n'est pas activé.

Quatre familles d'invariants sont verrouillées :

1. **l'entrée naît désactivée** — comportement ET signature : une option qui
   n'existe pas ne peut pas être mal employée ;
2. **l'activation exige une preuve recoupée** — même modèle, mêmes paramètres,
   même matériel, même runtime, mesure fraîche, chemin public, budget tenu ;
3. **rien n'est perdu** — commentaires de l'exploitant préservés, entrée modifiée
   à la main jamais écrasée, sauvegarde avant écriture, écriture atomique ;
4. **la simulation n'écrit rien** — pas un octet dans le répertoire du registre,
   et le diff exact de ce qui serait appliqué.

Chaque test d'ABSENCE porte son contrôle positif : « rien n'a été écrit » n'a de
valeur que si le même test sait constater une écriture quand il y en a une.
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import model_registry
from bootstrap import catalog as cat
from bootstrap import execution as ex
from bootstrap import registry_writer as rw
from bootstrap import schema as sc

# Instant de référence de tous les tests. Aucun n'appelle l'horloge réelle : la
# fraîcheur d'une preuve doit être testable sans attendre 24 heures.
T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime(rw.PROOF_TIMESTAMP_FORMAT)


# ── Fabriques ─────────────────────────────────────────────────────────────────

def _catalog_dict(**surcharges) -> dict:
    """Une entrée de catalogue SÉRIALISÉE, au format de `CatalogEntry.to_dict()`."""
    base = {
        "id": "qwen2.5-0.5b-instruct-q4_k_m",
        "family": "qwen2.5",
        "display_name": "Qwen2.5 0.5B Instruct — Q4_K_M",
        "description": "Modèle de conversation minimal.",
        "use_cases": ["smoke_test", "chat"],
        "source": {
            "provider": "huggingface",
            "repo_id": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
            "repo_url": "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF",
            "revision": "9217f5db79a29953eb74d5343926648285ec7e67",
            "revision_recorded_on": "2026-07-31",
            "files": [
                {
                    "name": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
                    "role": "weights",
                    "sha256": "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db",
                    "size_bytes": 491400032,
                    "pinned": True,
                },
            ],
            "total_bytes": 491400032,
        },
        "license": {
            "base_model": {"id": "apache-2.0", "url": None, "repo_id": None, "permissive": True},
            "fine_tune": {"id": "apache-2.0", "url": None, "repo_id": None, "permissive": True},
            "usage_terms": None,
            "gated": False,
            "redistribution_allowed": True,
            "operator_acceptance_required": True,
            "permissive": True,
            "notes": None,
        },
        "runtime": {
            "min_llama_build": 0,
            "capabilities": ["text_generation", "streaming"],
            "requires_mmproj": False,
            "defaults": {
                "ctx_size": 8192,
                "parallel": 1,
                "cache_type_k": "q8_0",
                "cache_type_v": "q8_0",
            },
        },
        "resources": {
            "kind": "estimation",
            "disk_gb": 0.46,
            "initial_vram_gb": 1.32,
            "initial_ram_gb": 1.0,
            "estimation_basis": "gguf_header",
            "avertissement": "Valeurs conservatrices, non mesurées.",
        },
        "verification": "pinned",
        "plannable": True,
        "findings": [],
    }
    base.update(surcharges)
    return base


MODEL_ID = "qwen2.5-0.5b-instruct-q4_k_m"

# Registre d'exemple portant des commentaires d'exploitation : ce sont eux qui ne
# doivent jamais disparaître. Le commentaire de fin de ligne sur `vram_gb` est
# volontaire — il est retouché par l'activation.
REGISTRE_EXISTANT = """# Registre des modèles — EVA Inference Gateway
#
# IMPORTANT — estimation vram_gb :
#   Inclure poids + KV cache à concurrence nominale.

models:
  # Modèle principal, réglé à la main après mesure sur site.
  - id: "llama-3.1-8b-instruct"
    path: "{models_dir}/Llama-3.1-8B-Instruct-Q4_K_M.gguf"
    description: "Llama 3.1 8B — modèle léger"
    vram_gb: 5.5             # relevé au nvidia-smi le 2026-07-30
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
def atelier(tmp_path: Path) -> dict:
    """Un hôte de test complet : répertoires, registre, configuration."""
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    registry = tmp_path / "registry" / "models.yaml"
    registry.parent.mkdir()
    registry.write_text(
        REGISTRE_EXISTANT.format(models_dir=models_dir), encoding="utf-8"
    )
    return {
        "tmp_path": tmp_path,
        "models_dir": models_dir,
        "scratch": scratch,
        "registry": registry,
    }


def _config(atelier: dict, *, moment: datetime = T0, **surcharges) -> rw.WriterConfig:
    params = {
        "registry_path": atelier["registry"],
        "models_dir": atelier["models_dir"],
        "allowed_model_dirs": (atelier["models_dir"],),
        "runtime_version": "b6042",
        "hardware_fingerprint": "L40S-48G-driver-570.86",
        "vram_budget_gb": 43.6,
        "catalog_entries": {MODEL_ID: _catalog_dict()},
        "now": lambda: moment,
        "scratch_dir": atelier["scratch"],
    }
    params.update(surcharges)
    return rw.WriterConfig(**params)


def _calibration(**surcharges) -> dict:
    base = {
        "model_id": MODEL_ID,
        "runtime_version": "b6042",
        "hardware_fingerprint": "L40S-48G-driver-570.86",
        "params_fingerprint": rw.params_fingerprint(
            {"ctx_size": 8192, "parallel": 1, "cache_type_k": "q8_0", "cache_type_v": "q8_0"}
        ),
        "peak_vram_gb": 1.8,
        "peak_ram_gb": 0.9,
        "load_seconds": 3.2,
        "measured_at": _iso(T0 - timedelta(minutes=5)),
    }
    base.update(surcharges)
    return base


def _smoke_test(**surcharges) -> dict:
    base = {
        "model_id": MODEL_ID,
        "endpoint": "/v1/chat/completions",
        "http_status": 200,
        "ttft_ms": 412,
        "completion_tokens": 16,
        "measured_at": _iso(T0 - timedelta(minutes=2)),
    }
    base.update(surcharges)
    return base


def _preuve(calibration: dict | None = None, smoke_test: dict | None = None) -> rw.ActivationProof:
    return rw.ActivationProof.from_mapping({
        "calibration": calibration if calibration is not None else _calibration(),
        "smoke_test": smoke_test if smoke_test is not None else _smoke_test(),
    })


def _empreinte_repertoire(chemin: Path) -> dict[str, bytes]:
    """Contenu intégral d'un répertoire : ce qu'une simulation ne doit pas modifier."""
    return {
        p.name: p.read_bytes()
        for p in sorted(chemin.iterdir())
        if p.is_file()
    }


def _entree(registry: Path, model_id: str) -> dict:
    document = yaml.safe_load(registry.read_text(encoding="utf-8"))
    for entry in document["models"]:
        if entry["id"] == model_id:
            return entry
    raise AssertionError(f"« {model_id} » absent du registre")


def _set_enabled_yaml(registry: Path, model_id: str, value) -> None:
    document = yaml.safe_load(registry.read_text(encoding="utf-8"))
    for entry in document["models"]:
        if entry["id"] == model_id:
            entry["enabled"] = value
            registry.write_text(yaml.safe_dump(document), encoding="utf-8")
            return
    raise AssertionError(f"« {model_id} » absent du registre")


def _step(action: str, target: str, order: int = 1) -> sc.PlanStep:
    return sc.PlanStep(
        order=order,
        action=action,
        target=target,
        detail="étape de test",
        requires_root=True,
        reversible=True,
    )


def _context(mode: ex.ExecutionMode, journal: list[str] | None = None) -> ex.ExecutionContext:
    return ex.ExecutionContext(
        mode=mode,
        allowed_roots=(Path("/"),),
        now=lambda: _iso(T0),
        log=(journal.append if journal is not None else (lambda m: None)),
    )


# ══ 1. L'entrée naît désactivée ═══════════════════════════════════════════════

def test_entree_generee_est_desactivee(atelier):
    entry = rw.build_registry_entry(
        _catalog_dict(),
        models_dir=atelier["models_dir"],
        allowed_model_dirs=(atelier["models_dir"],),
    )
    assert entry["enabled"] is False
    # Contrôle positif : le champ existe bien et est lu — un `enabled` absent
    # vaudrait `true` au chargement du registre.
    assert "enabled" in entry


def test_aucune_api_publique_ne_permet_d_ecrire_une_entree_activee():
    """
    Barrière structurelle : aucune signature n'expose de quoi contourner le défaut.

    Un test purement comportemental laisserait quelqu'un ajouter demain un
    `enabled: bool = False` — le comportement par défaut resterait vert, et
    l'invariant du jalon deviendrait une politesse.
    """
    for fonction in (rw.build_registry_entry, rw.write_model_entry):
        parametres = set(inspect.signature(fonction).parameters)
        assert "enabled" not in parametres, (
            f"{fonction.__name__} expose un paramètre « enabled » : l'activation "
            "cesserait d'être une action distincte"
        )
    # Contrôle positif : l'introspection voit bien les autres paramètres.
    assert "models_dir" in inspect.signature(rw.build_registry_entry).parameters
    assert "mode" in inspect.signature(rw.write_model_entry).parameters


def test_ecriture_reelle_pose_enabled_false_dans_le_fichier(atelier):
    config = _config(atelier)
    change = rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    assert change.status == ex.STEP_DONE
    assert _entree(atelier["registry"], MODEL_ID)["enabled"] is False
    assert "enabled: false" in atelier["registry"].read_text(encoding="utf-8")


def test_invariant_desactive_verifie_avant_ecriture(atelier, monkeypatch):
    """Si la génération rendait une entrée activée, l'écriture s'arrêterait avant le disque."""
    original = rw.build_registry_entry

    def _sabotage(*args, **kwargs):
        entry = original(*args, **kwargs)
        entry["enabled"] = True
        return entry

    monkeypatch.setattr(rw, "build_registry_entry", _sabotage)
    avant = atelier["registry"].read_bytes()
    with pytest.raises(rw.RegistryWriterError, match="invariant AUT-007"):
        rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.APPLY)
    assert atelier["registry"].read_bytes() == avant


# ══ 2. Le fichier produit est chargeable — la leçon de COR-014 ════════════════

def test_le_fichier_ecrit_est_charge_par_model_registry(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)

    registre = model_registry.ModelRegistry(
        atelier["registry"], allowed_model_dirs=[str(atelier["models_dir"])]
    )
    modele = registre.get(MODEL_ID)
    assert modele is not None
    assert modele.enabled is False
    assert modele.path == atelier["models_dir"] / "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    assert modele.llama_params.ctx_size == 8192
    assert modele.llama_params.parallel == 1
    assert modele.sha256 == _catalog_dict()["source"]["files"][0]["sha256"]


def test_ecriture_refuse_enabled_false_entre_guillemets(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    _set_enabled_yaml(atelier["registry"], MODEL_ID, "false")
    avant = atelier["registry"].read_bytes()

    with pytest.raises(
        rw.RegistryWriterError, match="enabled.*booléen YAML réel.*non quoté"
    ):
        rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)

    assert atelier["registry"].read_bytes() == avant


def test_l_entree_reelle_du_catalogue_livre_produit_un_registre_chargeable(atelier):
    """
    Bout en bout sur les VRAIES données : `catalog.yaml` → `models.yaml` → chargement.

    Le catalogue est la source de ce module ; le tester contre une fixture seule
    laisserait passer une divergence entre la forme sérialisée réelle et celle
    qu'on imagine.
    """
    catalogue = cat.load_catalog()
    entrees = {e.id: e.to_dict() for e in catalogue.plannable_entries()}
    assert entrees, "le catalogue livré ne contient aucune entrée planifiable"

    config = _config(atelier, catalog_entries=entrees)
    for model_id in entrees:
        change = rw.write_model_entry(config, model_id, mode=ex.ExecutionMode.APPLY)
        assert change.status == ex.STEP_DONE, change.error

    registre = model_registry.ModelRegistry(
        atelier["registry"], allowed_model_dirs=[str(atelier["models_dir"])]
    )
    assert {m.id for m in registre.list_all()} >= set(entrees)
    assert all(m.enabled is False for m in registre.list_all() if m.id in entrees)


def test_un_candidat_illisible_par_le_registre_n_est_jamais_publie(atelier):
    """
    Contrôle de la garde elle-même : si le candidat ne charge pas, rien ne bouge.

    Simulé par un `vram_gb` nul, que `ModelRegistry` refuse — c'est la validation
    réelle qui doit dire non, pas une pré-vérification maison.
    """
    catalogue = _catalog_dict()
    catalogue["resources"] = dict(catalogue["resources"], initial_vram_gb=0.0)
    config = _config(atelier, catalog_entries={MODEL_ID: catalogue})
    avant = atelier["registry"].read_bytes()
    with pytest.raises(rw.RegistryWriterError):
        rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    assert atelier["registry"].read_bytes() == avant
    assert not list(atelier["registry"].parent.glob("*.tmp"))


def test_le_temporaire_est_efface_si_la_validation_echoue(atelier, monkeypatch):
    monkeypatch.setattr(
        rw, "_validate_loadable",
        lambda *a, **k: (_ for _ in ()).throw(rw.RegistryWriterError("candidat refusé")),
    )
    avant = _empreinte_repertoire(atelier["registry"].parent)
    with pytest.raises(rw.RegistryWriterError, match="candidat refusé"):
        rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.APPLY)
    apres = _empreinte_repertoire(atelier["registry"].parent)
    # La sauvegarde est légitimement présente ; le temporaire, jamais.
    assert atelier["registry"].read_bytes() == avant["models.yaml"]
    assert not [n for n in apres if n.endswith(".tmp")]


# ══ 3. Préservation du travail de l'exploitant ════════════════════════════════

def test_les_commentaires_du_registre_survivent_a_une_ecriture(atelier):
    avant = atelier["registry"].read_text(encoding="utf-8")
    rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.APPLY)
    apres = atelier["registry"].read_text(encoding="utf-8")

    assert apres.startswith(avant), (
        "l'écriture doit AJOUTER en fin de document, sans réécrire un octet de l'existant"
    )
    for commentaire in (
        "# IMPORTANT — estimation vram_gb :",
        "# Modèle principal, réglé à la main après mesure sur site.",
        "# relevé au nvidia-smi le 2026-07-30",
    ):
        assert commentaire in apres


def test_les_commentaires_survivent_aussi_a_une_activation(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    change = rw.enable_model_entry(config, MODEL_ID, _preuve(), mode=ex.ExecutionMode.APPLY)
    assert change.status == ex.STEP_DONE, change.error

    apres = atelier["registry"].read_text(encoding="utf-8")
    for commentaire in (
        "# IMPORTANT — estimation vram_gb :",
        "# Modèle principal, réglé à la main après mesure sur site.",
        "# relevé au nvidia-smi le 2026-07-30",
        "# Entrée générée par le bootstrap EVARuntime",
    ):
        assert commentaire in apres
    # L'entrée voisine n'a pas bougé d'un iota.
    assert _entree(atelier["registry"], "llama-3.1-8b-instruct")["vram_gb"] == 5.5
    assert _entree(atelier["registry"], "llama-3.1-8b-instruct")["enabled"] is False


def test_le_commentaire_de_fin_de_ligne_survit_a_la_retouche_de_vram(atelier):
    """
    Une activation retouche `vram_gb` ; le commentaire qui documente cette valeur
    est de l'information d'exploitation, pas du décor.
    """
    config = _config(atelier)
    # On active l'entrée écrite à la main, celle qui porte le commentaire.
    calibration = _calibration(
        model_id="llama-3.1-8b-instruct",
        params_fingerprint=rw.params_fingerprint(
            _entree(atelier["registry"], "llama-3.1-8b-instruct")["llama_params"]
        ),
        peak_vram_gb=6.0,
    )
    preuve = _preuve(calibration, _smoke_test(model_id="llama-3.1-8b-instruct"))
    change = rw.enable_model_entry(
        config, "llama-3.1-8b-instruct", preuve, mode=ex.ExecutionMode.APPLY
    )
    assert change.status == ex.STEP_DONE, change.error
    texte = atelier["registry"].read_text(encoding="utf-8")
    assert "# relevé au nvidia-smi le 2026-07-30" in texte
    assert _entree(atelier["registry"], "llama-3.1-8b-instruct")["vram_gb"] == 6.9
    assert _entree(atelier["registry"], "llama-3.1-8b-instruct")["enabled"] is True


def test_une_entree_modifiee_par_l_exploitant_n_est_pas_ecrasee(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)

    # L'exploitant réduit le contexte pour cohabiter avec un autre modèle.
    texte = atelier["registry"].read_text(encoding="utf-8")
    texte = texte.replace("ctx_size: 8192", "ctx_size: 4096")
    atelier["registry"].write_text(texte, encoding="utf-8")
    avant = atelier["registry"].read_bytes()

    change = rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    assert change.status == ex.STEP_FAILED
    assert "llama_params" in change.evidence["diverging_fields"]
    assert change.evidence["written"] is False
    assert atelier["registry"].read_bytes() == avant
    assert [f.code for f in change.findings] == ["registre_entree_divergente"]


def test_un_vram_abaisse_par_l_exploitant_est_traite_comme_une_divergence(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    texte = atelier["registry"].read_text(encoding="utf-8").replace(
        "vram_gb: 1.4", "vram_gb: 0.9"
    )
    atelier["registry"].write_text(texte, encoding="utf-8")

    change = rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    assert change.status == ex.STEP_FAILED
    assert change.evidence["diverging_fields"] == ["vram_gb"]


def test_une_entree_deja_activee_ne_declenche_pas_de_conflit(atelier):
    """
    Contrôle positif du test précédent : la seule évolution SANCTIONNÉE — activée
    et capacité relevée — ne doit pas faire échouer une réexécution du plan.
    """
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    rw.enable_model_entry(config, MODEL_ID, _preuve(), mode=ex.ExecutionMode.APPLY)

    change = rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    assert change.status == ex.STEP_ALREADY_SATISFIED
    assert change.evidence["enabled"] is True


def test_reappliquer_ne_duplique_pas_l_entree(atelier):
    config = _config(atelier)
    premier = rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    second = rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)

    assert premier.status == ex.STEP_DONE
    assert second.status == ex.STEP_ALREADY_SATISFIED
    assert second.evidence["written"] is False
    document = yaml.safe_load(atelier["registry"].read_text(encoding="utf-8"))
    assert [e["id"] for e in document["models"]].count(MODEL_ID) == 1


def test_un_identifiant_duplique_dans_le_fichier_bloque_toute_ecriture(atelier):
    document = yaml.safe_load(atelier["registry"].read_text(encoding="utf-8"))
    document["models"].append(copy.deepcopy(document["models"][0]))
    atelier["registry"].write_text(yaml.safe_dump(document), encoding="utf-8")
    config = _config(
        atelier,
        catalog_entries={"llama-3.1-8b-instruct": _catalog_dict(id="llama-3.1-8b-instruct")},
    )
    with pytest.raises(rw.RegistryWriterError, match="figure 2 fois"):
        rw.write_model_entry(config, "llama-3.1-8b-instruct", mode=ex.ExecutionMode.APPLY)


def test_un_registre_illisible_n_est_pas_reecrit(atelier):
    atelier["registry"].write_text("models:\n  - id: [oups\n", encoding="utf-8")
    avant = atelier["registry"].read_bytes()
    with pytest.raises(rw.RegistryWriterError, match="n'est pas un YAML lisible"):
        rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.APPLY)
    assert atelier["registry"].read_bytes() == avant


def test_un_document_sans_cle_models_est_refuse(atelier):
    atelier["registry"].write_text("autre_chose: 1\n", encoding="utf-8")
    with pytest.raises(rw.RegistryWriterError, match="clé « models » est absente"):
        rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.APPLY)


def test_un_registre_absent_est_cree_avec_son_entete(atelier):
    cible = atelier["tmp_path"] / "neuf" / "models.yaml"
    cible.parent.mkdir()
    config = _config(atelier, registry_path=cible)
    change = rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)

    assert change.status == ex.STEP_DONE
    assert change.evidence["backup"] is None  # rien à sauvegarder
    texte = cible.read_text(encoding="utf-8")
    assert "enabled: false" in texte
    assert "L'activation est une" in texte
    model_registry.ModelRegistry(cible, allowed_model_dirs=[str(atelier["models_dir"])])


def test_une_mise_en_page_qui_empeche_l_ajout_sur_est_refusee(atelier):
    """
    L'ajout en fin de fichier n'est sûr QUE parce que le résultat est reparsé.

    Ici `models` n'est pas la dernière clé : ajouter en fin de document
    rattacherait l'entrée à la mauvaise clé. Le refus est la bonne issue — un
    repli sur une réécriture globale détruirait les commentaires.
    """
    atelier["registry"].write_text(
        "models: []\n\ndivers:\n  - a\n", encoding="utf-8"
    )
    avant = atelier["registry"].read_bytes()
    with pytest.raises(rw.RegistryWriterError, match="ne signifie pas ce qui était prévu"):
        rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.APPLY)
    assert atelier["registry"].read_bytes() == avant


# ── COR-028 — l'ordre des clés d'une entrée ne décide pas de sa lisibilité ────
#
# `yaml.safe_dump` trie les clés et place `capabilities` avant `id`. C'est ce que
# produisait `ModelRegistry._save()` avant COR-020, donc ce qu'on trouve sur tout
# hôte dont le `models.yaml` est déjà passé par une mutation admin. Le
# localisateur d'entrée ancrait sur « - id: <model_id> » : sur ces fichiers-là,
# l'étape `enable_model` — maillon de la chaîne de preuve M2 — refusait en
# fail-closed un fichier parfaitement valide.

def _reserialiser_trie(registry: Path) -> None:
    """Réécrit le registre comme `yaml.safe_dump` le ferait : clés triées."""
    document = yaml.safe_load(registry.read_text(encoding="utf-8"))
    registry.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=True), encoding="utf-8"
    )


def test_une_entree_dont_id_n_est_pas_la_premiere_cle_reste_activable(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    _reserialiser_trie(atelier["registry"])
    texte = atelier["registry"].read_text(encoding="utf-8")
    # Contrôle positif de la mise en scène : `id` n'est vraiment plus en tête.
    assert f"- id: {MODEL_ID}" not in texte
    assert "capabilities:" in texte

    change = rw.enable_model_entry(config, MODEL_ID, _preuve(), mode=ex.ExecutionMode.APPLY)

    assert change.status == ex.STEP_DONE, change.error
    assert _entree(atelier["registry"], MODEL_ID)["enabled"] is True


def test_l_activation_provisoire_lit_aussi_une_entree_a_cles_triees(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    _reserialiser_trie(atelier["registry"])

    change = rw.provisionally_enable_model_entry(
        config, MODEL_ID,
        rw.CalibrationProof.from_mapping(_calibration()),
        mode=ex.ExecutionMode.APPLY,
    )

    assert change.status == ex.STEP_DONE, change.error
    # L'activation provisoire ne touche pas le disque : c'est tout son intérêt.
    assert _entree(atelier["registry"], MODEL_ID)["enabled"] is False
    assert change.evidence["disk_enabled"] is False


def test_le_rollback_lit_aussi_une_entree_a_cles_triees(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    rw.enable_model_entry(config, MODEL_ID, _preuve(), mode=ex.ExecutionMode.APPLY)
    _reserialiser_trie(atelier["registry"])

    change = rw.rollback_provisional_model_entry(
        config, MODEL_ID, reason="recette non confirmée", mode=ex.ExecutionMode.APPLY
    )

    assert change.status == ex.STEP_DONE, change.error
    assert _entree(atelier["registry"], MODEL_ID)["enabled"] is False


def test_une_entree_reellement_introuvable_est_toujours_refusee(atelier):
    """Le localisateur voit plus large ; il ne refuse pas moins. Fail-closed intact."""
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    lignes = atelier["registry"].read_text(encoding="utf-8").split("\n")

    with pytest.raises(rw.RegistryWriterError, match="entrée"):
        rw._entry_block_bounds(lignes, "modele-qui-n-existe-pas")
    # Contrôle positif : le même appel sait bien trouver une entrée présente.
    debut, fin, indent = rw._entry_block_bounds(lignes, MODEL_ID)
    assert fin > debut and indent > 0


def test_le_localisateur_est_celui_du_registre_pas_un_second(atelier):
    """
    COR-028 : une seule logique de localisation dans le dépôt, pas deux.

    Deux implémentations de la même politique d'écriture sur le même fichier est
    le défaut que COR-020 a fermé côté admin ; l'entretenir ici l'aurait rouvert.
    """
    lignes = (
        "models:\n"
        "  - capabilities: [text_generation]\n"
        f"    id: {MODEL_ID}\n"
        "    vram_gb: 1.4\n"
    ).split("\n")

    assert rw._entry_block_bounds(lignes, MODEL_ID) == model_registry._entry_bounds(
        list(lignes), MODEL_ID
    )


# ══ 4. Sauvegarde et écriture atomique ════════════════════════════════════════

def _sauvegardes(registry: Path) -> list[Path]:
    return sorted(registry.parent.glob(f"{registry.name}{rw.BACKUP_INFIX}*{rw.BACKUP_SUFFIX}"))


def test_une_sauvegarde_precede_toute_ecriture(atelier):
    avant = atelier["registry"].read_bytes()
    change = rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.APPLY)

    sauvegardes = _sauvegardes(atelier["registry"])
    assert len(sauvegardes) == 1
    assert sauvegardes[0].read_bytes() == avant, (
        "la sauvegarde doit contenir l'ÉTAT D'AVANT, pas le résultat"
    )
    assert change.evidence["backup"] == str(sauvegardes[0])
    assert atelier["registry"].read_bytes() != avant  # contrôle positif


def test_une_operation_sans_ecriture_ne_produit_pas_de_sauvegarde(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    apres_premier = len(_sauvegardes(atelier["registry"]))

    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)  # already_satisfied
    assert len(_sauvegardes(atelier["registry"])) == apres_premier == 1


def test_les_sauvegardes_sont_bornees(atelier):
    """
    OPS-002 constate que les `*.pre-migration.*.bak` s'accumulent sans purge.
    Ne pas reproduire le défaut : la rétention est bornée et ne touche QUE le motif.
    """
    config = _config(atelier, backup_retention=2)
    # Un intrus qui ressemble à une sauvegarde sans en être une.
    intrus = atelier["registry"].parent / "models.yaml.pre-migration.v3.20260101T000000Z.bak"
    intrus.write_text("ne pas toucher", encoding="utf-8")

    for i in range(4):
        moment = T0 + timedelta(minutes=i)
        rw.enable_model_entry(
            _config(atelier, moment=moment, backup_retention=2),
            "llama-3.1-8b-instruct",
            _preuve(
                _calibration(
                    model_id="llama-3.1-8b-instruct",
                    params_fingerprint=rw.params_fingerprint(
                        _entree(atelier["registry"], "llama-3.1-8b-instruct")["llama_params"]
                    ),
                    peak_vram_gb=6.0 + i,
                    measured_at=_iso(moment - timedelta(minutes=1)),
                ),
                _smoke_test(
                    model_id="llama-3.1-8b-instruct",
                    measured_at=_iso(moment - timedelta(minutes=1)),
                ),
            ),
            mode=ex.ExecutionMode.APPLY,
        )

    assert len(_sauvegardes(atelier["registry"])) == 2
    assert intrus.exists(), "la purge ne doit toucher que son propre motif"
    assert intrus.read_text(encoding="utf-8") == "ne pas toucher"
    # Contrôle positif : sans borne, il y en aurait bien eu quatre.
    assert config.backup_retention == 2


def test_une_retention_nulle_est_refusee(atelier):
    with pytest.raises(rw.RegistryWriterError, match="backup_retention"):
        _config(atelier, backup_retention=0)


def test_la_sauvegarde_reprend_les_permissions_de_l_original(atelier):
    atelier["registry"].chmod(0o640)
    rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.APPLY)
    sauvegarde = _sauvegardes(atelier["registry"])[0]
    assert sauvegarde.stat().st_mode & 0o777 == 0o640
    assert atelier["registry"].stat().st_mode & 0o777 == 0o640


def test_deux_ecritures_dans_la_meme_seconde_ne_s_ecrasent_pas(atelier):
    """`O_EXCL` : une sauvegarde existante n'est jamais recouverte."""
    config = _config(atelier)  # horloge figée : même horodatage pour les deux
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    rw.enable_model_entry(config, MODEL_ID, _preuve(), mode=ex.ExecutionMode.APPLY)
    sauvegardes = _sauvegardes(atelier["registry"])
    assert len(sauvegardes) == 2
    assert sauvegardes[0].read_bytes() != sauvegardes[1].read_bytes()


# ══ 5. La simulation n'écrit rien ═════════════════════════════════════════════

def test_simulation_d_ecriture_ne_touche_pas_un_octet(atelier):
    avant = _empreinte_repertoire(atelier["registry"].parent)
    change = rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.DRY_RUN)

    assert change.status == ex.STEP_WOULD_APPLY
    assert change.evidence["written"] is False
    assert _empreinte_repertoire(atelier["registry"].parent) == avant
    assert list(atelier["registry"].parent.iterdir()) == [atelier["registry"]]
    # Contrôle positif : la même opération en mode application modifie bien tout.
    rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.APPLY)
    assert _empreinte_repertoire(atelier["registry"].parent) != avant


def test_simulation_ne_laisse_rien_dans_son_repertoire_de_travail(atelier):
    rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.DRY_RUN)
    assert list(atelier["scratch"].iterdir()) == []


def test_la_simulation_valide_quand_meme_que_le_resultat_se_chargerait(atelier):
    """
    Une simulation qui ne validerait rien promettrait « ceci s'appliquerait » sans
    l'avoir vérifié. Contrôle : un candidat invalide échoue AUSSI en simulation.
    """
    sain = rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.DRY_RUN)
    assert sain.evidence["models_after"] == 2

    catalogue = _catalog_dict()
    catalogue["resources"] = dict(catalogue["resources"], initial_vram_gb=0.0)
    with pytest.raises(rw.RegistryWriterError):
        rw.write_model_entry(
            _config(atelier, catalog_entries={MODEL_ID: catalogue}),
            MODEL_ID,
            mode=ex.ExecutionMode.DRY_RUN,
        )


def test_la_simulation_montre_le_diff_exact(atelier):
    change = rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.DRY_RUN)
    diff = change.evidence["diff"]
    ajouts = [ligne[1:] for ligne in diff if ligne.startswith("+") and not ligne.startswith("+++")]
    retraits = [ligne for ligne in diff if ligne.startswith("-") and not ligne.startswith("---")]

    assert retraits == [], "une écriture d'entrée ne retire aucune ligne"
    assert any(f"- id: {MODEL_ID}" in ligne for ligne in ajouts)
    assert any("enabled: false" in ligne for ligne in ajouts)

    # Le diff annoncé est exactement ce que l'application produit.
    rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.APPLY)
    reel = atelier["registry"].read_text(encoding="utf-8").split("\n")
    for ligne in ajouts:
        assert ligne in reel


def test_simulation_d_activation_ne_touche_pas_un_octet(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    avant = _empreinte_repertoire(atelier["registry"].parent)

    change = rw.enable_model_entry(config, MODEL_ID, _preuve(), mode=ex.ExecutionMode.DRY_RUN)
    assert change.status == ex.STEP_WOULD_APPLY
    assert _empreinte_repertoire(atelier["registry"].parent) == avant
    assert _entree(atelier["registry"], MODEL_ID)["enabled"] is False

    diff = [ligne for ligne in change.evidence["diff"] if ligne[:1] in "+-" and ligne[:3] not in ("+++", "---")]
    assert any("enabled: true" in ligne for ligne in diff)
    assert any("enabled: false" in ligne for ligne in diff)


# ══ 6. L'activation exige une preuve, et la recoupe ═══════════════════════════

def test_sans_preuve_le_modele_reste_desactive(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    avant = atelier["registry"].read_bytes()

    change = rw.enable_model_entry(config, MODEL_ID, None, mode=ex.ExecutionMode.APPLY)
    assert change.status == ex.STEP_FAILED
    assert [f.code for f in change.findings] == ["activation_sans_preuve"]
    assert atelier["registry"].read_bytes() == avant
    assert _entree(atelier["registry"], MODEL_ID)["enabled"] is False

    # Contrôle positif : la MÊME opération avec preuve active réellement.
    ok = rw.enable_model_entry(config, MODEL_ID, _preuve(), mode=ex.ExecutionMode.APPLY)
    assert ok.status == ex.STEP_DONE
    assert _entree(atelier["registry"], MODEL_ID)["enabled"] is True


def test_activation_refuse_enabled_false_entre_guillemets(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    _set_enabled_yaml(atelier["registry"], MODEL_ID, "false")
    avant = atelier["registry"].read_bytes()

    with pytest.raises(
        rw.RegistryWriterError, match="enabled.*booléen YAML réel.*non quoté"
    ):
        rw.enable_model_entry(
            config, MODEL_ID, _preuve(), mode=ex.ExecutionMode.APPLY
        )

    assert atelier["registry"].read_bytes() == avant


def test_budget_refuse_enabled_non_booleen_sur_un_autre_modele(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    _set_enabled_yaml(atelier["registry"], "llama-3.1-8b-instruct", "false")
    avant = atelier["registry"].read_bytes()

    with pytest.raises(
        rw.RegistryWriterError, match="llama-3.1-8b-instruct.*enabled.*booléen YAML"
    ):
        rw.enable_model_entry(
            config, MODEL_ID, _preuve(), mode=ex.ExecutionMode.APPLY
        )

    assert atelier["registry"].read_bytes() == avant


@pytest.mark.parametrize("preuve_nue", [True, 1, "ok", {"calibration": True}])
def test_un_booleen_nu_n_est_pas_une_preuve(atelier, preuve_nue):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    change = rw.enable_model_entry(config, MODEL_ID, preuve_nue, mode=ex.ExecutionMode.APPLY)
    assert change.status == ex.STEP_FAILED
    assert change.evidence["enabled"] is False
    assert _entree(atelier["registry"], MODEL_ID)["enabled"] is False


def test_la_preuve_exige_ses_deux_volets():
    with pytest.raises(rw.ProofRejected, match="calibration.*smoke_test"):
        rw.ActivationProof.from_mapping({"calibration": _calibration()})
    with pytest.raises(rw.ProofRejected, match="calibration.*smoke_test"):
        rw.ActivationProof.from_mapping({"smoke_test": _smoke_test()})
    # Contrôle positif : les deux ensemble sont acceptés.
    assert rw.ActivationProof.from_mapping(
        {"calibration": _calibration(), "smoke_test": _smoke_test()}
    ).model_id == MODEL_ID


def test_les_deux_volets_doivent_parler_du_meme_modele():
    with pytest.raises(rw.ProofRejected, match="pas du même modèle"):
        rw.ActivationProof.from_mapping({
            "calibration": _calibration(),
            "smoke_test": _smoke_test(model_id="autre-modele"),
        })


def test_une_cle_inconnue_dans_la_preuve_est_refusee():
    with pytest.raises(rw.ProofRejected, match="clés inconnues"):
        rw.CalibrationProof.from_mapping(_calibration(succeeded=True))


@pytest.mark.parametrize("surcharge, motif", [
    ({"peak_vram_gb": 0.0}, "peak_vram_gb"),
    ({"load_seconds": 0.0}, "load_seconds"),
    ({"peak_vram_gb": True}, "doit être un nombre"),
    ({"params_fingerprint": "abc"}, "params_fingerprint"),
    ({"measured_at": "2026-08-01 12:00:00"}, "measured_at"),
    ({"model_id": "MAJUSCULES"}, "model_id"),
])
def test_une_calibration_mal_formee_est_refusee(surcharge, motif):
    with pytest.raises(rw.ProofRejected, match=motif):
        rw.CalibrationProof.from_mapping(_calibration(**surcharge))


@pytest.mark.parametrize("surcharge, motif", [
    ({"http_status": 500}, "http_status"),
    ({"ttft_ms": 0}, "ttft_ms"),
    ({"completion_tokens": 0}, "completion_tokens"),
    ({"endpoint": "http://127.0.0.1:8081/completion"}, "chemin public"),
    ({"endpoint": "/admin/models"}, "chemin public"),
    ({"ttft_ms": True}, "doit être un entier"),
])
def test_une_recette_mal_formee_est_refusee(surcharge, motif):
    with pytest.raises(rw.ProofRejected, match=motif):
        rw.SmokeTestProof.from_mapping(_smoke_test(**surcharge))


def test_une_calibration_faite_avec_d_autres_parametres_ne_vaut_rien(atelier):
    """
    Le recoupement le plus important : l'empreinte des paramètres est RECALCULÉE
    depuis l'entrée du registre, pas recopiée depuis la preuve.
    """
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)

    autre = rw.params_fingerprint({"ctx_size": 32768, "parallel": 8})
    change = rw.enable_model_entry(
        config, MODEL_ID, _preuve(_calibration(params_fingerprint=autre)),
        mode=ex.ExecutionMode.APPLY,
    )
    assert change.status == ex.STEP_FAILED
    assert autre in change.findings[0].message
    assert rw.params_fingerprint(_entree(atelier["registry"], MODEL_ID)["llama_params"]) \
        in change.findings[0].message
    assert _entree(atelier["registry"], MODEL_ID)["enabled"] is False


@pytest.mark.parametrize("surcharge, fragment", [
    ({"runtime_version": "b5000"}, "runtime"),
    ({"hardware_fingerprint": "RTX4090-24G"}, "matériel"),
    ({"model_id": "autre-modele"}, "concerne"),
])
def test_une_mesure_d_un_autre_contexte_est_refusee(atelier, surcharge, fragment):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    calibration = _calibration(**surcharge)
    smoke_test = _smoke_test(model_id=calibration["model_id"])
    change = rw.enable_model_entry(
        config, MODEL_ID, _preuve(calibration, smoke_test), mode=ex.ExecutionMode.APPLY
    )
    assert change.status == ex.STEP_FAILED
    assert fragment in change.findings[0].message
    assert _entree(atelier["registry"], MODEL_ID)["enabled"] is False


def test_une_preuve_perimee_est_refusee(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    vieille = _iso(T0 - timedelta(days=3))
    change = rw.enable_model_entry(
        config, MODEL_ID,
        _preuve(_calibration(measured_at=vieille), _smoke_test(measured_at=vieille)),
        mode=ex.ExecutionMode.APPLY,
    )
    assert change.status == ex.STEP_FAILED
    assert "fraîcheur" in change.findings[0].message
    assert _entree(atelier["registry"], MODEL_ID)["enabled"] is False


def test_une_preuve_datee_du_futur_est_refusee(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    futur = _iso(T0 + timedelta(hours=1))
    change = rw.enable_model_entry(
        config, MODEL_ID, _preuve(_calibration(measured_at=futur)),
        mode=ex.ExecutionMode.APPLY,
    )
    assert change.status == ex.STEP_FAILED
    assert "futur" in change.findings[0].message


def test_un_leger_decalage_d_horloge_reste_accepte(atelier):
    """Contrôle positif du test précédent : la tolérance existe et fonctionne."""
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    proche = _iso(T0 + timedelta(seconds=30))
    change = rw.enable_model_entry(
        config, MODEL_ID, _preuve(_calibration(measured_at=proche)),
        mode=ex.ExecutionMode.APPLY,
    )
    assert change.status == ex.STEP_DONE, change.error


def test_l_horloge_injectee_est_reellement_celle_qui_decide(atelier):
    """La fraîcheur dépend de `config.now`, pas de l'heure de la machine de test."""
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    mesure = _iso(T0 - timedelta(hours=12))
    preuve = _preuve(_calibration(measured_at=mesure), _smoke_test(measured_at=mesure))

    tot = rw.enable_model_entry(config, MODEL_ID, preuve, mode=ex.ExecutionMode.DRY_RUN)
    assert tot.status == ex.STEP_WOULD_APPLY

    tard = rw.enable_model_entry(
        _config(atelier, moment=T0 + timedelta(days=2)), MODEL_ID, preuve,
        mode=ex.ExecutionMode.DRY_RUN,
    )
    assert tard.status == ex.STEP_FAILED


def test_activer_un_modele_absent_du_registre_echoue(atelier):
    change = rw.enable_model_entry(
        _config(atelier), MODEL_ID, _preuve(), mode=ex.ExecutionMode.APPLY
    )
    assert change.status == ex.STEP_FAILED
    assert [f.code for f in change.findings] == ["activation_entree_absente"]


def test_activer_deux_fois_est_idempotent(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    premier = rw.enable_model_entry(config, MODEL_ID, _preuve(), mode=ex.ExecutionMode.APPLY)
    empreinte = atelier["registry"].read_bytes()
    second = rw.enable_model_entry(config, MODEL_ID, _preuve(), mode=ex.ExecutionMode.APPLY)

    assert premier.status == ex.STEP_DONE
    assert second.status == ex.STEP_ALREADY_SATISFIED
    assert atelier["registry"].read_bytes() == empreinte


# ══ 7. Conservatisme des ressources ═══════════════════════════════════════════

def test_l_estimation_ecrite_est_arrondie_vers_le_haut(atelier):
    rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.APPLY)
    # 1.32 Go estimés → 1.4, jamais 1.3.
    assert _entree(atelier["registry"], MODEL_ID)["vram_gb"] == 1.4


def test_la_calibration_releve_la_capacite_avec_marge(atelier):
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    rw.enable_model_entry(
        config, MODEL_ID, _preuve(_calibration(peak_vram_gb=1.8)),
        mode=ex.ExecutionMode.APPLY,
    )
    # 1.8 × 1.15 = 2.07 → arrondi supérieur au dixième = 2.1
    assert _entree(atelier["registry"], MODEL_ID)["vram_gb"] == 2.1


def test_la_calibration_n_abaisse_jamais_la_capacite(atelier):
    """
    Un pic bas peut venir d'un contexte réduit ou d'un prompt trop court. §0.9 a
    déjà payé l'optimisme d'estimation une fois : la valeur ne descend pas.
    """
    config = _config(atelier)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    avant = _entree(atelier["registry"], MODEL_ID)["vram_gb"]
    rw.enable_model_entry(
        config, MODEL_ID, _preuve(_calibration(peak_vram_gb=0.2)),
        mode=ex.ExecutionMode.APPLY,
    )
    assert _entree(atelier["registry"], MODEL_ID)["vram_gb"] == avant == 1.4


def test_un_modele_hors_budget_n_est_pas_active(atelier):
    config = _config(atelier, vram_budget_gb=10.0)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    change = rw.enable_model_entry(
        config, MODEL_ID, _preuve(_calibration(peak_vram_gb=40.0)),
        mode=ex.ExecutionMode.APPLY,
    )
    assert change.status == ex.STEP_FAILED
    assert [f.code for f in change.findings] == ["activation_hors_budget"]
    assert _entree(atelier["registry"], MODEL_ID)["enabled"] is False
    # Contrôle positif : sous le budget, le même modèle s'active.
    ok = rw.enable_model_entry(
        _config(atelier, vram_budget_gb=60.0), MODEL_ID,
        _preuve(_calibration(peak_vram_gb=40.0)), mode=ex.ExecutionMode.APPLY,
    )
    assert ok.status == ex.STEP_DONE


def test_une_somme_d_actives_superieure_au_budget_avertit_sans_bloquer(atelier):
    """
    Ce n'est pas un invariant de la gateway (la file de capacité évince), mais
    l'exploitant doit le savoir : ces modèles ne coexisteront pas en VRAM.
    """
    config = _config(atelier, vram_budget_gb=6.0)
    rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    # On active d'abord le voisin à 5.5 Go.
    texte = atelier["registry"].read_text(encoding="utf-8").replace(
        "enabled: false\n    capabilities", "enabled: true\n    capabilities", 1
    )
    atelier["registry"].write_text(texte, encoding="utf-8")

    change = rw.enable_model_entry(
        config, MODEL_ID, _preuve(_calibration(peak_vram_gb=1.8)),
        mode=ex.ExecutionMode.APPLY,
    )
    assert change.status == ex.STEP_DONE
    assert [f.code for f in change.findings] == ["registre_budget_sursouscrit"]
    assert [f.level for f in change.findings] == ["warn"]


def test_le_budget_doit_etre_declare(atelier):
    with pytest.raises(rw.RegistryWriterError, match="vram_budget_gb"):
        _config(atelier, vram_budget_gb=0.0)


def test_une_marge_inferieure_a_un_est_refusee(atelier):
    with pytest.raises(rw.RegistryWriterError, match="vram_safety_factor"):
        _config(atelier, vram_safety_factor=0.9)


# ══ 8. Chemins de modèles et allowlist ════════════════════════════════════════

def test_allowed_model_dirs_vide_est_refuse(atelier):
    with pytest.raises(rw.RegistryWriterError, match="allowed_model_dirs est vide"):
        _config(atelier, allowed_model_dirs=())


def test_un_models_dir_hors_allowlist_est_refuse(atelier):
    dehors = atelier["tmp_path"] / "ailleurs"
    dehors.mkdir()
    with pytest.raises(ex.ExecutionError, match="hors des racines autorisées"):
        _config(atelier, models_dir=dehors)


def test_un_nom_de_fichier_qui_remonte_l_arborescence_est_refuse(atelier):
    catalogue = _catalog_dict()
    catalogue["source"]["files"][0]["name"] = "../../etc/evil.gguf"
    with pytest.raises(rw.RegistryWriterError, match="nom simple est attendu"):
        rw.build_registry_entry(
            catalogue,
            models_dir=atelier["models_dir"],
            allowed_model_dirs=(atelier["models_dir"],),
        )


def test_un_fichier_qui_n_est_pas_un_gguf_est_refuse(atelier):
    catalogue = _catalog_dict()
    catalogue["source"]["files"][0]["name"] = "modele.bin"
    with pytest.raises(rw.RegistryWriterError, match="gguf"):
        rw.build_registry_entry(
            catalogue,
            models_dir=atelier["models_dir"],
            allowed_model_dirs=(atelier["models_dir"],),
        )


def test_le_chemin_genere_reste_dans_l_allowlist(atelier):
    entry = rw.build_registry_entry(
        _catalog_dict(),
        models_dir=atelier["models_dir"],
        allowed_model_dirs=(atelier["models_dir"],),
    )
    assert Path(entry["path"]).is_relative_to(atelier["models_dir"].resolve())


def test_la_generation_refuse_un_models_dir_hors_allowlist(atelier):
    """
    `build_registry_entry()` est PUBLIQUE et reçoit `models_dir` et l'allowlist
    séparément : elle ne peut pas se reposer sur la validation de `WriterConfig`.
    Sans ce contrôle, un appelant produirait une entrée pointant où il veut.
    """
    dehors = atelier["tmp_path"] / "ailleurs"
    dehors.mkdir()
    with pytest.raises(ex.ExecutionError, match="hors des racines autorisées"):
        rw.build_registry_entry(
            _catalog_dict(),
            models_dir=dehors,
            allowed_model_dirs=(atelier["models_dir"],),
        )
    # Contrôle positif : la même génération réussit dans un répertoire autorisé.
    assert rw.build_registry_entry(
        _catalog_dict(),
        models_dir=atelier["models_dir"],
        allowed_model_dirs=(atelier["models_dir"],),
    )["path"]


def test_la_generation_refuse_une_allowlist_vide(atelier):
    """Une liste vide n'autorise rien — elle ne signifie pas « pas de contrainte »."""
    with pytest.raises(ex.ExecutionError, match="aucune racine autorisée"):
        rw.build_registry_entry(
            _catalog_dict(),
            models_dir=atelier["models_dir"],
            allowed_model_dirs=(),
        )


# ══ 9. Refus d'entrées de catalogue non fiables ═══════════════════════════════

def test_une_entree_non_epinglee_n_entre_pas_au_registre(atelier):
    """
    `plannable: true` est un champ DÉRIVÉ du catalogue : il est recalculé ici, et
    ne suffit pas à autoriser l'écriture s'il ment.
    """
    catalogue = _catalog_dict()
    catalogue["source"]["files"][0]["sha256"] = None
    catalogue["plannable"] = True  # champ dérivé mensonger, délibérément
    catalogue["verification"] = "pinned"
    with pytest.raises(rw.RegistryWriterError, match="SHA-256 manquant"):
        rw.build_registry_entry(
            catalogue,
            models_dir=atelier["models_dir"],
            allowed_model_dirs=(atelier["models_dir"],),
        )


def test_une_entree_sans_revision_n_entre_pas_au_registre(atelier):
    catalogue = _catalog_dict()
    catalogue["source"]["revision"] = None
    with pytest.raises(rw.RegistryWriterError, match="révision épinglée"):
        rw.build_registry_entry(
            catalogue,
            models_dir=atelier["models_dir"],
            allowed_model_dirs=(atelier["models_dir"],),
        )


def test_un_depot_gated_n_entre_pas_au_registre(atelier):
    catalogue = _catalog_dict()
    catalogue["license"] = dict(catalogue["license"], gated=True)
    with pytest.raises(rw.RegistryWriterError, match="gated"):
        rw.build_registry_entry(
            catalogue,
            models_dir=atelier["models_dir"],
            allowed_model_dirs=(atelier["models_dir"],),
        )


def test_un_ensemble_sans_poids_est_refuse(atelier):
    catalogue = _catalog_dict()
    catalogue["source"]["files"] = [{
        "name": "mmproj-f16.gguf", "role": "mmproj",
        "sha256": "a" * 64, "size_bytes": 10, "pinned": True,
    }]
    with pytest.raises(rw.RegistryWriterError, match="aucun fichier de poids"):
        rw.build_registry_entry(
            catalogue,
            models_dir=atelier["models_dir"],
            allowed_model_dirs=(atelier["models_dir"],),
        )


def test_un_ensemble_splitte_pointe_sur_le_premier_shard(atelier):
    catalogue = _catalog_dict()
    catalogue["source"]["files"] = [
        {
            "name": f"modele-{i:05d}-of-00003.gguf", "role": "weights_shard",
            "sha256": f"{i}" * 64, "size_bytes": 10, "pinned": True,
        }
        for i in (2, 1, 3)
    ]
    entry = rw.build_registry_entry(
        catalogue,
        models_dir=atelier["models_dir"],
        allowed_model_dirs=(atelier["models_dir"],),
    )
    assert entry["path"].endswith("modele-00001-of-00003.gguf")
    assert entry["sha256"] == "1" * 64


def test_un_modele_exigeant_un_mmproj_sans_mmproj_est_refuse(atelier):
    catalogue = _catalog_dict()
    catalogue["runtime"] = dict(catalogue["runtime"], requires_mmproj=True)
    with pytest.raises(rw.RegistryWriterError, match="projecteur multimodal"):
        rw.build_registry_entry(
            catalogue,
            models_dir=atelier["models_dir"],
            allowed_model_dirs=(atelier["models_dir"],),
        )


def test_un_modele_vision_sans_mmproj_est_refuse_meme_si_flag_incoherent(atelier):
    catalogue = _catalog_dict()
    catalogue["runtime"] = dict(
        catalogue["runtime"],
        capabilities=["text_generation", "vision"],
        requires_mmproj=False,
    )
    with pytest.raises(rw.RegistryWriterError, match="projecteur multimodal"):
        rw.build_registry_entry(
            catalogue,
            models_dir=atelier["models_dir"],
            allowed_model_dirs=(atelier["models_dir"],),
        )


@pytest.mark.parametrize("role", ["weights", "mmproj"])
def test_empreinte_artefact_invalide_est_refusee(atelier, role):
    catalogue = _catalog_dict()
    if role == "weights":
        catalogue["source"]["files"][0]["sha256"] = "pas-un-sha"
    else:
        catalogue["runtime"] = dict(
            catalogue["runtime"],
            capabilities=["text_generation", "vision"],
            requires_mmproj=True,
        )
        catalogue["source"] = dict(
            catalogue["source"],
            files=[
                *catalogue["source"]["files"],
                {
                    "name": "mmproj-f16.gguf",
                    "role": "mmproj",
                    "sha256": "pas-un-sha",
                    "size_bytes": 10,
                    "pinned": True,
                },
            ],
        )
    with pytest.raises(rw.RegistryWriterError, match="SHA-256 invalide"):
        rw.build_registry_entry(
            catalogue,
            models_dir=atelier["models_dir"],
            allowed_model_dirs=(atelier["models_dir"],),
        )


def test_un_modele_vision_emet_l_empreinte_de_son_mmproj(atelier):
    catalogue = _catalog_dict()
    catalogue["runtime"] = dict(
        catalogue["runtime"],
        capabilities=["text_generation", "vision"],
        requires_mmproj=True,
    )
    catalogue["source"] = dict(
        catalogue["source"],
        files=[
            *catalogue["source"]["files"],
            {
                "name": "mmproj-f16.gguf",
                "role": "mmproj",
                "sha256": "b" * 64,
                "size_bytes": 10,
                "pinned": True,
            },
        ],
    )

    entry = rw.build_registry_entry(
        catalogue,
        models_dir=atelier["models_dir"],
        allowed_model_dirs=(atelier["models_dir"],),
    )

    assert entry["mmproj_path"].endswith("mmproj-f16.gguf")
    assert entry["mmproj_sha256"] == "b" * 64


def test_un_modele_inconnu_du_catalogue_ne_produit_aucune_entree(atelier):
    change = rw.write_model_entry(_config(atelier), "modele-fantome", mode=ex.ExecutionMode.APPLY)
    assert change.status == ex.STEP_FAILED
    assert [f.code for f in change.findings] == ["registre_modele_hors_catalogue"]
    assert change.evidence["written"] is False


def test_des_parametres_refuses_par_le_registre_sont_refuses_ici(atelier):
    catalogue = _catalog_dict()
    catalogue["runtime"]["defaults"] = dict(
        catalogue["runtime"]["defaults"], cache_type_k="q3_0"
    )
    with pytest.raises(rw.RegistryWriterError, match="refusés par le registre"):
        rw.build_registry_entry(
            catalogue,
            models_dir=atelier["models_dir"],
            allowed_model_dirs=(atelier["models_dir"],),
        )


# ══ 10. Empreinte des paramètres ══════════════════════════════════════════════

def test_l_empreinte_des_parametres_normalise_les_defauts():
    """Deux écritures équivalentes donnent la même empreinte."""
    partiel = rw.params_fingerprint({"ctx_size": 8192, "parallel": 1})
    complet = rw.params_fingerprint({
        "ctx_size": 8192, "parallel": 1, "n_gpu_layers": 999,
        "batch_size": 4096, "ubatch_size": 512, "cache_type_k": "q8_0",
        "cache_type_v": "q8_0", "flash_attn": True, "threads": 8,
        "threads_http": 4, "cpu_moe": False,
    })
    assert partiel == complet
    assert partiel.startswith("sha256:")
    # Contrôle positif : un paramètre réellement différent change l'empreinte.
    assert rw.params_fingerprint({"ctx_size": 4096, "parallel": 1}) != partiel


def test_l_empreinte_refuse_des_parametres_inexploitables():
    with pytest.raises(rw.RegistryWriterError, match="empreinte"):
        rw.params_fingerprint({"ctx_size": 10})  # < 512, refusé par LlamaParams


def test_l_empreinte_de_l_entree_ecrite_est_celle_annoncee(atelier):
    change = rw.write_model_entry(_config(atelier), MODEL_ID, mode=ex.ExecutionMode.APPLY)
    entree = _entree(atelier["registry"], MODEL_ID)
    assert change.evidence["params_fingerprint"] == rw.params_fingerprint(entree["llama_params"])


# ══ 11. Exécuteurs, registre d'actions, rapport ═══════════════════════════════

def test_register_executors_branche_les_deux_actions(atelier):
    registre = ex.ExecutorRegistry()
    rw.register_executors(registre, _config(atelier))
    assert set(registre.registered_actions()) == {
        sc.ACTION_WRITE_REGISTRY, sc.ACTION_ENABLE_MODEL
    }
    # Aucune autre action n'est capturée au passage.
    assert sc.ACTION_DOWNLOAD_MODEL not in registre


def test_un_second_enregistrement_est_refuse(atelier):
    registre = ex.ExecutorRegistry()
    rw.register_executors(registre, _config(atelier))
    with pytest.raises(ex.ExecutionError, match="déjà enregistré"):
        rw.register_executors(registre, _config(atelier))


@pytest.mark.parametrize("action, cible, attendu", [
    (sc.ACTION_WRITE_REGISTRY, f"models.yaml → {MODEL_ID}", MODEL_ID),
    (sc.ACTION_ENABLE_MODEL, MODEL_ID, MODEL_ID),
    (sc.ACTION_WRITE_REGISTRY, f"  models.yaml →  {MODEL_ID} ", MODEL_ID),
])
def test_la_cible_d_etape_est_decodee(action, cible, attendu):
    assert rw.model_id_from_target(action, cible) == attendu


@pytest.mark.parametrize("cible", ["", "MAJUSCULES", "../etc/passwd", "models.yaml → ", "a b"])
def test_une_cible_inexploitable_est_refusee(cible):
    with pytest.raises(rw.RegistryWriterError, match="cible d'étape"):
        rw.model_id_from_target(sc.ACTION_ENABLE_MODEL, cible)


def test_le_parcours_complet_en_simulation_puis_en_application(atelier):
    """
    Les deux étapes enchaînées par `execute_plan`, dans les deux modes.

    La simulation ne doit rien écrire et le rapport doit être publiable ; puis
    l'application doit produire une entrée activée et un rapport valide.
    """
    config = _config(atelier)
    etapes = (
        _step(sc.ACTION_WRITE_REGISTRY, f"models.yaml → {MODEL_ID}", 1),
        _step(sc.ACTION_ENABLE_MODEL, MODEL_ID, 2),
    )
    plan = ex.LoadedPlan(
        document={}, steps=etapes, fingerprint="sha256:" + "0" * 64,
        generated_at=_iso(T0), mode="local", origin="<test>",
    )
    config = _config(atelier, activation_proofs={MODEL_ID: _preuve()})

    registre = ex.ExecutorRegistry()
    rw.register_executors(registre, config)

    avant = _empreinte_repertoire(atelier["registry"].parent)
    simulation = asyncio.run(
        ex.execute_plan(plan, registre, _context(ex.ExecutionMode.DRY_RUN))
    )
    assert [r.status for r in simulation.results] == [ex.STEP_WOULD_APPLY, ex.STEP_WOULD_APPLY]
    assert simulation.verdict() == ex.VERDICT_PARTIAL
    assert _empreinte_repertoire(atelier["registry"].parent) == avant
    assert ex.validate_execution_document(simulation.to_dict()) == ()
    assert json.loads(ex.render_execution_json(simulation))["applied"] is False

    registre2 = ex.ExecutorRegistry()
    rw.register_executors(registre2, config)
    application = asyncio.run(
        ex.execute_plan(plan, registre2, _context(ex.ExecutionMode.APPLY))
    )
    assert [r.status for r in application.results] == [ex.STEP_DONE, ex.STEP_DONE]
    assert application.verdict() == ex.VERDICT_OK
    assert _entree(atelier["registry"], MODEL_ID)["enabled"] is True
    assert ex.validate_execution_document(application.to_dict()) == ()
    ex.render_execution_json(application)  # refuse de publier un rapport qui fuit


def test_l_activation_sans_preuve_arrete_la_sequence(atelier):
    """
    Fail-closed de bout en bout : sans preuve, l'étape échoue et l'exécution
    s'arrête — le modèle n'est ni activé, ni préchauffé.
    """
    config = _config(atelier)  # aucune preuve fournie
    etapes = (
        _step(sc.ACTION_WRITE_REGISTRY, f"models.yaml → {MODEL_ID}", 1),
        _step(sc.ACTION_ENABLE_MODEL, MODEL_ID, 2),
        _step(sc.ACTION_WARMUP_MODEL, MODEL_ID, 3),
    )
    plan = ex.LoadedPlan(
        document={}, steps=etapes, fingerprint="sha256:" + "1" * 64,
        generated_at=_iso(T0), mode="local", origin="<test>",
    )
    registre = ex.ExecutorRegistry()
    rw.register_executors(registre, config)
    rapport = asyncio.run(ex.execute_plan(plan, registre, _context(ex.ExecutionMode.APPLY)))

    assert [r.status for r in rapport.results] == [
        ex.STEP_DONE, ex.STEP_FAILED, ex.STEP_NOT_ATTEMPTED
    ]
    assert rapport.verdict() == ex.VERDICT_FAILED
    assert _entree(atelier["registry"], MODEL_ID)["enabled"] is False
    assert ex.validate_execution_document(rapport.to_dict()) == ()


def test_les_preuves_d_execution_sont_serialisables_et_sans_secret(atelier):
    config = _config(atelier)
    ecriture = rw.write_model_entry(config, MODEL_ID, mode=ex.ExecutionMode.APPLY)
    activation = rw.enable_model_entry(config, MODEL_ID, _preuve(), mode=ex.ExecutionMode.APPLY)

    for change in (ecriture, activation):
        json.dumps(change.evidence)  # sérialisable
        assert sc.find_secret_leaks(change.evidence) == ()
    # Contrôle positif : le détecteur employé sait bien voir une fuite.
    assert sc.find_secret_leaks({"note": "hf_" + "A" * 24}) != ()


def test_l_executeur_journalise_par_le_chemin_expurge(atelier):
    journal: list[str] = []
    config = _config(atelier)
    executeur = rw.make_write_registry_executor(config)
    etape = _step(sc.ACTION_WRITE_REGISTRY, f"models.yaml → {MODEL_ID}")
    resultat = asyncio.run(executeur(etape, _context(ex.ExecutionMode.APPLY, journal)))

    assert resultat.status == ex.STEP_DONE
    assert any(MODEL_ID in ligne for ligne in journal)


def test_la_simulation_recoupe_la_preuve_contre_l_entree_projetee(atelier):
    """
    En simulation, l'entrée n'existe pas encore : l'étape d'écriture n'a rien
    appliqué. La preuve est alors recoupée contre l'entrée qui SERAIT écrite —
    c'est le seul moment où l'opérateur peut découvrir qu'elle est mauvaise sans
    avoir déjà touché à sa machine.
    """
    config = _config(atelier)
    bonne = rw.enable_model_entry(config, MODEL_ID, _preuve(), mode=ex.ExecutionMode.DRY_RUN)
    assert bonne.status == ex.STEP_WOULD_APPLY
    assert bonne.evidence["simulated_on_projected_entry"] is True
    assert bonne.evidence["diff"] == []

    mauvaise = rw.enable_model_entry(
        config, MODEL_ID,
        _preuve(_calibration(params_fingerprint=rw.params_fingerprint({"ctx_size": 32768}))),
        mode=ex.ExecutionMode.DRY_RUN,
    )
    assert mauvaise.status == ex.STEP_FAILED
    assert _empreinte_repertoire(atelier["registry"].parent).keys() == {"models.yaml"}


def test_en_application_une_entree_absente_reste_un_echec(atelier):
    """Contrôle du test précédent : la tolérance est propre à la SIMULATION."""
    change = rw.enable_model_entry(
        _config(atelier), MODEL_ID, _preuve(), mode=ex.ExecutionMode.APPLY
    )
    assert change.status == ex.STEP_FAILED
    assert [f.code for f in change.findings] == ["activation_entree_absente"]


def test_la_preuve_est_publiable_sous_ses_deux_formes():
    """
    Le résumé d'étape ET la sérialisation complète doivent rester publiables.

    La seconde ne l'était pas : `find_secret_leaks()` refusait alors tout champ
    dont le NOM contenait « token », et `completion_tokens` suffisait à rendre
    un rapport impubliable. Le défaut est corrigé dans `schema` ; `digest()`
    subsiste comme résumé compact, plus comme contournement.
    """
    preuve = _preuve()
    assert sc.find_secret_leaks(preuve.digest()) == ()
    assert preuve.digest()["smoke_produced_output"] is True
    assert sc.find_secret_leaks(preuve.to_dict()) == ()
    # Contrôle positif : le détecteur voit toujours une vraie fuite dans ce document.
    assert sc.find_secret_leaks({**preuve.to_dict(), "hf_token": "x"}) != ()
