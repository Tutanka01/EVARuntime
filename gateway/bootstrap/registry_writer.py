"""
AUT-007 — écrire `models.yaml`, puis n'activer que sur preuve (jalon M2).

Ce que ce module est
--------------------
Le pont entre le **catalogue** (ce que le bootstrap a le droit de proposer) et le
**registre opérationnel** (ce que CETTE installation sert). §8 exige que les deux
restent distincts ; ce module les relie sans jamais les confondre : il lit une
entrée de catalogue *sérialisée* et en dérive une entrée de registre. Il
n'importe pas `bootstrap.catalog` — la règle d'indépendance des producteurs de la
vague 5 vaut aussi pour les exécuteurs de la vague 6.

Il exécute deux actions du contrat, et deux seulement :

- `schema.ACTION_WRITE_REGISTRY` — écrire l'entrée, **toujours `enabled: false`** ;
- `schema.ACTION_ENABLE_MODEL` — autoriser son ouverture provisoire **en
  mémoire** sur calibration, puis persister cette activation sur preuve complète
  (DEC-010).

Pourquoi deux actions et pas une
--------------------------------
Le critère d'acceptation du backlog est « entrée désactivée tant que la
calibration et le smoke test n'ont pas réussi ». Un seul exécuteur portant un
drapeau `enabled=…` aurait rendu l'activation atteignable par un argument par
défaut mal choisi. Ici, `build_registry_entry()` ne prend **aucun paramètre**
permettant d'écrire autre chose que `false`. L'ouverture provisoire exige une
calibration structurée et fraîche ; le fichier reste à `false` pendant cette
fenêtre, afin qu'un crash ou un redémarrage revienne à l'état sûr. L'activation
définitive exige les deux volets de preuve. La compensation ferme le registre
vivant et vérifie que le disque est toujours à `false`.

La preuve exigée pour activer
-----------------------------
Deux autres chantiers la produisent (AUT-008 calibration, AUT-009 recette du
premier token) et ce module ne les importe pas. La preuve lui est donc **fournie
en entrée**, sous la forme d'un `ActivationProof` — jamais un booléen. Un booléen
nu ne dit ni de quel modèle il parle, ni avec quels paramètres, ni sur quel
matériel, ni quand : il n'est pas recoupable, et la vague 5 a passé trois passes
de revue à fermer des champs dérivés que personne ne recoupait.

Sont donc recoupés, à la confirmation définitive :

1. les deux volets de la preuve nomment le **même** `model_id`, et c'est celui de
   l'entrée du registre ;
2. `params_fingerprint` de la calibration égale l'empreinte **recalculée ici** à
   partir des `llama_params` réellement écrits dans le registre. Une calibration
   faite à `ctx_size: 4096` n'autorise pas une entrée qui servira à `32768` ;
3. `runtime_version` et `hardware_fingerprint` égalent ceux de l'hôte courant
   (§9 : « une mesure n'est réutilisable que si matériel, runtime et paramètres
   sont compatibles ») ;
4. les deux mesures sont datées, ni futures ni périmées, selon l'horloge injectée ;
5. la recette a traversé le **chemin public** (`/v1/…`), a répondu `200` et a
   produit au moins un token avec un TTFT mesuré (§10).

Un seul de ces points en défaut ⇒ refus et compensation immédiate par
l'applicateur ; l'entrée revient désactivée.

Ce que la génération garantit
-----------------------------
- **`enabled: false` à l'écriture**, sans exception ni option de contournement ;
- **le fichier produit est chargeable** : avant tout renommage, le candidat est
  validé par `model_registry.ModelRegistry` lui-même, pas par une reformulation
  de ses règles. C'est la leçon de COR-014, où trois réglages documentés étaient
  impossibles à charger et où le `.env.example` livré produisait un service mort ;
- **écriture atomique et sauvegarde préalable** : sauvegarde horodatée
  `models.yaml.pre-bootstrap.<stamp>.bak` (convention des migrations SQLite de
  `database.py`), puis fichier temporaire dans le même répertoire, `fsync`,
  validation, `os.replace`, `fsync` du répertoire. Contrairement aux sauvegardes
  de migration — qui ne sont **pas purgées** (OPS-002) —, celles-ci sont bornées
  par `backup_retention` ;
- **préservation** : le texte existant n'est jamais réécrit par un `yaml.dump`
  global. Une nouvelle entrée est **ajoutée** en fin de document, une activation
  est une **retouche de deux lignes**, et dans les deux cas le texte candidat est
  reparsé puis comparé au document attendu, structure comprise. Les commentaires
  de l'exploitant — qui sont de la documentation d'exploitation, pas du bruit —
  survivent intégralement ;
- **idempotence** : réappliquer ne duplique rien. Une entrée déjà conforme rend
  `already_satisfied` sans écrire ni sauvegarder ;
- **aucune écriture en simulation** : ni le registre, ni son répertoire, ni une
  sauvegarde. Le résultat porte le diff unifié exact qui *serait* appliqué.

Règle de fusion, tranchée et assumée
------------------------------------
Une entrée existante n'est **jamais fusionnée** avec l'entrée générée. Si elle
diffère, l'exécuteur **refuse** et nomme les champs qui divergent. Deux
exceptions, et seulement dans le sens sanctionné par `enable_model` :
`enabled` passé à `true`, et `vram_gb` **augmenté**. Tout le reste — un
`ctx_size` réduit par l'exploitant, un `path` déplacé, un `vram_gb` abaissé —
est du réglage d'exploitant, et l'écraser serait une perte de données.

Conservatisme des ressources
----------------------------
§9 : « le registre initial doit rester conservateur ». À l'écriture, `vram_gb`
vaut l'estimation du catalogue arrondie au dixième **supérieur**. À l'activation,
il vaut `max(estimation déjà écrite, pic mesuré × marge)` : la calibration ne
peut que **relever** la capacité réservée, jamais l'abaisser. Une mesure basse
peut venir d'un contexte réduit ou d'un prompt trop court ; l'abaissement reste
une décision d'exploitant, prise à la main. Et un modèle dont la capacité dépasse
le budget net n'est pas activé du tout.
"""
from __future__ import annotations

import asyncio
import copy
import difflib
import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

import model_registry

from . import execution, schema

# Version du générateur. Figure dans les preuves d'exécution pour qu'un registre
# régénéré plus tard soit rattachable à la logique qui l'a produit.
REGISTRY_WRITER_VERSION = 2

# Convention de sauvegarde, calquée sur `database._backup_path()` :
# « <nom d'origine>.<raison>.<horodatage>.bak ».
BACKUP_INFIX = ".pre-bootstrap."
BACKUP_SUFFIX = ".bak"

# Nombre de sauvegardes conservées. OPS-002 constate que les `*.pre-migration.*`
# s'accumulent sans purge ; ne pas reproduire le défaut sans le borner.
DEFAULT_BACKUP_RETENTION = 5

# Au-delà, une mesure décrit un autre état de la machine (§9).
DEFAULT_PROOF_MAX_AGE_SECONDS = 24 * 3600

# Tolérance d'horloge : une preuve légèrement « dans le futur » vient d'un
# décalage NTP, pas d'une falsification. Au-delà, c'est un refus.
CLOCK_SKEW_TOLERANCE_SECONDS = 120

# Marge appliquée au pic mesuré avant d'écrire `vram_gb`. Le pic d'une
# calibration n'est pas un plafond : fragmentation du driver, graphes CUDA et
# batchs réels s'y ajoutent (§9).
DEFAULT_VRAM_SAFETY_FACTOR = 1.15

# Granularité d'arrondi de `vram_gb`, toujours vers le HAUT.
VRAM_ROUNDING_GB = 0.1

# La recette du premier token doit passer par le chemin public réel (§10).
PUBLIC_ENDPOINT_PREFIX = "/v1/"

# Horodatage des preuves : le format de `execution._utc_now_iso()`, à la seconde.
PROOF_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Préfixe employé par le planificateur pour la cible d'une écriture de registre.
_WRITE_TARGET_PREFIX = "models.yaml"

# Champs qu'`enable_model` a le droit de faire évoluer, et eux seuls. Toute autre
# divergence entre l'entrée présente et l'entrée générée est un réglage
# d'exploitant : on refuse plutôt que d'écraser.
_MUTABLE_BY_ACTIVATION: tuple[str, ...] = ("enabled", "vram_gb")

# Regex de `model_registry._MODEL_ID_RE`, redite ici pour valider AVANT d'écrire
# plutôt qu'après. La règle de vérité reste celle du registre : le candidat lui
# est soumis en entier avant tout renommage.
_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_SHARD_FIRST_RE = re.compile(r"-00001-of-\d{5}\.gguf$")


# ── Erreurs ───────────────────────────────────────────────────────────────────

class RegistryWriterError(execution.ExecutionError):
    """L'écriture du registre ne peut pas avoir lieu. Fail-closed, toujours."""


class ProofRejected(RegistryWriterError):
    """La preuve d'activation est absente, mal formée ou ne concerne pas ce cas."""


class RegistryConflict(RegistryWriterError):
    """L'entrée présente diffère de l'entrée générée : refus d'écraser."""


def _enabled_state(entry: Mapping[str, Any], model_id: str | None = None) -> bool:
    """Lit ``enabled`` sans coercition ; absent conserve le défaut historique ``true``."""
    value = entry.get("enabled", True)
    if type(value) is not bool:
        raw_id = model_id if model_id is not None else entry.get("id")
        entry_id = raw_id if isinstance(raw_id, str) else "entrée sans id"
        raise RegistryWriterError(
            f"« {entry_id} » : enabled doit être un booléen YAML réel "
            f"(true ou false non quoté), reçu {value!r}"
        )
    return value


# ── Horloge injectable ────────────────────────────────────────────────────────

