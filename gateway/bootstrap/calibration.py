"""
AUT-008 — calibration RÉELLE de la RAM et de la VRAM d'un modèle (§9, jalon M2).

Ce que ce module est
--------------------
L'exécuteur de `schema.ACTION_CALIBRATE_MODEL`. Il applique littéralement les
neuf étapes de la calibration réelle : relever le repos,
charger à contexte réduit, relever les pics **pendant** la charge, faire un
prompt court, répéter au contexte et au parallélisme cibles, conserver le
maximum observé, ajouter une marge, écrire les mesures dans un rapport séparé,
et **proposer** une valeur `vram_gb` sans l'appliquer.

Il succède à `gguf_meta` dans la hiérarchie de §9 :

    header GGUF + paramètres → estimation conservatrice
                             → chargement réel
                             → mesure des pics          ← ce module
                             → valeur de capacité approuvée

Le devoir symétrique de `gguf_meta`
-----------------------------------
`gguf_meta` prend soin de ne jamais faire passer une estimation pour une
mesure : le mot « estimation » figure dans le nom de ses types, dans son rendu,
et il publie `FACTEURS_IGNORES`. Le devoir de ce module est l'exact miroir : ne
jamais faire passer une **mesure** pour une **garantie**. Un pic relevé une fois,
sur un hôte, à un instant, avec un GPU autrement inoccupé, n'est pas une borne
supérieure du comportement en production. `LIMITES_MESURE` est publié dans le
rapport et dans le rendu humain pour la même raison que `FACTEURS_IGNORES`.

Ce que ce module ne fait pas
----------------------------
- Il **n'écrit jamais `models.yaml`**. L'écriture du registre appartient à un
  autre chantier. La sortie d'ici est une *preuve* (`CalibrationProof`) qu'il
  consommera pour autoriser — ou non — l'activation d'un modèle.
- Il ne lance aucun sous-processus et n'ouvre aucune socket lui-même : tout
  contact avec l'hôte passe par les sondes injectées (`CalibrationProbes`),
  suivant la forme éprouvée par `inventory` (AUT-002).
- Il n'importe que `schema` et `execution`, comme tous les modules du paquet.

Réutilisabilité d'une mesure — une décision, pas un commentaire
---------------------------------------------------------------
§9 conclut : « une mesure n'est réutilisable que si matériel, runtime et
paramètres sont compatibles ». `evaluate_reuse()` en fait une décision testable.
Une preuve prise sur un autre GPU, avec un autre build de `llama-server` ou
d'autres paramètres de service est **refusée**, et le verdict nomme laquelle des
empreintes diverge. C'est ce qui rend l'idempotence honnête : `already_satisfied`
n'est rendu que pour le triplet exact.

Fail-closed
-----------
Une mesure ratée n'est jamais une mesure optimiste. Si les pics n'ont pas pu
être relevés, si le runtime chargé n'est pas celui qu'on croyait, si le prompt
n'a rien produit de mesurable, ou si le modèle n'a pas pu être déchargé, l'étape
**échoue**. Elle ne retombe à aucun moment sur l'estimation statique en la
présentant comme mesurée.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from . import execution as ex
from . import schema

# Version du document de calibration. Indépendante du plan et du journal
# d'exécution : les trois évoluent séparément.
# v2 : l'empreinte couvre désormais TOUS les paramètres réellement servis,
# selon le même document canonique que `registry_writer` / `LlamaParams`.
CALIBRATION_SCHEMA_VERSION = 2

# Discriminant publié en tête de rapport. Le pendant de `"kind": "estimation"`
# de `gguf_meta.FootprintEstimate.to_dict()` — un consommateur doit pouvoir
# distinguer les deux documents sans deviner.
CALIBRATION_KIND = "mesure"

CALIBRATION_TOOL_NAME = "eva-bootstrap-calibrate"

_FINGERPRINT_PREFIX = "sha256:"
_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Marge de sécurité appliquée au pic mesuré pour proposer `vram_gb`. Explicite,
# configurable, et TOUJOURS publiée à côté de la valeur brute : un opérateur qui
# ne peut pas refaire le calcul ne peut pas le contester.
DEFAULT_SAFETY_MARGIN = 0.10

# Échantillonnage des pics. Trois bornes, parce qu'une seule ne suffit pas :
# l'intervalle décide de la finesse, le budget borne le temps réel, le plafond
# borne le nombre de tours si l'horloge injectée n'avance pas.
DEFAULT_SAMPLE_INTERVAL_SECONDS = 0.5
DEFAULT_SAMPLE_BUDGET_SECONDS = 900.0
DEFAULT_MAX_SAMPLES = 4000

# Paramètres de la passe « contexte réduit » (§9, étape 2). Volontairement
# petits : c'est la passe qui doit échouer vite et sans engager beaucoup de VRAM
# si le modèle ne tient pas du tout.
DEFAULT_REDUCED_CTX_SIZE = 512
DEFAULT_REDUCED_PARALLEL = 1

# Prompt court de l'étape 4. Aucune donnée utilisateur, aucun secret.
DEFAULT_PROMPT = "Réponds simplement : bonjour."

PHASE_REDUCED = "contexte_reduit"
PHASE_TARGET = "contexte_cible"
PHASES: tuple[str, ...] = (PHASE_REDUCED, PHASE_TARGET)

REPORT_PREFIX = "calibration"

# Les neuf étapes de §9, dans leur ordre littéral. Publiées dans le rapport et
# rendues en simulation : c'est la réponse à « qu'est-ce qui serait mesuré ? ».
SEQUENCE_CALIBRATION: tuple[str, ...] = (
    "relever RAM/VRAM au repos",
    "charger le modèle avec un contexte réduit",
    "relever les pics RAM/VRAM",
    "effectuer un prompt court",
    "répéter au contexte et parallélisme cibles",
    "conserver le maximum observé",
    "ajouter une marge",
    "écrire les mesures dans un rapport séparé",
    "proposer, sans l'appliquer silencieusement, une nouvelle valeur vram_gb",
)

# Ce qu'une mesure NE garantit PAS. Le pendant exact de
# `gguf_meta.FACTEURS_IGNORES`, publié aux mêmes endroits et pour la même
# raison : une mesure dont on cache les limites finit par être lue comme une
# garantie de non-dépassement, ce qu'elle n'est pas.
LIMITES_MESURE: tuple[str, ...] = (
    "le pic est un maximum ÉCHANTILLONNÉ : une pointe plus brève que l'intervalle "
    "d'échantillonnage a pu être manquée",
    "mesure prise sur cet hôte, à cet instant, GPU supposé autrement inoccupé",
    "un second modèle résident sur le même GPU déplace le pic",
    "la fragmentation du driver évolue avec la durée de service",
    "un prompt plus long, ou un cache KV effectivement rempli, déplacent le pic",
    "un autre build de llama.cpp alloue différemment, même à paramètres égaux",
    "la marge ajoutée est un choix d'exploitation, pas une preuve de non-dépassement",
)

_ABSENT = object()

# Les débits portent les noms LITTÉRAUX de §9.
#
# Ils ont d'abord été publiés sous les alias `prompt_tps` / `generation_tps` :
# `schema._SECRET_KEY_RE` traitait alors toute clé contenant « TOKEN » comme
# sensible, si bien que le rapport prescrit par §9 était impubliable — le rendu
# le refusait, y compris à travers le journal d'exécution qui transporte
# l'`evidence` de cette étape. Trois chantiers de la vague 6 ont buté sur ce
# défaut indépendamment ; il est corrigé à sa source, dans `schema`, et les
# alias ont disparu avec lui. `debit_unite` reste, il lève une ambiguïté réelle.
SECTION9_DEBIT_KEYS: tuple[str, ...] = (
    "prompt_tokens_per_second",
    "generation_tokens_per_second",
)

DEBIT_UNITE = "jetons par seconde"


class CalibrationError(ex.ExecutionError):
    """La calibration ne peut pas produire de mesure exploitable. Fail-closed."""


# ── Empreintes ────────────────────────────────────────────────────────────────

def _fingerprint(payload: Any) -> str:
    """Empreinte stable `sha256:<hex>` d'une structure, via son JSON canonique."""
    texte = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return _FINGERPRINT_PREFIX + hashlib.sha256(texte.encode("utf-8")).hexdigest()


def is_fingerprint(value: Any) -> bool:
    """Vrai si la valeur a la forme d'une empreinte de ce module."""
    return isinstance(value, str) and bool(_FINGERPRINT_RE.match(value))


