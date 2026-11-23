"""
AUT-006 → AUT-011 — contrat d'EXÉCUTION du plan de bootstrap (jalon M2).

Ce que ce module est
--------------------
Le pendant de `schema` côté application. `schema` fige ce qu'un plan DIT ;
celui-ci fige ce qu'une application du plan FAIT, et surtout ce qu'elle a le
droit de faire. Il porte quatre promesses, symétriques de celles du plan :

1. **relecture sûre** — `load_plan_document()` accepte ou refuse un plan relu
   depuis un fichier, jamais « au mieux ». C'est la porte d'entrée de M2, et
   c'est là que se joue la leçon de la vague 5 : un applicateur ne doit jamais
   pouvoir être convaincu d'agir par un champ dérivé que personne ne recoupe.
   La validation de `schema` est réutilisée **et** recoupée ici, indépendamment ;
2. **application délibérée** — `ExecutionMode` n'a pas de valeur par défaut. Nul
   ne peut appliquer par omission d'argument ; la simulation est un mode à part
   entière, pas un drapeau qu'on oublie de passer ;
3. **journal fidèle** — `StepResult`/`ExecutionReport` distinguent « fait »,
   « déjà satisfait », « simulé », « sauté », « échoué » et « non tenté ». Ces
   six-là ne sont pas des nuances de rédaction : rapporter comme « sauté » une
   étape qui n'a jamais été atteinte ment à l'opérateur sur l'état de sa machine ;
4. **sans secret** — un rapport d'exécution est bien plus exposé qu'un plan : il
   porte des chemins, des URL et des sorties de commandes. Le rendu **refuse**
   de publier un document qui fuit, et la journalisation expurge avant d'écrire.

Ce que ce module n'est pas
--------------------------
Il n'exécute aucune action métier. Il ne télécharge rien, n'écrit aucun registre,
ne lance aucun sous-processus et n'ouvre aucune socket. Il n'importe que la
bibliothèque standard et `bootstrap.schema` : c'est ce qui permet aux chantiers
AUT-006 → AUT-011 d'être menés en parallèle contre un contrat figé, exactement
comme les producteurs de la vague 5 l'ont été contre `schema`.

Contrat pour un exécuteur d'action
----------------------------------
```python
async def executer(step: schema.PlanStep, context: ExecutionContext) -> StepResult:
    if context.dry_run:
        return StepResult.for_step(step, status=STEP_WOULD_APPLY, summary="…")
    ...
    return StepResult.for_step(step, status=STEP_DONE, summary="…", evidence={...})

registry.register(schema.ACTION_DOWNLOAD_MODEL, executer)
```

Trois règles s'imposent à lui, et le lanceur les fait respecter :

- il rend un résultat **pour l'étape reçue** (ordre, action et cible identiques) ;
- en simulation il ne rend jamais `done`, en application jamais `would_apply` —
  un journal qui se trompe de mode est pire qu'un journal absent ;
- `evidence` est sérialisable JSON et sans secret. Une empreinte, une taille, un
  code HTTP : oui. Un en-tête d'autorisation : jamais.

Une action de `schema.PLAN_ACTIONS` sans exécuteur enregistré ne s'exécute pas et
ne réussit jamais — elle produit un échec explicite (fail-closed), et les étapes
suivantes sont marquées non tentées.

Codes de sortie — alignés sur ceux du plan, volontairement
----------------------------------------------------------
| Code | Signification                                                       |
|------|---------------------------------------------------------------------|
| 0    | Tout ce qui devait être fait l'a été (ou l'était déjà).              |
| 1    | Au moins un échec, ou des étapes non tentées.                        |
| 2    | RÉSERVÉ : erreur d'usage de la CLI (convention Typer/Click).         |
| 3    | Exécution partielle : simulation, ou étapes volontairement sautées.  |
| 4    | Erreur de l'applicateur lui-même.                                    |

Une simulation complète sort donc en 3, jamais en 0 : rien n'a été appliqué, et
un script d'exploitation ne doit pas pouvoir confondre les deux.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, Protocol, Sequence

from . import schema

log = logging.getLogger(__name__)

# Version du rapport d'exécution. Indépendante de `PLAN_SCHEMA_VERSION` : le
# journal peut évoluer sans que le plan change, et l'inverse est vrai aussi.
EXECUTION_SCHEMA_VERSION = 1

# Nom de l'outil, présent dans le rapport pour qu'un JSON isolé reste
# identifiable sans son contexte d'appel.
EXECUTION_TOOL_NAME = "eva-bootstrap-apply"

EXIT_OK = schema.EXIT_OK
EXIT_FAILED = schema.EXIT_BLOCKED
EXIT_USAGE = schema.EXIT_USAGE
EXIT_PARTIAL = schema.EXIT_WARNINGS
EXIT_ERROR = schema.EXIT_ERROR


# ── Erreurs ───────────────────────────────────────────────────────────────────

class ExecutionError(schema.PlanError):
    """L'exécution ne peut pas avoir lieu, ou son journal est incohérent."""


class PlanRefused(ExecutionError):
    """
    Le plan relu ne sera pas exécuté. Porte TOUTES les raisons, pas la première.

    Un opérateur qui répare un plan une raison à la fois relance l'outil autant
    de fois qu'il y a de défauts. Les raisons sont donc accumulées puis rendues
    ensemble, comme le fait `validate_plan_dict()`.
    """

    def __init__(self, origin: str, reasons: Sequence[str]) -> None:
        self.origin = origin
        self.reasons: tuple[str, ...] = tuple(reasons)
        super().__init__(
            f"plan refusé ({origin}) : " + " ; ".join(self.reasons)
        )


# ── Mode d'exécution ──────────────────────────────────────────────────────────

class ExecutionMode(Enum):
    """
    Simulation ou application. Une énumération, pas une chaîne, et pas de défaut.

    Trois propriétés qu'une chaîne libre n'aurait pas données :

    - **aucun défaut applicatif** : `ExecutionContext` exige le mode en premier
      argument positionnel. Personne n'applique par omission ;
    - **aucune faute de frappe permissive** : `"apply "` ou `"APPLY"` ne sont pas
      `ExecutionMode.APPLY` — ils ne sont rien du tout, et l'appel échoue ;
    - **aucune vérité booléenne accidentelle** : `if mode:` est vrai pour les
      deux membres, donc le code doit nommer celui qu'il teste. C'est
      exactement le piège de `strict: "false"` de la vague 5, fermé en amont.
    """
    DRY_RUN = "dry-run"
    APPLY = "apply"

    @classmethod
    def from_value(cls, value: Any) -> ExecutionMode:
        """Reconstruit un mode depuis un rapport sérialisé. Refuse tout le reste."""
        for mode in cls:
            if isinstance(value, str) and value == mode.value:
                return mode
        raise ExecutionError(
            f"mode d'exécution inconnu : {value!r} "
            f"(attendu parmi {[m.value for m in cls]})"
        )


EXECUTION_MODES: tuple[str, ...] = tuple(mode.value for mode in ExecutionMode)


# ── Statuts d'étape ───────────────────────────────────────────────────────────