def _utc_now() -> datetime:
    """Horloge réelle. Défaut de production, remplacée dans les tests."""
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any, champ: str) -> datetime:
    """Horodatage ISO-8601 UTC strict. Aucune tolérance de format : fail-closed."""
    if not isinstance(value, str) or not value:
        raise ProofRejected(f"{champ} doit être une chaîne non vide, reçu {value!r}")
    try:
        parsed = datetime.strptime(value, PROOF_TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise ProofRejected(
            f"{champ} doit suivre « {PROOF_TIMESTAMP_FORMAT} » (UTC), reçu {value!r} : {exc}"
        ) from exc
    return parsed.replace(tzinfo=timezone.utc)


# ── Empreinte des paramètres de service ───────────────────────────────────────

def params_fingerprint(llama_params: Mapping[str, Any] | None) -> str:
    """
    Empreinte `sha256:<hex>` des paramètres de service EFFECTIFS d'un modèle.

    Normalisée par `model_registry.LlamaParams` avant hachage : les valeurs
    omises prennent la valeur par défaut du registre, donc deux écritures
    équivalentes donnent la même empreinte — et un changement de défaut dans le
    registre en donne une autre, ce qui **invalide** les calibrations
    antérieures. C'est voulu : elles ne décrivent plus ce qui sera servi.

    C'est la valeur que la calibration (AUT-008) doit reporter dans
    `params_fingerprint` pour que son résultat soit acceptable ici.
    """
    raw = dict(llama_params or {})
    try:
        effective = model_registry.LlamaParams(**raw)
    except (TypeError, ValueError) as exc:
        raise RegistryWriterError(
            f"llama_params inexploitables pour le calcul d'empreinte : {exc}"
        ) from exc
    payload = json.dumps(asdict(effective), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── Preuve d'activation ───────────────────────────────────────────────────────

def _require_float(node: Mapping[str, Any], key: str, path: str, *, minimum: float) -> float:
    value = node.get(key)
    # `bool` est un `int` : sans ce rejet, `True` passerait pour `1.0`.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProofRejected(
            f"{path}.{key} doit être un nombre, reçu {type(value).__name__} ({value!r})"
        )
    if not math.isfinite(float(value)) or float(value) < minimum:
        raise ProofRejected(f"{path}.{key} doit être un nombre fini >= {minimum}, reçu {value!r}")
    return float(value)


def _require_int(node: Mapping[str, Any], key: str, path: str, *, minimum: int) -> int:
    value = node.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProofRejected(
            f"{path}.{key} doit être un entier, reçu {type(value).__name__} ({value!r})"
        )
    if value < minimum:
        raise ProofRejected(f"{path}.{key} doit être >= {minimum}, reçu {value!r}")
    return value


def _require_str(node: Mapping[str, Any], key: str, path: str) -> str:
    value = node.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProofRejected(f"{path}.{key} doit être une chaîne non vide, reçu {value!r}")
    return value.strip()


def _reject_unknown(node: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
    """
    Une clé inconnue est un refus, pas un silence.

    Une preuve qui porte un champ que ce module ignore décrit peut-être une
    condition que ce module ne vérifie pas. L'accepter en l'ignorant, c'est
    exactement le repli silencieux que le dépôt proscrit.
    """
    unknown = sorted(set(node) - allowed)
    if unknown:
        raise ProofRejected(
            f"{path} porte des clés inconnues : {unknown} (attendu parmi {sorted(allowed)})"
        )


_CALIBRATION_KEYS = frozenset({
    "model_id", "runtime_version", "hardware_fingerprint", "params_fingerprint",
    "peak_vram_gb", "peak_ram_gb", "load_seconds", "measured_at",
})

# Exposé publiquement pour que l'applicateur PROJETTE un document de producteur
# sur ce jeu de clés au lieu d'en recopier la liste chez lui. Une seconde liste
# tenue à la main finirait par diverger de celle qui décide réellement, et c'est
# celle qui n'a pas été mise à jour qui laisserait passer une preuve incomplète.
CALIBRATION_PROOF_KEYS: frozenset[str] = _CALIBRATION_KEYS


@dataclass(frozen=True)
class CalibrationProof:
    """
    Résultat d'un chargement RÉEL, au format du rapport de §9.

    Aucun champ « succeeded » : la présence de pics mesurés strictement positifs
    et d'une durée de chargement EST le succès. Un booléen séparé pourrait
    contredire les mesures qu'il accompagne, et c'est toujours le booléen qu'on
    lit en diagonale.
    """
    model_id: str
    runtime_version: str
    hardware_fingerprint: str
    params_fingerprint: str
    peak_vram_gb: float
    peak_ram_gb: float
    load_seconds: float
    measured_at: str

    def __post_init__(self) -> None:
        if not _MODEL_ID_RE.match(self.model_id):
            raise ProofRejected(f"calibration.model_id invalide : {self.model_id!r}")
        if self.peak_vram_gb <= 0:
            raise ProofRejected(
                "calibration.peak_vram_gb doit être > 0 — un pic nul signifie que rien "
                "n'a été chargé, donc que rien n'a été calibré"
            )
        if self.load_seconds <= 0:
            raise ProofRejected(
                "calibration.load_seconds doit être > 0 — un chargement instantané n'a "
                "pas eu lieu"
            )
        if not self.params_fingerprint.startswith("sha256:"):
            raise ProofRejected(
                f"calibration.params_fingerprint doit valoir « sha256:<hex> », reçu "
                f"{self.params_fingerprint!r} — employez params_fingerprint()"
            )
        _parse_timestamp(self.measured_at, "calibration.measured_at")

    @classmethod
    def from_mapping(cls, node: Any) -> CalibrationProof:
        """Construit depuis un JSON/YAML relu, en refusant tout ce qui dévie."""
        if not isinstance(node, Mapping):
            raise ProofRejected(
                f"calibration doit être un objet, reçu {type(node).__name__}"
            )
        _reject_unknown(node, _CALIBRATION_KEYS, "calibration")
        return cls(
            model_id=_require_str(node, "model_id", "calibration"),
            runtime_version=_require_str(node, "runtime_version", "calibration"),
            hardware_fingerprint=_require_str(node, "hardware_fingerprint", "calibration"),
            params_fingerprint=_require_str(node, "params_fingerprint", "calibration"),
            peak_vram_gb=_require_float(node, "peak_vram_gb", "calibration", minimum=0.0),
            peak_ram_gb=_require_float(node, "peak_ram_gb", "calibration", minimum=0.0),
            load_seconds=_require_float(node, "load_seconds", "calibration", minimum=0.0),
            measured_at=_require_str(node, "measured_at", "calibration"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "runtime_version": self.runtime_version,
            "hardware_fingerprint": self.hardware_fingerprint,
            "params_fingerprint": self.params_fingerprint,
            "peak_vram_gb": self.peak_vram_gb,
            "peak_ram_gb": self.peak_ram_gb,
            "load_seconds": self.load_seconds,
            "measured_at": self.measured_at,
        }


_SMOKE_TEST_KEYS = frozenset({
    "model_id", "endpoint", "http_status", "ttft_ms", "completion_tokens", "measured_at",
})

SMOKE_TEST_PROOF_KEYS: frozenset[str] = _SMOKE_TEST_KEYS


@dataclass(frozen=True)
class SmokeTestProof:
    """
    Recette du premier token, telle que §10 l'exige : par le chemin public réel.

    `endpoint` n'est pas décoratif. Une recette qui aurait interrogé
    `llama-server` en direct prouverait que le modèle charge, pas que la chaîne
    nginx → gateway → llama-server sert. C'est la seconde qui conditionne
    l'activation.
    """
    model_id: str
    endpoint: str
    http_status: int
    ttft_ms: int
    completion_tokens: int
    measured_at: str

    def __post_init__(self) -> None:
        if not _MODEL_ID_RE.match(self.model_id):
            raise ProofRejected(f"smoke_test.model_id invalide : {self.model_id!r}")
        if not self.endpoint.startswith(PUBLIC_ENDPOINT_PREFIX):
            raise ProofRejected(
                f"smoke_test.endpoint doit être un chemin public commençant par "
                f"« {PUBLIC_ENDPOINT_PREFIX} », reçu {self.endpoint!r} : une recette qui "
                "court-circuite le chemin public ne prouve pas qu'il sert (§10)"
            )
        if self.http_status != 200:
            raise ProofRejected(
                f"smoke_test.http_status doit valoir 200, reçu {self.http_status!r}"
            )
        if self.ttft_ms <= 0:
            raise ProofRejected("smoke_test.ttft_ms doit être > 0 — un TTFT nul n'a pas été mesuré")
        if self.completion_tokens <= 0:
            raise ProofRejected(
                "smoke_test.completion_tokens doit être > 0 — aucun token produit, "
                "donc aucun premier token"
            )
        _parse_timestamp(self.measured_at, "smoke_test.measured_at")

    @classmethod
    def from_mapping(cls, node: Any) -> SmokeTestProof:
        if not isinstance(node, Mapping):
            raise ProofRejected(f"smoke_test doit être un objet, reçu {type(node).__name__}")
        _reject_unknown(node, _SMOKE_TEST_KEYS, "smoke_test")
        return cls(
            model_id=_require_str(node, "model_id", "smoke_test"),
            endpoint=_require_str(node, "endpoint", "smoke_test"),
            http_status=_require_int(node, "http_status", "smoke_test", minimum=0),
            ttft_ms=_require_int(node, "ttft_ms", "smoke_test", minimum=0),
            completion_tokens=_require_int(node, "completion_tokens", "smoke_test", minimum=0),
            measured_at=_require_str(node, "measured_at", "smoke_test"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "endpoint": self.endpoint,
            "http_status": self.http_status,
            "ttft_ms": self.ttft_ms,
            "completion_tokens": self.completion_tokens,
            "measured_at": self.measured_at,
        }


@dataclass(frozen=True)
class ActivationProof:
    """
    Les DEUX volets, indissociables. Seul cet objet confirme l'activation définitive.

    L'unique autre chemin vers `enabled: true` est l'ouverture provisoire de
    DEC-010, sur `CalibrationProof`, qui porte une obligation de rollback jusqu'à
    la recette suivante. Il n'existe ni drapeau, ni option, ni valeur par défaut.
    """
    calibration: CalibrationProof
    smoke_test: SmokeTestProof

    def __post_init__(self) -> None:
        if self.calibration.model_id != self.smoke_test.model_id:
            raise ProofRejected(
                "les deux volets de la preuve ne parlent pas du même modèle : "
                f"calibration={self.calibration.model_id!r}, "
                f"smoke_test={self.smoke_test.model_id!r}"
            )

    @property
    def model_id(self) -> str:
        return self.calibration.model_id

    @classmethod
    def from_mapping(cls, node: Any) -> ActivationProof:
        if not isinstance(node, Mapping):
            raise ProofRejected(f"la preuve doit être un objet, reçu {type(node).__name__}")
        _reject_unknown(node, frozenset({"calibration", "smoke_test"}), "preuve")
        if "calibration" not in node or "smoke_test" not in node:
            raise ProofRejected(
                "la preuve doit porter « calibration » ET « smoke_test » : une "
                "calibration réussie sans premier token ne prouve pas que le modèle sert, "
                "et l'inverse ne prouve pas qu'il tient en mémoire"
            )
        return cls(
            calibration=CalibrationProof.from_mapping(node["calibration"]),
            smoke_test=SmokeTestProof.from_mapping(node["smoke_test"]),
        )

    def to_dict(self) -> dict[str, Any]:
        """
        Sérialisation complète de la preuve — pour un rapport d'installation, pas
        pour `StepResult.evidence`.

        La chausse-trappe qui motivait cette note a été fermée : `schema` n'ancre
        plus son motif de secret sur « token » en sous-chaîne, et
        `completion_tokens` est publiable. `digest()` reste néanmoins ce que les
        preuves d'étape publient — non par contournement, mais parce qu'un
        `StepResult.evidence` doit porter ce qu'un lecteur RECOUPE, pas la preuve
        entière recopiée une seconde fois dans le journal.
        """
        return {
            "calibration": self.calibration.to_dict(),
            "smoke_test": self.smoke_test.to_dict(),
        }

    def digest(self) -> dict[str, Any]:
        """
        Résumé de la preuve destiné à `StepResult.evidence` : recoupable et publiable.

        Ce qu'un lecteur de rapport doit pouvoir vérifier sans le rapport de
        calibration sous les yeux : sur quoi la mesure a été faite, quand, et ce
        que la chaîne publique a effectivement rendu.
        """
        return {
            "calibration_measured_at": self.calibration.measured_at,
            "calibration_runtime_version": self.calibration.runtime_version,
            "calibration_hardware_fingerprint": self.calibration.hardware_fingerprint,
            "calibration_params_fingerprint": self.calibration.params_fingerprint,
            "calibration_peak_vram_gb": self.calibration.peak_vram_gb,
            "calibration_peak_ram_gb": self.calibration.peak_ram_gb,
            "smoke_measured_at": self.smoke_test.measured_at,
            "smoke_endpoint": self.smoke_test.endpoint,
            "smoke_http_status": self.smoke_test.http_status,
            "smoke_ttft_ms": self.smoke_test.ttft_ms,
            "smoke_produced_output": self.smoke_test.completion_tokens > 0,
        }


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WriterConfig:
    """
    Tout ce que les exécuteurs ont besoin de savoir. Immuable, sans secret.

    `now` et `scratch_dir` sont les deux points d'injection : l'horloge, pour que
    la fraîcheur d'une preuve soit testable sans attendre, et le répertoire de
    travail de la simulation, pour qu'un test puisse prouver qu'AUCUN octet ne
    sort de son `tmp_path`. Les deux ont un défaut réel en production.
    """
    registry_path: Path
    models_dir: Path
    allowed_model_dirs: tuple[Path, ...]
    runtime_version: str
    hardware_fingerprint: str
    vram_budget_gb: float
    catalog_entries: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    activation_proofs: Mapping[str, ActivationProof] = field(default_factory=dict)
    now: Callable[[], datetime] = _utc_now
    scratch_dir: Path | None = None
    backup_retention: int = DEFAULT_BACKUP_RETENTION
    proof_max_age_seconds: int = DEFAULT_PROOF_MAX_AGE_SECONDS
    vram_safety_factor: float = DEFAULT_VRAM_SAFETY_FACTOR

    def __post_init__(self) -> None:
        if not self.allowed_model_dirs:
            raise RegistryWriterError(
                "allowed_model_dirs est vide : aucun chemin de modèle n'est donc "
                "autorisé. Une liste vide ne signifie pas « pas de contrainte » — "
                "déclarez explicitement les répertoires de modèles (ALLOWED_MODEL_DIRS)."
            )
        if self.backup_retention < 1:
            raise RegistryWriterError(
                f"backup_retention doit être >= 1, reçu {self.backup_retention} : "
                "aucune écriture de registre ne se fait sans sauvegarde conservée."
            )
        if self.vram_budget_gb <= 0:
            raise RegistryWriterError(
                f"vram_budget_gb doit être > 0, reçu {self.vram_budget_gb!r} — sans budget "
                "net connu, aucune activation ne peut être jugée sûre."
            )
        if self.vram_safety_factor < 1.0:
            raise RegistryWriterError(
                f"vram_safety_factor doit être >= 1.0, reçu {self.vram_safety_factor!r} : "
                "une marge inférieure à 1 réduirait la capacité sous le pic mesuré."
            )
        if self.proof_max_age_seconds < 1:
            raise RegistryWriterError(
                f"proof_max_age_seconds doit être >= 1, reçu {self.proof_max_age_seconds}"
            )
        # `models_dir` doit lui-même être dans l'allowlist, sinon toute entrée
        # générée pointerait hors des répertoires autorisés.
        execution.ensure_within_allowed_roots(self.models_dir, self.allowed_model_dirs)


# ── Résultat d'une opération de registre ──────────────────────────────────────

@dataclass(frozen=True)
class RegistryChange:
    """Ce qu'une opération a fait (ou ferait), au vocabulaire d'`execution`."""
    status: str
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    findings: tuple[schema.Finding, ...] = ()
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.status == execution.STEP_FAILED


def _failure(summary: str, code: str, message: str, evidence: dict | None = None) -> RegistryChange:
    """Échec consigné : un code stable, une phrase française, et de quoi agir."""
    return RegistryChange(
        status=execution.STEP_FAILED,
        summary=summary,
        evidence=evidence or {},
        findings=(schema.Finding(code=code, level="fail", message=message),),
        error=summary,
    )


# ── Génération d'une entrée de registre ───────────────────────────────────────

def _round_up_gb(value: float, step: float = VRAM_ROUNDING_GB) -> float:
    """Arrondi au pas SUPÉRIEUR. Jamais vers le bas : §9 impose le conservatisme."""
    # `round(..., 9)` absorbe l'imprécision binaire (5.5/0.1 == 54.99999…) sans
    # transformer l'arrondi supérieur en arrondi au plus proche.
    return round(math.ceil(round(value / step, 9)) * step, 4)


def _catalog_str(node: Mapping[str, Any], key: str, path: str) -> str:
    value = node.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryWriterError(f"{path}.{key} doit être une chaîne non vide, reçu {value!r}")
    return value.strip()


def _catalog_map(node: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    value = node.get(key)
    if not isinstance(value, Mapping):
        raise RegistryWriterError(
            f"{path}.{key} doit être un objet, reçu {type(value).__name__}"
        )
    return value


def _weights_and_mmproj(files: Sequence[Any], entry_id: str) -> tuple[Mapping, Mapping | None]:
    """
    Isole le fichier à mettre dans `path`, et le projecteur multimodal éventuel.

    Un ensemble splitté n'a qu'un seul point d'entrée pour `llama-server` : le
    shard `-00001-of-…`. Choisir un autre shard produirait une entrée qui charge
    un fragment.
    """
    weights = [f for f in files if isinstance(f, Mapping) and f.get("role") == "weights"]
    shards = [f for f in files if isinstance(f, Mapping) and f.get("role") == "weights_shard"]
    mmprojs = [f for f in files if isinstance(f, Mapping) and f.get("role") == "mmproj"]

    if weights and shards:
        raise RegistryWriterError(
            f"« {entry_id} » déclare à la fois des poids monolithiques et des shards"
        )
    if weights:
        if len(weights) != 1:
            raise RegistryWriterError(
                f"« {entry_id} » déclare {len(weights)} fichiers de rôle « weights », "
                "un seul peut servir de `path`"
            )
        principal = weights[0]
    elif shards:
        premiers = [f for f in shards if _SHARD_FIRST_RE.search(str(f.get("name", "")))]
        if len(premiers) != 1:
            raise RegistryWriterError(
                f"« {entry_id} » : impossible d'identifier le premier shard "
                f"(« …-00001-of-NNNNN.gguf ») parmi {[f.get('name') for f in shards]}"
            )
        principal = premiers[0]
    else:
        raise RegistryWriterError(
            f"« {entry_id} » ne déclare aucun fichier de poids — un mmproj seul ne charge rien"
        )

    if len(mmprojs) > 1:
        raise RegistryWriterError(f"« {entry_id} » déclare {len(mmprojs)} projecteurs multimodaux")
    return principal, (mmprojs[0] if mmprojs else None)


def _assert_catalog_entry_usable(node: Mapping[str, Any], entry_id: str) -> None:
    """
    Recoupe l'épinglage et le statut gated **depuis les faits**, pas depuis les
    champs dérivés du catalogue.

    `plannable` et `verification` sont calculés par `bootstrap.catalog`. Les lire
    sans les recalculer reviendrait à faire confiance à un champ dérivé pour
    décider d'écrire de la configuration de production — la faute exacte que la
    vague 5 a mis trois passes à fermer.
    """
    source = _catalog_map(node, "source", entry_id)
    licence = _catalog_map(node, "license", entry_id)

    revision = source.get("revision")
    if not isinstance(revision, str) or not revision.strip():
        raise RegistryWriterError(
            f"« {entry_id} » n'a pas de révision épinglée : rien de reproductible ne peut "
            "être écrit au registre. Relevez la vraie valeur avant d'installer."
        )

    files = source.get("files")
    if not isinstance(files, list) or not files:
        raise RegistryWriterError(f"« {entry_id} » ne déclare aucun fichier")
    non_epingles = [
        str(f.get("name")) for f in files
        if not isinstance(f, Mapping) or not isinstance(f.get("sha256"), str)
    ]
    if non_epingles:
        raise RegistryWriterError(
            f"« {entry_id} » : SHA-256 manquant pour {non_epingles}. Une entrée non "
            "épinglée n'entre pas au registre — la vérification d'intégrité deviendrait "
            "du théâtre."
        )

    if licence.get("gated") is not False:
        raise RegistryWriterError(
            f"« {entry_id} » : dépôt gated (gated={licence.get('gated')!r}). Le bootstrap "
            "n'écrit pas d'entrée pour un modèle dont l'accès exige une acceptation "
            "nominative."
        )


def build_registry_entry(
    catalog_entry: Mapping[str, Any],
    *,
    models_dir: Path,
    allowed_model_dirs: Sequence[Path],
) -> dict[str, Any]:
    """
    Dérive une entrée de `models.yaml` depuis une entrée de catalogue SÉRIALISÉE.

    L'argument attendu est exactement ce que rend `catalog.CatalogEntry.to_dict()`.
    Prendre le dictionnaire plutôt que l'objet est ce qui permet de ne pas
    importer `bootstrap.catalog` : les exécuteurs de la vague 6 restent
    indépendants les uns des autres, comme les producteurs de la vague 5.

    **Il n'existe aucun paramètre `enabled`.** L'entrée sort toujours désactivée.
    L'activation est `enable_model_entry()`, et elle exige une preuve.
    """
    if not isinstance(catalog_entry, Mapping):
        raise RegistryWriterError(
            f"l'entrée de catalogue doit être un objet, reçu {type(catalog_entry).__name__}"
        )

    entry_id = _catalog_str(catalog_entry, "id", "catalogue")
    if not _MODEL_ID_RE.match(entry_id):
        raise RegistryWriterError(
            f"« {entry_id} » n'est pas un identifiant acceptable pour le registre "
            "(minuscules, chiffres, « . », « _ », « - », 63 caractères au plus)"
        )

    _assert_catalog_entry_usable(catalog_entry, entry_id)

    source = _catalog_map(catalog_entry, "source", entry_id)
    runtime = _catalog_map(catalog_entry, "runtime", entry_id)
    defaults = _catalog_map(runtime, "defaults", f"{entry_id}.runtime")
    resources = _catalog_map(catalog_entry, "resources", entry_id)

    principal, mmproj = _weights_and_mmproj(list(source["files"]), entry_id)

    path = _resolve_model_file(models_dir, str(principal.get("name", "")), allowed_model_dirs)

    capabilities = runtime.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        raise RegistryWriterError(f"« {entry_id} » : runtime.capabilities doit être une liste non vide")
    if any(not isinstance(c, str) or not c for c in capabilities):
        raise RegistryWriterError(f"« {entry_id} » : runtime.capabilities contient une valeur non textuelle")

    if (runtime.get("requires_mmproj") is True or "vision" in capabilities) and mmproj is None:
        raise RegistryWriterError(
            f"« {entry_id} » exige un projecteur multimodal mais n'en déclare aucun : "
            "l'entrée servirait des HTTP 500 sur toute requête avec image"
        )

    initial_vram = resources.get("initial_vram_gb")
    if isinstance(initial_vram, bool) or not isinstance(initial_vram, (int, float)):
        raise RegistryWriterError(
            f"« {entry_id} » : resources.initial_vram_gb doit être un nombre, "
            f"reçu {initial_vram!r}"
        )
    vram_gb = _round_up_gb(float(initial_vram))
    if vram_gb <= 0:
        raise RegistryWriterError(
            f"« {entry_id} » : estimation VRAM nulle ou négative ({initial_vram!r}). Le "
            "registre refuserait l'entrée, et une valeur inventée serait pire."
        )

    # Les paramètres sont écrits EFFECTIFS, pas seulement ceux du catalogue : une
    # entrée qui s'appuie sur les défauts de `LlamaParams` changerait de
    # comportement à la première évolution de ces défauts, sans qu'aucune ligne du
    # registre ne bouge. « Boring et explicite », comme les fichiers de déploiement.
    llama_raw = {
        "ctx_size": defaults.get("ctx_size"),
        "parallel": defaults.get("parallel"),
        "cache_type_k": defaults.get("cache_type_k"),
        "cache_type_v": defaults.get("cache_type_v"),
    }
    llama_raw = {k: v for k, v in llama_raw.items() if v is not None}
    try:
        effective = model_registry.LlamaParams(**llama_raw)
    except (TypeError, ValueError) as exc:
        raise RegistryWriterError(
            f"« {entry_id} » : paramètres de service refusés par le registre — {exc}"
        ) from exc

    llama_params = asdict(effective)
    if not llama_params.get("cpu_moe"):
        # `ModelDefinition.to_dict()` omet `cpu_moe` quand il est faux ; l'entrée
        # générée doit ressembler à ce que le registre lui-même écrirait.
        llama_params.pop("cpu_moe", None)

    revision = str(source["revision"])
    entry: dict[str, Any] = {
        "id": entry_id,
        "path": str(path),
        "description": (
            f"{_catalog_str(catalog_entry, 'display_name', entry_id)} — issu du catalogue "
            f"EVARuntime ({_catalog_str(source, 'repo_id', entry_id)}@{revision[:12]}), "
            "entrée générée par le bootstrap (AUT-007)"
        ),
        "vram_gb": vram_gb,
        "enabled": False,  # AUT-007 — jamais autre chose, jamais paramétrable.
        "capabilities": list(capabilities),
        "llama_params": llama_params,
    }

    if mmproj is not None:
        entry["mmproj_path"] = str(
            _resolve_model_file(models_dir, str(mmproj.get("name", "")), allowed_model_dirs)
        )
        mmproj_sha = mmproj.get("sha256")
        if not isinstance(mmproj_sha, str) or not _SHA256_RE.fullmatch(mmproj_sha):
            raise RegistryWriterError(
                f"« {entry_id} » : SHA-256 invalide pour le projecteur multimodal"
            )
        entry["mmproj_sha256"] = mmproj_sha.lower()

    sha = principal.get("sha256")
    if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
        raise RegistryWriterError(
            f"« {entry_id} » : SHA-256 invalide pour le fichier de poids principal"
        )
    if sha:
        # Épingle l'artefact servi : `ModelDefinition.verify_integrity()` s'en sert
        # comme garde-fou supply-chain.
        entry["sha256"] = sha.lower()

    return entry


def _resolve_model_file(
    models_dir: Path, name: str, allowed_model_dirs: Sequence[Path]
) -> Path:
    """
    Compose le chemin d'un fichier de modèle et prouve qu'il reste dans l'allowlist.

    Le nom vient du catalogue, donc d'un fichier de configuration : il est traité
    comme une entrée non fiable. Un `../` y suffirait à sortir de `models_dir`,
    et l'allowlist `ALLOWED_MODEL_DIRS` ne doit jamais être affaiblie.
    """
    if not name or not name.endswith(".gguf"):
        raise RegistryWriterError(
            f"nom de fichier de modèle inexploitable : {name!r} (un « .gguf » est attendu)"
        )
    if "/" in name or "\\" in name or name in (".", ".."):
        raise RegistryWriterError(
            f"nom de fichier de modèle refusé : {name!r} — un nom simple est attendu, "
            "pas un chemin"
        )
    candidat = Path(models_dir) / name
    return execution.ensure_within_allowed_roots(candidat, tuple(allowed_model_dirs))


# ── Lecture et rendu du document ──────────────────────────────────────────────

_NEW_FILE_HEADER = (
    "# Registre des modèles — fichier créé par le bootstrap EVARuntime (AUT-007).\n"
    "#\n"
    "# Toute entrée écrite ici l'est avec `enabled: false`. L'activation est une\n"
    "# action distincte, subordonnée à une calibration réussie ET à une recette du\n"
    "# premier token réussie. Les commentaires ajoutés à la main sont préservés :\n"
    "# le bootstrap ajoute en fin de document et ne réécrit jamais l'existant.\n"
    "\n"
    "models:\n"
)


@dataclass(frozen=True)
class _Document:
    """Le registre tel qu'il est sur le disque : texte brut ET structure."""
    text: str
    data: dict[str, Any]
    exists: bool

    @property
    def models(self) -> list[dict[str, Any]]:
        entries = self.data.get("models")
        return list(entries) if isinstance(entries, list) else []

    def index_of(self, model_id: str) -> int | None:
        trouves = [
            i for i, e in enumerate(self.models)
            if isinstance(e, Mapping) and e.get("id") == model_id
        ]
        if len(trouves) > 1:
            raise RegistryWriterError(
                f"« {model_id} » figure {len(trouves)} fois dans le registre : le fichier "
                "est déjà incohérent, le bootstrap n'y touchera pas"
            )
        return trouves[0] if trouves else None


def _read_document(path: Path) -> _Document:
    """Lit le registre. Un fichier absent est un cas normal, pas une erreur."""
    if not path.exists():
        return _Document(text="", data={"models": []}, exists=False)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryWriterError(f"lecture impossible de {path} : {exc}") from exc

    try:
        data = yaml.safe_load(text)  # safe_load — jamais yaml.load()
    except yaml.YAMLError as exc:
        raise RegistryWriterError(
            f"{path} n'est pas un YAML lisible ({exc}). Le bootstrap ne réécrit pas un "
            "fichier qu'il ne comprend pas : réparez-le ou déplacez-le."
        ) from exc

    if data is None:
        return _Document(text=text, data={"models": []}, exists=True)
    if not isinstance(data, dict) or "models" not in data:
        raise RegistryWriterError(
            f"{path} : la clé « models » est absente ou la racine n'est pas un objet. "
            "Ce n'est pas un registre de modèles."
        )
    if data.get("models") is None:
        data = dict(data)
        data["models"] = []
    if not isinstance(data["models"], list):
        raise RegistryWriterError(
            f"{path} : « models » doit être une liste, reçu {type(data['models']).__name__}"
        )
    return _Document(text=text, data=data, exists=True)


def _list_indent(text: str) -> int:
    """Indentation employée par la liste `models` du fichier existant."""
    for line in text.splitlines():
        match = re.match(r"^(\s*)-\s", line)
        if match:
            return len(match.group(1))
    return 2


def _render_entry_block(entry: Mapping[str, Any], indent: int, stamp: str) -> str:
    """Rend une entrée en YAML, indentée pour s'insérer sous `models:`."""
    dumped = yaml.safe_dump(
        [dict(entry)],
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=100,
    )
    prefix = " " * indent
    lignes = [
        f"{prefix}# Entrée générée par le bootstrap EVARuntime le {stamp} (AUT-007).",
        f"{prefix}# Désactivée par construction : à activer par `enable_model`, sur preuve",
        f"{prefix}# de calibration et de recette du premier token.",
    ]
    lignes += [prefix + ligne if ligne else "" for ligne in dumped.rstrip("\n").split("\n")]
    return "\n".join(lignes) + "\n"


def _append_entry_text(document: _Document, entry: Mapping[str, Any], stamp: str) -> str:
    """
    Ajoute l'entrée en fin de document, sans toucher un octet de l'existant.

    Aucun `yaml.dump` global : il détruirait les commentaires d'exploitation du
    fichier, qui sont de la documentation opérationnelle (les 55 premières lignes
    de `models.yaml` livré en sont). Le texte candidat est ensuite reparsé et
    comparé au document attendu — si l'ajout naïf n'a pas produit exactement ce
    qui était voulu, on refuse au lieu de se rabattre sur une réécriture.
    """
    if not document.exists or not document.text.strip():
        base = _NEW_FILE_HEADER
        indent = 2
    else:
        base = document.text if document.text.endswith("\n") else document.text + "\n"
        indent = _list_indent(document.text)
    return base + _render_entry_block(entry, indent, stamp)


def _entry_block_bounds(lines: Sequence[str], model_id: str) -> tuple[int, int, int]:
    """
    Bornes textuelles de l'entrée `model_id` : (début, fin exclue, indentation des champs).

    COR-028 — **il n'y a qu'un localisateur dans le dépôt**, et il vit dans
    `model_registry`. Cette fonction n'en est plus qu'un adaptateur d'exception.

    L'implémentation d'origine ancrait sur une ligne `- id: <model_id>`, donc sur
    les seules entrées dont `id` est la PREMIÈRE clé. C'est vrai du `models.yaml`
    livré et de ce que ce module écrit, mais pas d'un fichier passé par
    `yaml.safe_dump`, qui trie les clés et met `capabilities` en tête — c'est-à-dire
    pas du fichier de tout hôte ayant déjà subi une mutation admin. Sur ces
    hôtes-là, `enable_model_entry` et `provisionally_enable_model_entry`, donc
    l'étape `enable_model` de la chaîne de preuve M2, refusaient en fail-closed un
    registre parfaitement valide et arrêtaient l'amorçage.

    COR-020 a livré dans `model_registry._entry_bounds` un localisateur
    indépendant de l'ordre des clés. Entretenir ici un second localisateur sur le
    même fichier aurait rouvert exactement le défaut que COR-020 a fermé : deux
    implémentations de la même politique d'écriture, dont c'est toujours celle
    qu'on a oublié de corriger qui touche le fichier de production.

    Sens de la dépendance, au regard de la règle posée par `bootstrap/__init__.py` :
    un exécuteur peut importer un module de la gateway **quand c'est lui qui fait
    autorité**, et c'est déjà à ce titre que ce module soumet chacun de ses
    candidats à `model_registry.ModelRegistry`. `model_registry` est le lecteur de
    référence de `models.yaml` ; savoir où vit une entrée dans ce texte lui
    revient. La dépendance inverse existe déjà et reste tardive
    (`model_registry._text_write_policy()` importe ce module pour la politique
    d'écriture, dont il réemploie `_set_scalar_field` et `_render_scalar`) : le
    couplage est symétrique, assumé, et documenté des deux côtés. Un troisième
    module de contrat n'aurait rien ajouté qu'un niveau d'indirection.

    Seule l'exception est traduite : les appelants de ce module attendent un
    `RegistryWriterError`, et un refus doit rester un refus d'exécuteur. Le
    fail-closed est **inchangé** — zéro ou plusieurs entrées identifiées restent
    un refus, avec sa phrase.
    """
    try:
        return model_registry._entry_bounds(list(lines), model_id)
    except model_registry.RegistryWriteRefused as exc:
        raise RegistryWriterError(str(exc)) from exc


def _set_scalar_field(
    lines: list[str], debut: int, fin: int, champ_indent: int, champ: str, rendu: str
) -> list[str]:
    """
    Remplace (ou insère) `champ: <valeur>` dans le bloc, en gardant le commentaire de fin.

    Un commentaire de fin de ligne (« # à affiner via nvidia-smi ») est de
    l'information d'exploitation : le perdre en changeant une valeur serait une
    perte de données silencieuse.
    """
    motif = re.compile(r"^(\s*)" + re.escape(champ) + r":\s*(?P<valeur>[^#]*)(?P<comment>#.*)?$")
    sortie = list(lines)
    for i in range(debut, fin):
        match = motif.match(sortie[i])
        if not match or len(match.group(1)) != champ_indent:
            continue
        comment = match.group("comment")
        suffixe = f"  {comment.strip()}" if comment else ""
        sortie[i] = f"{' ' * champ_indent}{champ}: {rendu}{suffixe}"
        return sortie
    # Champ absent — cas d'une entrée écrite à la main qui s'appuie sur les
    # défauts du registre. On l'insère juste après la ligne `- id:`.
    sortie.insert(debut + 1, f"{' ' * champ_indent}{champ}: {rendu}")
    return sortie


def _render_scalar(value: Any) -> str:
    """Rendu YAML d'un scalaire, tel que `yaml.safe_dump` l'écrirait."""
    return yaml.safe_dump(value, default_flow_style=True).strip().rstrip("...").strip()


def _assert_candidate_matches(candidate_text: str, expected: dict[str, Any], contexte: str) -> None:
    """
    Reparse le texte candidat et le compare, structure comprise, au résultat voulu.

    C'est ce contrôle qui rend sûres l'insertion en fin de fichier et la retouche
    de ligne : quelle que soit la mise en page du fichier de l'exploitant, si le
    texte produit ne signifie pas EXACTEMENT ce qui était prévu, on refuse. Aucun
    repli sur une réécriture globale — elle détruirait ses commentaires.
    """
    try:
        relu = yaml.safe_load(candidate_text)
    except yaml.YAMLError as exc:
        raise RegistryWriterError(
            f"{contexte} : le texte produit n'est pas un YAML valide ({exc}). Rien n'a été écrit."
        ) from exc
    if relu != expected:
        raise RegistryWriterError(
            f"{contexte} : le texte produit ne signifie pas ce qui était prévu — la mise en "
            "page du registre empêche une modification sûre. Rien n'a été écrit ; "
            "modifiez l'entrée à la main."
        )


# ── Écriture atomique, sauvegarde, validation ─────────────────────────────────

def _stamp(config: WriterConfig) -> str:
    return config.now().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_path(target: Path, stamp: str) -> Path:
    return target.with_name(f"{target.name}{BACKUP_INFIX}{stamp}{BACKUP_SUFFIX}")


def _backup(target: Path, stamp: str, retention: int) -> tuple[Path, tuple[str, ...]]:
    """
    Copie horodatée du registre AVANT toute écriture, puis purge bornée.

    `O_EXCL` : on n'écrase jamais une sauvegarde existante — même à la seconde
    près, on prend un suffixe libre plutôt que de recouvrir une copie. Les
    permissions du fichier d'origine sont reprises : une sauvegarde ne doit jamais
    être plus largement lisible que l'original.

    La purge est la différence assumée avec les sauvegardes de migration, que
    OPS-002 constate non purgées : seuls les fichiers de CE motif exact, dans CE
    répertoire, au-delà des `retention` plus récents, sont supprimés.
    """
    contenu = target.read_bytes()
    mode = os.stat(target).st_mode & 0o777

    chemin = _backup_path(target, stamp)
    for essai in range(1, 100):
        try:
            fd = os.open(chemin, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            chemin = _backup_path(target, f"{stamp}-{essai}")
    else:
        raise RegistryWriterError(
            f"impossible de créer une sauvegarde libre pour {target} — 100 tentatives"
        )
    with os.fdopen(fd, "wb") as handle:
        handle.write(contenu)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(chemin, mode)

    return chemin, _purge_backups(target, retention)


def _purge_backups(target: Path, retention: int) -> tuple[str, ...]:
    """Ne garde que les `retention` sauvegardes les plus récentes. Motif strict."""
    motif = re.compile(
        r"^" + re.escape(target.name + BACKUP_INFIX)
        + r"\d{8}T\d{6}Z(-\d+)?" + re.escape(BACKUP_SUFFIX) + r"$"
    )
    existantes = sorted(
        (p for p in target.parent.iterdir() if p.is_file() and motif.match(p.name)),
        key=lambda p: p.name,
    )
    supprimees: list[str] = []
    for vieille in existantes[:-retention] if retention < len(existantes) else []:
        try:
            vieille.unlink()
            supprimees.append(vieille.name)
        except OSError:
            # Une sauvegarde non supprimable n'est pas un motif d'échec de
            # l'installation : elle occupe de la place, elle ne casse rien.
            continue
    return tuple(supprimees)


def _validate_loadable(path: Path, allowed_model_dirs: Sequence[Path]) -> int:
    """
    Prouve que le fichier candidat est chargeable, avec le validateur RÉEL.

    Pas une reformulation de ses règles : `ModelRegistry` lui-même. C'est la seule
    façon d'éviter la classe de défaut COR-014, où de la configuration livrée était
    conforme à sa documentation et refusée par son chargeur.
    """
    try:
        registre = model_registry.ModelRegistry(
            path, allowed_model_dirs=[str(d) for d in allowed_model_dirs]
        )
    except Exception as exc:
        raise RegistryWriterError(
            f"le registre produit n'est pas chargeable par model_registry : {exc}. "
            "Rien n'a été installé — un models.yaml illisible met la gateway à terre."
        ) from exc
    return len(registre.list_all())


def _atomic_write(
    target: Path, text: str, allowed_model_dirs: Sequence[Path], stamp: str
) -> int:
    """
    Écrit puis publie : temporaire dans le même répertoire → validation → `os.replace`.

    Le temporaire est dans le répertoire cible pour que `os.replace` soit un
    renommage atomique sur le même système de fichiers, comme le fait déjà
    `ModelRegistry._save()` et comme `update.sh` bascule ses artefacts. Le
    répertoire est `fsync`é après le renommage : sans cela, un arrêt brutal peut
    laisser le répertoire pointant sur l'ancien inode.
    """
    tmp = target.with_name(f".{target.name}.bootstrap-{stamp}.tmp")
    essai = 0
    while tmp.exists() and essai < 100:
        essai += 1
        tmp = target.with_name(f".{target.name}.bootstrap-{stamp}-{essai}.tmp")

    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            os.chmod(tmp, os.stat(target).st_mode & 0o777)
        nombre = _validate_loadable(tmp, allowed_model_dirs)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    dir_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return nombre


def _validate_candidate_without_writing(
    config: WriterConfig, text: str, stamp: str
) -> int:
    """
    Valide le candidat en simulation, SANS toucher au registre ni à son répertoire.

    Le validateur réel exige un fichier ; la simulation en fabrique un dans un
    répertoire de travail à part — jamais celui du registre —, puis l'efface. Le
    répertoire est injectable précisément pour qu'un test puisse prouver que rien
    ne sort de son `tmp_path`. Sans ce détour, une simulation promettrait
    « ceci s'appliquerait » sans avoir vérifié que le résultat se charge.
    """
    base = Path(config.scratch_dir) if config.scratch_dir is not None else None
    if base is None:
        import tempfile

        base = Path(tempfile.mkdtemp(prefix="eva-bootstrap-registry-"))
        ephemere = True
    else:
        base.mkdir(parents=True, exist_ok=True)
        ephemere = False

    sonde = base / f"models.candidate-{stamp}.yaml"
    essai = 0
    while sonde.exists() and essai < 100:
        essai += 1
        sonde = base / f"models.candidate-{stamp}-{essai}.yaml"
    try:
        sonde.write_text(text, encoding="utf-8")
        return _validate_loadable(sonde, config.allowed_model_dirs)
    finally:
        sonde.unlink(missing_ok=True)
        if ephemere:
            try:
                base.rmdir()
            except OSError:
                pass


def _diff(avant: str, apres: str, path: Path) -> list[str]:
    """Diff unifié exact, celui que la simulation doit montrer."""
    return list(difflib.unified_diff(
        avant.splitlines(),
        apres.splitlines(),
        fromfile=f"a/{path.name}",
        tofile=f"b/{path.name}",
        lineterm="",
        n=3,
    ))


# ── Action 1 : écrire l'entrée (toujours désactivée) ──────────────────────────

def _compare_entries(present: Mapping[str, Any], genere: Mapping[str, Any]) -> list[str]:
    """
    Champs par lesquels l'entrée présente diverge de celle qui serait générée.

    `enabled` passé à `true` et `vram_gb` **relevé** sont les deux évolutions que
    `enable_model` produit légitimement : les compter comme des divergences
    rendrait une réexécution du plan impossible après activation. Tout le reste
    est du réglage d'exploitant.
    """
    divergents: list[str] = []
    for cle in sorted(set(present) | set(genere)):
        valeur_presente = present.get(cle)
        valeur_generee = genere.get(cle)
        if valeur_presente == valeur_generee:
            continue
        if cle == "enabled" and valeur_presente is True and valeur_generee is False:
            continue
        if cle == "vram_gb":
            try:
                if float(valeur_presente) >= float(valeur_generee):
                    continue
            except (TypeError, ValueError):
                pass
        divergents.append(cle)
    return divergents


def write_model_entry(
    config: WriterConfig, model_id: str, *, mode: execution.ExecutionMode
) -> RegistryChange:
    """
    Écrit l'entrée de `model_id` dans `models.yaml`, **désactivée**.

    Trois issues, et aucune quatrième : l'entrée est absente et elle est ajoutée
    (`done` / `would_apply`) ; elle est présente et conforme (`already_satisfied`,
    rien n'est écrit) ; elle est présente et divergente (`failed`, rien n'est
    écrit et les champs qui divergent sont nommés).
    """
    catalog_entry = config.catalog_entries.get(model_id)
    if catalog_entry is None:
        return _failure(
            f"« {model_id} » n'est pas au catalogue fourni à l'applicateur",
            "registre_modele_hors_catalogue",
            f"L'étape demande d'écrire « {model_id} » au registre, mais aucune entrée de "
            f"catalogue de ce nom n'a été fournie (connues : "
            f"{sorted(config.catalog_entries) or 'aucune'}). Le bootstrap n'invente pas "
            "d'entrée de registre.",
            evidence={"model_id": model_id, "written": False, "enabled": False},
        )

    genere = build_registry_entry(
        catalog_entry,
        models_dir=config.models_dir,
        allowed_model_dirs=config.allowed_model_dirs,
    )
    if genere.get("enabled") is not False:
        # Ceinture et bretelles : si un jour quelqu'un rendait `enabled`
        # paramétrable, l'écriture s'arrêterait ici plutôt qu'en production.
        raise RegistryWriterError(
            "invariant AUT-007 violé : l'entrée générée n'est pas désactivée"
        )

    document = _read_document(config.registry_path)
    index = document.index_of(model_id)

    if index is not None:
        present = document.models[index]
        present_enabled = _enabled_state(present, model_id)
        divergents = _compare_entries(present, genere)
        if not divergents:
            return RegistryChange(
                status=execution.STEP_ALREADY_SATISFIED,
                summary=f"« {model_id} » est déjà au registre, à l'identique",
                evidence={
                    "model_id": model_id,
                    "registry": str(config.registry_path),
                    "written": False,
                    "enabled": present_enabled,
                },
            )
        return _failure(
            f"« {model_id} » existe déjà au registre et diverge sur {divergents}",
            "registre_entree_divergente",
            f"L'entrée « {model_id} » présente dans {config.registry_path} diffère de "
            f"celle que le catalogue produirait sur : {', '.join(divergents)}. Ces valeurs "
            "peuvent avoir été réglées par l'exploitant ; les écraser serait une perte de "
            "données. Rien n'a été écrit — alignez l'entrée à la main, ou retirez-la pour "
            "la faire regénérer.",
            evidence={
                "model_id": model_id,
                "registry": str(config.registry_path),
                "written": False,
                "diverging_fields": divergents,
                "generated": genere,
                "present": dict(present),
            },
        )

    stamp = _stamp(config)
    candidat = _append_entry_text(document, genere, stamp)
    attendu = copy.deepcopy(document.data)
    attendu["models"] = document.models + [genere]
    _assert_candidate_matches(candidat, attendu, f"écriture de « {model_id} »")

    diff = _diff(document.text, candidat, config.registry_path)
    evidence: dict[str, Any] = {
        "model_id": model_id,
        "registry": str(config.registry_path),
        "writer_version": REGISTRY_WRITER_VERSION,
        "enabled": False,
        "vram_gb": genere["vram_gb"],
        "params_fingerprint": params_fingerprint(genere["llama_params"]),
        "diff": diff,
    }

    if mode is execution.ExecutionMode.DRY_RUN:
        evidence["written"] = False
        evidence["models_after"] = _validate_candidate_without_writing(config, candidat, stamp)
        return RegistryChange(
            status=execution.STEP_WOULD_APPLY,
            summary=(
                f"ajouterait « {model_id} » à {config.registry_path.name} avec "
                "`enabled: false`"
            ),
            evidence=evidence,
        )

    sauvegarde: Path | None = None
    purgees: tuple[str, ...] = ()
    if document.exists:
        sauvegarde, purgees = _backup(config.registry_path, stamp, config.backup_retention)
    evidence["backup"] = str(sauvegarde) if sauvegarde else None
    evidence["backups_purged"] = list(purgees)
    evidence["models_after"] = _atomic_write(
        config.registry_path, candidat, config.allowed_model_dirs, stamp
    )
    evidence["written"] = True
    return RegistryChange(
        status=execution.STEP_DONE,
        summary=(
            f"« {model_id} » écrit dans {config.registry_path.name} avec `enabled: false` "
            f"(vram_gb={genere['vram_gb']})"
        ),
        evidence=evidence,
        findings=(schema.Finding(
            code="registre_entree_desactivee",
            level="info",
            message=(
                f"« {model_id} » est enregistré mais NON servable : il reste désactivé "
                "tant que la calibration et la recette du premier token n'ont pas réussi."
            ),
        ),),
    )


# ── Action 2 : activer, sur preuve ────────────────────────────────────────────

def _check_proof(
    config: WriterConfig, model_id: str, proof: ActivationProof, entry: Mapping[str, Any]
) -> None:
    """
    Recoupe la preuve contre l'entrée du registre et contre l'hôte. Fail-closed.

    Chaque contrôle répond à une façon dont une preuve peut être vraie et
    inapplicable : bonne mesure, mauvais modèle ; bon modèle, autres paramètres ;
    bons paramètres, autre machine ; tout bon, mais il y a six mois.
    """
    if proof.model_id != model_id:
        raise ProofRejected(
            f"la preuve concerne « {proof.model_id} », l'étape active « {model_id} »"
        )

    _check_calibration(config, model_id, proof.calibration, entry)

    maintenant = config.now().astimezone(timezone.utc)
    _check_proof_timestamp(
        "recette du premier token",
        proof.smoke_test.measured_at,
        maintenant=maintenant,
        max_age_seconds=config.proof_max_age_seconds,
    )


def _check_calibration(
    config: WriterConfig,
    model_id: str,
    calibration: CalibrationProof,
    entry: Mapping[str, Any],
) -> None:
    """Recoupe le volet mesuré qui autorise l'activation provisoire de DEC-010."""
    if calibration.model_id != model_id:
        raise ProofRejected(
            f"la calibration concerne « {calibration.model_id} », l'étape active "
            f"« {model_id} »"
        )

    attendue = params_fingerprint(entry.get("llama_params"))
    if calibration.params_fingerprint != attendue:
        raise ProofRejected(
            "la calibration n'a pas été faite avec les paramètres que le registre servira : "
            f"empreinte mesurée {calibration.params_fingerprint}, empreinte de "
            f"l'entrée {attendue}. Recalibrez avec les paramètres en vigueur — une mesure "
            "faite à un autre ctx_size ou parallélisme ne dit rien de celui-ci (§9)."
        )
    if calibration.runtime_version != config.runtime_version:
        raise ProofRejected(
            f"la calibration a été faite sur le runtime {calibration.runtime_version!r}, "
            f"l'hôte sert {config.runtime_version!r} : la mesure n'est pas réutilisable (§9)"
        )
    if calibration.hardware_fingerprint != config.hardware_fingerprint:
        raise ProofRejected(
            "la calibration a été faite sur un autre matériel "
            f"({calibration.hardware_fingerprint!r} contre "
            f"{config.hardware_fingerprint!r}) : la mesure n'est pas réutilisable (§9)"
        )

    maintenant = config.now().astimezone(timezone.utc)
    _check_proof_timestamp(
        "calibration",
        calibration.measured_at,
        maintenant=maintenant,
        max_age_seconds=config.proof_max_age_seconds,
    )


def _check_proof_timestamp(
    label: str,
    value: str,
    *,
    maintenant: datetime,
    max_age_seconds: int,
) -> None:
    """Une preuve future ou périmée ne peut autoriser aucune transition d'état."""
    limite = timedelta(seconds=max_age_seconds)
    tolerance = timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS)
    mesure = _parse_timestamp(value, label)
    if mesure > maintenant + tolerance:
        raise ProofRejected(
            f"la {label} est datée du futur ({value}, l'hôte est à "
            f"{maintenant.strftime(PROOF_TIMESTAMP_FORMAT)})"
        )
    if maintenant - mesure > limite:
        raise ProofRejected(
            f"la {label} date du {value} et dépasse la fraîcheur admise "
            f"({max_age_seconds} s). Rejouez-la : l'état de l'hôte a pu changer depuis."
        )


def _target_vram_gb_from_calibration(
    config: WriterConfig,
    entry: Mapping[str, Any],
    calibration: CalibrationProof,
) -> float:
    """
    Capacité à inscrire : le maximum entre l'estimation écrite et le pic mesuré + marge.

    La calibration ne peut que RELEVER. Un pic bas peut venir d'un contexte
    réduit ou d'un prompt trop court ; abaisser sur cette base, c'est
    l'optimisme d'estimation que §0.9 a déjà payé une fois.
    """
    mesure = _round_up_gb(calibration.peak_vram_gb * config.vram_safety_factor)
    try:
        courante = float(entry.get("vram_gb", 0.0))
    except (TypeError, ValueError):
        courante = 0.0
    return max(mesure, _round_up_gb(courante))


def _target_vram_gb(
    config: WriterConfig, entry: Mapping[str, Any], proof: ActivationProof
) -> float:
    """Compatibilité interne de l'activation définitive avec la preuve complète."""
    return _target_vram_gb_from_calibration(config, entry, proof.calibration)


def _registry_sha256(document: _Document) -> str:
    """Empreinte exacte des octets relus, utilisée par la synchronisation live."""
    return hashlib.sha256(document.text.encode("utf-8")).hexdigest()


def provisionally_enable_model_entry(
    config: WriterConfig,
    model_id: str,
    calibration: CalibrationProof | None,
    *,
    mode: execution.ExecutionMode,
) -> RegistryChange:
    """
    Autorise l'activation temporaire *en mémoire* pour la recette (DEC-010).

    Cette fonction valide la transition et calcule la capacité, mais laisse
    volontairement le fichier à ``enabled: false``. L'applicateur synchronise
    ensuite cet état comme une activation live éphémère auprès de la gateway.
    Ainsi, un crash ou un redémarrage avant le smoke test revient naturellement
    à l'état sûr du disque. Seule `enable_model_entry()` persiste ``true`` après
    la preuve complète.
    """
    if not isinstance(calibration, CalibrationProof):
        return _failure(
            f"calibration absente ou invalide pour « {model_id} »",
            "activation_provisoire_sans_calibration",
            f"« {model_id} » reste désactivé : une activation provisoire exige une "
            "CalibrationProof réelle et recoupable.",
            evidence={"model_id": model_id, "written": False, "enabled": False},
        )

    document = _read_document(config.registry_path)
    index = document.index_of(model_id)
    if index is None:
        return _failure(
            f"« {model_id} » est absent du registre",
            "activation_provisoire_entree_absente",
            f"« {model_id} » n'est pas dans {config.registry_path} : aucune activation "
            "provisoire n'est possible.",
            evidence={"model_id": model_id, "written": False, "enabled": False},
        )
    entry = dict(document.models[index])
    enabled_state = entry.get("enabled")
    if type(enabled_state) is not bool:
        return _failure(
            f"« {model_id} » porte un état d'activation invalide",
            "activation_provisoire_etat_inattendu",
            f"« {model_id} » porte enabled={enabled_state!r}, qui n'est pas un booléen. "
            "Aucune transition d'état n'est tentée.",
            evidence={"model_id": model_id, "written": False, "enabled": enabled_state},
        )

    try:
        _check_calibration(config, model_id, calibration, entry)
    except ProofRejected as exc:
        return _failure(
            f"calibration refusée pour « {model_id} »",
            "activation_provisoire_calibration_refusee",
            f"« {model_id} » reste désactivé : {exc}",
            evidence={"model_id": model_id, "written": False, "enabled": False},
        )

    cible_vram = _target_vram_gb_from_calibration(config, entry, calibration)
    if cible_vram > config.vram_budget_gb:
        return _failure(
            f"« {model_id} » dépasse le budget VRAM net",
            "activation_provisoire_hors_budget",
            f"« {model_id} » demanderait {cible_vram} Go pour un budget net de "
            f"{config.vram_budget_gb} Go. L'entrée reste désactivée.",
            evidence={
                "model_id": model_id,
                "written": False,
                "enabled": False,
                "vram_gb_required": cible_vram,
                "vram_budget_gb": config.vram_budget_gb,
            },
        )

    if enabled_state is True:
        return RegistryChange(
            status=execution.STEP_ALREADY_SATISFIED,
            summary=(
                f"« {model_id} » était déjà activé ; la recette va renouveler sa preuve"
            ),
            evidence={
                "model_id": model_id,
                "written": False,
                "enabled": True,
                "provisional": False,
                "preexisting_enabled": True,
                "confirmation_after_smoke_required": True,
                "vram_gb": entry.get("vram_gb"),
                "vram_gb_required": cible_vram,
                "calibration_measured_at": calibration.measured_at,
                "registry_sha256": _registry_sha256(document),
            },
            findings=_budget_findings(document, model_id, cible_vram, config),
        )

    # Le diff reste publié pour que la simulation décrive la transition qui
    # deviendra persistante après le smoke. Il n'est jamais écrit ici.
    lignes = document.text.split("\n")
    debut, fin, champ_indent = _entry_block_bounds(lignes, model_id)
    lignes = _set_scalar_field(lignes, debut, fin, champ_indent, "enabled", "true")
    debut, fin, champ_indent = _entry_block_bounds(lignes, model_id)
    lignes = _set_scalar_field(
        lignes, debut, fin, champ_indent, "vram_gb", _render_scalar(cible_vram)
    )
    candidat = "\n".join(lignes)
    attendu = copy.deepcopy(document.data)
    cible = dict(attendu["models"][index])
    cible["enabled"] = True
    cible["vram_gb"] = cible_vram
    attendu["models"][index] = cible
    _assert_candidate_matches(candidat, attendu, f"projection provisoire de « {model_id} »")

    evidence: dict[str, Any] = {
        "model_id": model_id,
        "registry": str(config.registry_path),
        "writer_version": REGISTRY_WRITER_VERSION,
        "enabled": True,
        "provisional": True,
        "live_only": True,
        "disk_enabled": False,
        "rollback_required": mode is execution.ExecutionMode.APPLY,
        "vram_gb": cible_vram,
        "calibration_measured_at": calibration.measured_at,
        "registry_sha256": _registry_sha256(document),
        "diff": _diff(document.text, candidat, config.registry_path),
        "written": False,
    }
    findings = _budget_findings(document, model_id, cible_vram, config)

    if mode is execution.ExecutionMode.DRY_RUN:
        evidence["models_after"] = _validate_candidate_without_writing(
            config, candidat, _stamp(config)
        )
        return RegistryChange(
            status=execution.STEP_WOULD_APPLY,
            summary=f"activerait provisoirement « {model_id} » pour sa recette publique",
            evidence=evidence,
            findings=findings,
        )

    return RegistryChange(
        status=execution.STEP_DONE,
        summary=(
            f"activation live provisoire de « {model_id} » autorisée pour sa recette"
        ),
        evidence=evidence,
        findings=findings + (schema.Finding(
            code="activation_provisoire",
            level="info",
            message=(
                f"« {model_id} » peut être rendu temporairement visible en mémoire afin "
                "de traverser le chemin public ; le disque reste enabled: false. Le smoke "
                "test suivant doit confirmer la preuve complète (DEC-010)."
            ),
        ),),
    )


def rollback_provisional_model_entry(
    config: WriterConfig,
    model_id: str,
    *,
    mode: execution.ExecutionMode,
    reason: str,
) -> RegistryChange:
    """Compensation DEC-010 : remet une activation provisoire à `enabled: false`."""
    document = _read_document(config.registry_path)
    index = document.index_of(model_id)
    if index is None:
        return _failure(
            f"rollback impossible : « {model_id} » est absent du registre",
            "rollback_activation_entree_absente",
            f"La compensation de « {model_id} » n'a rien trouvé dans {config.registry_path}.",
            evidence={"model_id": model_id, "written": False, "rollback": False},
        )
    entry = dict(document.models[index])
    if entry.get("enabled") is False:
        return RegistryChange(
            status=execution.STEP_ALREADY_SATISFIED,
            summary=f"« {model_id} » est déjà désactivé",
            evidence={
                "model_id": model_id,
                "written": False,
                "enabled": False,
                "rollback": True,
                "reason": reason,
                "registry_sha256": _registry_sha256(document),
            },
        )

    attendu = copy.deepcopy(document.data)
    cible = dict(attendu["models"][index])
    cible["enabled"] = False
    attendu["models"][index] = cible
    lignes = document.text.split("\n")
    debut, fin, champ_indent = _entry_block_bounds(lignes, model_id)
    candidat = "\n".join(
        _set_scalar_field(lignes, debut, fin, champ_indent, "enabled", "false")
    )
    _assert_candidate_matches(candidat, attendu, f"rollback de « {model_id} »")
    evidence: dict[str, Any] = {
        "model_id": model_id,
        "registry": str(config.registry_path),
        "enabled": False,
        "rollback": True,
        "reason": reason,
        "registry_sha256": hashlib.sha256(candidat.encode("utf-8")).hexdigest(),
        "diff": _diff(document.text, candidat, config.registry_path),
    }
    if mode is execution.ExecutionMode.DRY_RUN:
        evidence["written"] = False
        evidence["models_after"] = _validate_candidate_without_writing(
            config, candidat, _stamp(config)
        )
        return RegistryChange(
            status=execution.STEP_WOULD_APPLY,
            summary=f"désactiverait « {model_id} » par compensation",
            evidence=evidence,
        )

    stamp = _stamp(config)
    sauvegarde, purgees = _backup(config.registry_path, stamp, config.backup_retention)
    evidence["backup"] = str(sauvegarde)
    evidence["backups_purged"] = list(purgees)
    evidence["models_after"] = _atomic_write(
        config.registry_path, candidat, config.allowed_model_dirs, stamp
    )
    evidence["written"] = True
    return RegistryChange(
        status=execution.STEP_DONE,
        summary=f"« {model_id} » remis à enabled: false ({reason})",
        evidence=evidence,
        findings=(schema.Finding(
            code="activation_provisoire_annulee",
            level="warn",
            message=f"La recette de « {model_id} » n'a pas été confirmée : rollback appliqué.",
        ),),
    )


def enable_model_entry(
    config: WriterConfig,
    model_id: str,
    proof: ActivationProof | None,
    *,
    mode: execution.ExecutionMode,
) -> RegistryChange:
    """
    Confirme une entrée en `enabled: true`, **et seulement sur preuve recoupée**.

    Sans preuve, avec une preuve d'un autre modèle, d'autres paramètres, d'un
    autre matériel, d'un autre runtime, périmée, ou dont la capacité dépasse le
    budget net : refus, et l'entrée reste désactivée.
    """
    if proof is None:
        proof = config.activation_proofs.get(model_id)
    if proof is None:
        return _failure(
            f"aucune preuve d'activation fournie pour « {model_id} »",
            "activation_sans_preuve",
            f"« {model_id} » ne sera pas activé : l'applicateur n'a reçu ni résultat de "
            "calibration ni recette du premier token pour ce modèle. L'entrée reste "
            "désactivée — c'est le critère d'acceptation d'AUT-007, pas un incident.",
            evidence={"model_id": model_id, "written": False, "enabled": False},
        )
    if not isinstance(proof, ActivationProof):
        return _failure(
            f"preuve d'activation de type inattendu pour « {model_id} »",
            "activation_preuve_invalide",
            f"La preuve fournie pour « {model_id} » est un {type(proof).__name__}, pas un "
            "ActivationProof. Un booléen ou un dictionnaire libre ne sont pas des preuves : "
            "employez ActivationProof.from_mapping() pour les faire valider.",
            evidence={"model_id": model_id, "written": False, "enabled": False},
        )

    document = _read_document(config.registry_path)
    index = document.index_of(model_id)
    simule_sur_entree_projetee = False

    if index is None:
        # En simulation, l'étape d'écriture qui précède n'a rien appliqué : le
        # registre ne PEUT pas contenir l'entrée. Refuser ici rendrait toute
        # simulation de bout en bout impossible, et priverait l'opérateur du seul
        # moment où il peut découvrir qu'une preuve est mauvaise sans avoir déjà
        # touché à sa machine. On simule donc contre l'entrée qui SERAIT écrite,
        # et on le dit. En application, l'absence reste un échec.
        catalog_entry = config.catalog_entries.get(model_id)
        if mode is not execution.ExecutionMode.DRY_RUN or catalog_entry is None:
            return _failure(
                f"« {model_id} » est absent du registre",
                "activation_entree_absente",
                f"« {model_id} » n'est pas dans {config.registry_path} : il n'y a rien à "
                "activer. L'écriture de l'entrée (`write_registry`) précède son activation.",
                evidence={"model_id": model_id, "written": False, "enabled": False},
            )
        simule_sur_entree_projetee = True
        entry = build_registry_entry(
            catalog_entry,
            models_dir=config.models_dir,
            allowed_model_dirs=config.allowed_model_dirs,
        )
    else:
        entry = dict(document.models[index])

    try:
        _check_proof(config, model_id, proof, entry)
    except ProofRejected as exc:
        return _failure(
            f"preuve refusée pour « {model_id} »",
            "activation_preuve_refusee",
            f"« {model_id} » reste désactivé : {exc}",
            evidence={"model_id": model_id, "written": False, "enabled": False},
        )

    cible_vram = _target_vram_gb(config, entry, proof)
    if cible_vram > config.vram_budget_gb:
        return _failure(
            f"« {model_id} » dépasse le budget VRAM net",
            "activation_hors_budget",
            f"« {model_id} » demanderait {cible_vram} Go alors que le budget net de l'hôte "
            f"est de {config.vram_budget_gb} Go (pic mesuré "
            f"{proof.calibration.peak_vram_gb} Go × marge {config.vram_safety_factor}). "
            "§9 : un modèle ne doit pas être activé si sa capacité estimée dépasse le "
            "budget net. L'entrée reste désactivée.",
            evidence={
                "model_id": model_id,
                "written": False,
                "enabled": False,
                "vram_gb_required": cible_vram,
                "vram_budget_gb": config.vram_budget_gb,
            },
        )

    deja_actif = _enabled_state(entry, model_id)
    vram_courante = entry.get("vram_gb")
    if deja_actif and isinstance(vram_courante, (int, float)) and float(vram_courante) >= cible_vram:
        return RegistryChange(
            status=execution.STEP_ALREADY_SATISFIED,
            summary=f"« {model_id} » est déjà activé avec une capacité suffisante",
            evidence={
                "model_id": model_id,
                "written": False,
                "enabled": True,
                "vram_gb": float(vram_courante),
            },
        )

    evidence: dict[str, Any] = {
        "model_id": model_id,
        "registry": str(config.registry_path),
        "writer_version": REGISTRY_WRITER_VERSION,
        "enabled": True,
        "vram_gb": cible_vram,
        "vram_budget_gb": config.vram_budget_gb,
        "proof": proof.digest(),
        "registry_sha256": (
            _registry_sha256(document)
            if simule_sur_entree_projetee
            else ""
        ),
        "simulated_on_projected_entry": simule_sur_entree_projetee,
    }
    findings = _budget_findings(document, model_id, cible_vram, config)

    if simule_sur_entree_projetee:
        # Rien à retoucher : l'entrée n'existe pas encore sur le disque. Le diff
        # serait celui de l'étape d'écriture, pas de celle-ci.
        evidence["diff"] = []
        evidence["written"] = False
        return RegistryChange(
            status=execution.STEP_WOULD_APPLY,
            summary=(
                f"activerait « {model_id} » avec vram_gb={cible_vram} — preuve recoupée "
                "contre l'entrée que l'étape d'écriture produirait, celle-ci n'existant "
                "pas encore sur le disque"
            ),
            evidence=evidence,
            findings=findings,
        )

    attendu = copy.deepcopy(document.data)
    cible = dict(attendu["models"][index])
    cible["enabled"] = True
    cible["vram_gb"] = cible_vram
    attendu["models"][index] = cible

    lignes = document.text.split("\n")
    debut, fin, champ_indent = _entry_block_bounds(lignes, model_id)
    lignes = _set_scalar_field(lignes, debut, fin, champ_indent, "enabled", "true")
    debut, fin, champ_indent = _entry_block_bounds(lignes, model_id)
    lignes = _set_scalar_field(
        lignes, debut, fin, champ_indent, "vram_gb", _render_scalar(cible_vram)
    )
    candidat = "\n".join(lignes)
    _assert_candidate_matches(candidat, attendu, f"activation de « {model_id} »")
    evidence["registry_sha256"] = hashlib.sha256(candidat.encode("utf-8")).hexdigest()
    evidence["diff"] = _diff(document.text, candidat, config.registry_path)

    if mode is execution.ExecutionMode.DRY_RUN:
        evidence["written"] = False
        evidence["models_after"] = _validate_candidate_without_writing(
            config, candidat, _stamp(config)
        )
        return RegistryChange(
            status=execution.STEP_WOULD_APPLY,
            summary=f"activerait « {model_id} » avec vram_gb={cible_vram}",
            evidence=evidence,
            findings=findings,
        )

    stamp = _stamp(config)
    sauvegarde, purgees = _backup(config.registry_path, stamp, config.backup_retention)
    evidence["backup"] = str(sauvegarde)
    evidence["backups_purged"] = list(purgees)
    evidence["models_after"] = _atomic_write(
        config.registry_path, candidat, config.allowed_model_dirs, stamp
    )
    evidence["written"] = True
    return RegistryChange(
        status=execution.STEP_DONE,
        summary=(
            f"« {model_id} » activé (vram_gb={cible_vram}, TTFT mesuré "
            f"{proof.smoke_test.ttft_ms} ms)"
        ),
        evidence=evidence,
        findings=findings,
    )


def _budget_findings(
    document: _Document, model_id: str, cible_vram: float, config: WriterConfig
) -> tuple[schema.Finding, ...]:
    """
    Signale une somme d'entrées activées supérieure au budget — sans bloquer.

    Ce n'est pas un invariant de la gateway : la file de capacité évince en LRU,
    donc plusieurs modèles activés peuvent totaliser plus que le budget sans que
    rien ne casse. C'est en revanche une information d'exploitation : aucun de ces
    modèles ne peut être chargé en même temps.
    """
    total = cible_vram
    for entry in document.models:
        if not isinstance(entry, Mapping) or entry.get("id") == model_id:
            continue
        if not _enabled_state(entry):
            continue
        try:
            total += float(entry.get("vram_gb", 0.0))
        except (TypeError, ValueError):
            continue
    if total <= config.vram_budget_gb:
        return ()
    return (schema.Finding(
        code="registre_budget_sursouscrit",
        level="warn",
        message=(
            f"Les modèles activés totalisent {round(total, 2)} Go pour un budget net de "
            f"{config.vram_budget_gb} Go. Chacun tient seul, mais ils ne peuvent pas être "
            "chargés simultanément : la file de capacité en évincera. Attendez-vous à des "
            "chargements à froid."
        ),
    ),)


# ── Exécuteurs et enregistrement ──────────────────────────────────────────────

def model_id_from_target(action: str, target: str) -> str:
    """
    Extrait l'identifiant de modèle de la cible d'une étape de plan.

    Le planificateur écrit « models.yaml → <id> » pour l'écriture et « <id> » pour
    l'activation. Le préfixe est retiré s'il est là ; ce qui reste doit être un
    identifiant acceptable par le registre, sans quoi l'étape est refusée. Aucune
    tolérance au-delà : la cible est ce que l'opérateur a relu.
    """
    brut = (target or "").strip()
    if brut.startswith(_WRITE_TARGET_PREFIX):
        _, _, reste = brut.partition("→")
        brut = reste.strip() if reste.strip() else brut
    if not _MODEL_ID_RE.match(brut):
        raise RegistryWriterError(
            f"cible d'étape inexploitable pour l'action « {action} » : {target!r}. "
            "Attendu « models.yaml → <model_id> » ou « <model_id> »."
        )
    return brut


def _to_step_result(
    step: schema.PlanStep, change: RegistryChange
) -> execution.StepResult:
    return execution.StepResult.for_step(
        step,
        status=change.status,  # type: ignore[arg-type]
        summary=change.summary,
        evidence=change.evidence,
        findings=change.findings,
        error=change.error,
    )


def make_write_registry_executor(config: WriterConfig) -> execution.StepExecutor:
    """Exécuteur de `schema.ACTION_WRITE_REGISTRY`, lié à une configuration."""

    async def executer(
        step: schema.PlanStep, context: execution.ExecutionContext
    ) -> execution.StepResult:
        model_id = model_id_from_target(step.action, step.target)
        # L'écriture, le hachage et le `fsync` sont bloquants : les sortir de la
        # boucle garde l'async de bout en bout, comme l'exige le dépôt.
        change = await asyncio.to_thread(
            write_model_entry, config, model_id, mode=context.mode
        )
        context.journaliser(f"registre [{model_id}] → {change.status}")
        return _to_step_result(step, change)

    return executer


def make_enable_model_executor(config: WriterConfig) -> execution.StepExecutor:
    """Exécuteur de `schema.ACTION_ENABLE_MODEL`. Sans preuve, il refuse."""

    async def executer(
        step: schema.PlanStep, context: execution.ExecutionContext
    ) -> execution.StepResult:
        model_id = model_id_from_target(step.action, step.target)
        change = await asyncio.to_thread(
            enable_model_entry, config, model_id, None, mode=context.mode
        )
        context.journaliser(f"activation [{model_id}] → {change.status}")
        return _to_step_result(step, change)

    return executer


def register_executors(registry: execution.ExecutorRegistry, config: WriterConfig) -> None:
    """
    Branche les deux exécuteurs d'AUT-007 dans un registre d'exécution.

    `ExecutorRegistry.register()` refuse un second enregistrement pour la même
    action : appeler cette fonction deux fois lève, et c'est voulu — deux
    configurations concurrentes pour le même registre rendraient l'applicateur
    imprévisible.
    """
    registry.register(schema.ACTION_WRITE_REGISTRY, make_write_registry_executor(config))
    registry.register(schema.ACTION_ENABLE_MODEL, make_enable_model_executor(config))