def hardware_fingerprint(gpus: Sequence[Mapping[str, Any]]) -> str:
    """
    Empreinte du matériel pertinent pour une mesure de VRAM.

    Ne retient que ce qui change le résultat : le modèle de GPU, la VRAM
    réellement exposée, la version de pilote et le nombre de cartes. L'UUID est
    délibérément EXCLU — remplacer une L40S par une autre L40S de même pilote ne
    périme pas une mesure, et lier la preuve au numéro de série obligerait à
    tout recalibrer après un simple changement de carte. À l'inverse, la VRAM
    exposée est retenue : §0.9 rappelle que 48 Go nominaux valent ~45 Go réels,
    et c'est exactement le genre d'écart qu'une preuve ne doit pas transporter
    d'une machine à l'autre.
    """
    if not gpus:
        raise CalibrationError(
            "empreinte matérielle impossible : aucun GPU décrit. Une calibration "
            "sans matériel identifié serait une mesure que rien ne rattache à un hôte."
        )
    # Triés : l'ordre d'énumération de `nvidia-smi` ne doit pas changer l'empreinte
    # d'un hôte dont les cartes n'ont pas bougé.
    descripteurs = sorted(
        [
            str(gpu.get("name", "")),
            int(gpu.get("vram_total_mib", 0) or 0),
            str(gpu.get("driver_version", "")),
            str(gpu.get("compute_cap", "")),
        ]
        for gpu in gpus
    )
    return _fingerprint({"gpus": descripteurs, "count": len(descripteurs)})


@dataclass(frozen=True)
class CalibrationParams:
    """
    Paramètres de service complets, au même contrat que `model_registry.LlamaParams`.

    `reduced_ctx_size` / `reduced_parallel` décrivent la passe de mise en jambes
    (§9, étape 2). Ils sont EXCLUS de `fingerprint()` : ce sont un échafaudage de
    diagnostic, pas ce qui sera servi. Deux calibrations qui n'auraient différé
    que par leur passe réduite mesurent le même service, et se périmer l'une
    l'autre n'apporterait rien.
    """
    ctx_size: int
    parallel: int = 1
    cache_type_k: str = "f16"
    cache_type_v: str = "f16"
    n_gpu_layers: int = 999
    flash_attention: bool = False
    batch_size: int = 4096
    ubatch_size: int = 512
    threads: int = 8
    threads_http: int = 4
    cpu_moe: bool = False
    reduced_ctx_size: int = DEFAULT_REDUCED_CTX_SIZE
    reduced_parallel: int = DEFAULT_REDUCED_PARALLEL

    def __post_init__(self) -> None:
        for label, value in (
            ("ctx_size", self.ctx_size),
            ("parallel", self.parallel),
            ("batch_size", self.batch_size),
            ("ubatch_size", self.ubatch_size),
            ("threads", self.threads),
            ("threads_http", self.threads_http),
            ("reduced_ctx_size", self.reduced_ctx_size),
            ("reduced_parallel", self.reduced_parallel),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise CalibrationError(f"{label} doit être un entier >= 1, reçu {value!r}")
        if not isinstance(self.n_gpu_layers, int) or isinstance(self.n_gpu_layers, bool):
            raise CalibrationError(
                f"n_gpu_layers doit être un entier >= 0, reçu {self.n_gpu_layers!r}"
            )
        if self.n_gpu_layers < 0:
            raise CalibrationError(
                f"n_gpu_layers doit être >= 0, reçu {self.n_gpu_layers!r}"
            )
        if self.ubatch_size > self.batch_size:
            raise CalibrationError(
                f"ubatch_size ({self.ubatch_size}) dépasse batch_size "
                f"({self.batch_size})"
            )
        for label, value in (
            ("flash_attention", self.flash_attention),
            ("cpu_moe", self.cpu_moe),
        ):
            if type(value) is not bool:
                raise CalibrationError(f"{label} doit être un booléen, reçu {value!r}")
        if self.reduced_ctx_size > self.ctx_size:
            raise CalibrationError(
                f"reduced_ctx_size ({self.reduced_ctx_size}) dépasse ctx_size "
                f"({self.ctx_size}) : la passe « réduite » doit être plus petite que la "
                "passe cible, sinon elle n'apprend rien et engage plus de VRAM qu'elle ne "
                "devrait"
            )
        if self.reduced_parallel > self.parallel:
            raise CalibrationError(
                f"reduced_parallel ({self.reduced_parallel}) dépasse parallel "
                f"({self.parallel}) : même raison"
            )

    def target(self) -> dict[str, Any]:
        """Paramètres effectifs, nommés exactement comme dans le registre servi."""
        return {
            "n_gpu_layers": self.n_gpu_layers,
            "ctx_size": self.ctx_size,
            "parallel": self.parallel,
            "batch_size": self.batch_size,
            "ubatch_size": self.ubatch_size,
            "cache_type_k": self.cache_type_k,
            "cache_type_v": self.cache_type_v,
            "flash_attn": self.flash_attention,
            "threads": self.threads,
            "threads_http": self.threads_http,
            "cpu_moe": self.cpu_moe,
        }

    def fingerprint(self) -> str:
        return _fingerprint(self.target())

    def for_phase(self, phase: str) -> tuple[int, int]:
        """`(ctx_size, parallel)` de la passe demandée. Refuse une phase inconnue."""
        if phase == PHASE_REDUCED:
            return self.reduced_ctx_size, self.reduced_parallel
        if phase == PHASE_TARGET:
            return self.ctx_size, self.parallel
        raise CalibrationError(f"phase de calibration inconnue : {phase!r}")


@dataclass(frozen=True)
class CalibrationIdentity:
    """
    Ce à quoi une mesure est rattachée. Quatre champs, tous obligatoires.

    §9 en nomme trois (matériel, runtime, paramètres) ; `model_id` s'y ajoute
    parce qu'une mesure d'un autre modèle n'est pas « incompatible », elle parle
    d'autre chose. Les quatre sont comparés, et le verdict de réutilisation dit
    lequel diverge.
    """
    model_id: str
    runtime_version: str
    hardware_fingerprint: str
    params_fingerprint: str

    def __post_init__(self) -> None:
        for label, value in (
            ("model_id", self.model_id),
            ("runtime_version", self.runtime_version),
        ):
            if not isinstance(value, str) or not value.strip():
                raise CalibrationError(f"{label} doit être une chaîne non vide, reçu {value!r}")
        for label, value in (
            ("hardware_fingerprint", self.hardware_fingerprint),
            ("params_fingerprint", self.params_fingerprint),
        ):
            if not is_fingerprint(value):
                raise CalibrationError(
                    f"{label} doit valoir « sha256:<64 hexadécimaux> », reçu {value!r}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "runtime_version": self.runtime_version,
            "hardware_fingerprint": self.hardware_fingerprint,
            "params_fingerprint": self.params_fingerprint,
        }


# Libellés français des champs d'identité, pour que le message de divergence
# soit lisible sans connaître les noms de champs de §9.
_IDENTITY_LABEL: dict[str, str] = {
    "model_id": "le modèle",
    "runtime_version": "le runtime (build de llama-server)",
    "hardware_fingerprint": "le matériel (GPU, VRAM exposée, pilote)",
    "params_fingerprint": "les paramètres de service (contexte, parallélisme, cache KV)",
}


@dataclass(frozen=True)
class ReuseVerdict:
    """
    Décision de réutilisation d'une mesure existante. Testable, pas déclarative.

    `divergences` porte les NOMS de champs qui diffèrent — c'est ce qu'un test
    et un script recoupent ; `message` porte la phrase française qui nomme
    laquelle des empreintes diverge, c'est ce que l'opérateur lit.
    """
    reusable: bool
    divergences: tuple[str, ...]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reusable": self.reusable,
            "divergences": list(self.divergences),
            "message": self.message,
        }


def evaluate_reuse(proof: CalibrationProof, expected: CalibrationIdentity) -> ReuseVerdict:
    """
    « Une mesure n'est réutilisable que si matériel, runtime et paramètres sont
    compatibles » (§9), implémenté comme une décision.

    Compatible signifie ICI : identique. Aucune tolérance n'est accordée, et
    c'est délibéré — décider qu'un pilote « assez proche » ou un contexte « un
    peu plus petit » restent compatibles demanderait un modèle de compatibilité
    que personne n'a validé sur GPU réel. Une égalité stricte se trompe dans le
    sens sûr : elle recalibre pour rien, elle n'autorise jamais à tort.
    """
    obtenu = proof.identity.to_dict()
    attendu = expected.to_dict()
    divergences = tuple(cle for cle in attendu if obtenu.get(cle) != attendu[cle])
    if not divergences:
        return ReuseVerdict(
            reusable=True,
            divergences=(),
            message=(
                f"mesure réutilisable : {proof.identity.model_id} a été calibré le "
                f"{proof.measured_at} sur le même matériel, le même runtime et les mêmes "
                "paramètres"
            ),
        )
    details = " ; ".join(
        f"{_IDENTITY_LABEL.get(cle, cle)} diffère "
        f"(mesuré {obtenu.get(cle)!r}, attendu {attendu[cle]!r})"
        for cle in divergences
    )
    return ReuseVerdict(
        reusable=False,
        divergences=divergences,
        message=(
            "mesure NON réutilisable — " + details + ". Une mesure ne vaut que pour le "
            "triplet matériel/runtime/paramètres sur lequel elle a été prise."
        ),
    )


# ── Sondes injectables ────────────────────────────────────────────────────────
#
# Même forme que `inventory.NvidiaSmiProbe` : la sonde ne lève pas, elle rend un
# résultat qui DIT s'il est exploitable. La différence avec `inventory` est que
# les sondes sont ici asynchrones — l'échantillonneur tourne en concurrence du
# chargement, et une sonde bloquante figerait la boucle d'événements pendant
# précisément la fenêtre qu'il faut observer.

