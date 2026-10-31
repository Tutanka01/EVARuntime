"""
AUT-010 — pré-chauffage du modèle par défaut avant ouverture du trafic (jalon M2).

Le problème
-----------
Après une installation ou une mise à jour, le premier utilisateur qui arrive
déclenche le chargement du modèle et attend — jusqu'à cinq minutes sur le
registre livré. Décision : on charge AVANT d'ouvrir le
trafic. « Cela améliore beaucoup plus le premier token utilisateur qu'un
heartbeat pendant un chargement de dix minutes. »

Ce que ce module fait, dans l'ordre de §10 « Pré-chauffage »
------------------------------------------------------------
1. charger le modèle par défaut (`POST /admin/models/{id}/load`) ;
2. attendre sa **health réelle** — pas la fin de l'appel de chargement, mais la
   serving readiness observée sur `GET /ready` : `models_ready` doit contenir le
   modèle. C'est `readiness.py` qui répond, ce module ne réimplémente aucun de
   ses contrôles ;
3. exécuter une génération courte, via une sonde **injectée** ;
4. ne déclarer la version déployée qu'après succès ;
5. conserver l'ancienne version tant que la recette n'est pas terminée.

Ce module n'ouvre pas le trafic
-------------------------------
Il rend un `WarmupOutcome` qui **autorise ou refuse** cette ouverture, et une
`ReleaseDecision` qui dit s'il faut déclarer la nouvelle version ou garder
l'ancienne. Il ne touche ni `main.py`, ni les scripts de déploiement :
l'intégration appartient à l'orchestrateur.

La borne est DÉRIVÉE, jamais inventée
-------------------------------------
Un chargement peut légitimement demander jusqu'à 300 s
(`load_timeout_seconds` du registre pour `gemma-4-26b-a4b`), et c'est
précisément ce qui a forcé la dérivation des timeouts nginx depuis le registre
(COR-009, §0.9). `derive_warmup_timeout_seconds()` reprend **la formule de
`ServerManager.ensure_loaded`** — `(load_timeout_seconds du modèle ou
MODEL_LOAD_TIMEOUT_SECONDS) + 10` — et **refuse** de fonctionner si l'appelant
ne lui fournit aucune des deux valeurs. Aucune constante de délai n'est
inventée ici ; seule la grâce de 10 s est reprise telle quelle, et le test la
compare à `doctor._LOAD_TIMEOUT_GRACE_SECONDS` pour qu'une divergence casse la
suite au lieu de passer inaperçue.

Un dépassement est un **échec explicite**, jamais une attente sans fin : la
boucle d'attente est bornée par l'horloge injectée et sort en
`warmup_timeout`.

Pourquoi la génération est injectée
-----------------------------------
Prouver qu'un token sort suppose une identité, une clé et le chemin public
complet : c'est le métier d'AUT-009, et le dupliquer ici produirait deux
recettes qui divergeraient. Ce module reçoit donc une sonde asynchrone et
**refuse d'autoriser le trafic si personne ne lui en fournit une** — un
chargement réussi n'a jamais prouvé qu'un token sort. Fail-closed.

Comme les producteurs de la vague 5, ce module n'importe que
`bootstrap.schema` et `bootstrap.execution` : il ne connaît pas
`bootstrap.first_token`. Son protocole de client est un sous-ensemble strict de
celui d'AUT-009 (une seule méthode, `request`), de sorte qu'un même client
concret satisfasse les deux.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

from . import execution as ex
from . import schema

# Grâce ajoutée au timeout de chargement, reprise **littéralement** de
# `ServerManager.ensure_loaded` et de `doctor.required_load_timeout_seconds`.
# Ce n'est pas une valeur choisie ici : c'est celle que la gateway applique
# déjà, et le test le vérifie contre `doctor`.
LOAD_TIMEOUT_GRACE_SECONDS = 10

# Causes stables. Elles sortent dans l'`evidence` du `StepResult` et dans les
# journaux d'exploitation : elles ne changent pas de nom sans raison.
REASON_OK = "ok"
REASON_LOAD_FAILED = "model_load_failed"
REASON_WARMUP_TIMEOUT = "warmup_timeout"
REASON_NOT_SERVING = "serving_readiness_missing"
REASON_NO_PROBE = "generation_not_probed"
REASON_PROBE_FAILED = "generation_failed"
REASON_READY_UNREACHABLE = "ready_unreachable"

STAGE_PASS = "pass"
STAGE_FAIL = "fail"
STAGE_SKIP = "skip"
STAGE_WARN = "warn"

STAGE_STATUSES: frozenset[str] = frozenset({STAGE_PASS, STAGE_FAIL, STAGE_SKIP, STAGE_WARN})


class WarmupError(ex.ExecutionError):
    """Le pré-chauffage ne peut pas être construit : réglages inexploitables."""


# ── Borne dérivée ─────────────────────────────────────────────────────────────

def derive_warmup_timeout_seconds(
    *,
    model_load_timeout_seconds: int | None,
    default_load_timeout_seconds: int | None,
) -> int:
    """
    Attente maximale admise pour un pré-chauffage, dérivée du registre.

    Exactement la formule de `ServerManager.ensure_loaded` : le
    `load_timeout_seconds` du modèle s'il en porte un, sinon
    `MODEL_LOAD_TIMEOUT_SECONDS`, plus la grâce de 10 s.

    Fail-closed sur trois bords, parce qu'une borne fausse est pire qu'une
    absence de borne :

    - **aucune des deux valeurs** : refus. Inventer 60 s ou 600 s ici ferait
      échouer un chargement sain ou laisserait passer un blocage réel ;
    - **valeur non entière ou booléenne** : refus (`True == 1` en Python, un
      `True` égaré donnerait une borne de 11 s) ;
    - **valeur nulle ou négative** : refus.
    """
    for nom, valeur in (
        ("model_load_timeout_seconds", model_load_timeout_seconds),
        ("default_load_timeout_seconds", default_load_timeout_seconds),
    ):
        if valeur is None:
            continue
        if not isinstance(valeur, int) or isinstance(valeur, bool) or valeur <= 0:
            raise WarmupError(
                f"{nom} doit être un entier > 0, reçu {valeur!r} — la borne du "
                "pré-chauffage est dérivée du registre, elle ne se devine pas"
            )
        return valeur + LOAD_TIMEOUT_GRACE_SECONDS

    raise WarmupError(
        "aucune borne dérivable : fournissez le load_timeout_seconds du modèle ou "
        "MODEL_LOAD_TIMEOUT_SECONDS. Le pré-chauffage refuse de s'exécuter sur une "
        "constante inventée — un chargement peut légitimement durer 300 s (COR-009)"
    )


# ── Contrat du client et de la sonde ──────────────────────────────────────────

@dataclass(frozen=True)
class HttpResponse:
    """Réponse unitaire. `body` est le JSON décodé, ou `None` s'il ne l'est pas."""
    status: int
    body: Any = None


class AdminClient(Protocol):
    """
    Plan de contrôle seulement : ce module ne streame rien.

    Signature volontairement identique à celle de `first_token.HttpClient.request`
    pour qu'un seul client concret serve les deux modules sans adaptateur.
    """

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> Any:
        ...


@dataclass(frozen=True)
class ProbeOutcome:
    """Résultat de la génération courte de §10. `served` est le seul feu vert."""
    served: bool
    reason: str = REASON_OK
    ttft_ms: int = -1
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "served": self.served,
            "reason": self.reason,
            "ttft_ms": self.ttft_ms,
            "detail": self.detail,
        }


GenerationProbe = Callable[[], Awaitable[ProbeOutcome]]
AsyncSleep = Callable[[float], Awaitable[None]]


async def _no_sleep(_seconds: float) -> None:
    """Sommeil par défaut : aucun. L'appelant réel injecte `asyncio.sleep`."""