# `done` et `already_satisfied` sont volontairement séparés : c'est la preuve
# d'idempotence. Rejouer une application sur un hôte déjà installé doit produire
# un rapport plein d'`already_satisfied` et vide de `done` ; si les deux se
# confondaient, une application qui refait tout le travail serait indiscernable
# d'une application qui n'a rien eu à faire.
STEP_DONE = "done"
STEP_ALREADY_SATISFIED = "already_satisfied"

# Ce que la simulation produit : l'action reste à faire, elle n'a pas été faite.
STEP_WOULD_APPLY = "would_apply"

# Décision délibérée de ne pas exécuter (option de l'opérateur, préalable non
# réuni mais non bloquant). À ne jamais confondre avec `not_attempted`.
STEP_SKIPPED = "skipped"

STEP_FAILED = "failed"

# Étape jamais atteinte, parce qu'une précédente a échoué. L'information n'est
# pas la même que « sautée » : ici, l'état de la machine pour cette étape est
# INCONNU, et l'opérateur doit relancer, pas conclure.
STEP_NOT_ATTEMPTED = "not_attempted"

StepStatus = Literal[
    "done", "already_satisfied", "would_apply", "skipped", "failed", "not_attempted",
]

# Ordre de rendu : ce qui a été fait, ce qui a été simulé, ce qui a été sauté,
# ce qui a échoué, ce qui n'a pas été tenté.
STEP_STATUS_ORDER: tuple[str, ...] = (
    STEP_DONE,
    STEP_ALREADY_SATISFIED,
    STEP_WOULD_APPLY,
    STEP_SKIPPED,
    STEP_FAILED,
    STEP_NOT_ATTEMPTED,
)
STEP_STATUSES: frozenset[str] = frozenset(STEP_STATUS_ORDER)

# Statuts qui attestent qu'une action a réellement été appliquée sur l'hôte.
APPLIED_STATUSES: frozenset[str] = frozenset({STEP_DONE})

# ── Verdict global ────────────────────────────────────────────────────────────

VERDICT_OK = "ok"
VERDICT_PARTIAL = "partial"
VERDICT_INCOMPLETE = "incomplete"
VERDICT_FAILED = "failed"

VERDICTS: frozenset[str] = frozenset({
    VERDICT_OK, VERDICT_PARTIAL, VERDICT_INCOMPLETE, VERDICT_FAILED,
})

_VERDICT_EXIT: dict[str, int] = {
    VERDICT_OK: EXIT_OK,
    VERDICT_PARTIAL: EXIT_PARTIAL,
    VERDICT_INCOMPLETE: EXIT_FAILED,
    VERDICT_FAILED: EXIT_FAILED,
}

_ABSENT = object()

_COUNT_KEYS: tuple[str, ...] = STEP_STATUS_ORDER + ("total",)

_FINGERPRINT_PREFIX = "sha256:"


# ── Horloge et journalisation ─────────────────────────────────────────────────

def _utc_now_iso() -> str:
    """Horodatage ISO-8601 UTC. Remplaçable en test, jamais lu ailleurs."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _discard_log(message: str) -> None:
    """Journal par défaut : silencieux. Le contrat n'impose pas de destination."""


def redact_for_log(message: str) -> str:
    """
    Expurge un message avant journalisation. Réutilise les motifs de `schema`.

    Volontairement adossé à `schema.find_secret_leaks()` plutôt qu'à une seconde
    table de motifs : deux détecteurs de secrets finissent toujours par diverger,
    et c'est celui qui n'a pas été mis à jour qui laisse passer le token. Le
    message expurgé nomme le TYPE détecté, jamais la valeur.
    """
    leaks = schema.find_secret_leaks(message)
    if not leaks:
        return message
    labels = sorted({leak.rsplit("(", 1)[-1].rstrip(")") for leak in leaks})
    return (
        "[message expurgé — il contenait ce qui ressemble à un secret : "
        + ", ".join(labels)
        + "]"
    )


# errno d'un système de fichiers qui ne sait pas `fsync` un répertoire
# (certains NFS, FUSE, CIFS) : ce n'est pas une panne d'écriture.
_DIRECTORY_FSYNC_UNSUPPORTED = frozenset(
    code for code in (
        errno.EINVAL,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    ) if code is not None
)


def fsync_directory(directory: Path | str) -> bool:
    """
    `fsync` d'un répertoire après un `os.replace` DÉJÀ effectué (thread worker).

    Discipline commune à `registry_writer._atomic_write` et au téléchargeur :
    le `fsync` du FICHIER avant le renommage reste obligatoire chez l'appelant ;
    celui du répertoire rend le renommage durable. Sur un système de fichiers
    qui ne le supporte pas (`EINVAL`, `ENOTSUP`), le fichier est néanmoins
    publié et vérifié : déclarer l'étape en échec mentirait sur l'état de
    l'hôte. On journalise et on rend `False`. Toute autre erreur (`EIO`…) est
    une vraie panne de durabilité et remonte.
    """
    descripteur = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descripteur)
    except OSError as exc:
        if exc.errno not in _DIRECTORY_FSYNC_UNSUPPORTED:
            raise
        log.warning(
            "fsync du répertoire %s non supporté par ce système de fichiers (%s) : "
            "le renommage est publié, sa durabilité après coupure n'est pas garantie.",
            directory, exc.strerror or exc.errno,
        )
        return False
    finally:
        os.close(descripteur)
    return True


def ensure_within_allowed_roots(candidate: Path | str, roots: Sequence[Path]) -> Path:
    """
    Résout un chemin et refuse tout ce qui sort des racines autorisées.

    Fail-closed sur deux points : une liste de racines VIDE n'autorise rien (elle
    ne signifie pas « pas de contrainte »), et la comparaison porte sur le chemin
    **résolu**, donc un lien symbolique pointant hors des racines est refusé
    comme le serait un `../..` explicite.
    """
    resolved = Path(candidate).expanduser().resolve()
    if not roots:
        raise ExecutionError(
            f"{resolved} : aucune racine autorisée n'est déclarée dans le contexte "
            "d'exécution — aucun chemin n'est donc écrivable. Déclarez explicitement "
            "les répertoires que l'application a le droit de toucher."
        )
    for root in roots:
        base = Path(root).expanduser().resolve()
        if resolved == base or resolved.is_relative_to(base):
            return resolved
    raise ExecutionError(
        f"{resolved} est hors des racines autorisées "
        f"({', '.join(str(Path(r).expanduser().resolve()) for r in roots)})"
    )