@dataclass(frozen=True)
class MemoryReading:
    """Un relevé mémoire, exploitable ou non. `detail` explique un échec."""
    ok: bool
    used_bytes: int = 0
    total_bytes: int = 0
    detail: str = ""


@dataclass(frozen=True)
class LoadRequest:
    """Ce qu'on demande au chargeur. Aucun secret ne transite par ici."""
    model_id: str
    phase: str
    ctx_size: int
    parallel: int


@dataclass(frozen=True)
class LoadOutcome:
    """
    Résultat d'un chargement. `runtime_version` est **mesuré**, pas déclaré.

    Il est recoupé contre la version annoncée par les options : un rapport qui
    porterait l'empreinte d'un build alors qu'un autre a servi serait une preuve
    fausse, et c'est exactement la classe d'erreur que M2 doit rendre impossible.
    """
    ok: bool
    runtime_version: str = ""
    detail: str = ""


@dataclass(frozen=True)
class UnloadOutcome:
    """Résultat d'un déchargement. Un échec ici est un échec de calibration."""
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class PromptOutcome:
    """Résultat du prompt court : les débits de §9, tels que mesurés."""
    ok: bool
    ttft_ms: int = 0
    prompt_tokens: int = 0
    prompt_seconds: float = 0.0
    generation_tokens: int = 0
    generation_seconds: float = 0.0
    detail: str = ""


MemoryProbe = Callable[[], Awaitable[MemoryReading]]
LoadProbe = Callable[[LoadRequest], Awaitable[LoadOutcome]]
UnloadProbe = Callable[[str], Awaitable[UnloadOutcome]]
PromptProbe = Callable[[str, str], Awaitable[PromptOutcome]]
SleepProbe = Callable[[float], Awaitable[None]]
EnvironmentValidator = Callable[[CalibrationIdentity], Awaitable[None]]


async def _noop_environment_validator(_identity: CalibrationIdentity) -> None:
    """Compatibilité des doubles : aucune attestation hôte n'est inventée."""
    return None


@dataclass(frozen=True)
class CalibrationProbes:
    """
    La totalité de ce que la calibration touche du monde extérieur.

    Aucune valeur par défaut : le module ne sait pas parler à `llama-server`, et
    prétendre le contraire avec une sonde « par défaut » ferait échouer la
    calibration au premier appel réel plutôt qu'à la construction. Le chantier
    qui possède le client de chargement les fournit ; les tests en fournissent
    des doubles, et ne lancent donc ni processus ni attente réelle.
    """
    read_vram: MemoryProbe
    read_ram: MemoryProbe
    load_model: LoadProbe
    unload_model: UnloadProbe
    run_prompt: PromptProbe
    sleep: SleepProbe
    validate_environment: EnvironmentValidator = _noop_environment_validator


# ── Échantillonnage des pics ──────────────────────────────────────────────────

class PeakSampler:
    """
    Relève RAM et VRAM **pendant** une opération, jamais après.

    C'est le cœur du chantier. Un relevé unique effectué après le retour au
    repos ne mesure rien : le pic d'un chargement `llama.cpp` est transitoire
    (buffers de calcul, graphes CUDA, copie des poids) et il est retombé avant
    que la commande ne rende la main. L'échantillonneur tourne donc dans une
    tâche concurrente de l'opération observée.

    Trois bornes, aucune n'étant redondante :

    - `interval_seconds` décide de la finesse — et donc de ce qui peut être
      manqué, ce que `LIMITES_MESURE` dit à voix haute ;
    - `budget_seconds` borne le temps réel, pour qu'un chargement qui ne rend
      jamais la main ne laisse pas une boucle de sondage tourner indéfiniment ;
    - `max_samples` borne le nombre de tours, parce qu'une horloge injectée
      qui n'avance pas rendrait la borne temporelle inopérante.

    Un tour d'échantillonnage est TOUJOURS effectué avant le premier test
    d'arrêt : une opération qui rend la main immédiatement doit quand même
    produire un relevé, sinon une calibration correcte échouerait par hasard.
    """

    def __init__(
        self,
        probes: CalibrationProbes,
        *,
        monotonic: Callable[[], float],
        interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
        budget_seconds: float = DEFAULT_SAMPLE_BUDGET_SECONDS,
        max_samples: int = DEFAULT_MAX_SAMPLES,
    ) -> None:
        if interval_seconds <= 0:
            raise CalibrationError(
                f"interval_seconds doit être > 0, reçu {interval_seconds!r} — un intervalle "
                "nul ferait tourner le sondage sans laisser progresser le chargement"
            )
        if budget_seconds <= 0:
            raise CalibrationError(f"budget_seconds doit être > 0, reçu {budget_seconds!r}")
        if not isinstance(max_samples, int) or isinstance(max_samples, bool) or max_samples < 1:
            raise CalibrationError(f"max_samples doit être un entier >= 1, reçu {max_samples!r}")

        self._probes = probes
        self._monotonic = monotonic
        self._interval = interval_seconds
        self._budget = budget_seconds
        self._max_samples = max_samples

        self._arret = True
        self._peak_vram: int | None = None
        self._peak_ram: int | None = None
        self._vram_total: int | None = None
        self._vram_samples = 0
        self._ram_samples = 0
        self._rounds = 0
        self._echecs: list[str] = []
        self._raisons_arret: list[str] = []

    # ── Lecture d'état ────────────────────────────────────────────────────────

    @property
    def peak_vram_bytes(self) -> int | None:
        return self._peak_vram

    @property
    def peak_ram_bytes(self) -> int | None:
        return self._peak_ram

    @property
    def vram_total_bytes(self) -> int | None:
        return self._vram_total

    @property
    def vram_samples(self) -> int:
        return self._vram_samples

    @property
    def ram_samples(self) -> int:
        return self._ram_samples

    @property
    def rounds(self) -> int:
        return self._rounds

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(self._echecs)

    def stop_reasons(self) -> tuple[str, ...]:
        return tuple(self._raisons_arret)

    # ── Boucle ────────────────────────────────────────────────────────────────

    async def _un_tour(self) -> None:
        vram = await self._probes.read_vram()
        if vram.ok:
            self._vram_samples += 1
            self._peak_vram = max(self._peak_vram or 0, int(vram.used_bytes))
            if vram.total_bytes:
                self._vram_total = max(self._vram_total or 0, int(vram.total_bytes))
        else:
            self._echecs.append(f"relevé VRAM indisponible : {vram.detail or 'sans détail'}")

        ram = await self._probes.read_ram()
        if ram.ok:
            self._ram_samples += 1
            self._peak_ram = max(self._peak_ram or 0, int(ram.used_bytes))
        else:
            self._echecs.append(f"relevé RAM indisponible : {ram.detail or 'sans détail'}")

        self._rounds += 1

    async def _boucler(self) -> None:
        debut = self._monotonic()
        while True:
            await self._un_tour()
            if self._arret:
                return
            if self._monotonic() - debut >= self._budget:
                self._raisons_arret.append(
                    f"budget d'échantillonnage de {self._budget:g} s épuisé"
                )
                return
            if self._rounds >= self._max_samples:
                self._raisons_arret.append(
                    f"plafond de {self._max_samples} échantillons atteint"
                )
                return
            await self._probes.sleep(self._interval)

    async def during(self, operation: Awaitable[Any]) -> Any:
        """
        Exécute `operation` en échantillonnant en parallèle, et rend son résultat.

        L'arrêt et l'attente de la tâche d'échantillonnage sont dans un `finally` :
        une opération qui lève ne laisse pas une boucle de sondage orpheline
        derrière elle, et les relevés déjà pris restent acquis.
        """
        self._arret = False
        tache = asyncio.create_task(self._boucler())
        try:
            return await operation
        finally:
            self._arret = True
            await tache


# ── Mesures ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PassMeasurement:
    """Ce qu'une passe (réduite, puis cible) a produit. Immuable."""
    phase: str
    ctx_size: int
    parallel: int
    load_seconds: float
    peak_vram_bytes: int
    peak_ram_bytes: int
    vram_total_bytes: int
    ttft_ms: int
    prompt_tokens_per_second: float
    generation_tokens_per_second: float
    sample_rounds: int
    probe_failures: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "ctx_size": self.ctx_size,
            "parallel": self.parallel,
            "load_seconds": round(self.load_seconds, 3),
            "peak_vram_bytes": self.peak_vram_bytes,
            "peak_vram_gb": _gib(self.peak_vram_bytes),
            "peak_ram_bytes": self.peak_ram_bytes,
            "peak_ram_gb": _gib(self.peak_ram_bytes),
            "vram_total_bytes": self.vram_total_bytes,
            "ttft_ms": self.ttft_ms,
            "prompt_tokens_per_second": self.prompt_tokens_per_second,
            "generation_tokens_per_second": self.generation_tokens_per_second,
            "sample_rounds": self.sample_rounds,
            "probe_failures": list(self.probe_failures),
        }