# ── Réglages ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WarmupSettings:
    """
    Ce qui paramètre le pré-chauffage. **Aucun secret.**

    `admin_url` vise la gateway EN DIRECT : le secret admin ne doit pas
    traverser inutilement la façade et un proxy ancien/personnalisé peut porter
    un timeout de chargement insuffisant.

    `timeout_seconds` n'a pas de défaut : il vient de
    `derive_warmup_timeout_seconds()`, donc du registre.
    """
    admin_url: str
    model_id: str
    timeout_seconds: int
    poll_interval_s: float = 2.0
    ready_timeout_s: float = 20.0

    def __post_init__(self) -> None:
        if not isinstance(self.admin_url, str) or not self.admin_url.strip():
            raise WarmupError(f"admin_url doit être une URL non vide, reçu {self.admin_url!r}")
        if "@" in self.admin_url.split("//", 1)[-1].split("/", 1)[0]:
            raise WarmupError(
                "admin_url porte des identifiants dans l'URL — refusé : ils finiraient "
                "dans le rapport et dans les journaux"
            )
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise WarmupError(f"model_id doit être un identifiant non vide, reçu {self.model_id!r}")
        if not isinstance(self.timeout_seconds, int) or isinstance(self.timeout_seconds, bool):
            raise WarmupError(
                f"timeout_seconds doit être un entier, reçu {self.timeout_seconds!r} — "
                "utilisez derive_warmup_timeout_seconds()"
            )
        if self.timeout_seconds <= LOAD_TIMEOUT_GRACE_SECONDS:
            # Une borne inférieure ou égale à la seule grâce signifie que la
            # valeur n'a pas été dérivée du registre : elle a été posée à la main.
            raise WarmupError(
                f"timeout_seconds vaut {self.timeout_seconds} s, ce qui n'excède même pas la "
                f"grâce de {LOAD_TIMEOUT_GRACE_SECONDS} s : la borne n'a pas été dérivée du "
                "registre. Passez par derive_warmup_timeout_seconds()"
            )
        for champ in ("poll_interval_s", "ready_timeout_s"):
            valeur = getattr(self, champ)
            if not isinstance(valeur, (int, float)) or isinstance(valeur, bool) or valeur <= 0:
                raise WarmupError(f"{champ} doit être un délai > 0, reçu {valeur!r}")

    def public_view(self) -> dict[str, Any]:
        return {
            "admin_url": self.admin_url,
            "model": self.model_id,
            "timeout_seconds": self.timeout_seconds,
            "poll_interval_s": self.poll_interval_s,
        }