@dataclass(frozen=True)
class ExecutionContext:
    """
    Ce que tous les exécuteurs partagent. Immuable, et sans secret par contrat.

    `mode` est le premier champ et n'a pas de défaut : construire un contexte
    oblige à nommer ce qu'on veut faire. `monotonic` et `now` sont injectables
    pour la même raison que `generated_at` l'est dans `schema` — un test ne doit
    jamais dépendre de l'heure réelle, et un rapport doit être reproductible.

    `log` n'est pas appelé directement par les exécuteurs : ils passent par
    `journaliser()`, qui expurge. Un exécuteur qui écrirait dans `log` sans
    passer par là contournerait la seule barrière du paquet contre un secret
    dans un journal systemd.
    """
    mode: ExecutionMode
    allowed_roots: tuple[Path, ...] = ()
    monotonic: Callable[[], float] = time.monotonic
    now: Callable[[], str] = _utc_now_iso
    log: Callable[[str], None] = _discard_log

    @property
    def dry_run(self) -> bool:
        return self.mode is ExecutionMode.DRY_RUN

    @property
    def applying(self) -> bool:
        return self.mode is ExecutionMode.APPLY

    def journaliser(self, message: str) -> None:
        """Journalise après expurgation. Seul chemin d'écriture admis."""
        self.log(redact_for_log(message))

    def resolve_path(self, candidate: Path | str) -> Path:
        """Chemin résolu et prouvé dans les racines autorisées, ou `ExecutionError`."""
        return ensure_within_allowed_roots(candidate, self.allowed_roots)


# ── Résultat d'étape ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StepResult:
    """
    Ce qu'une étape a produit. Immuable : un résultat consigné ne se réécrit pas.

    `order`, `action` et `target` recopient l'étape d'origine — c'est ce qui
    permet de recouper le journal contre le plan sans faire confiance à l'ordre
    d'arrivée. `evidence` porte les preuves machine-lisibles (empreinte,
    octets écrits, code HTTP, durée mesurée par l'exécuteur) ; `findings`
    réutilise `schema.Finding` plutôt que d'inventer un second type de constat.
    """
    order: int
    action: str
    target: str
    status: StepStatus
    summary: str
    # `None` signifie « l'exécuteur n'a rien mesuré » et laisse le lanceur
    # renseigner la durée. C'est distinct de `0`, qui est une mesure honnête
    # d'étape sub-milliseconde — la confusion des deux rendait indistinguables
    # un exécuteur muet et un exécuteur rapide.
    duration_ms: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    findings: tuple[schema.Finding, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in STEP_STATUSES:
            raise ExecutionError(
                f"statut d'étape inconnu : {self.status!r} "
                f"(attendu parmi {list(STEP_STATUS_ORDER)})"
            )
        if self.action not in schema.PLAN_ACTIONS:
            raise ExecutionError(f"action inconnue dans un résultat : {self.action!r}")
        if not isinstance(self.order, int) or isinstance(self.order, bool) or self.order < 1:
            raise ExecutionError(f"order doit être un entier >= 1, reçu {self.order!r}")
        if not self.summary:
            raise ExecutionError(
                f"étape {self.order} : summary est obligatoire — un résultat sans "
                "phrase française n'est pas lisible par l'opérateur"
            )
        if self.duration_ms is not None and (
            not isinstance(self.duration_ms, int)
            or isinstance(self.duration_ms, bool)
            or self.duration_ms < 0
        ):
            raise ExecutionError(
                f"étape {self.order} : duration_ms doit être un entier >= 0, "
                f"reçu {self.duration_ms!r}"
            )
        # Un échec sans message d'erreur n'est pas exploitable ; un succès qui en
        # porte un est un journal qui se contredit. Les deux sont refusés à la
        # construction, donc aucun rapport ne peut les contenir.
        if self.status == STEP_FAILED and not self.error:
            raise ExecutionError(
                f"étape {self.order} : un échec doit porter un message d'erreur"
            )
        if self.status != STEP_FAILED and self.error:
            raise ExecutionError(
                f"étape {self.order} : seul un échec peut porter un message d'erreur "
                f"(statut {self.status!r})"
            )
        if not isinstance(self.evidence, dict):
            raise ExecutionError(f"étape {self.order} : evidence doit être un objet")
        try:
            json.dumps(self.evidence)
        except TypeError as exc:
            # Détecté ICI, en nommant l'étape, plutôt qu'au rendu final où l'objet
            # fautif n'aurait plus de contexte.
            raise ExecutionError(
                f"étape {self.order} : evidence doit être sérialisable JSON ({exc})"
            ) from exc

    @classmethod
    def for_step(
        cls,
        step: schema.PlanStep,
        *,
        status: StepStatus,
        summary: str,
        duration_ms: int | None = None,
        evidence: dict[str, Any] | None = None,
        findings: tuple[schema.Finding, ...] = (),
        error: str | None = None,
    ) -> StepResult:
        """
        Fabrique le résultat d'une étape en recopiant son identité depuis le plan.

        C'est la voie normale pour un exécuteur : elle rend impossible la faute
        de frappe qui ferait pointer un résultat vers une autre cible que celle
        que l'opérateur a relue.
        """
        return cls(
            order=step.order,
            action=step.action,
            target=step.target,
            status=status,
            summary=summary,
            duration_ms=duration_ms,
            evidence=dict(evidence or {}),
            findings=findings,
            error=error,
        )

    @property
    def failed(self) -> bool:
        return self.status == STEP_FAILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "action": self.action,
            "target": self.target,
            "status": self.status,
            "summary": self.summary,
            "duration_ms": 0 if self.duration_ms is None else self.duration_ms,
            "evidence": self.evidence,
            "findings": [f.to_dict() for f in self.findings],
            "error": self.error,
        }


def not_attempted_result(step: schema.PlanStep, reason: str) -> StepResult:
    """Étape jamais atteinte. L'état de la machine pour cette action est inconnu."""
    return StepResult.for_step(
        step,
        status=STEP_NOT_ATTEMPTED,
        summary=f"non tentée : {reason}",
    )


def unsupported_result(step: schema.PlanStep) -> StepResult:
    """
    Aucun exécuteur enregistré pour cette action : échec explicite, fail-closed.

    Le silence serait le pire des choix — une action du plan non exécutée mais
    rapportée comme réussie ferait conclure à une installation complète. Un
    « sauté » serait à peine mieux : il suggère une décision, alors qu'il s'agit
    d'un trou dans l'applicateur.
    """
    return StepResult.for_step(
        step,
        status=STEP_FAILED,
        summary=f"aucun exécuteur enregistré pour l'action « {step.action} »",
        findings=(schema.Finding(
            code="executeur_absent",
            level="fail",
            message=(
                f"L'action « {step.action} » figure au plan mais aucun exécuteur ne la "
                "prend en charge dans cette version de l'applicateur. Rien n'a été "
                "tenté pour cette étape ni pour les suivantes."
            ),
        ),),
        error=f"aucun exécuteur enregistré pour l'action « {step.action} »",
    )