def _gib(octets: int) -> float:
    """Octets → Gio (2^30). Deux décimales, comme `gguf_meta`."""
    return round(octets / (1024 ** 3), 2)


@dataclass(frozen=True)
class CalibrationProof:
    """
    **La preuve consommable par un autre chantier.** Sa forme est le contrat.

    C'est ce qu'un exécuteur de `write_registry` ou d'`enable_model` doit lire
    pour autoriser l'activation d'un modèle. Elle porte l'identité complète de
    la mesure, la valeur BRUTE mesurée, la marge, la valeur PROPOSÉE, et le
    chemin du rapport détaillé.

    `applied` vaut toujours `False` : ce module ne touche pas `models.yaml`. Le
    champ existe pour que le consommateur n'ait pas à le supposer.

    `load_seconds` et `measured_at` sont dans la preuve, et pas seulement à la
    racine du rapport, parce que le consommateur d'AUT-007 les exige DANS le
    bloc `calibration` : une durée de chargement rangée un niveau au-dessus
    obligeait l'applicateur à aller la chercher ailleurs, donc à recomposer une
    preuve à partir de deux endroits — exactement le raccord où quelqu'un finit
    par mettre une valeur par défaut.
    """
    identity: CalibrationIdentity
    idle_vram_bytes: int
    peak_vram_bytes: int
    peak_ram_bytes: int
    load_seconds: float
    safety_margin: float
    measured_at: str
    report_path: str = ""

    @property
    def measured_vram_gb(self) -> float:
        """La valeur brute mesurée, sans marge. Publiée pour être contestable."""
        return _gib(self.peak_vram_bytes)

    @property
    def proposed_vram_gb(self) -> float:
        """
        La valeur PROPOSÉE pour `models.yaml` — proposée, jamais appliquée ici.

        Arrondie vers le HAUT au centième : arrondir à la baisse une capacité
        mémoire est la seule des deux erreurs qui fait échouer un chargement en
        production.

        Le calcul passe par des centièmes entiers plutôt que par un `ceil` sur
        des flottants. `12.0 × 1.1` vaut `13.200000000000001` en binaire, et un
        `ceil` naïf proposerait 13,21 Gio là où 13,20 est le résultat exact —
        une preuve dont l'arithmétique dépend du bruit flottant n'est pas
        recoupable par un tiers, or c'est précisément ce qu'on lui demande.
        """
        centiemes_mesures = round(self.measured_vram_gb * 100)
        brut = centiemes_mesures * (1.0 + self.safety_margin)
        plafond = int(brut)
        if brut - plafond > 1e-9:
            plafond += 1
        return round(plafond / 100, 2)

    def margin_formula(self) -> str:
        """Le calcul, en toutes lettres, pour que l'opérateur puisse le refaire."""
        return (
            f"{self.measured_vram_gb:.2f} Gio mesurés × (1 + {self.safety_margin:g}) "
            f"= {self.proposed_vram_gb:.2f} Gio proposés"
        )

    def to_dict(self) -> dict[str, Any]:
        document = self.identity.to_dict()
        document.update({
            "idle_vram_gb": _gib(self.idle_vram_bytes),
            "peak_vram_gb": self.measured_vram_gb,
            "peak_ram_gb": _gib(self.peak_ram_bytes),
            "load_seconds": round(self.load_seconds, 3),
            "measured_vram_gb": self.measured_vram_gb,
            "safety_margin": self.safety_margin,
            "proposed_vram_gb": self.proposed_vram_gb,
            "margin_formula": self.margin_formula(),
            "applied": False,
            "measured_at": self.measured_at,
            "report_path": self.report_path,
        })
        return document


@dataclass(frozen=True)
class CalibrationReport:
    """
    Le rapport SÉPARÉ de §9 étape 8. Ni le journal d'exécution, ni le registre.

    Tous les champs récapitulatifs sont dérivés (`proposed_vram_gb` se recalcule
    depuis le pic et la marge), et `validate_calibration_document()` refait le
    calcul à la relecture. Un rapport retouché à la main pour abaisser la
    proposition sous ce que la mesure impose est rejeté — c'est la leçon de la
    vague 5, appliquée au seul document qui autorisera l'activation d'un modèle.
    """
    identity: CalibrationIdentity
    idle_vram_bytes: int
    idle_ram_bytes: int
    passes: tuple[PassMeasurement, ...]
    safety_margin: float
    measured_at: str
    sample_interval_seconds: float
    report_path: str = ""

    def __post_init__(self) -> None:
        if not self.passes:
            raise CalibrationError(
                "un rapport de calibration sans aucune passe ne mesure rien"
            )
        if self.safety_margin < 0:
            raise CalibrationError(
                f"safety_margin doit être >= 0, reçu {self.safety_margin!r}"
            )

    # ── Étape 6 : conserver le maximum observé ────────────────────────────────

    @property
    def peak_vram_bytes(self) -> int:
        return max(p.peak_vram_bytes for p in self.passes)

    @property
    def peak_ram_bytes(self) -> int:
        return max(p.peak_ram_bytes for p in self.passes)

    @property
    def target_pass(self) -> PassMeasurement:
        """
        La passe cible : c'est d'ELLE que viennent TTFT et débits.

        Les débits de la passe réduite ne décrivent pas le service : un contexte
        de 512 jetons répond plus vite que le contexte cible, et publier ce
        chiffre-là comme performance du modèle serait flatteur et faux.
        """
        for passe in self.passes:
            if passe.phase == PHASE_TARGET:
                return passe
        return self.passes[-1]

    def proof(self) -> CalibrationProof:
        return CalibrationProof(
            identity=self.identity,
            idle_vram_bytes=self.idle_vram_bytes,
            peak_vram_bytes=self.peak_vram_bytes,
            peak_ram_bytes=self.peak_ram_bytes,
            load_seconds=self.target_pass.load_seconds,
            safety_margin=self.safety_margin,
            measured_at=self.measured_at,
            report_path=self.report_path,
        )

    def to_dict(self) -> dict[str, Any]:
        """Projection JSON. Les clés de §9 sont présentes sous leurs noms exacts."""
        cible = self.target_pass
        preuve = self.proof()
        document: dict[str, Any] = {
            "tool": CALIBRATION_TOOL_NAME,
            "kind": CALIBRATION_KIND,
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "avertissement": (
                "Mesure réelle par chargement (§9). Ce n'est PAS une garantie de "
                "non-dépassement : voir « limites_mesure ». La valeur proposée n'a été "
                "APPLIQUÉE nulle part — ce module n'écrit jamais models.yaml."
            ),
            "unite": "Gio (2^30 octets)",
            "calibration": preuve.to_dict(),
            "idle_ram_gb": _gib(self.idle_ram_bytes),
            "load_seconds": round(cible.load_seconds, 3),
            "ttft_ms": cible.ttft_ms,
            "prompt_tokens_per_second": cible.prompt_tokens_per_second,
            "generation_tokens_per_second": cible.generation_tokens_per_second,
            "debit_unite": DEBIT_UNITE,
            "sample_interval_seconds": self.sample_interval_seconds,
            "sequence": list(SEQUENCE_CALIBRATION),
            "passes": [p.to_dict() for p in self.passes],
            "limites_mesure": list(LIMITES_MESURE),
        }
        return document


# ── Validation du rapport rendu ───────────────────────────────────────────────

_REQUIRED_CALIBRATION_KEYS: tuple[str, ...] = (
    "model_id",
    "runtime_version",
    "hardware_fingerprint",
    "params_fingerprint",
    "idle_vram_gb",
    "peak_vram_gb",
    "peak_ram_gb",
    "load_seconds",
    "measured_vram_gb",
    "safety_margin",
    "proposed_vram_gb",
    "applied",
    "measured_at",
)