# ── Résultat ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WarmupStage:
    """Une étape du pré-chauffage, avec son verdict et sa cause stable."""
    name: str
    status: str
    code: str
    message: str

    def __post_init__(self) -> None:
        if self.status not in STAGE_STATUSES:
            raise WarmupError(f"statut d'étape inconnu : {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class ReleaseDecision:
    """
    Ce que l'orchestrateur a le droit de faire de la version qu'il déploie.

    Les deux drapeaux sont rendus séparément **exprès**. Tant que la recette
    n'est pas terminée avec succès, l'ancienne version doit rester en place :
    lire un seul booléen conduirait tôt ou tard à supprimer l'ancienne release
    parce que la nouvelle « n'a pas encore échoué ».
    """
    declare_new_version: bool
    retain_previous_version: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "declare_new_version": self.declare_new_version,
            "retain_previous_version": self.retain_previous_version,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WarmupOutcome:
    """
    Journal du pré-chauffage. Les verdicts sont DÉRIVÉS, jamais stockés.

    `traffic_authorized` est la seule question que l'orchestrateur pose, et sa
    réponse exige les TROIS conditions de §10 réunies : le modèle chargé, sa
    health réelle observée, et une génération courte réussie.
    """
    reason: str
    stages: tuple[WarmupStage, ...]
    model: str
    settings_view: dict[str, Any]
    model_loaded: bool = False
    serving_ready: bool = False
    probe: ProbeOutcome | None = None
    load_wait_ms: int = 0
    ready_wait_ms: int = 0
    observed_at: str = ""
    dry_run: bool = False

    @property
    def traffic_authorized(self) -> bool:
        """Fail-closed : toute condition absente refuse l'ouverture du trafic."""
        return (
            not self.dry_run
            and self.reason == REASON_OK
            and self.model_loaded
            and self.serving_ready
            and self.probe is not None
            and self.probe.served
        )

    def release_decision(self) -> ReleaseDecision:
        """
        Points 4 et 5 de §10, rendus explicites.

        Tant que le trafic n'est pas autorisé, la nouvelle version n'est PAS
        déclarée et l'ancienne est conservée. Après succès, la nouvelle est
        déclarée et l'ancienne peut être libérée par l'orchestrateur.
        """
        if self.traffic_authorized:
            return ReleaseDecision(
                declare_new_version=True,
                retain_previous_version=False,
                reason="pré-chauffage réussi : modèle chargé, health réelle observée, génération prouvée",
            )
        return ReleaseDecision(
            declare_new_version=False,
            retain_previous_version=True,
            reason=(
                f"pré-chauffage non concluant ({self.reason}) : l'ancienne version reste "
                "en place et la nouvelle n'est pas déclarée"
            ),
        )

    def findings(self) -> tuple[schema.Finding, ...]:
        constats: list[schema.Finding] = []
        for stage in self.stages:
            if stage.status == STAGE_FAIL:
                constats.append(schema.Finding(code=stage.code, level="fail", message=stage.message))
            elif stage.status == STAGE_WARN:
                constats.append(schema.Finding(code=stage.code, level="warn", message=stage.message))
        return tuple(constats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "traffic_authorized": self.traffic_authorized,
            "model": self.model,
            "settings": self.settings_view,
            "model_loaded": self.model_loaded,
            "serving_ready": self.serving_ready,
            "load_wait_ms": self.load_wait_ms,
            "ready_wait_ms": self.ready_wait_ms,
            "probe": self.probe.to_dict() if self.probe is not None else None,
            "release": self.release_decision().to_dict(),
            "stages": [s.to_dict() for s in self.stages],
            "observed_at": self.observed_at,
            "dry_run": self.dry_run,
        }


@dataclass
class _WarmupState:
    stages: list[WarmupStage] = field(default_factory=list)
    model_loaded: bool = False
    serving_ready: bool = False
    probe: ProbeOutcome | None = None
    load_wait_ms: int = 0
    ready_wait_ms: int = 0

    def add(self, name: str, status: str, code: str, message: str) -> None:
        self.stages.append(WarmupStage(name=name, status=status, code=code, message=message))


def _ms(seconds: float) -> int:
    return max(int(round(seconds * 1000)), 0)


def _admin_headers(admin_secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_secret}"}


async def _call(
    client: AdminClient,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float,
) -> HttpResponse:
    """Appel unitaire. Une exception de transport devient un statut 0, jamais 200."""
    try:
        brut = await client.request(method, url, headers=headers, timeout=timeout)
    except Exception as exc:
        return HttpResponse(status=0, body={"transport_error": ex.redact_for_log(str(exc))})
    statut = getattr(brut, "status", None)
    if not isinstance(statut, int) or isinstance(statut, bool):
        return HttpResponse(status=0, body={"transport_error": "réponse sans statut HTTP exploitable"})
    return HttpResponse(status=statut, body=getattr(brut, "body", None))


# ── Le pré-chauffage ──────────────────────────────────────────────────────────

async def run_warmup(
    *,
    settings: WarmupSettings,
    client: AdminClient,
    admin_secret: str,
    context: ex.ExecutionContext,
    generation_probe: GenerationProbe | None = None,
    sleep: AsyncSleep = _no_sleep,
) -> WarmupOutcome:
    """
    Charge le modèle par défaut, attend sa health réelle, prouve qu'il génère.

    Ne lève jamais sur un échec métier : toute panne devient une cause consignée
    et un refus d'ouvrir le trafic.
    """
    if context.dry_run:
        return _dry_run_outcome(settings, context, generation_probe is not None)

    if not isinstance(admin_secret, str) or not admin_secret:
        raise WarmupError(
            "un ADMIN_SECRET non vide est requis : le pré-chauffage passe par le plan "
            "de contrôle"
        )

    state = _WarmupState()
    entetes = _admin_headers(admin_secret)
    depart = context.monotonic()
    limite = depart + float(settings.timeout_seconds)

    # 1. Charger le modèle par défaut.
    charge = await _call(
        client, "POST", f"{settings.admin_url}/admin/models/{settings.model_id}/load",
        headers=entetes, timeout=float(settings.timeout_seconds),
    )
    state.load_wait_ms = _ms(context.monotonic() - depart)
    if charge.status != 200:
        detail = ""
        if charge.status == 504:
            detail = (" Un 504 signale probablement un proxy dont le timeout de "
                      "chargement est trop court : visez le plan de contrôle en direct.")
        state.add("model_load", STAGE_FAIL, REASON_LOAD_FAILED,
                  f"POST /admin/models/{settings.model_id}/load a répondu {charge.status} "
                  f"après {state.load_wait_ms} ms.{detail}")
        return _outcome(REASON_LOAD_FAILED, settings, context, state)
    state.model_loaded = True
    state.add("model_load", STAGE_PASS, "model_loaded",
              f"Chargement accepté en {state.load_wait_ms} ms "
              f"(borne dérivée : {settings.timeout_seconds} s).")

    # 2. Attendre la health RÉELLE, pas la fin de l'appel de chargement.
    cause = await _await_serving(settings=settings, client=client, entetes=entetes,
                                 context=context, sleep=sleep, state=state,
                                 depart=depart, limite=limite)
    if cause != REASON_OK:
        return _outcome(cause, settings, context, state)

    # 3. Génération courte. Absente, elle ne s'invente pas : le trafic reste fermé.
    if generation_probe is None:
        state.add("generation", STAGE_FAIL, REASON_NO_PROBE,
                  "Aucune sonde de génération n'a été fournie : le modèle est chargé et sa "
                  "health est réelle, mais rien ne prouve qu'un token sort. §10 exige une "
                  "génération courte avant d'ouvrir le trafic.")
        return _outcome(REASON_NO_PROBE, settings, context, state)

    try:
        sonde = await generation_probe()
    except Exception as exc:
        sonde = ProbeOutcome(
            served=False, reason=REASON_PROBE_FAILED,
            detail=ex.redact_for_log(f"{type(exc).__name__}: {exc}"),
        )
    if not isinstance(sonde, ProbeOutcome):
        sonde = ProbeOutcome(
            served=False, reason=REASON_PROBE_FAILED,
            detail=f"la sonde a rendu {type(sonde).__name__} au lieu d'un ProbeOutcome",
        )
    state.probe = sonde
    if not sonde.served:
        state.add("generation", STAGE_FAIL, REASON_PROBE_FAILED,
                  f"La génération courte n'a rien prouvé (cause : {sonde.reason}). "
                  f"{sonde.detail}".strip())
        return _outcome(REASON_PROBE_FAILED, settings, context, state)

    state.add("generation", STAGE_PASS, "generation_ok",
              f"Génération courte prouvée (TTFT : {sonde.ttft_ms} ms). Le premier utilisateur "
              "ne déclenchera pas le chargement.")
    context.journaliser(f"pré-chauffage de {settings.model_id} : trafic autorisé")
    return _outcome(REASON_OK, settings, context, state)


async def _await_serving(
    *,
    settings: WarmupSettings,
    client: AdminClient,
    entetes: Mapping[str, str],
    context: ex.ExecutionContext,
    sleep: AsyncSleep,
    state: _WarmupState,
    depart: float,
    limite: float,
) -> str:
    """
    Attend que `GET /ready` annonce le modèle dans `models_ready`. Bornée.

    C'est `readiness.py` qui décide : ce module lit `levels.serving` et
    `models_ready`, il ne refait aucun de ses contrôles. Le dépassement de la
    borne dérivée est un **échec explicite** — jamais une attente infinie, et
    jamais un repli silencieux sur « c'est sans doute bon ».
    """
    dernier_statut = 0
    while True:
        pret = await _call(client, "GET", f"{settings.admin_url}/ready",
                           headers=entetes, timeout=settings.ready_timeout_s)
        dernier_statut = pret.status
        corps = pret.body if isinstance(pret.body, dict) else {}
        niveaux = corps.get("levels") if isinstance(corps.get("levels"), dict) else {}
        charges = corps.get("models_ready")
        charges = charges if isinstance(charges, list) else []
        if pret.status == 200 and niveaux.get("serving") is True and settings.model_id in charges:
            state.ready_wait_ms = _ms(context.monotonic() - depart)
            state.serving_ready = True
            state.add("serving_readiness", STAGE_PASS, "serving_ready",
                      f"Health réelle observée après {state.ready_wait_ms} ms : "
                      f"{settings.model_id} figure dans models_ready.")
            return REASON_OK

        if context.monotonic() >= limite:
            state.ready_wait_ms = _ms(context.monotonic() - depart)
            if dernier_statut != 200:
                state.add("serving_readiness", STAGE_FAIL, REASON_READY_UNREACHABLE,
                          f"GET /ready a répondu {dernier_statut} jusqu'à l'expiration de la borne "
                          f"dérivée ({settings.timeout_seconds} s).")
                return REASON_READY_UNREACHABLE
            state.add("serving_readiness", STAGE_FAIL, REASON_WARMUP_TIMEOUT,
                      f"{settings.model_id} n'a pas atteint la serving readiness dans la borne "
                      f"dérivée du registre ({settings.timeout_seconds} s = load_timeout_seconds "
                      f"+ {LOAD_TIMEOUT_GRACE_SECONDS} s). Le trafic reste fermé.")
            return REASON_WARMUP_TIMEOUT

        await sleep(settings.poll_interval_s)


def _outcome(
    reason: str,
    settings: WarmupSettings,
    context: ex.ExecutionContext,
    state: _WarmupState,
) -> WarmupOutcome:
    resultat = WarmupOutcome(
        reason=reason,
        stages=tuple(state.stages),
        model=settings.model_id,
        settings_view=settings.public_view(),
        model_loaded=state.model_loaded,
        serving_ready=state.serving_ready,
        probe=state.probe,
        load_wait_ms=state.load_wait_ms,
        ready_wait_ms=state.ready_wait_ms,
        observed_at=context.now(),
        dry_run=False,
    )
    return _guard_no_leak(resultat)


# Ce que porte un champ dont la valeur a été retirée d'un résultat de repli.
REDACTED = "<expurgé>"


def _guard_no_leak(outcome: WarmupOutcome) -> WarmupOutcome:
    """
    Un résultat qui fuit n'est pas publié : il devient un refus explicite.

    Le résultat de repli ne recopie RIEN de l'original — la fuite peut se
    trouver dans n'importe lequel de ses champs, identifiant de modèle compris.
    Seuls les chemins fautifs sont cités ; `find_secret_leaks()` garantit qu'ils
    ne contiennent jamais la valeur.
    """
    fuites = schema.find_secret_leaks(outcome.to_dict())
    if not fuites:
        return outcome
    return WarmupOutcome(
        reason="report_leak",
        stages=(WarmupStage(
            name="report", status=STAGE_FAIL, code="report_leak",
            message=(
                "Le rapport de pré-chauffage exposait des valeurs sensibles et n'a pas été "
                "publié : " + " ; ".join(fuites)
            ),
        ),),
        model=REDACTED,
        settings_view={},
        model_loaded=outcome.model_loaded,
        serving_ready=False,
        probe=None,
        observed_at=outcome.observed_at,
        dry_run=False,
    )


def _dry_run_outcome(
    settings: WarmupSettings, context: ex.ExecutionContext, avec_sonde: bool
) -> WarmupOutcome:
    """
    Simulation : aucune requête, aucun chargement, aucun feu vert.

    `traffic_authorized` est faux par construction en simulation — une
    simulation ne peut pas autoriser l'ouverture du trafic, quel que soit son
    contenu.
    """
    etapes = [
        WarmupStage(
            name="model_load", status=STAGE_SKIP, code="would_exercise",
            message=(
                f"serait exercé : POST {settings.admin_url}/admin/models/{settings.model_id}/load "
                f"(borne dérivée : {settings.timeout_seconds} s)"
            ),
        ),
        WarmupStage(
            name="serving_readiness", status=STAGE_SKIP, code="would_exercise",
            message=(
                f"serait exercé : GET {settings.admin_url}/ready jusqu'à ce que "
                f"{settings.model_id} figure dans models_ready"
            ),
        ),
        WarmupStage(
            name="generation", status=STAGE_SKIP if avec_sonde else STAGE_WARN,
            code="would_exercise" if avec_sonde else REASON_NO_PROBE,
            message=(
                "serait exercé : une génération courte via la sonde fournie"
                if avec_sonde else
                "aucune sonde de génération n'est branchée : en application réelle, le trafic "
                "resterait fermé"
            ),
        ),
    ]
    return WarmupOutcome(
        reason=REASON_OK,
        stages=tuple(etapes),
        model=settings.model_id,
        settings_view=settings.public_view(),
        model_loaded=False,
        serving_ready=False,
        probe=None,
        observed_at=context.now(),
        dry_run=True,
    )


# ── Exécuteur et enregistrement ───────────────────────────────────────────────

def make_warmup_executor(
    *,
    settings: WarmupSettings,
    client: AdminClient,
    admin_secret: str,
    generation_probe: GenerationProbe | None = None,
    sleep: AsyncSleep = _no_sleep,
) -> ex.StepExecutor:
    """Fabrique l'exécuteur de `schema.ACTION_WARMUP_MODEL`."""

    async def executer(step: schema.PlanStep, context: ex.ExecutionContext) -> ex.StepResult:
        resultat = await run_warmup(
            settings=settings, client=client, admin_secret=admin_secret,
            context=context, generation_probe=generation_probe, sleep=sleep,
        )
        if context.dry_run:
            return ex.StepResult.for_step(
                step, status=ex.STEP_WOULD_APPLY,
                summary=(
                    f"pré-chauffage de {settings.model_id} simulé : aucune requête émise, "
                    f"borne dérivée de {settings.timeout_seconds} s, trafic non autorisé"
                ),
                evidence=resultat.to_dict(),
            )
        if resultat.traffic_authorized:
            return ex.StepResult.for_step(
                step, status=ex.STEP_DONE,
                summary=(
                    f"{settings.model_id} pré-chauffé et prouvé : chargement en "
                    f"{resultat.load_wait_ms} ms, health réelle à {resultat.ready_wait_ms} ms — "
                    "le premier utilisateur ne déclenchera pas le chargement"
                ),
                duration_ms=max(resultat.ready_wait_ms, resultat.load_wait_ms),
                evidence=resultat.to_dict(),
                findings=resultat.findings(),
            )
        return ex.StepResult.for_step(
            step, status=ex.STEP_FAILED,
            summary=(
                f"pré-chauffage de {settings.model_id} non concluant : le trafic reste fermé "
                "et l'ancienne version est conservée"
            ),
            duration_ms=max(resultat.ready_wait_ms, resultat.load_wait_ms),
            evidence=resultat.to_dict(),
            findings=resultat.findings(),
            error=f"cause : {resultat.reason}",
        )

    return executer


def register_executors(
    registry: ex.ExecutorRegistry,
    *,
    settings: WarmupSettings,
    client: AdminClient,
    admin_secret: str,
    generation_probe: GenerationProbe | None = None,
    sleep: AsyncSleep = _no_sleep,
) -> None:
    """Branche l'exécuteur de `ACTION_WARMUP_MODEL` dans un registre."""
    registry.register(
        schema.ACTION_WARMUP_MODEL,
        make_warmup_executor(
            settings=settings, client=client, admin_secret=admin_secret,
            generation_probe=generation_probe, sleep=sleep,
        ),
    )


__all__ = [
    "AdminClient",
    "GenerationProbe",
    "HttpResponse",
    "LOAD_TIMEOUT_GRACE_SECONDS",
    "ProbeOutcome",
    "ReleaseDecision",
    "WarmupError",
    "WarmupOutcome",
    "WarmupSettings",
    "WarmupStage",
    "derive_warmup_timeout_seconds",
    "make_warmup_executor",
    "register_executors",
    "run_warmup",
]