# ── Rapport d'exécution ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExecutionReport:
    """
    Journal complet d'une exécution. Verdict et compteurs sont DÉRIVÉS.

    Aucun champ récapitulatif n'est stocké : `verdict()`, `counts()`,
    `failures()` et `exit_code()` se recalculent depuis `results` à chaque appel,
    et `validate_execution_document()` refait le même calcul sur le document
    rendu. C'est la leçon la plus chère de la vague 5, appliquée d'emblée : un
    champ dérivé que personne ne recoupe finit par mentir.

    `plan_fingerprint` est ce qui rattache ce rapport à un plan précis. Sans lui,
    un journal « tout est vert » ne prouve rien : il pourrait parler d'un autre
    plan que celui qui a été relu et validé.
    """
    started_at: str
    finished_at: str
    mode: ExecutionMode
    plan_fingerprint: str
    plan_generated_at: str
    results: tuple[StepResult, ...] = ()
    schema_version: int = EXECUTION_SCHEMA_VERSION

    # ── Lecture d'état ────────────────────────────────────────────────────────

    def counts(self) -> dict[str, int]:
        counts = {status: 0 for status in STEP_STATUS_ORDER}
        for result in self.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        counts["total"] = len(self.results)
        return counts

    def failures(self) -> tuple[StepResult, ...]:
        return tuple(r for r in self.results if r.status == STEP_FAILED)

    def verdict(self) -> str:
        """
        Verdict global, recalculé. L'échec l'emporte, puis l'inconnu, puis le partiel.

        L'ordre n'est pas arbitraire : `incomplete` passe avant `partial` parce
        qu'une étape non tentée laisse la machine dans un état non déterminé,
        ce qui est plus grave qu'une étape volontairement sautée.
        """
        statuses = {r.status for r in self.results}
        if STEP_FAILED in statuses:
            return VERDICT_FAILED
        if STEP_NOT_ATTEMPTED in statuses:
            return VERDICT_INCOMPLETE
        if statuses & {STEP_WOULD_APPLY, STEP_SKIPPED}:
            return VERDICT_PARTIAL
        return VERDICT_OK

    def exit_code(self) -> int:
        return _VERDICT_EXIT[self.verdict()]

    @property
    def applied(self) -> bool:
        """Vrai seulement si le mode était l'application réelle."""
        return self.mode is ExecutionMode.APPLY

    def changed(self) -> bool:
        """Au moins une action a réellement modifié l'hôte."""
        return any(r.status in APPLIED_STATUSES for r in self.results)

    def result(self, order: int) -> StepResult | None:
        for item in self.results:
            if item.order == order:
                return item
        return None

    # ── Sérialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": EXECUTION_TOOL_NAME,
            "schema_version": self.schema_version,
            "plan_schema_version": schema.PLAN_SCHEMA_VERSION,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "mode": self.mode.value,
            "applied": self.applied,
            "plan_fingerprint": self.plan_fingerprint,
            "plan_generated_at": self.plan_generated_at,
            "verdict": self.verdict(),
            "exit_code": self.exit_code(),
            "counts": self.counts(),
            "results": [r.to_dict() for r in self.results],
            "failures": [
                {
                    "order": r.order,
                    "action": r.action,
                    "target": r.target,
                    "error": r.error,
                }
                for r in self.failures()
            ],
        }


# Jeu de clés racine du rapport, fermé dans les deux sens : ce que `to_dict()`
# émet est exactement ce que la validation accepte. Sans cette fermeture, un
# rapport enrichi d'un « conclusion: tout est vert » que rien ne recoupe passait
# la validation — même classe de trou que celle fermée en trois passes sur le
# contrat du plan en vague 5.
_DOCUMENT_KEYS: frozenset[str] = frozenset({
    "tool", "schema_version", "plan_schema_version", "started_at", "finished_at",
    "mode", "applied", "plan_fingerprint", "plan_generated_at", "verdict",
    "exit_code", "counts", "results", "failures",
})


# ── Empreinte du plan ─────────────────────────────────────────────────────────

def plan_fingerprint(document: Any) -> str:
    """
    Empreinte stable d'un document de plan : `sha256:<hex>`.

    Calculée sur le JSON canonique (clés triées, séparateurs compacts), donc
    insensible à l'ordre des clés et à la mise en forme, mais sensible à la
    moindre valeur. Deux rendus du même plan donnent la même empreinte ; un plan
    retouché d'un caractère en donne une autre. C'est ce qui permet d'affirmer
    qu'un rapport parle bien du plan qui a été relu.
    """
    payload = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return _FINGERPRINT_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── Relecture d'un plan ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class LoadedPlan:
    """Plan relu, validé et recoupé. Sa seule existence vaut autorisation d'agir."""
    document: dict[str, Any]
    steps: tuple[schema.PlanStep, ...]
    fingerprint: str
    generated_at: str
    mode: str
    origin: str


def load_plan_file(path: Path | str) -> LoadedPlan:
    """Lit le fichier puis délègue. La lecture disque est le seul I/O du module."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanRefused(str(target), (f"lecture impossible ({exc})",)) from exc
    return load_plan_document(text, origin=str(target))


def load_plan_document(text: str, *, origin: str = "<plan>") -> LoadedPlan:
    """
    Charge un plan produit par `cli.py bootstrap-plan` et l'autorise, ou refuse.

    Aucune exécution partielle n'existe ici : soit le document est complet, à la
    bonne version, valide, sans secret, applicable et cohérent avec lui-même,
    soit rien ne se passe. `PlanRefused` porte toutes les raisons du refus.
    """
    try:
        document = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise PlanRefused(origin, (f"JSON illisible : {exc}",)) from exc

    if not isinstance(document, dict):
        raise PlanRefused(origin, (
            f"la racine du plan doit être un objet JSON, reçu {type(document).__name__}",
        ))

    reasons = _refusal_reasons(document)
    if reasons:
        raise PlanRefused(origin, reasons)

    return LoadedPlan(
        document=document,
        steps=tuple(_step_from_dict(step) for step in document["steps"]),
        fingerprint=plan_fingerprint(document),
        generated_at=str(document["generated_at"]),
        mode=str(document["mode"]),
        origin=origin,
    )


def _refusal_reasons(document: dict[str, Any]) -> tuple[str, ...]:
    """Toutes les raisons de ne pas exécuter ce document, dans l'ordre du contrôle."""
    version = document.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        return (
            f"schema_version doit être un entier, reçu {version!r} — un document "
            "sans version n'est pas un plan de bootstrap",
        )
    if version != schema.PLAN_SCHEMA_VERSION:
        # Égalité stricte, et pas « <= » : un plan d'une autre version décrit
        # peut-être les mêmes champs avec d'autres significations. L'appliquer
        # « au mieux » est exactement ce que M2 ne doit jamais faire.
        return (
            f"le plan est en version de schéma {version}, l'applicateur attend "
            f"exactement {schema.PLAN_SCHEMA_VERSION} — régénérez le plan avec la "
            "version d'eva-bootstrap qui va l'appliquer",
        )

    reasons: list[str] = []
    reasons.extend(
        f"structure invalide : {error}" for error in schema.validate_plan_dict(document)
    )
    reasons.extend(
        f"secret exposé : {leak}" for leak in schema.find_secret_leaks(document)
    )
    reasons.extend(_applicability_reasons(document))
    reasons.extend(_steps_reasons(document.get("steps")))
    return tuple(reasons)