def validate_calibration_document(document: Any) -> tuple[str, ...]:
    """
    Contrôle un rapport de calibration relu. Retourne les erreurs, vide si sain.

    Trois familles de contrôles, et la troisième est la seule qui compte
    vraiment :

    1. la forme — clés de §9 présentes, empreintes bien formées, `kind` correct ;
    2. l'honnêteté du document — `applied` doit valoir `False`, un rapport de ce
       module ne pouvant pas avoir appliqué quoi que ce soit ;
    3. **le recoupement du dérivé** — `proposed_vram_gb` est RECALCULÉ depuis
       `peak_vram_gb` et `safety_margin`. Un rapport dont la proposition a été
       abaissée à la main, pour faire tenir un modèle dans un budget, est
       rejeté. Sans ce contrôle, la preuve ne prouverait rien.
    """
    errors: list[str] = []

    if not isinstance(document, dict):
        return (f"le rapport doit être un objet JSON, reçu {type(document).__name__}",)

    if document.get("tool") != CALIBRATION_TOOL_NAME:
        errors.append(
            f"tool doit valoir « {CALIBRATION_TOOL_NAME} », reçu {document.get('tool')!r}"
        )
    if document.get("kind") != CALIBRATION_KIND:
        errors.append(
            f"kind doit valoir « {CALIBRATION_KIND} », reçu {document.get('kind')!r} — "
            "une estimation ne se relit pas comme une mesure"
        )

    version = document.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        errors.append(f"schema_version doit être un entier, reçu {version!r}")
    elif version != CALIBRATION_SCHEMA_VERSION:
        errors.append(
            f"schema_version {version} n'est pas celle de ce module "
            f"({CALIBRATION_SCHEMA_VERSION})"
        )

    calibration = document.get("calibration")
    if not isinstance(calibration, dict):
        errors.append("calibration doit être un objet")
        return tuple(errors)

    for cle in _REQUIRED_CALIBRATION_KEYS:
        if calibration.get(cle, _ABSENT) is _ABSENT:
            errors.append(f"calibration.{cle} est obligatoire (§9)")

    for cle in ("model_id", "runtime_version", "measured_at"):
        valeur = calibration.get(cle)
        if not isinstance(valeur, str) or not valeur:
            errors.append(f"calibration.{cle} doit être une chaîne non vide")

    for cle in ("hardware_fingerprint", "params_fingerprint"):
        if not is_fingerprint(calibration.get(cle)):
            errors.append(
                f"calibration.{cle} doit valoir « sha256:<64 hexadécimaux> », "
                f"reçu {calibration.get(cle)!r}"
            )

    for cle in ("idle_vram_gb", "peak_vram_gb", "peak_ram_gb", "load_seconds",
                "measured_vram_gb", "safety_margin", "proposed_vram_gb"):
        valeur = calibration.get(cle)
        if isinstance(valeur, bool) or not isinstance(valeur, (int, float)):
            errors.append(f"calibration.{cle} doit être un nombre, reçu {valeur!r}")
        elif valeur < 0:
            errors.append(f"calibration.{cle} doit être >= 0, reçu {valeur!r}")

    applied = calibration.get("applied", _ABSENT)
    if applied is not _ABSENT and applied is not False:
        errors.append(
            f"calibration.applied doit valoir false, reçu {applied!r} — ce module "
            "propose une valeur, il ne l'applique jamais"
        )

    passes = document.get("passes")
    if not isinstance(passes, list) or not passes:
        errors.append("passes doit être une liste non vide : sans passe, rien n'a été mesuré")

    if not isinstance(document.get("limites_mesure"), list) or not document.get("limites_mesure"):
        errors.append(
            "limites_mesure doit être une liste non vide — un rapport qui tait ce que la "
            "mesure ne garantit pas se fait lire comme une garantie"
        )

    if errors:
        return tuple(errors)

    errors.extend(_recouper_proposition(calibration))
    errors.extend(_recouper_pic(calibration, passes))
    errors.extend(_recouper_chargement(calibration, document))
    return tuple(errors)


def _recouper_chargement(
    calibration: Mapping[str, Any], document: Mapping[str, Any]
) -> list[str]:
    """
    `load_seconds` figure à deux endroits : les deux doivent dire la même chose.

    La durée de chargement est publiée à la racine (§9) ET dans la preuve, parce
    que le consommateur d'AUT-007 l'exige dans le bloc. Deux copies d'une même
    mesure sont une divergence en attente ; ici elle est recoupée, donc elle ne
    peut plus s'installer en silence.
    """
    racine = document.get("load_seconds")
    if isinstance(racine, bool) or not isinstance(racine, (int, float)):
        return [f"load_seconds (racine) doit être un nombre, reçu {racine!r}"]
    if abs(float(calibration["load_seconds"]) - float(racine)) > 0.0011:
        return [
            f"calibration.load_seconds annonce {calibration['load_seconds']!r} alors que "
            f"load_seconds à la racine vaut {racine!r} : les deux nomment la même mesure"
        ]
    return []


def _recouper_proposition(calibration: Mapping[str, Any]) -> list[str]:
    """Recalcule `proposed_vram_gb` depuis le pic et la marge, et confronte."""
    mesure = float(calibration["measured_vram_gb"])
    marge = float(calibration["safety_margin"])
    temoin = CalibrationProof(
        identity=CalibrationIdentity(
            model_id=str(calibration["model_id"]),
            runtime_version=str(calibration["runtime_version"]),
            hardware_fingerprint=str(calibration["hardware_fingerprint"]),
            params_fingerprint=str(calibration["params_fingerprint"]),
        ),
        idle_vram_bytes=0,
        peak_vram_bytes=int(mesure * (1024 ** 3)),
        peak_ram_bytes=0,
        load_seconds=float(calibration["load_seconds"]),
        safety_margin=marge,
        measured_at=str(calibration["measured_at"]),
    )
    erreurs: list[str] = []
    if abs(float(calibration["peak_vram_gb"]) - mesure) > 0.005:
        erreurs.append(
            f"peak_vram_gb annonce {calibration['peak_vram_gb']!r} alors que "
            f"measured_vram_gb vaut {mesure!r} : les deux nomment la même mesure brute"
        )
    attendu = temoin.proposed_vram_gb
    if abs(float(calibration["proposed_vram_gb"]) - attendu) > 0.011:
        erreurs.append(
            f"proposed_vram_gb annonce {calibration['proposed_vram_gb']!r}, attendu "
            f"{attendu!r} d'après {mesure!r} Gio mesurés et une marge de {marge!r}"
        )
    return erreurs


def _recouper_pic(calibration: Mapping[str, Any], passes: Sequence[Any]) -> list[str]:
    """
    Recalcule le pic conservé depuis les passes (§9 étape 6 : « le maximum observé »).

    Un rapport dont le pic déclaré serait inférieur au maximum de ses propres
    passes proposerait une capacité que ses propres mesures contredisent.
    """
    releves = [
        p.get("peak_vram_gb") for p in passes
        if isinstance(p, dict) and isinstance(p.get("peak_vram_gb"), (int, float))
        and not isinstance(p.get("peak_vram_gb"), bool)
    ]
    if not releves:
        return ["aucune passe ne porte de peak_vram_gb exploitable"]
    attendu = max(float(v) for v in releves)
    if abs(float(calibration["measured_vram_gb"]) - attendu) > 0.011:
        return [
            f"measured_vram_gb annonce {calibration['measured_vram_gb']!r} alors que le "
            f"maximum des passes vaut {attendu!r} — §9 impose de conserver le maximum observé"
        ]
    return []


def assert_valid_calibration_document(document: Any) -> None:
    """Lève `CalibrationError` si le rapport est incohérent. Fail-closed."""
    erreurs = validate_calibration_document(document)
    if erreurs:
        raise CalibrationError(
            "rapport de calibration incohérent : " + " ; ".join(erreurs)
        )


def render_calibration_json(report: CalibrationReport) -> str:
    """Rend le rapport, après contrôle de non-divulgation ET de cohérence."""
    document = report.to_dict()
    schema.assert_no_secrets(document)
    assert_valid_calibration_document(document)
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False)


def render_calibration_human(report: CalibrationReport) -> str:
    """
    Rend le rapport en français. La valeur brute et la valeur proposée côte à côte.

    Aucun balisage : ce texte finit dans un ticket ou un journal systemd, comme
    celui de `execution.render_execution_human()`.
    """
    document = report.to_dict()
    schema.assert_no_secrets(document)
    assert_valid_calibration_document(document)

    preuve = report.proof()
    lignes: list[str] = [
        "RAPPORT DE CALIBRATION EVARUNTIME",
        f"  Modèle        : {report.identity.model_id}",
        f"  Runtime       : {report.identity.runtime_version}",
        f"  Matériel      : {report.identity.hardware_fingerprint}",
        f"  Paramètres    : {report.identity.params_fingerprint}",
        f"  Mesuré le     : {report.measured_at}",
        "",
        "MESURES (Gio, 2^30 octets)",
        f"  VRAM au repos : {_gib(report.idle_vram_bytes):.2f}",
        f"  VRAM au pic   : {preuve.measured_vram_gb:.2f}",
        f"  RAM au pic    : {_gib(report.peak_ram_bytes):.2f}",
        f"  Chargement    : {report.target_pass.load_seconds:.2f} s",
        f"  TTFT          : {report.target_pass.ttft_ms} ms",
        f"  Débit prompt  : {report.target_pass.prompt_tokens_per_second:.2f} tok/s",
        f"  Débit génér.  : {report.target_pass.generation_tokens_per_second:.2f} tok/s",
        "",
        "PASSES",
    ]
    for passe in report.passes:
        lignes.append(
            f"  - {passe.phase} (ctx={passe.ctx_size}, parallel={passe.parallel}) : "
            f"pic VRAM {_gib(passe.peak_vram_bytes):.2f} Gio, "
            f"pic RAM {_gib(passe.peak_ram_bytes):.2f} Gio, "
            f"{passe.sample_rounds} relevé(s)"
        )
    lignes.extend([
        "",
        "PROPOSITION (non appliquée)",
        f"  {preuve.margin_formula()}",
        f"  vram_gb proposé : {preuve.proposed_vram_gb:.2f}",
        "  Aucune écriture n'a été faite dans models.yaml : la décision d'activation",
        "  appartient à l'opérateur et à l'étape d'écriture du registre.",
        "",
        "CE QUE CETTE MESURE NE GARANTIT PAS",
    ])
    lignes.extend(f"  · {limite}" for limite in LIMITES_MESURE)
    return "\n".join(lignes)