def _applicability_reasons(document: dict[str, Any]) -> list[str]:
    """
    Recoupe l'applicabilité **sans faire confiance** au verdict du document.

    `validate_plan_dict()` recalcule déjà ces champs, et ce contrôle-ci fait
    doublon avec lui — c'est délibéré. L'applicateur est le seul consommateur
    dont une erreur modifie une machine : il ne délègue pas à un unique appel la
    décision d'agir. Les bloqueurs sont donc recalculés ici depuis les sections,
    par le même critère que `BootstrapPlan.blockers` (un constat `fail`, ou une
    section `fail` sans constat), et confrontés à ce que le document annonce.
    """
    reasons: list[str] = []

    status = document.get("status")
    if status in ("blocked", "fail"):
        reasons.append(
            f"le plan sort en statut {status!r} : il n'est pas applicable, même partiellement"
        )

    if document.get("applicable") is not True:
        reasons.append(
            f"applicable vaut {document.get('applicable')!r} : seul un plan explicitement "
            "applicable peut être exécuté"
        )

    declared = document.get("blockers")
    if not isinstance(declared, list):
        reasons.append(f"blockers doit être une liste, reçu {type(declared).__name__}")
    elif declared:
        reasons.append(
            f"le plan porte {len(declared)} bloqueur(s) : "
            + " ; ".join(str(b.get("code")) for b in declared if isinstance(b, dict))
        )

    recomputed = _recompute_blockers(document.get("sections"))
    if recomputed:
        reasons.append(
            "les sections du plan portent "
            f"{len(recomputed)} constat(s) bloquant(s) recalculé(s) ici — "
            + " ; ".join(recomputed)
            + " — quoi qu'annonce le document"
        )
    return reasons


def _recompute_blockers(sections: Any) -> tuple[str, ...]:
    """Codes des constats bloquants, recalculés depuis les sections elles-mêmes."""
    if not isinstance(sections, list):
        return ()
    codes: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        findings = section.get("findings")
        findings = findings if isinstance(findings, list) else []
        fails = [
            str(f.get("code"))
            for f in findings
            if isinstance(f, dict) and f.get("level") == "fail"
        ]
        codes.extend(fails)
        if section.get("status") == "fail" and not fails:
            codes.append(f"{section.get('name')}_failed")
    return tuple(codes)


def _steps_reasons(steps: Any) -> list[str]:
    """
    Contrôle l'ordre et les actions des étapes, indépendamment de `schema`.

    L'ordre est le seul contenu opérationnel d'un plan : un trou, un doublon ou
    une numérotation décroissante signifient qu'une étape a été perdue ou
    déplacée entre la relecture par l'opérateur et l'exécution. Une action
    inconnue de `PLAN_ACTIONS` est refusée ici aussi — c'est la garantie qu'un
    document fabriqué à la main ne peut pas nommer librement ce qu'il veut voir
    exécuter.
    """
    if not isinstance(steps, list):
        return [f"steps doit être une liste, reçu {type(steps).__name__}"]

    reasons: list[str] = []
    precedent = 0
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            reasons.append(f"steps[{index}] doit être un objet")
            continue
        action = step.get("action")
        if action not in schema.PLAN_ACTIONS:
            reasons.append(
                f"steps[{index}].action inconnue : {action!r} — l'applicateur "
                "n'exécute que les actions du contrat"
            )
        order = step.get("order")
        if not isinstance(order, int) or isinstance(order, bool):
            reasons.append(f"steps[{index}].order doit être un entier, reçu {order!r}")
            continue
        if order != precedent + 1:
            reasons.append(
                f"steps[{index}].order vaut {order}, attendu {precedent + 1} : la "
                "numérotation doit être continue et strictement croissante depuis 1 "
                "(trou, doublon ou étape déplacée)"
            )
        precedent = order
    return reasons


def _step_from_dict(step: dict[str, Any]) -> schema.PlanStep:
    """Reconstruit l'étape typée. N'est appelé qu'après validation complète."""
    return schema.PlanStep(
        order=int(step["order"]),
        action=str(step["action"]),
        target=str(step["target"]),
        detail=str(step["detail"]),
        requires_root=bool(step["requires_root"]),
        reversible=bool(step["reversible"]),
        estimated_bytes=step.get("estimated_bytes"),
    )


# ── Exécuteurs et registre ────────────────────────────────────────────────────

class StepExecutor(Protocol):
    """
    Ce qu'un chantier AUT-006 → AUT-011 doit fournir : un appelable asynchrone.

    Asynchrone parce que les actions réelles — téléchargement, chargement de
    calibration, recette du premier token — sont des attentes réseau ou
    processus, et que le dépôt exige de garder l'async de bout en bout. Une
    fonction `async def` ordinaire satisfait ce protocole sans classe.
    """

    async def __call__(
        self, step: schema.PlanStep, context: ExecutionContext
    ) -> StepResult:
        ...


class ExecutorRegistry:
    """
    Relie une action du plan à son exécuteur. Fail-closed sur les trois bords.

    - une action absente de `schema.PLAN_ACTIONS` ne peut pas être enregistrée :
      le registre ne sert pas de porte dérobée pour exécuter autre chose que ce
      que le plan a le droit de décrire ;
    - un second enregistrement pour la même action est refusé plutôt que
      d'écraser silencieusement le premier — deux chantiers qui se marchent
      dessus doivent l'apprendre au démarrage, pas en production ;
    - une action sans exécuteur ne s'exécute pas : `execute_plan()` produit un
      échec explicite, jamais un succès ni un silence.
    """

    def __init__(self) -> None:
        self._executors: dict[str, StepExecutor] = {}

    def register(self, action: str, executor: StepExecutor) -> None:
        if action not in schema.PLAN_ACTIONS:
            raise ExecutionError(
                f"action inconnue : {action!r} — seules les actions de "
                "schema.PLAN_ACTIONS peuvent recevoir un exécuteur"
            )
        if action in self._executors:
            raise ExecutionError(
                f"un exécuteur est déjà enregistré pour l'action {action!r} ; "
                "l'écraser silencieusement rendrait l'applicateur imprévisible"
            )
        self._executors[action] = executor

    def get(self, action: str) -> StepExecutor | None:
        return self._executors.get(action)

    def __contains__(self, action: object) -> bool:
        return action in self._executors

    def registered_actions(self) -> tuple[str, ...]:
        return tuple(sorted(self._executors))

    def missing_actions(self, steps: Sequence[schema.PlanStep]) -> tuple[str, ...]:
        """
        Actions du plan qu'aucun exécuteur ne prend en charge.

        Permet à un appelant de refuser AVANT de commencer, plutôt que d'échouer
        à la moitié du parcours en laissant l'hôte à mi-chemin.
        """
        return tuple(sorted({s.action for s in steps if s.action not in self._executors}))


# ── Lanceur ───────────────────────────────────────────────────────────────────