# ── Relecture d'une preuve ────────────────────────────────────────────────────

def proof_from_document(document: Mapping[str, Any], *, origin: str = "<rapport>") -> CalibrationProof:
    """
    Reconstruit une preuve depuis un rapport relu, ou lève.

    La validation complète précède la reconstruction : une preuve n'est jamais
    fabriquée « au mieux » depuis un document douteux, sans quoi le contrôle de
    réutilisabilité s'appuierait sur des empreintes qu'on n'a pas vérifiées.
    """
    erreurs = validate_calibration_document(document)
    if erreurs:
        raise CalibrationError(f"{origin} : " + " ; ".join(erreurs))
    calibration = document["calibration"]
    return CalibrationProof(
        identity=CalibrationIdentity(
            model_id=str(calibration["model_id"]),
            runtime_version=str(calibration["runtime_version"]),
            hardware_fingerprint=str(calibration["hardware_fingerprint"]),
            params_fingerprint=str(calibration["params_fingerprint"]),
        ),
        idle_vram_bytes=int(float(calibration["idle_vram_gb"]) * (1024 ** 3)),
        peak_vram_bytes=int(float(calibration["measured_vram_gb"]) * (1024 ** 3)),
        peak_ram_bytes=int(float(calibration["peak_ram_gb"]) * (1024 ** 3)),
        load_seconds=float(calibration["load_seconds"]),
        safety_margin=float(calibration["safety_margin"]),
        measured_at=str(calibration["measured_at"]),
        report_path=str(calibration.get("report_path") or ""),
    )


def load_proof_file(path: Path | str) -> CalibrationProof:
    """Lit et valide un rapport sur disque. Lève `CalibrationError` sur tout défaut."""
    cible = Path(path)
    try:
        texte = cible.read_text(encoding="utf-8")
    except OSError as exc:
        raise CalibrationError(f"{cible} : lecture impossible ({exc})") from exc
    try:
        document = json.loads(texte)
    except (TypeError, ValueError) as exc:
        raise CalibrationError(f"{cible} : JSON illisible ({exc})") from exc
    return proof_from_document(document, origin=str(cible))


def _slug(model_id: str) -> str:
    """Nom de fichier sûr dérivé d'un identifiant de modèle."""
    nettoye = re.sub(r"[^a-z0-9._-]+", "-", model_id.strip().lower()).strip("-._")
    if not nettoye:
        raise CalibrationError(
            f"identifiant de modèle inexploitable pour nommer un rapport : {model_id!r}"
        )
    return nettoye


def report_filename(identity: CalibrationIdentity) -> str:
    """
    Nom du rapport, porteur des empreintes (critère d'acceptation d'AUT-008).

    Le triplet apparaît dans le nom pour qu'une calibration sur un autre GPU ou
    d'autres paramètres n'ÉCRASE PAS la précédente : deux hôtes qui partagent un
    répertoire de rapports doivent pouvoir y coexister, et l'écrasement
    silencieux ferait disparaître une preuve encore valable ailleurs.
    """
    return (
        f"{REPORT_PREFIX}-{_slug(identity.model_id)}"
        f"-{_court(identity.params_fingerprint)}"
        f"-{_court(identity.hardware_fingerprint)}.json"
    )


def _court(empreinte: str) -> str:
    """Douze hexadécimaux d'une empreinte : assez pour nommer un fichier."""
    return empreinte[len(_FINGERPRINT_PREFIX):][:12]


@dataclass(frozen=True)
class ExistingProofs:
    """Ce que le répertoire de rapports contient déjà, et ce qu'on en conclut."""
    reusable: CalibrationProof | None
    verdicts: tuple[tuple[str, ReuseVerdict], ...] = ()
    unreadable: tuple[str, ...] = ()


def find_reusable_proof(report_dir: Path | str, identity: CalibrationIdentity) -> ExistingProofs:
    """
    Cherche dans `report_dir` une mesure réutilisable pour ce triplet exact.

    Toutes les preuves du même modèle sont examinées, pas seulement celle dont
    le nom correspond : c'est ce qui permet de DIRE à l'opérateur qu'une mesure
    existe mais qu'elle a été prise ailleurs, plutôt que de la recalibrer sans
    explication. Un fichier illisible ou incohérent n'est jamais réutilisé — il
    est signalé, et la calibration a lieu.
    """
    dossier = Path(report_dir)
    if not dossier.is_dir():
        return ExistingProofs(reusable=None)

    verdicts: list[tuple[str, ReuseVerdict]] = []
    illisibles: list[str] = []
    reutilisable: CalibrationProof | None = None

    for chemin in sorted(dossier.glob(f"{REPORT_PREFIX}-{_slug(identity.model_id)}-*.json")):
        try:
            preuve = load_proof_file(chemin)
        except CalibrationError as exc:
            illisibles.append(f"{chemin.name} : {exc}")
            continue
        verdict = evaluate_reuse(preuve, identity)
        verdicts.append((chemin.name, verdict))
        if verdict.reusable and reutilisable is None:
            reutilisable = preuve

    return ExistingProofs(
        reusable=reutilisable,
        verdicts=tuple(verdicts),
        unreadable=tuple(illisibles),
    )


# ── Options ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CalibrationOptions:
    """
    Tout ce que la calibration ne mesure pas elle-même.

    `runtime_version` et `hardware_fingerprint` sont DÉCLARÉS par l'appelant
    (résolveur de runtime AUT-003, inventaire AUT-002) : il faut les connaître
    avant de charger quoi que ce soit pour décider de l'idempotence. La version
    déclarée est ensuite RECOUPÉE contre celle que rend le chargeur — une preuve
    étiquetée d'un build alors qu'un autre a servi serait fausse.
    """
    probes: CalibrationProbes
    runtime_version: str
    hardware_fingerprint: str
    report_dir: Path
    params: Mapping[str, CalibrationParams] = field(default_factory=dict)
    prompt: str = DEFAULT_PROMPT
    safety_margin: float = DEFAULT_SAFETY_MARGIN
    sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS
    sample_budget_seconds: float = DEFAULT_SAMPLE_BUDGET_SECONDS
    max_samples: int = DEFAULT_MAX_SAMPLES
    expected_load_seconds: float = 120.0
    expected_prompt_seconds: float = 30.0
    validate_environment: EnvironmentValidator | None = None

    def __post_init__(self) -> None:
        if self.validate_environment is None:
            object.__setattr__(
                self,
                "validate_environment",
                getattr(
                    self.probes,
                    "validate_environment",
                    _noop_environment_validator,
                ),
            )
        if not isinstance(self.runtime_version, str) or not self.runtime_version.strip():
            raise CalibrationError(
                "runtime_version doit être une chaîne non vide : une mesure qui ne sait "
                "pas quel build de llama-server l'a produite n'est réutilisable nulle part"
            )
        if not is_fingerprint(self.hardware_fingerprint):
            raise CalibrationError(
                "hardware_fingerprint doit valoir « sha256:<64 hexadécimaux> », reçu "
                f"{self.hardware_fingerprint!r} — utilisez hardware_fingerprint()"
            )
        if not isinstance(self.safety_margin, (int, float)) or isinstance(self.safety_margin, bool):
            raise CalibrationError(f"safety_margin doit être un nombre, reçu {self.safety_margin!r}")
        if self.safety_margin < 0:
            raise CalibrationError(f"safety_margin doit être >= 0, reçu {self.safety_margin!r}")
        if not self.prompt.strip():
            raise CalibrationError("prompt doit être une chaîne non vide (§9, étape 4)")

    def params_for(self, model_id: str) -> CalibrationParams:
        """Paramètres du modèle, ou refus explicite. Aucun défaut implicite."""
        parametres = self.params.get(model_id)
        if parametres is None:
            raise CalibrationError(
                f"aucun paramètre de service déclaré pour « {model_id} » : impossible de "
                "calibrer un modèle sans savoir à quel contexte ni à quel parallélisme il "
                "sera servi. Une valeur par défaut produirait une mesure sans rapport avec "
                "le service réel."
            )
        return parametres

    def identity_for(self, model_id: str) -> CalibrationIdentity:
        return CalibrationIdentity(
            model_id=model_id,
            runtime_version=self.runtime_version,
            hardware_fingerprint=self.hardware_fingerprint,
            params_fingerprint=self.params_for(model_id).fingerprint(),
        )

    def estimated_seconds(self) -> float:
        """Durée qu'une calibration réelle prendrait — deux passes complètes."""
        return round(
            len(PHASES) * (self.expected_load_seconds + self.expected_prompt_seconds), 1
        )


# ── Séquence de §9 ────────────────────────────────────────────────────────────