async def execute_plan(
    plan: LoadedPlan,
    registry: ExecutorRegistry,
    context: ExecutionContext,
) -> ExecutionReport:
    """
    Exécute les étapes dans l'ordre et rend le journal. Ne lève pas sur un échec métier.

    Un échec **arrête** la séquence : les étapes suivantes sont consignées
    `not_attempted`, jamais `skipped`. Une étape sautée est une décision ; une
    étape non tentée est un état inconnu, et l'opérateur doit pouvoir les
    distinguer d'un coup d'œil pour savoir s'il relance ou s'il enquête.

    Trois gardes s'appliquent à ce que rendent les exécuteurs, parce que le
    journal est la seule trace de ce qui est arrivé à la machine : une exception
    devient un échec consigné, un résultat qui ne correspond pas à son étape est
    refusé, et un résultat qui contredit le mode l'est aussi.
    """
    started_at = context.now()
    results: list[StepResult] = []
    echec = False

    for step in plan.steps:
        if echec:
            results.append(not_attempted_result(
                step, "une étape précédente a échoué, la séquence est arrêtée"
            ))
            continue

        executor = registry.get(step.action)
        if executor is None:
            result = unsupported_result(step)
        else:
            debut = context.monotonic()
            try:
                brut = await executor(step, context)
            # Large volontairement : un exécuteur qui lève ne doit pas emporter le
            # journal avec lui. `CancelledError` dérive de `BaseException` et
            # continue donc de remonter — une annulation n'est pas un échec métier.
            except Exception as exc:
                brut = StepResult.for_step(
                    step,
                    status=STEP_FAILED,
                    summary=f"l'exécuteur de « {step.action} » a levé une exception",
                    error=redact_for_log(f"{type(exc).__name__}: {exc}"),
                )
            result = _guard_result(brut, step, context, _ms(context.monotonic() - debut))

        context.journaliser(f"étape {result.order} [{result.action}] → {result.status}")
        results.append(result)
        echec = result.status == STEP_FAILED

    return ExecutionReport(
        started_at=started_at,
        finished_at=context.now(),
        mode=context.mode,
        plan_fingerprint=plan.fingerprint,
        plan_generated_at=plan.generated_at,
        results=tuple(results),
    )


def _ms(seconds: float) -> int:
    return max(int(seconds * 1000), 0)


def _guard_result(
    result: Any,
    step: schema.PlanStep,
    context: ExecutionContext,
    mesure_ms: int,
) -> StepResult:
    """
    Vérifie qu'un résultat d'exécuteur est utilisable, ou le remplace par un échec.

    Deux incohérences sont traitées comme des échecs, pas comme des détails :

    - **résultat d'une autre étape** : ordre, action ou cible différents. Le
      journal servirait alors de preuve pour une action qui n'a pas eu lieu ;
    - **contradiction avec le mode** : `done` en simulation signifierait qu'un
      exécuteur a modifié l'hôte alors que l'opérateur ne l'a pas demandé ;
      `would_apply` en application signifierait qu'il a fait semblant. Les deux
      arrêtent la séquence, et c'est voulu.

    La durée mesurée par le lanceur ne remplace celle de l'exécuteur que si
    celui-ci n'en a pas fourni : lui seul sait exclure son temps de préparation.
    """
    if not isinstance(result, StepResult):
        return StepResult.for_step(
            step,
            status=STEP_FAILED,
            summary=f"l'exécuteur de « {step.action} » n'a pas rendu un StepResult",
            duration_ms=mesure_ms,
            error=f"type rendu : {type(result).__name__}",
        )

    if (result.order, result.action, result.target) != (step.order, step.action, step.target):
        return StepResult.for_step(
            step,
            status=STEP_FAILED,
            summary="résultat incohérent avec l'étape exécutée",
            duration_ms=mesure_ms,
            findings=(schema.Finding(
                code="resultat_incoherent",
                level="fail",
                message=(
                    "L'exécuteur a rendu un résultat qui ne désigne pas l'étape reçue : "
                    f"attendu {step.order}/{step.action}/{step.target}, reçu "
                    f"{result.order}/{result.action}/{result.target}."
                ),
            ),),
            error="l'exécuteur a rendu le résultat d'une autre étape",
        )

    if context.dry_run and result.status == STEP_DONE:
        return StepResult.for_step(
            step,
            status=STEP_FAILED,
            summary="l'exécuteur déclare avoir agi alors que le mode est la simulation",
            duration_ms=mesure_ms if result.duration_ms is None else result.duration_ms,
            findings=(schema.Finding(
                code="mutation_en_simulation",
                level="fail",
                message=(
                    f"L'exécuteur de « {step.action} » a rendu le statut « {STEP_DONE} » "
                    "en mode simulation. Une simulation ne modifie rien : le résultat est "
                    "refusé et la séquence est arrêtée."
                ),
            ),),
            error="statut « done » rendu en mode simulation",
        )

    if context.applying and result.status == STEP_WOULD_APPLY:
        return StepResult.for_step(
            step,
            status=STEP_FAILED,
            summary="l'exécuteur n'a fait que simuler alors que le mode est l'application",
            duration_ms=mesure_ms if result.duration_ms is None else result.duration_ms,
            findings=(schema.Finding(
                code="simulation_en_application",
                level="fail",
                message=(
                    f"L'exécuteur de « {step.action} » a rendu le statut "
                    f"« {STEP_WOULD_APPLY} » en mode application. Le journal affirmerait "
                    "une exécution qui n'a pas eu lieu."
                ),
            ),),
            error="statut « would_apply » rendu en mode application",
        )

    if result.duration_ms is not None:
        return result
    return StepResult(
        order=result.order,
        action=result.action,
        target=result.target,
        status=result.status,
        summary=result.summary,
        duration_ms=mesure_ms,
        evidence=result.evidence,
        findings=result.findings,
        error=result.error,
    )


# ── Validation du rapport rendu ───────────────────────────────────────────────

def validate_execution_document(document: Any) -> tuple[str, ...]:
    """
    Contrôle un rapport d'exécution sérialisé. Retourne les erreurs, vide si sain.

    Le rapport est ce qu'un opérateur, un ticket d'incident ou un script de
    déploiement liront pour conclure qu'une machine est installée. Ses champs
    dérivés — verdict, compteurs, liste d'échecs — sont donc **recalculés** ici
    depuis `results` et confrontés à ce que le document annonce. Un rapport
    retouché à la main pour transformer un échec en succès est rejeté.
    """
    errors: list[str] = []

    if not isinstance(document, dict):
        return (f"le rapport doit être un objet JSON, reçu {type(document).__name__}",)

    if document.get("tool") != EXECUTION_TOOL_NAME:
        errors.append(
            f"tool doit valoir « {EXECUTION_TOOL_NAME} », reçu {document.get('tool')!r}"
        )

    unknown = sorted(set(document) - _DOCUMENT_KEYS)
    if unknown:
        errors.append(
            "clés inconnues à la racine du rapport : " + ", ".join(unknown)
            + " — un champ que personne ne recoupe se lit comme une conclusion"
        )

    version = document.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        errors.append(f"schema_version doit être un entier, reçu {version!r}")
    elif version != EXECUTION_SCHEMA_VERSION:
        errors.append(
            f"schema_version {version} n'est pas celle de cet applicateur "
            f"({EXECUTION_SCHEMA_VERSION})"
        )

    # Le rapport atteste contre QUEL contrat de plan l'exécution a eu lieu.
    # Le champ était émis mais jamais relu : un journal archivé annonçant une
    # version de plan arbitraire validait sans une erreur, ce qui vidait
    # l'attestation de son sens.
    plan_version = document.get("plan_schema_version")
    if not isinstance(plan_version, int) or isinstance(plan_version, bool):
        errors.append(
            f"plan_schema_version doit être un entier, reçu {plan_version!r}"
        )
    elif plan_version != schema.PLAN_SCHEMA_VERSION:
        errors.append(
            f"plan_schema_version {plan_version} n'est pas celle de ce contrat "
            f"({schema.PLAN_SCHEMA_VERSION})"
        )

    for key in ("started_at", "finished_at", "plan_generated_at"):
        if not isinstance(document.get(key), str) or not document.get(key):
            errors.append(f"{key} doit être une chaîne non vide")

    fingerprint = document.get("plan_fingerprint")
    if not _is_fingerprint(fingerprint):
        errors.append(
            f"plan_fingerprint doit valoir « {_FINGERPRINT_PREFIX}<64 hexadécimaux> », "
            f"reçu {fingerprint!r}"
        )

    mode = document.get("mode")
    if mode not in EXECUTION_MODES:
        errors.append(f"mode invalide : {mode!r} (attendu parmi {list(EXECUTION_MODES)})")

    applied = document.get("applied", _ABSENT)
    if applied is _ABSENT:
        errors.append("applied est obligatoire")
    elif not isinstance(applied, bool):
        errors.append(f"applied doit être un booléen, reçu {type(applied).__name__}")
    elif mode in EXECUTION_MODES and applied is not (mode == ExecutionMode.APPLY.value):
        errors.append(
            f"applied annonce {applied!r} alors que le mode est {mode!r}"
        )

    errors.extend(_validate_results(document.get("results")))

    # Comme dans `schema` : le verdict n'est recoupé que si la structure tient.
    # Recalculer depuis des résultats mal formés produirait du bruit.
    if not errors:
        errors.extend(_validate_execution_verdict(document))

    return tuple(errors)