async def _relever_repos(probes: CalibrationProbes) -> tuple[int, int]:
    """
    §9 étape 1 — RAM et VRAM au repos. Un relevé raté est un échec, pas un zéro.

    Prendre 0 pour « rien d'occupé » ferait passer une sonde muette pour un GPU
    vide, et donc gonflerait artificiellement la marge disponible.
    """
    vram = await probes.read_vram()
    if not vram.ok:
        raise CalibrationError(
            "VRAM au repos non relevable : " + (vram.detail or "sans détail") +
            " — sans point de départ, aucun pic n'est interprétable"
        )
    ram = await probes.read_ram()
    if not ram.ok:
        raise CalibrationError(
            "RAM au repos non relevable : " + (ram.detail or "sans détail")
        )
    return int(vram.used_bytes), int(ram.used_bytes)


async def _executer_passe(
    phase: str,
    model_id: str,
    params: CalibrationParams,
    options: CalibrationOptions,
    context: ex.ExecutionContext,
) -> PassMeasurement:
    """
    Une passe complète : charger, échantillonner, prompter, décharger.

    Le déchargement est dans un `finally`, y compris quand la passe échoue en
    cours de route. `AGENTS.md` est explicite : un modèle qui reste chargé après
    un outil de diagnostic est une fuite de VRAM en production. Un déchargement
    qui échoue transforme la passe en échec même si les mesures étaient bonnes —
    la machine est alors dans un état que l'opérateur doit connaître.
    """
    ctx_size, parallel = params.for_phase(phase)
    probes = options.probes
    echantillonneur = PeakSampler(
        probes,
        monotonic=context.monotonic,
        interval_seconds=options.sample_interval_seconds,
        budget_seconds=options.sample_budget_seconds,
        max_samples=options.max_samples,
    )

    charge = False
    debut = context.monotonic()
    try:
        # §9 étapes 2 et 3 — charger, et relever les pics PENDANT la charge.
        resultat = await echantillonneur.during(
            probes.load_model(LoadRequest(
                model_id=model_id, phase=phase, ctx_size=ctx_size, parallel=parallel,
            ))
        )
        load_seconds = max(context.monotonic() - debut, 0.0)
        if not isinstance(resultat, LoadOutcome) or not resultat.ok:
            detail = resultat.detail if isinstance(resultat, LoadOutcome) else str(type(resultat))
            raise CalibrationError(
                f"chargement de « {model_id} » en phase {phase} impossible "
                f"(ctx={ctx_size}, parallel={parallel}) : {detail or 'sans détail'}"
            )
        charge = True

        if resultat.runtime_version != options.runtime_version:
            raise CalibrationError(
                f"le runtime qui a servi ({resultat.runtime_version!r}) n'est pas celui "
                f"annoncé ({options.runtime_version!r}) : la mesure serait étiquetée d'un "
                "build qui ne l'a pas produite, et donc réutilisée à tort"
            )

        # §9 étape 4 — prompt court, en continuant d'échantillonner.
        prompt = await echantillonneur.during(probes.run_prompt(model_id, options.prompt))
        if not isinstance(prompt, PromptOutcome) or not prompt.ok:
            detail = prompt.detail if isinstance(prompt, PromptOutcome) else str(type(prompt))
            raise CalibrationError(
                f"prompt court sur « {model_id} » en phase {phase} sans résultat : "
                f"{detail or 'sans détail'}"
            )
        debits = _debits(prompt, phase)
    finally:
        if charge:
            sortie = await probes.unload_model(model_id)
            if not isinstance(sortie, UnloadOutcome) or not sortie.ok:
                detail = sortie.detail if isinstance(sortie, UnloadOutcome) else str(type(sortie))
                raise CalibrationError(
                    f"« {model_id} » n'a pas pu être déchargé après la phase {phase} : "
                    f"{detail or 'sans détail'}. La VRAM reste occupée par un modèle chargé "
                    "pour un simple diagnostic — c'est une fuite, pas un détail."
                )

    pic_vram = echantillonneur.peak_vram_bytes
    pic_ram = echantillonneur.peak_ram_bytes
    if pic_vram is None or pic_ram is None:
        # Le point le plus important du module : PAS de repli sur une estimation.
        manquants = ", ".join(
            nom for nom, valeur in (("VRAM", pic_vram), ("RAM", pic_ram)) if valeur is None
        )
        raise CalibrationError(
            f"aucun pic {manquants} relevé pendant la phase {phase} sur {echantillonneur.rounds} "
            f"tour(s) d'échantillonnage : " + (
                " ; ".join(echantillonneur.failures[:3]) or "les sondes n'ont rien rendu"
            ) + ". Une calibration sans pic mesuré ÉCHOUE ; elle ne retombe pas sur "
            "l'estimation statique en la présentant comme mesurée."
        )

    return PassMeasurement(
        phase=phase,
        ctx_size=ctx_size,
        parallel=parallel,
        load_seconds=load_seconds,
        peak_vram_bytes=pic_vram,
        peak_ram_bytes=pic_ram,
        vram_total_bytes=echantillonneur.vram_total_bytes or 0,
        ttft_ms=prompt.ttft_ms,
        prompt_tokens_per_second=debits[0],
        generation_tokens_per_second=debits[1],
        sample_rounds=echantillonneur.rounds,
        probe_failures=echantillonneur.failures,
    )


def _debits(prompt: PromptOutcome, phase: str) -> tuple[float, float]:
    """
    Débits de §9. Refuse une durée ou un compte nuls plutôt que d'écrire 0.

    Un `0 tok/s` publié comme mesure serait indiscernable d'un modèle qui ne
    répond pas, et un `tokens / 0 s` est une division qui n'a pas de sens : dans
    les deux cas la mesure n'existe pas, et le dire est la seule conduite juste.
    """
    if prompt.ttft_ms <= 0:
        raise CalibrationError(
            f"phase {phase} : TTFT mesuré à {prompt.ttft_ms} ms — une latence nulle ou "
            "négative n'est pas une mesure"
        )
    for label, jetons, secondes in (
        ("prompt", prompt.prompt_tokens, prompt.prompt_seconds),
        ("génération", prompt.generation_tokens, prompt.generation_seconds),
    ):
        if jetons <= 0 or secondes <= 0:
            raise CalibrationError(
                f"phase {phase} : débit {label} non mesurable ({jetons} jeton(s) en "
                f"{secondes} s) — publier 0 tok/s ferait passer une absence de mesure "
                "pour une performance"
            )
    return (
        round(prompt.prompt_tokens / prompt.prompt_seconds, 2),
        round(prompt.generation_tokens / prompt.generation_seconds, 2),
    )


async def calibrate(
    model_id: str,
    options: CalibrationOptions,
    context: ex.ExecutionContext,
) -> CalibrationReport:
    """
    Applique les neuf étapes de §9 dans leur ordre littéral et rend le rapport.

    N'écrit rien : `write_report()` s'en charge (étape 8), et l'étape 9 se lit
    dans `CalibrationReport.proof().proposed_vram_gb`.
    """
    params = options.params_for(model_id)
    identity = options.identity_for(model_id)

    validator = options.validate_environment
    if validator is None:  # garde de production, y compris sous `python -O`
        raise CalibrationError("validateur d'environnement de calibration absent")
    await validator(identity)

    return await _calibrate_validated(model_id, params, identity, options, context)


async def _calibrate_validated(
    model_id: str,
    params: CalibrationParams,
    identity: CalibrationIdentity,
    options: CalibrationOptions,
    context: ex.ExecutionContext,
) -> CalibrationReport:
    """Mesure après attestation du runtime et du matériel courants."""

    idle_vram, idle_ram = await _relever_repos(options.probes)  # étape 1

    passes: list[PassMeasurement] = []
    for phase in PHASES:  # étapes 2 à 5
        passes.append(await _executer_passe(phase, model_id, params, options, context))

    # étapes 6 et 7 : le maximum est conservé par `CalibrationReport.peak_*`, la
    # marge est appliquée par `CalibrationProof.proposed_vram_gb`. Les deux sont
    # DÉRIVÉS, donc recalculables et recoupables à la relecture.
    return CalibrationReport(
        identity=identity,
        idle_vram_bytes=idle_vram,
        idle_ram_bytes=idle_ram,
        passes=tuple(passes),
        safety_margin=options.safety_margin,
        measured_at=context.now(),
        sample_interval_seconds=options.sample_interval_seconds,
    )


def write_report(
    report: CalibrationReport,
    options: CalibrationOptions,
    context: ex.ExecutionContext,
) -> Path:
    """
    §9 étape 8 — écrit les mesures dans un rapport SÉPARÉ, et rien d'autre.

    Le chemin passe par `context.resolve_path()` : le répertoire de rapports
    doit être dans les racines autorisées du contexte d'exécution, sinon rien
    n'est écrit. Aucun autre fichier n'est touché — en particulier pas
    `models.yaml`, dont l'écriture appartient à un autre chantier.
    """
    # Contrôlé AVANT le `mkdir`, et pas après : un contrôle placé plus bas
    # laisserait l'arborescence se créer hors des racines autorisées avant que le
    # refus ne survienne. Le nom de fichier, lui, sort de `report_filename()` qui
    # translittère l'identifiant de modèle — il ne peut pas ressortir du dossier,
    # et le recontrôler ne serait qu'une redondance qu'aucun test ne pourrait
    # distinguer d'une ligne morte.
    dossier = context.resolve_path(options.report_dir)
    dossier.mkdir(parents=True, exist_ok=True)
    cible = dossier / report_filename(report.identity)

    # Le chemin est réinjecté dans le document pour qu'une preuve relue sache
    # d'où elle vient, sans que l'appelant ait à le retenir.
    situe = CalibrationReport(
        identity=report.identity,
        idle_vram_bytes=report.idle_vram_bytes,
        idle_ram_bytes=report.idle_ram_bytes,
        passes=report.passes,
        safety_margin=report.safety_margin,
        measured_at=report.measured_at,
        sample_interval_seconds=report.sample_interval_seconds,
        report_path=str(cible),
    )
    cible.write_text(render_calibration_json(situe), encoding="utf-8")
    return cible