def _is_fingerprint(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith(_FINGERPRINT_PREFIX):
        return False
    digest = value[len(_FINGERPRINT_PREFIX):]
    return len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


def _validate_results(results: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(results, list):
        return [f"results doit être une liste, reçu {type(results).__name__}"]

    for index, result in enumerate(results):
        path = f"results[{index}]"
        if not isinstance(result, dict):
            errors.append(f"{path} doit être un objet")
            continue

        if result.get("action") not in schema.PLAN_ACTIONS:
            errors.append(f"{path}.action inconnue : {result.get('action')!r}")
        if result.get("status") not in STEP_STATUSES:
            errors.append(f"{path}.status invalide : {result.get('status')!r}")

        order = result.get("order")
        if not isinstance(order, int) or isinstance(order, bool):
            errors.append(f"{path}.order doit être un entier, reçu {order!r}")
        elif order != index + 1:
            errors.append(
                f"{path}.order vaut {order}, attendu {index + 1} "
                "(numérotation continue depuis 1)"
            )

        for key in ("target", "summary"):
            if not isinstance(result.get(key), str) or not result.get(key):
                errors.append(f"{path}.{key} doit être une chaîne non vide")

        duration = result.get("duration_ms")
        if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
            errors.append(f"{path}.duration_ms doit être un entier >= 0, reçu {duration!r}")

        if not isinstance(result.get("evidence"), dict):
            errors.append(f"{path}.evidence doit être un objet")

        error = result.get("error", _ABSENT)
        if error is _ABSENT:
            errors.append(f"{path}.error est obligatoire (null si l'étape n'a pas échoué)")
        elif error is not None and (not isinstance(error, str) or not error):
            errors.append(f"{path}.error doit être une chaîne non vide ou null")
        elif result.get("status") == STEP_FAILED and not error:
            errors.append(f"{path}.error est obligatoire pour un échec")
        elif result.get("status") != STEP_FAILED and error:
            errors.append(
                f"{path}.error est renseigné alors que le statut est "
                f"{result.get('status')!r} : seul un échec porte une erreur"
            )

        errors.extend(_validate_result_findings(result.get("findings"), path))

    return errors


def _validate_result_findings(findings: Any, path: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(findings, list):
        return [f"{path}.findings doit être une liste"]
    for index, finding in enumerate(findings):
        sub = f"{path}.findings[{index}]"
        if not isinstance(finding, dict):
            errors.append(f"{sub} doit être un objet")
            continue
        if not isinstance(finding.get("code"), str) or not finding.get("code"):
            errors.append(f"{sub}.code doit être une chaîne non vide")
        if finding.get("level") not in ("info", "warn", "fail"):
            errors.append(f"{sub}.level invalide : {finding.get('level')!r}")
        if not isinstance(finding.get("message"), str) or not finding.get("message"):
            errors.append(f"{sub}.message doit être une chaîne non vide")
    return errors


def _validate_execution_counts(counts: dict) -> list[str]:
    """
    Valide `counts` clé par clé, sans coercition numérique.

    Repris littéralement de `schema._validate_counts`, pour la même raison :
    `True == 1`, `False == 0` et `1.0 == 1`. Un rapport entièrement booléen
    passerait l'égalité de dictionnaires tout en violant le contrat.
    """
    errors: list[str] = []
    for key in _COUNT_KEYS:
        value = counts.get(key, _ABSENT)
        if value is _ABSENT:
            errors.append(f"counts.{key} est obligatoire")
        elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(
                f"counts.{key} doit être un entier >= 0, reçu {type(value).__name__}"
            )
    for key in counts:
        if key not in _COUNT_KEYS:
            errors.append(f"counts contient une clé inconnue : {key!r}")
    return errors


def _validate_execution_verdict(document: dict) -> list[str]:
    """Recalcule verdict, code de sortie, compteurs et échecs depuis `results`."""
    errors: list[str] = []

    for champ, attendu, libelle in (
        ("verdict", str, "une chaîne"),
        ("exit_code", int, "un entier"),
        ("counts", dict, "un objet"),
        ("failures", list, "une liste"),
    ):
        valeur = document.get(champ, _ABSENT)
        if valeur is _ABSENT:
            errors.append(f"{champ} est obligatoire")
        elif not isinstance(valeur, attendu) or (attendu is int and isinstance(valeur, bool)):
            errors.append(f"{champ} doit être {libelle}, reçu {type(valeur).__name__}")
    if isinstance(document.get("counts"), dict):
        errors.extend(_validate_execution_counts(document["counts"]))
    if errors:
        return errors

    results = document["results"]
    statuses = [r["status"] for r in results]

    counts = {status: 0 for status in STEP_STATUS_ORDER}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    counts["total"] = len(results)

    if STEP_FAILED in statuses:
        expected_verdict = VERDICT_FAILED
    elif STEP_NOT_ATTEMPTED in statuses:
        expected_verdict = VERDICT_INCOMPLETE
    elif set(statuses) & {STEP_WOULD_APPLY, STEP_SKIPPED}:
        expected_verdict = VERDICT_PARTIAL
    else:
        expected_verdict = VERDICT_OK

    if document["verdict"] != expected_verdict:
        errors.append(
            f"verdict annonce {document['verdict']!r} alors que les résultats donnent "
            f"{expected_verdict!r}"
        )
    if document["exit_code"] != _VERDICT_EXIT[expected_verdict]:
        errors.append(
            f"exit_code annonce {document['exit_code']!r}, attendu "
            f"{_VERDICT_EXIT[expected_verdict]} pour le verdict {expected_verdict!r}"
        )
    if document["counts"] != counts:
        errors.append(f"counts annonce {document['counts']!r}, attendu {counts!r}")

    expected_failures = [
        {
            "order": r["order"],
            "action": r["action"],
            "target": r["target"],
            "error": r["error"],
        }
        for r in results
        if r["status"] == STEP_FAILED
    ]
    errors.extend(_compare_failures(document["failures"], expected_failures))

    # Un rapport de simulation qui déclare avoir agi est un rapport qui ment sur
    # l'état de la machine — le contrôle vaut donc aussi à la lecture, et pas
    # seulement au moment où le lanceur reçoit le résultat.
    if document.get("mode") == ExecutionMode.DRY_RUN.value and counts[STEP_DONE]:
        errors.append(
            f"le rapport est en mode simulation mais déclare {counts[STEP_DONE]} "
            f"étape(s) « {STEP_DONE} » : une simulation ne modifie rien"
        )
    if document.get("mode") == ExecutionMode.APPLY.value and counts[STEP_WOULD_APPLY]:
        errors.append(
            f"le rapport est en mode application mais déclare "
            f"{counts[STEP_WOULD_APPLY]} étape(s) « {STEP_WOULD_APPLY} »"
        )

    return errors


def _compare_failures(declares: list, attendus: list, champ: str = "failures") -> list[str]:
    """
    Compare INTÉGRALEMENT la liste d'échecs aux résultats recalculés.

    Même leçon que `schema._compare_findings` : comparer les longueurs laisse
    passer une liste entièrement inventée de la bonne taille, et c'est
    précisément cette liste qu'un opérateur lit en diagonale.
    """
    if len(declares) != len(attendus):
        return [
            f"{champ} en contient {len(declares)}, attendu {len(attendus)} "
            "d'après les résultats"
        ]
    erreurs: list[str] = []
    for index, (declare, attendu) in enumerate(zip(declares, attendus)):
        if not isinstance(declare, dict):
            erreurs.append(f"{champ}[{index}] doit être un objet, reçu {type(declare).__name__}")
            continue
        for cle in ("order", "action", "target", "error"):
            if declare.get(cle) != attendu.get(cle):
                erreurs.append(
                    f"{champ}[{index}].{cle} annonce {declare.get(cle)!r}, "
                    f"attendu {attendu.get(cle)!r} d'après les résultats"
                )
    return erreurs


def assert_valid_execution_document(document: Any) -> None:
    """Lève `ExecutionError` si le rapport est incohérent. Fail-closed."""
    errors = validate_execution_document(document)
    if errors:
        raise ExecutionError(
            "rapport d'exécution incohérent : " + " ; ".join(errors)
        )


# ── Rendu ─────────────────────────────────────────────────────────────────────

def render_execution_json(report: ExecutionReport) -> str:
    """
    Rend le rapport en JSON, après contrôle de non-divulgation ET de cohérence.

    Les deux contrôles sont faits au rendu, et pas seulement à la construction :
    c'est le rendu qui publie. Un rapport qui expose un chemin de clé privée ou
    dont les compteurs ne correspondent pas à ses résultats n'est pas publié du
    tout — il ne sort pas « avec un avertissement ».
    """
    document = report.to_dict()
    schema.assert_no_secrets(document)
    assert_valid_execution_document(document)
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False)


_MODE_LABEL: dict[str, str] = {
    ExecutionMode.DRY_RUN.value: "SIMULATION — rien n'a été appliqué",
    ExecutionMode.APPLY.value: "APPLICATION RÉELLE",
}

_VERDICT_LABEL: dict[str, str] = {
    VERDICT_OK: "SUCCÈS",
    VERDICT_PARTIAL: "PARTIEL",
    VERDICT_INCOMPLETE: "INCOMPLET — des étapes n'ont pas été tentées",
    VERDICT_FAILED: "ÉCHEC",
}

_GROUP_TITLE: tuple[tuple[tuple[str, ...], str], ...] = (
    ((STEP_DONE, STEP_ALREADY_SATISFIED), "CE QUI A ÉTÉ FAIT"),
    ((STEP_WOULD_APPLY,), "CE QUI SERAIT FAIT (simulation)"),
    ((STEP_SKIPPED,), "CE QUI A ÉTÉ SAUTÉ, ET POURQUOI"),
    ((STEP_FAILED,), "CE QUI A ÉCHOUÉ"),
    ((STEP_NOT_ATTEMPTED,), "CE QUI N'A PAS ÉTÉ TENTÉ"),
)

_STATUS_MARK: dict[str, str] = {
    STEP_DONE: "[ok]",
    STEP_ALREADY_SATISFIED: "[==]",
    STEP_WOULD_APPLY: "[~~]",
    STEP_SKIPPED: "[--]",
    STEP_FAILED: "[XX]",
    STEP_NOT_ATTEMPTED: "[??]",
}


def render_execution_human(report: ExecutionReport) -> str:
    """
    Rend le rapport en français, dans l'ordre où l'opérateur en a besoin.

    Ce qui a été fait, ce qui a été simulé, ce qui a été sauté et pourquoi, ce
    qui a échoué, ce qui n'a pas été tenté. Pas de balisage `rich` : ce texte
    finit dans un journal systemd ou un ticket, où les séquences de couleur ne
    sont que du bruit — et où elles fragmenteraient un secret au point de le
    rendre indétectable par une recherche de sous-chaîne (leçon de la vague 5).
    """
    document = report.to_dict()
    schema.assert_no_secrets(document)
    assert_valid_execution_document(document)

    counts = report.counts()
    lines: list[str] = [
        "RAPPORT D'EXÉCUTION EVARUNTIME",
        f"  Mode          : {_MODE_LABEL.get(report.mode.value, report.mode.value)}",
        f"  Démarré le    : {report.started_at}",
        f"  Terminé le    : {report.finished_at}",
        f"  Schéma        : v{report.schema_version}",
        f"  Plan          : {report.plan_generated_at}",
        f"  Empreinte     : {report.plan_fingerprint}",
        f"  Verdict       : {_VERDICT_LABEL.get(report.verdict(), report.verdict())}",
        f"  Étapes        : {counts['total']}",
    ]

    for statuses, title in _GROUP_TITLE:
        groupe = [r for r in report.results if r.status in statuses]
        if not groupe:
            continue
        lines.extend(["", title])
        for result in groupe:
            mark = _STATUS_MARK.get(result.status, "[??]")
            duree = f"  ({result.duration_ms} ms)" if result.duration_ms else ""
            lines.append(f"  {result.order:>2}. {mark} [{result.action}] {result.target}{duree}")
            lines.append(f"      {result.summary}")
            if result.error:
                lines.append(f"      erreur : {result.error}")
            for finding in result.findings:
                if finding.level != "info":
                    lines.append(f"      · {finding.level} [{finding.code}] {finding.message}")

    if not report.results:
        lines.extend(["", "AUCUNE ÉTAPE", "  Le plan ne décrivait aucune action."])

    lines.extend([
        "",
        "COMPTEURS",
        "  " + ", ".join(f"{status} : {counts[status]}" for status in STEP_STATUS_ORDER),
        "",
        f"Sortie : {report.exit_code()}",
    ])
    return "\n".join(lines)