# ── Exécuteur ─────────────────────────────────────────────────────────────────

def _finding(code: str, level: str, message: str) -> schema.Finding:
    return schema.Finding(code=code, level=level, message=message)  # type: ignore[arg-type]


def _resultat_simulation(
    step: schema.PlanStep,
    options: CalibrationOptions,
    identity: CalibrationIdentity,
    params: CalibrationParams,
) -> ex.StepResult:
    """
    Simulation : aucun chargement, aucun sous-processus, aucune sonde appelée.

    Le résultat dit ce qui SERAIT mesuré — les neuf étapes, les deux passes,
    leurs paramètres — et combien de temps cela prendrait. C'est la seule chose
    qu'un opérateur a besoin de savoir avant d'autoriser une opération qui
    immobilise un GPU plusieurs minutes.
    """
    return ex.StepResult.for_step(
        step,
        status=ex.STEP_WOULD_APPLY,
        summary=(
            f"calibrerait « {identity.model_id} » en {len(PHASES)} passes "
            f"(ctx={params.reduced_ctx_size} parallel={params.reduced_parallel}, puis "
            f"ctx={params.ctx_size} parallel={params.parallel}), soit environ "
            f"{options.estimated_seconds():g} s de GPU immobilisé"
        ),
        evidence={
            "kind": "simulation",
            "sequence": list(SEQUENCE_CALIBRATION),
            "identity": identity.to_dict(),
            "passes": [
                {"phase": phase, "ctx_size": ctx, "parallel": par}
                for phase, (ctx, par) in ((p, params.for_phase(p)) for p in PHASES)
            ],
            "safety_margin": options.safety_margin,
            "estimated_seconds": options.estimated_seconds(),
            "report_dir": str(options.report_dir),
            "report_filename": report_filename(identity),
            "probes_called": 0,
            "limites_mesure": list(LIMITES_MESURE),
        },
        findings=(_finding(
            "calibration_simulee",
            "info",
            "Aucun modèle n'a été chargé et aucune sonde n'a été interrogée : ce résultat "
            "décrit ce qui serait mesuré, pas ce qui l'a été.",
        ),),
    )


def _resultat_deja_satisfait(
    step: schema.PlanStep,
    preuve: CalibrationProof,
    verdict: ReuseVerdict,
) -> ex.StepResult:
    """Idempotence : une mesure valide pour CE triplet, sans rien recharger."""
    return ex.StepResult.for_step(
        step,
        status=ex.STEP_ALREADY_SATISFIED,
        summary=(
            f"« {preuve.identity.model_id} » a déjà été calibré le {preuve.measured_at} sur ce "
            f"matériel, ce runtime et ces paramètres : "
            f"{preuve.measured_vram_gb:.2f} Gio mesurés, {preuve.proposed_vram_gb:.2f} Gio "
            "proposés — aucun modèle n'a été rechargé"
        ),
        evidence={
            "kind": CALIBRATION_KIND,
            "reused": True,
            "reuse": verdict.to_dict(),
            "calibration": preuve.to_dict(),
            "limites_mesure": list(LIMITES_MESURE),
        },
    )


def make_executor(options: CalibrationOptions) -> ex.StepExecutor:
    """
    Fabrique l'exécuteur de `schema.ACTION_CALIBRATE_MODEL` pour ces options.

    `step.target` porte l'identifiant du modèle : c'est ce que le planificateur y
    met, et c'est ce que l'opérateur a relu.
    """

    async def executer(step: schema.PlanStep, context: ex.ExecutionContext) -> ex.StepResult:
        model_id = step.target
        try:
            params = options.params_for(model_id)
            identity = options.identity_for(model_id)
        except CalibrationError as exc:
            return _echec(step, str(exc), "calibration_impossible")

        if context.dry_run:
            return _resultat_simulation(step, options, identity, params)

        validator = options.validate_environment
        if validator is None:  # garde de production, y compris sous `python -O`
            return _echec(
                step,
                "validateur d'environnement de calibration absent",
                "environnement_calibration_invalide",
            )
        try:
            await validator(identity)
        except (CalibrationError, ex.ExecutionError, OSError) as exc:
            return _echec(
                step,
                f"{type(exc).__name__}: {exc}",
                "environnement_calibration_invalide",
            )

        try:
            existantes = find_reusable_proof(options.report_dir, identity)
        except CalibrationError as exc:
            return _echec(step, str(exc), "preuves_illisibles")

        if existantes.reusable is not None:
            verdict = evaluate_reuse(existantes.reusable, identity)
            return _resultat_deja_satisfait(step, existantes.reusable, verdict)

        # Aucune mesure réutilisable. Si des mesures existent pour ce modèle, on
        # DIT laquelle des empreintes diverge, plutôt que de recalibrer sans mot.
        constats = [
            _finding("mesure_non_reutilisable", "info", f"{nom} : {verdict.message}")
            for nom, verdict in existantes.verdicts if not verdict.reusable
        ]
        constats.extend(
            _finding("rapport_illisible", "warn", f"rapport ignoré — {detail}")
            for detail in existantes.unreadable
        )

        debut = context.monotonic()
        try:
            rapport = await _calibrate_validated(
                model_id, params, identity, options, context
            )
            chemin = write_report(rapport, options, context)
        except (CalibrationError, ex.ExecutionError, OSError) as exc:
            return _echec(
                step,
                f"{type(exc).__name__}: {exc}",
                "calibration_echouee",
                findings=tuple(constats),
                duration_ms=_ms(context.monotonic() - debut),
            )

        preuve = rapport.proof()
        context.journaliser(
            f"calibration de {model_id} : {preuve.measured_vram_gb:.2f} Gio mesurés, "
            f"{preuve.proposed_vram_gb:.2f} Gio proposés"
        )
        return ex.StepResult.for_step(
            step,
            status=ex.STEP_DONE,
            summary=(
                f"« {model_id} » calibré : {preuve.measured_vram_gb:.2f} Gio mesurés au pic, "
                f"{preuve.proposed_vram_gb:.2f} Gio proposés (marge {options.safety_margin:g}). "
                "Rien n'a été écrit dans models.yaml"
            ),
            duration_ms=_ms(context.monotonic() - debut),
            evidence={
                "kind": CALIBRATION_KIND,
                "reused": False,
                "calibration": preuve.to_dict(),
                "report_path": str(chemin),
                "sequence": list(SEQUENCE_CALIBRATION),
                "passes": [p.to_dict() for p in rapport.passes],
                "registry_written": False,
                "limites_mesure": list(LIMITES_MESURE),
            },
            findings=tuple(constats) + (_finding(
                "vram_gb_propose",
                "info",
                f"Nouvelle valeur vram_gb proposée pour « {model_id} » : "
                f"{preuve.proposed_vram_gb:.2f} Gio ({preuve.margin_formula()}). "
                "Elle n'a PAS été appliquée : l'écriture du registre est une étape distincte.",
            ),),
        )

    return executer


def _ms(secondes: float) -> int:
    return max(int(secondes * 1000), 0)


def _echec(
    step: schema.PlanStep,
    detail: str,
    code: str,
    *,
    findings: tuple[schema.Finding, ...] = (),
    duration_ms: int = 0,
) -> ex.StepResult:
    """
    Échec explicite. Aucune valeur `vram_gb` n'est proposée dans ce cas.

    C'est délibéré et c'est vérifié par les tests : une calibration ratée ne
    doit rien laisser derrière elle qu'un consommateur pourrait prendre pour une
    autorisation d'activer le modèle.
    """
    message = ex.redact_for_log(detail)
    return ex.StepResult.for_step(
        step,
        status=ex.STEP_FAILED,
        summary=f"calibration de « {step.target} » impossible",
        duration_ms=duration_ms,
        evidence={"kind": "echec", "registry_written": False},
        findings=findings + (_finding(code, "fail", message),),
        error=message,
    )


def register_executor(registry: ex.ExecutorRegistry, options: CalibrationOptions) -> None:
    """
    Branche l'exécuteur de calibration dans un registre.

    Point d'entrée unique du module pour l'applicateur : `ExecutorRegistry`
    refuse un second enregistrement pour la même action, donc appeler ceci deux
    fois lève — ce qui est le comportement voulu.
    """
    registry.register(schema.ACTION_CALIBRATE_MODEL, make_executor(options))
