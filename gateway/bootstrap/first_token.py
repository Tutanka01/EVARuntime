"""
AUT-009 — recette automatique du premier token (jalon M2).

Ce que ce module prouve
-----------------------
Qu'une installation **sert**, et pas seulement qu'elle **répond**. La distinction
porte le nom du jalon : le chemin
public complet doit être traversé de bout en bout —

```text
client → TLS/nginx si configuré → authentification gateway → quota/rate limit
       → résolution du modèle → llama-server → chunk SSE AVEC du contenu
       → log d'usage
```

« un simple `/health` ou `/ready` ne suffit pas ». Un flux qui s'ouvre en 200,
émet un chunk de rôle et se termine proprement sur `[DONE]` **sans jamais avoir
produit de contenu** est un ÉCHEC : c'est le défaut COR-006, reproduit puis
corrigé par le projet, et rejoué avec succès lors du premier déploiement réel
(§0.10) où une régression `generation:no_content` volontairement injectée a bien
déclenché le rollback.

Séquence appliquée, dans l'ordre littéral de §10
-------------------------------------------------
1. liveness (`GET /health` sur le chemin **public**) ;
2. readiness structurelle (`GET /ready` sur le plan de **contrôle**) ;
3. création d'un utilisateur et d'une clé éphémères ;
4. chargement explicite du modèle ;
5. `POST /v1/chat/completions` avec `stream: true` ;
6. temps jusqu'aux en-têtes ;
7. temps jusqu'au premier delta **utile** ;
8. attente de `[DONE]` ;
9. contrôle de l'enveloppe, du modèle et de l'usage ;
10. contrôle de l'écriture du log d'usage ;
11. révocation de la clé et **anonymisation** de l'utilisateur ;
12. rapport sans clé ni contenu sensible.

Ce que ce module n'est pas
--------------------------
Il n'ouvre aucune socket lui-même : le client HTTP, l'horloge, le sommeil et le
tirage d'identité sont **injectés**. Il n'importe que la bibliothèque standard,
`bootstrap.schema` et `bootstrap.execution` — ni `config`, ni `readiness`, ni
`model_registry`. C'est ce qui le rend exécutable hors d'un hôte gateway et
testable sans lancer un serveur.

Conséquence assumée : la sémantique de `GET /ready` est consommée **par contrat
de sortie** (`level`, `levels.liveness/structural/serving`, `reason` — cf.
`readiness.ReadinessReport.public_body()`), pas par import. Ce module ne
réimplémente aucun contrôle de `readiness`, il en lit le verdict. Le couplage de
vocabulaire est verrouillé côté tests, qui eux importent `readiness` et
comparent les constantes : un renommage de niveau casse la suite au lieu de
rendre ce module silencieusement aveugle.

Sur l'anonymisation (DEC-001)
-----------------------------
`DELETE /admin/users/{username}` n'efface pas la ligne : il **anonymise**, coupe
le compte et révoque les clés, en conservant l'historique d'usage sous
pseudonyme. La recette en tient compte — elle attend `200` avec
`status ∈ {anonymized, already_anonymized}`, jamais un `204` destructeur — et
elle nettoie **même quand la recette échoue en cours de route**. Une identité de
smoke test résiduelle est une porte d'entrée, pas un détail cosmétique.

Sur les secrets
---------------
Le `ADMIN_SECRET` et la clé éphémère ne sont jamais rangés dans un attribut
sérialisable : ils transitent par un paramètre local et un en-tête. Avant de
rendre quoi que ce soit, la preuve et les indices sont repassés à
`schema.find_secret_leaks()` ; une fuite détectée **échoue** l'étape au lieu de
la publier. Le prompt et le texte généré ne sont jamais recopiés : seuls des
compteurs (`content_chunks`, `content_chars`) le sont.

Les compteurs s'appellent `*_tokens`, et c'est une correction
-------------------------------------------------------------
Ils se sont appelés `prompt_units` / `completion_units` / `generation_limit`
pendant une livraison, parce que `schema._SECRET_KEY_RE` frappait alors en
sous-chaîne **tout** nom contenant `token` : un `completion_tokens` suffisait à
rendre le rapport d'exécution entier impubliable. Le motif est désormais ancré
sur les frontières de composant (`(^|_)TOKEN(_|$)`), un compte au pluriel passe
et un porteur d'authentification au singulier reste retenu. Le contournement est
donc retiré : les compteurs portent les noms de l'API OpenAI, qui sont ceux que
l'exploitant lit dans `usage_log`.

La preuve nomme comme le consommateur
-------------------------------------
`proof()` est le SEUL document que ce module publie pour un tiers, et son
consommateur unique est `registry_writer.SmokeTestProof` (AUT-007). Ses clés
sont donc celles que ce contrat exige — `model_id`, `http_status`,
`completion_tokens`, `measured_at`, `endpoint` — et non le vocabulaire interne
de `StreamOutcome`. Faire porter la traduction par l'applicateur aurait créé un
troisième endroit où lire une preuve, donc un endroit de plus où être indulgent.
"""
from __future__ import annotations

import json
import secrets as _secrets
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol

from . import execution as ex
from . import schema

# ── Codes de cause ────────────────────────────────────────────────────────────
# Volontairement IDENTIQUES à ceux de `gateway/deploy/smoke_test.sh` : les deux
# recettes doivent produire le même mot pour la même panne, sinon un opérateur
# qui passe de l'une à l'autre pendant un incident doit traduire.

REASON_OK = "ok"
REASON_HTTP_STATUS = "http_status"
REASON_UPSTREAM_ERROR = "upstream_error"
REASON_NO_SSE_DATA = "no_sse_data"
REASON_NO_CONTENT = "no_content"
REASON_NO_DONE = "no_done"
REASON_MODEL_MISMATCH = "model_mismatch"
REASON_BAD_ENVELOPE = "bad_envelope"
REASON_NO_USAGE = "no_usage"
REASON_STREAM_TIMEOUT = "stream_timeout"
REASON_TRANSPORT = "transport_error"

STREAM_REASONS: frozenset[str] = frozenset({
    REASON_OK, REASON_HTTP_STATUS, REASON_UPSTREAM_ERROR, REASON_NO_SSE_DATA,
    REASON_NO_CONTENT, REASON_NO_DONE, REASON_MODEL_MISMATCH, REASON_BAD_ENVELOPE,
    REASON_NO_USAGE, REASON_STREAM_TIMEOUT, REASON_TRANSPORT,
})

# Causes hors flux, propres à la recette.
REASON_LIVENESS = "liveness_failed"
REASON_STRUCTURAL = "structural_readiness_failed"
REASON_MODEL_UNRESOLVED = "model_unresolved"
REASON_IDENTITY_CREATE = "identity_create_failed"
REASON_IDENTITY_KEY = "identity_key_failed"
REASON_MODEL_LOAD = "model_load_failed"
REASON_NO_USAGE_LOG = "usage_log_missing"
REASON_LEAK = "report_leak"

# ── Niveaux de readiness (§10, « Readiness à trois niveaux ») ─────────────────
# Recopiés du contrat de sortie de `readiness.ReadinessReport.public_body()`.
# Le test de ce module importe `readiness` et vérifie l'égalité : ces trois
# chaînes ne peuvent pas diverger en silence.

LEVEL_NONE = "none"
LEVEL_STRUCTURAL = "structural"
LEVEL_SERVING = "serving"

READINESS_LEVELS: tuple[str, ...] = (LEVEL_NONE, LEVEL_STRUCTURAL, LEVEL_SERVING)

# ── Preuve de recette ─────────────────────────────────────────────────────────

PROOF_KIND = "eva.first_token.proof"
PROOF_VERSION = 1

# Le chemin PUBLIC que la recette exerce, et le seul. Publié dans la preuve
# parce que le consommateur (AUT-007) refuse une recette qui n'aurait pas
# traversé `/v1/` : une génération obtenue en interrogeant `llama-server` en
# direct prouve que le modèle charge, pas que la chaîne nginx → gateway →
# llama-server sert (§10). Le chemin est une CONSTANTE et non un réglage : s'il
# devenait paramétrable, la preuve pourrait attester d'un chemin privé.
GENERATION_PATH = "/v1/chat/completions"

# Le SEUL verdict qui vaut feu vert. Toute autre valeur — y compris une valeur
# inconnue d'une version future — refuse, par construction (`==`, jamais `!=`).
PROOF_SERVED = "served"
PROOF_NOT_SERVED = "not_served"

# ── Détection d'un SSE bufferisé ──────────────────────────────────────────────
# `proxy_buffering on` côté nginx laisse passer les en-têtes tout de suite puis
# retient le CORPS : la signature est « premier delta utile tardif, puis tous les
# deltas d'un coup ». Ces deux seuils bornent cette signature.
#
# Limite assumée, écrite ici parce qu'elle sera lue : un backend réellement
# rapide qui traite un long prompt puis crache sa réponse en une rafale produit
# EXACTEMENT la même signature. Le constat est donc un `warn`, jamais un `fail`,
# et il ne fait pas basculer le verdict. Depuis le client seul, les deux cas ne
# sont pas distinguables ; seule une inspection de la configuration nginx
# (`doctor`) tranche.

BUFFERING_GAP_TOLERANCE_MS = 0
BUFFERING_MIN_FIRST_DELTA_MS = 250

# Ce que porte un champ dont la valeur a été retirée d'un rapport de repli.
REDACTED = "<expurgé>"

# ── Réglages ──────────────────────────────────────────────────────────────────

# Préfixe de l'identité éphémère. Le nom est TOUJOURS généré : la recette
# n'accepte pas de nom fourni par l'opérateur, ce qui garantit qu'aucun nom
# d'utilisateur réel ne peut atterrir dans un rapport ni dans un journal.
IDENTITY_PREFIX = "smoke-test"

IDENTITY_NOTES = (
    "Identité éphémère de recette du premier token (AUT-009), anonymisée en fin d'exécution."
)


class FirstTokenError(ex.ExecutionError):
    """La recette ne peut pas être construite ou ses réglages sont inexploitables."""


@dataclass(frozen=True)
class FirstTokenSettings:
    """
    Ce qui paramètre la recette. **Aucun secret**, par construction.

    `base_url` est le chemin PUBLIC : le viser sur nginx (`https://…`) est la
    seule façon de couvrir TLS, `limit_req` et surtout `proxy_buffering off`.
    `admin_url` est le plan de CONTRÔLE et vise la gateway en direct : le secret
    admin ne doit pas traverser inutilement la façade et un proxy
    ancien/personnalisé peut encore porter un timeout de chargement insuffisant.

    `model_id` à `None` fait dériver le modèle du plus petit modèle ACTIVÉ
    annoncé par `GET /admin/models` : une recette ne doit pas mobiliser 42 Go de
    VRAM pour prouver qu'un token sort.
    """
    base_url: str
    admin_url: str
    model_id: str | None = None
    prompt: str = "Réponds uniquement par le mot: ok"
    max_tokens: int = 16
    ttft_threshold_ms: int = 0        # 0 = seuil désactivé
    fail_on_ttft: bool = False
    ready_timeout_s: float = 20.0
    admin_timeout_s: float = 30.0
    load_timeout_s: float = 310.0
    stream_timeout_s: float = 120.0
    usage_timeout_s: float = 15.0
    usage_poll_interval_s: float = 1.0

    def __post_init__(self) -> None:
        for champ in ("base_url", "admin_url"):
            valeur = getattr(self, champ)
            if not isinstance(valeur, str) or not valeur.strip():
                raise FirstTokenError(f"{champ} doit être une URL non vide, reçu {valeur!r}")
            if "@" in valeur.split("//", 1)[-1].split("/", 1)[0]:
                # `https://user:pass@hôte` ferait fuiter des identifiants dans
                # chaque ligne de rapport. Refusé à la construction.
                raise FirstTokenError(
                    f"{champ} porte des identifiants dans l'URL — refusé : ils finiraient "
                    "dans le rapport et dans les journaux"
                )
        if not isinstance(self.max_tokens, int) or isinstance(self.max_tokens, bool) or self.max_tokens < 1:
            raise FirstTokenError(f"max_tokens doit être un entier >= 1, reçu {self.max_tokens!r}")
        if self.ttft_threshold_ms < 0:
            raise FirstTokenError(f"ttft_threshold_ms doit être >= 0, reçu {self.ttft_threshold_ms!r}")
        if self.fail_on_ttft and self.ttft_threshold_ms <= 0:
            raise FirstTokenError(
                "fail_on_ttft exige un ttft_threshold_ms > 0 : échouer sur un seuil "
                "désactivé rendrait la recette rouge sans raison exploitable"
            )
        for champ in (
            "ready_timeout_s", "admin_timeout_s", "load_timeout_s",
            "stream_timeout_s", "usage_timeout_s", "usage_poll_interval_s",
        ):
            valeur = getattr(self, champ)
            if not isinstance(valeur, (int, float)) or isinstance(valeur, bool) or valeur <= 0:
                raise FirstTokenError(f"{champ} doit être un délai > 0, reçu {valeur!r}")

    def public_view(self) -> dict[str, Any]:
        """Vue publiable des réglages. Ne contient ni secret ni prompt."""
        return {
            "base_url": self.base_url,
            "admin_url": self.admin_url,
            "model": self.model_id or "<dérivé du plus petit modèle activé>",
            "max_tokens": self.max_tokens,
            "ttft_threshold_ms": self.ttft_threshold_ms,
            "fail_on_ttft": self.fail_on_ttft,
        }


# ── Contrat du client HTTP ────────────────────────────────────────────────────

@dataclass(frozen=True)
class HttpResponse:
    """Réponse unitaire. `body` est le JSON décodé, ou `None` s'il ne l'est pas."""
    status: int
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)


class StreamedResponse(Protocol):
    """Réponse streamée : le statut et les en-têtes sont lisibles AVANT le corps."""

    @property
    def status(self) -> int:
        ...

    @property
    def headers(self) -> Mapping[str, str]:
        ...

    def aiter_lines(self) -> AsyncIterator[str]:
        ...


class HttpClient(Protocol):
    """
    Ce que la recette exige d'un transport. Volontairement minimal.

    `stream()` rend un gestionnaire de contexte **asynchrone** — même forme que
    `httpx.AsyncClient.stream` — parce que c'est ce qui permet d'horodater
    l'arrivée des en-têtes séparément de celle du premier octet de corps. Sans
    cette séparation, la mesure de §10 points 6 et 7 serait la même mesure.
    """

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> HttpResponse:
        ...

    def stream(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> AbstractAsyncContextManager[StreamedResponse]:
        ...


AsyncSleep = Callable[[float], Awaitable[None]]


async def _no_sleep(_seconds: float) -> None:
    """Sommeil par défaut : aucun. L'appelant réel injecte `asyncio.sleep`."""


def _default_identity_suffix() -> str:
    return _secrets.token_hex(4)


# ── Analyse du flux SSE ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class StreamOutcome:
    """
    Verdict fonctionnel sur un flux `/v1/chat/completions`.

    `headers_ms` et `ttft_ms` sont DEUX mesures distinctes, et c'est le point de
    §10 : `headers_ms` est le temps jusqu'aux en-têtes, `ttft_ms` le temps
    jusqu'au premier delta portant réellement du contenu. Le premier octet
    arrive typiquement avec un chunk de rôle (`{"delta": {"role": "assistant"}}`)
    qui ne prouve rien — un backend peut ouvrir un 200, émettre ce chunk, puis
    ne jamais générer.
    """
    reason: str
    http_code: int
    headers_ms: int = -1
    ttft_ms: int = -1
    total_ms: int = -1
    content_chunks: int = 0
    content_chars: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    stream_model: str = ""
    saw_done: bool = False
    max_inter_delta_gap_ms: int = -1
    buffering_suspected: bool = False

    @property
    def served(self) -> bool:
        return self.reason == REASON_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "http_code": self.http_code,
            "headers_ms": self.headers_ms,
            "ttft_ms": self.ttft_ms,
            "total_ms": self.total_ms,
            "content_chunks": self.content_chunks,
            "content_chars": self.content_chars,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "stream_model": self.stream_model,
            "saw_done": self.saw_done,
            "max_inter_delta_gap_ms": self.max_inter_delta_gap_ms,
            "buffering_suspected": self.buffering_suspected,
        }


def _ms(seconds: float) -> int:
    return max(int(round(seconds * 1000)), 0)


@dataclass
class _StreamTally:
    """État mutable du dépouillement d'un flux. Interne, jamais publié tel quel."""
    saw_data: bool = False
    saw_done: bool = False
    content_chunks: int = 0
    content_chars: int = 0
    bad_envelope: int = 0
    upstream_error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    models: set = field(default_factory=set)
    t_first_content: float | None = None
    t_last_content: float | None = None
    max_gap: float = 0.0


def _consume_chunk(chunk: Any, tally: _StreamTally, now: float) -> None:
    """Dépouille un objet SSE décodé. Ne recopie jamais le contenu généré."""
    if not isinstance(chunk, dict):
        tally.bad_envelope += 1
        return
    # La gateway convertit une panne upstream en chunk SSE d'erreur suivi de
    # `[DONE]` : un flux « propre » peut donc masquer un 502/504 (COR-013).
    if isinstance(chunk.get("error"), dict):
        tally.upstream_error = str(chunk["error"].get("type") or "server_error")
        return
    if chunk.get("object") != "chat.completion.chunk" or not chunk.get("id"):
        tally.bad_envelope += 1
    if chunk.get("model"):
        tally.models.add(str(chunk["model"]))
    usage = chunk.get("usage")
    if isinstance(usage, dict):
        tally.prompt_tokens = _positive_int(usage.get("prompt_tokens"), tally.prompt_tokens)
        tally.completion_tokens = _positive_int(usage.get("completion_tokens"), tally.completion_tokens)
    for choice in chunk.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        # `!= ""` et non `.strip()` : un token composé d'espaces EST du contenu
        # généré. Seul un delta vide ou absent ne prouve rien.
        if isinstance(content, str) and content != "":
            tally.content_chunks += 1
            tally.content_chars += len(content)
            if tally.t_first_content is None:
                tally.t_first_content = now
            elif tally.t_last_content is not None:
                tally.max_gap = max(tally.max_gap, now - tally.t_last_content)
            tally.t_last_content = now


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _stream_reason(tally: _StreamTally, http_code: int, expected_model: str) -> str:
    """
    Ordre de priorité repris littéralement de `deploy/smoke_test.sh`.

    L'ordre est le contenu : `no_content` DOIT passer avant `no_done`, sinon un
    flux vide mais bien terminé serait rapporté comme un problème de terminaison
    au lieu du défaut COR-006 qu'il est.
    """
    if http_code != 200:
        return REASON_HTTP_STATUS
    if tally.upstream_error is not None:
        return REASON_UPSTREAM_ERROR
    if not tally.saw_data:
        return REASON_NO_SSE_DATA
    if tally.content_chunks == 0:
        return REASON_NO_CONTENT
    if not tally.saw_done:
        return REASON_NO_DONE
    if expected_model and any(m != expected_model for m in tally.models):
        return REASON_MODEL_MISMATCH
    if tally.bad_envelope:
        return REASON_BAD_ENVELOPE
    if tally.prompt_tokens <= 0 or tally.completion_tokens <= 0:
        return REASON_NO_USAGE
    return REASON_OK


async def analyse_sse_stream(
    lines: AsyncIterator[str],
    *,
    http_code: int,
    expected_model: str,
    t_start: float,
    t_headers: float,
    monotonic: Callable[[], float],
    deadline: float,
) -> StreamOutcome:
    """
    Dépouille un flux SSE et rend un verdict fonctionnel. N'attend jamais en réel.

    `deadline` est exprimée dans la base de temps de `monotonic` et contrôlée
    **après chaque ligne** : un flux qui ne se termine jamais mais continue
    d'émettre est borné ici, et rapporté `stream_timeout`. Un pair qui cesse
    complètement d'émettre entre deux lignes n'est PAS borné par ce module — il
    l'est par le `timeout` passé au transport, exactement comme `--max-time` le
    fait pour curl dans `deploy/smoke_test.sh`. Cette responsabilité partagée
    est délibérée : ce module n'a pas de socket à fermer.
    """
    tally = _StreamTally()
    timed_out = False

    async for raw in lines:
        now = monotonic()
        if now >= deadline:
            timed_out = True
            break
        line = raw.rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            tally.saw_done = True
            continue
        tally.saw_data = True
        try:
            chunk = json.loads(payload)
        except ValueError:
            tally.bad_envelope += 1
            continue
        _consume_chunk(chunk, tally, now)

    t_end = monotonic()
    reason = REASON_STREAM_TIMEOUT if timed_out else _stream_reason(tally, http_code, expected_model)

    headers_ms = _ms(t_headers - t_start)
    total_ms = _ms(t_end - t_start)
    ttft_ms = _ms(tally.t_first_content - t_start) if tally.t_first_content is not None else -1
    gap_ms = _ms(tally.max_gap) if tally.content_chunks > 1 else -1

    buffering = (
        tally.content_chunks > 1
        and gap_ms <= BUFFERING_GAP_TOLERANCE_MS
        and ttft_ms >= 0
        and (ttft_ms - headers_ms) >= BUFFERING_MIN_FIRST_DELTA_MS
    )

    return StreamOutcome(
        reason=reason,
        http_code=http_code,
        headers_ms=headers_ms,
        ttft_ms=ttft_ms,
        total_ms=total_ms,
        content_chunks=tally.content_chunks,
        content_chars=tally.content_chars,
        prompt_tokens=tally.prompt_tokens,
        completion_tokens=tally.completion_tokens,
        stream_model=sorted(tally.models)[0] if tally.models else "",
        saw_done=tally.saw_done,
        max_inter_delta_gap_ms=gap_ms,
        buffering_suspected=bool(buffering),
    )


# ── Étapes et rapport de recette ──────────────────────────────────────────────

STAGE_PASS = "pass"
STAGE_FAIL = "fail"
STAGE_SKIP = "skip"
STAGE_WARN = "warn"

STAGE_STATUSES: frozenset[str] = frozenset({STAGE_PASS, STAGE_FAIL, STAGE_SKIP, STAGE_WARN})


@dataclass(frozen=True)
class RecipeStage:
    """Une étape de la séquence de §10, avec son verdict et sa cause stable."""
    name: str
    status: str
    code: str
    message: str

    def __post_init__(self) -> None:
        if self.status not in STAGE_STATUSES:
            raise FirstTokenError(f"statut d'étape inconnu : {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class RecipeReport:
    """
    Journal complet d'une recette. Le verdict est DÉRIVÉ, jamais stocké.

    Même leçon que `execution.ExecutionReport` : un champ récapitulatif que
    personne ne recoupe finit par mentir. `served` se recalcule depuis la cause
    et les étapes à chaque appel.
    """
    reason: str
    stages: tuple[RecipeStage, ...]
    model: str
    settings_view: dict[str, Any]
    stream: StreamOutcome | None = None
    usage_entries: int = 0
    identity_created: bool = False
    identity_cleaned: bool = False
    readiness_levels: dict[str, bool] = field(default_factory=dict)
    observed_at: str = ""
    dry_run: bool = False

    @property
    def served(self) -> bool:
        """
        Vrai seulement si un token a traversé le chemin public ET a été facturé.

        Le log d'usage fait partie de la condition, pas du décor : une
        génération qui n'est pas comptabilisée est un défaut de facturation
        silencieux, et l'installation ne doit pas être déclarée bonne pour la
        production sur cette base.
        """
        return (
            not self.dry_run
            and self.reason == REASON_OK
            and self.stream is not None
            and self.stream.served
            and self.usage_entries > 0
        )

    def failed_stages(self) -> tuple[RecipeStage, ...]:
        return tuple(s for s in self.stages if s.status == STAGE_FAIL)

    def findings(self) -> tuple[schema.Finding, ...]:
        """Constats destinés au `StepResult`. Un `skip` ne produit rien."""
        constats: list[schema.Finding] = []
        for stage in self.stages:
            if stage.status == STAGE_FAIL:
                constats.append(schema.Finding(code=stage.code, level="fail", message=stage.message))
            elif stage.status == STAGE_WARN:
                constats.append(schema.Finding(code=stage.code, level="warn", message=stage.message))
        return tuple(constats)

    def proof(self) -> dict[str, Any]:
        """
        **Preuve de recette** — le document qu'un autre chantier consomme.

        Sa forme est figée par `PROOF_KIND`/`PROOF_VERSION`. Un consommateur
        n'autorise l'activation d'un modèle que si `proof_authorizes()` le dit ;
        il ne doit jamais réécrire cette règle chez lui.

        Les noms sont ceux du contrat consommateur (`registry_writer`) : `model_id`,
        `http_status`, `measured_at`, `completion_tokens`, `endpoint`. Traduire
        chez le consommateur aurait supposé une couche intermédiaire capable
        d'inventer un champ absent ; nommer juste ici supprime la couche.
        """
        stream = self.stream.to_dict() if self.stream is not None else None
        return {
            "kind": PROOF_KIND,
            "version": PROOF_VERSION,
            "verdict": PROOF_SERVED if self.served else PROOF_NOT_SERVED,
            "reason": self.reason,
            "model_id": self.model,
            "base_url": self.settings_view.get("base_url", ""),
            "endpoint": GENERATION_PATH,
            "measured_at": self.observed_at,
            "dry_run": self.dry_run,
            "readiness": {
                "liveness": bool(self.readiness_levels.get("liveness")),
                "structural": bool(self.readiness_levels.get("structural")),
                "serving": bool(self.readiness_levels.get("serving")),
            },
            "http_status": stream["http_code"] if stream else -1,
            "headers_ms": stream["headers_ms"] if stream else -1,
            "ttft_ms": stream["ttft_ms"] if stream else -1,
            "total_ms": stream["total_ms"] if stream else -1,
            "content_chunks": stream["content_chunks"] if stream else 0,
            "content_chars": stream["content_chars"] if stream else 0,
            "prompt_tokens": stream["prompt_tokens"] if stream else 0,
            "completion_tokens": stream["completion_tokens"] if stream else 0,
            "sse_buffering_suspected": bool(stream["buffering_suspected"]) if stream else False,
            "usage_entries": self.usage_entries,
            "usage_logged": self.usage_entries > 0,
            "identity_created": self.identity_created,
            "identity_cleaned": self.identity_cleaned,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "served": self.served,
            "model": self.model,
            "settings": self.settings_view,
            "stages": [s.to_dict() for s in self.stages],
            "stream": self.stream.to_dict() if self.stream is not None else None,
            "proof": self.proof(),
        }


def proof_authorizes(proof: Any, model_id: str) -> bool:
    """
    Seule règle admise pour transformer une preuve en feu vert. Fail-closed.

    Un consommateur (activation d'un modèle, ouverture du trafic) appelle CECI
    et rien d'autre. Toutes les conditions sont exprimées en égalité positive :
    un document tronqué, d'une autre version, d'un autre modèle, issu d'une
    simulation, ou dont le verdict est inconnu, n'autorise rien.
    """
    if not isinstance(proof, dict):
        return False
    if proof.get("kind") != PROOF_KIND:
        return False
    if proof.get("version") != PROOF_VERSION:
        return False
    if proof.get("verdict") != PROOF_SERVED:
        return False
    if proof.get("dry_run") is not False:
        return False
    if not isinstance(model_id, str) or not model_id:
        return False
    if proof.get("model_id") != model_id:
        return False
    if proof.get("usage_logged") is not True:
        return False
    return proof.get("reason") == REASON_OK


# ── La recette ────────────────────────────────────────────────────────────────

def _admin_headers(admin_secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_secret}"}


def _smallest_enabled_model(models: Any) -> str | None:
    """
    Le plus petit modèle ACTIVÉ. Même dérivation que `deploy/smoke_test.sh`.

    Départagé par identifiant pour que deux exécutions consécutives choisissent
    le même modèle : une recette qui change de cible d'un run à l'autre ne
    permet pas de comparer deux TTFT.
    """
    if not isinstance(models, list):
        return None
    actifs = [m for m in models if isinstance(m, dict) and m.get("enabled") and m.get("id")]
    if not actifs:
        return None

    def taille(entry: dict) -> float:
        try:
            return float(entry.get("vram_gb") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    actifs.sort(key=lambda m: (taille(m), str(m["id"])))
    return str(actifs[0]["id"])


@dataclass
class _RecipeState:
    """Accumulateur interne de la recette. Ne sort jamais du module tel quel."""
    stages: list[RecipeStage] = field(default_factory=list)
    model: str = ""
    username: str = ""
    key_prefix: str = ""
    identity_created: bool = False
    identity_cleaned: bool = False
    usage_entries: int = 0
    levels: dict[str, bool] = field(default_factory=lambda: {
        "liveness": False, "structural": False, "serving": False,
    })
    stream: StreamOutcome | None = None

    def add(self, name: str, status: str, code: str, message: str) -> None:
        self.stages.append(RecipeStage(name=name, status=status, code=code, message=message))


async def run_first_token_recipe(
    *,
    settings: FirstTokenSettings,
    client: HttpClient,
    admin_secret: str,
    context: ex.ExecutionContext,
    sleep: AsyncSleep = _no_sleep,
    identity_suffix: Callable[[], str] = _default_identity_suffix,
) -> RecipeReport:
    """
    Exécute la séquence de §10 et rend son journal. Ne lève pas sur un échec métier.

    Le nettoyage de l'identité éphémère est dans un `finally` : il a lieu que la
    recette réussisse, échoue à mi-parcours, ou lève. Une identité de smoke test
    qui survit à un échec est une porte d'entrée résiduelle.
    """
    if context.dry_run:
        return _dry_run_report(settings, context)

    if not isinstance(admin_secret, str) or not admin_secret:
        raise FirstTokenError(
            "un ADMIN_SECRET non vide est requis : la recette crée et retire une "
            "identité éphémère, elle ne peut pas s'en passer"
        )

    state = _RecipeState()
    reason = REASON_OK
    try:
        reason = await _run_stages(
            settings=settings, client=client, admin_secret=admin_secret,
            context=context, sleep=sleep, identity_suffix=identity_suffix, state=state,
        )
    finally:
        await _cleanup_identity(
            settings=settings, client=client, admin_secret=admin_secret,
            context=context, state=state,
        )

    if reason == REASON_OK and not state.identity_cleaned:
        # Le service SERT, mais une identité résiduelle reste une porte ouverte :
        # la recette n'est pas réussie tant qu'elle n'a pas refermé derrière elle.
        reason = "identity_residual"

    report = RecipeReport(
        reason=reason,
        stages=tuple(state.stages),
        model=state.model,
        settings_view=settings.public_view(),
        stream=state.stream,
        usage_entries=state.usage_entries,
        identity_created=state.identity_created,
        identity_cleaned=state.identity_cleaned,
        readiness_levels=dict(state.levels),
        observed_at=context.now(),
        dry_run=False,
    )
    return _guard_no_leak(report)


def _guard_no_leak(report: RecipeReport) -> RecipeReport:
    """
    Dernier filet : un rapport qui fuit n'est pas publié, il échoue.

    Le contrôle porte sur le document RENDU (`to_dict()`, preuve incluse), pas
    sur les objets internes : c'est le document qui sort. Fail-closed — la
    recette bascule en échec plutôt que de publier « avec un avertissement ».
    """
    fuites = schema.find_secret_leaks(report.to_dict())
    if not fuites:
        return report
    constat = schema.Finding(
        code=REASON_LEAK,
        level="fail",
        message=(
            "Le rapport de recette exposait des valeurs sensibles et n'a pas été publié : "
            + " ; ".join(fuites)
        ),
    )
    # Le rapport de repli ne recopie RIEN de l'original : la fuite peut se
    # trouver dans n'importe lequel de ses champs, y compris l'identifiant de
    # modèle ou l'URL. Seuls les chemins fautifs — jamais les valeurs — sont
    # cités, `find_secret_leaks()` s'en portant garant.
    return RecipeReport(
        reason=REASON_LEAK,
        stages=(RecipeStage(
            name="report", status=STAGE_FAIL, code=REASON_LEAK, message=constat.message,
        ),),
        model=REDACTED,
        settings_view={},
        stream=None,
        usage_entries=0,
        identity_created=report.identity_created,
        identity_cleaned=report.identity_cleaned,
        readiness_levels=dict(report.readiness_levels),
        observed_at=report.observed_at,
        dry_run=False,
    )


def _dry_run_report(settings: FirstTokenSettings, context: ex.ExecutionContext) -> RecipeReport:
    """
    Simulation : aucune requête, aucune identité créée, aucun modèle chargé.

    Le rapport dit ce qui SERAIT exercé. Il ne porte volontairement aucune
    preuve exploitable : `proof_authorizes()` refuse un document `dry_run`,
    de sorte qu'une simulation ne puisse jamais autoriser l'activation d'un
    modèle par inadvertance.
    """
    etapes = (
        ("liveness", f"GET {settings.base_url}/health"),
        ("structural_readiness", f"GET {settings.admin_url}/ready"),
        ("model_resolution", f"GET {settings.admin_url}/admin/models"),
        ("identity", f"POST {settings.admin_url}/admin/users puis .../keys"),
        ("model_load", f"POST {settings.admin_url}/admin/models/<modèle>/load"),
        ("generation", f"POST {settings.base_url}{GENERATION_PATH} (stream: true)"),
        ("usage_log", f"GET {settings.admin_url}/admin/usage"),
        ("identity_cleanup", f"DELETE {settings.admin_url}/admin/keys/<préfixe> puis /admin/users/<éphémère>"),
    )
    stages = tuple(
        RecipeStage(
            name=nom, status=STAGE_SKIP, code="would_exercise",
            message=f"serait exercé : {cible}",
        )
        for nom, cible in etapes
    )
    return RecipeReport(
        reason=REASON_OK,
        stages=stages,
        model=settings.model_id or "<dérivé du plus petit modèle activé>",
        settings_view=settings.public_view(),
        stream=None,
        usage_entries=0,
        identity_created=False,
        identity_cleaned=False,
        readiness_levels={"liveness": False, "structural": False, "serving": False},
        observed_at=context.now(),
        dry_run=True,
    )


async def _call(
    client: HttpClient,
    method: str,
    url: str,
    *,
    json: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float,
) -> HttpResponse:
    """
    Appel unitaire. Une exception de transport devient un `HttpResponse` à 0.

    Fail-closed sans faire tomber la recette : le code `0` n'est aucun statut
    HTTP valide, aucune branche `== 200` ne peut donc l'accepter par mégarde, et
    le message est expurgé avant d'atteindre le journal.
    """
    try:
        return await client.request(method, url, json=json, headers=headers, timeout=timeout)
    except Exception as exc:  # transport, DNS, TLS, timeout — tous équivalents ici
        return HttpResponse(status=0, body={"transport_error": ex.redact_for_log(str(exc))})


async def _run_stages(
    *,
    settings: FirstTokenSettings,
    client: HttpClient,
    admin_secret: str,
    context: ex.ExecutionContext,
    sleep: AsyncSleep,
    identity_suffix: Callable[[], str],
    state: _RecipeState,
) -> str:
    """Points 1 à 10 de la séquence. Rend la cause, `ok` si tout a traversé."""
    entetes = _admin_headers(admin_secret)

    # 1. Liveness — sur le chemin PUBLIC. `/health` n'exige aucune autorisation :
    # y présenter l'ADMIN_SECRET l'exposerait à un intermédiaire pour rien.
    sante = await _call(client, "GET", f"{settings.base_url}/health", timeout=settings.ready_timeout_s)
    if sante.status != 200:
        state.add("liveness", STAGE_FAIL, REASON_LIVENESS,
                  f"GET /health a répondu {sante.status} sur le chemin public — "
                  "processus mort, ou le reverse-proxy ne le joint pas.")
        return REASON_LIVENESS
    state.levels["liveness"] = True
    state.add("liveness", STAGE_PASS, "liveness_ok", "Le processus répond sur le chemin public.")

    # 2. Readiness structurelle — sur le plan de CONTRÔLE : `/ready` n'est pas
    # proxifiée par `deploy/nginx.conf`, elle n'est joignable qu'en direct.
    pret = await _call(client, "GET", f"{settings.admin_url}/ready",
                       headers=entetes, timeout=settings.ready_timeout_s)
    corps = pret.body if isinstance(pret.body, dict) else {}
    niveaux = corps.get("levels") if isinstance(corps.get("levels"), dict) else {}
    if pret.status != 200:
        state.add("structural_readiness", STAGE_FAIL, REASON_STRUCTURAL,
                  f"GET /ready a répondu {pret.status} (cause : {corps.get('reason', 'inconnue')}).")
        return REASON_STRUCTURAL
    state.levels["structural"] = bool(niveaux.get("structural"))
    state.levels["serving"] = bool(niveaux.get("serving"))
    if not state.levels["structural"]:
        state.add("structural_readiness", STAGE_FAIL, REASON_STRUCTURAL,
                  "GET /ready répond 200 mais n'annonce pas la readiness structurelle "
                  f"(niveau : {corps.get('level', LEVEL_NONE)!r}).")
        return REASON_STRUCTURAL
    state.add("structural_readiness", STAGE_PASS, "structural_ok",
              f"Readiness structurelle atteinte (niveau annoncé : {corps.get('level', LEVEL_STRUCTURAL)!r}).")
    if not state.levels["serving"]:
        # §10 : le feu vert production exige la serving readiness OU un smoke
        # test explicite. Ici c'est le smoke test — le constat est informatif,
        # et c'est précisément le sens de la suite de la recette.
        state.add("serving_readiness", STAGE_WARN, "serving_not_yet",
                  "Aucun modèle n'est encore chargé : la serving readiness sera prouvée "
                  "par la génération de cette recette, pas par /ready.")

    # 3'. Résolution du modèle avant l'identité, pour ne pas créer d'utilisateur
    # si la cible n'est même pas dérivable.
    if settings.model_id:
        state.model = settings.model_id
    else:
        modeles = await _call(client, "GET", f"{settings.admin_url}/admin/models",
                              headers=entetes, timeout=settings.admin_timeout_s)
        choisi = _smallest_enabled_model(modeles.body) if modeles.status == 200 else None
        if not choisi:
            state.add("model_resolution", STAGE_FAIL, REASON_MODEL_UNRESOLVED,
                      f"GET /admin/models a répondu {modeles.status} : aucun modèle activé n'est dérivable.")
            return REASON_MODEL_UNRESOLVED
        state.model = choisi
    state.add("model_resolution", STAGE_PASS, "model_resolved", f"Modèle exercé : {state.model}.")

    # 3. Identité éphémère. Le nom est TOUJOURS généré : aucun nom fourni par un
    # opérateur ne peut atterrir dans ce rapport.
    state.username = f"{IDENTITY_PREFIX}-{identity_suffix()}"
    cree = await _call(
        client, "POST", f"{settings.admin_url}/admin/users",
        json={"username": state.username, "notes": IDENTITY_NOTES},
        headers=entetes, timeout=settings.admin_timeout_s,
    )
    # Armé même sur un statut inattendu : un timeout réseau qui aurait quand
    # même créé la ligne doit être nettoyé. Le nettoyage tolère un 404.
    state.identity_created = True
    if cree.status != 201:
        state.add("identity", STAGE_FAIL, REASON_IDENTITY_CREATE,
                  f"POST /admin/users a répondu {cree.status} : identité de recette impossible.")
        return REASON_IDENTITY_CREATE

    clef = await _call(
        client, "POST", f"{settings.admin_url}/admin/users/{state.username}/keys",
        json={"name": "first-token-recipe"}, headers=entetes, timeout=settings.admin_timeout_s,
    )
    corps_clef = clef.body if isinstance(clef.body, dict) else {}
    api_key = corps_clef.get("api_key")
    state.key_prefix = str(corps_clef.get("key_prefix") or "")
    if clef.status != 201 or not isinstance(api_key, str) or not api_key:
        state.add("identity", STAGE_FAIL, REASON_IDENTITY_KEY,
                  f"POST /admin/users/{{u}}/keys a répondu {clef.status} : aucune clé exploitable.")
        return REASON_IDENTITY_KEY
    state.add("identity", STAGE_PASS, "identity_ok",
              "Identité éphémère créée (la clé n'est ni journalisée ni rapportée).")

    # 4. Chargement explicite du modèle, borné par le timeout dérivé du registre.
    charge = await _call(
        client, "POST", f"{settings.admin_url}/admin/models/{state.model}/load",
        headers=entetes, timeout=settings.load_timeout_s,
    )
    if charge.status != 200:
        detail = ""
        if charge.status == 504:
            detail = (" Un 504 signale probablement un proxy dont le timeout de "
                      "chargement est trop court : visez le plan de contrôle en direct.")
        state.add("model_load", STAGE_FAIL, REASON_MODEL_LOAD,
                  f"POST /admin/models/{state.model}/load a répondu {charge.status}.{detail}")
        return REASON_MODEL_LOAD
    state.add("model_load", STAGE_PASS, "model_loaded", "Modèle chargé et annoncé prêt.")

    # 5 à 9. Génération streamée sur le chemin public.
    cause = await _generate(settings=settings, client=client, api_key=api_key,
                            context=context, state=state)
    if cause != REASON_OK:
        return cause

    # 10. Log d'usage. `log_usage` est écrit en tâche de fond après la fin du
    # générateur : on laisse le temps d'écrire, sans jamais attendre sans borne.
    return await _await_usage_log(settings=settings, client=client, entetes=entetes,
                                  context=context, sleep=sleep, state=state)


async def _generate(
    *,
    settings: FirstTokenSettings,
    client: HttpClient,
    api_key: str,
    context: ex.ExecutionContext,
    state: _RecipeState,
) -> str:
    """Points 5 à 9 : le flux SSE, ses deux mesures de temps, et son verdict."""
    corps = {
        "model": state.model,
        "messages": [{"role": "user", "content": settings.prompt}],
        "max_tokens": settings.max_tokens,
        "stream": True,
        "temperature": 0,
    }
    entetes = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    t_start = context.monotonic()
    deadline = t_start + settings.stream_timeout_s
    try:
        async with client.stream(
            "POST", f"{settings.base_url}{GENERATION_PATH}",
            json=corps, headers=entetes, timeout=settings.stream_timeout_s,
        ) as reponse:
            t_headers = context.monotonic()
            state.stream = await analyse_sse_stream(
                reponse.aiter_lines(),
                http_code=int(reponse.status),
                expected_model=state.model,
                t_start=t_start,
                t_headers=t_headers,
                monotonic=context.monotonic,
                deadline=deadline,
            )
    except Exception as exc:
        # Une coupure en plein flux ne doit pas emporter la recette : elle est
        # un ÉCHEC consigné, avec son message expurgé.
        state.stream = StreamOutcome(
            reason=REASON_TRANSPORT, http_code=0,
            headers_ms=_ms(context.monotonic() - t_start),
            total_ms=_ms(context.monotonic() - t_start),
        )
        state.add("generation", STAGE_FAIL, REASON_TRANSPORT,
                  "Le flux de génération a été interrompu : "
                  + ex.redact_for_log(f"{type(exc).__name__}: {exc}"))
        return REASON_TRANSPORT

    flux = state.stream
    if not flux.served:
        state.add("generation", STAGE_FAIL, flux.reason, _explain(flux, state.model))
        return flux.reason

    state.levels["serving"] = True
    state.add("generation", STAGE_PASS, "generation_ok",
              f"Premier delta utile à {flux.ttft_ms} ms (en-têtes à {flux.headers_ms} ms), "
              f"[DONE] atteint, enveloppe/modèle/usage conformes.")

    if flux.buffering_suspected:
        state.add("generation_buffering", STAGE_WARN, "sse_buffering_suspected",
                  f"Tous les deltas sont arrivés d'un bloc après {flux.ttft_ms - flux.headers_ms} ms : "
                  "le SSE est peut-être bufferisé par un intermédiaire. Indice non décisif — un "
                  "backend rapide qui répond en une rafale produit la même signature.")

    if settings.ttft_threshold_ms > 0 and flux.ttft_ms > settings.ttft_threshold_ms:
        if settings.fail_on_ttft:
            state.add("ttft", STAGE_FAIL, "ttft_threshold_exceeded",
                      f"TTFT de {flux.ttft_ms} ms au-dessus du seuil de {settings.ttft_threshold_ms} ms.")
            return "ttft_threshold_exceeded"
        state.add("ttft", STAGE_WARN, "ttft_threshold_exceeded",
                  f"TTFT de {flux.ttft_ms} ms au-dessus du seuil de {settings.ttft_threshold_ms} ms "
                  "(alerte, la génération reste fonctionnelle).")
    return REASON_OK


_EXPLICATIONS: dict[str, str] = {
    REASON_NO_CONTENT: (
        "Le flux s'est ouvert — et a pu se terminer proprement — mais n'a produit AUCUN delta "
        "portant du contenu : la version répond sans servir (défaut COR-006)."
    ),
    REASON_NO_DONE: "Le flux s'est interrompu avant [DONE].",
    REASON_NO_SSE_DATA: "Aucun événement SSE n'a été reçu.",
    REASON_UPSTREAM_ERROR: "Le backend d'inférence a renvoyé une erreur pendant le flux.",
    REASON_HTTP_STATUS: "Statut HTTP non-200 sur la génération.",
    REASON_MODEL_MISMATCH: "Le modèle annoncé dans le flux ne correspond pas au modèle demandé.",
    REASON_NO_USAGE: "Aucune comptabilisation de tokens dans le flux : l'usage ne serait pas facturé.",
    REASON_BAD_ENVELOPE: "Enveloppe SSE non conforme au format chat.completion.chunk.",
    REASON_STREAM_TIMEOUT: "Le flux n'a jamais atteint [DONE] dans le délai imparti.",
    REASON_TRANSPORT: "Le transport a échoué avant ou pendant le flux.",
}


def _explain(flux: StreamOutcome, model: str) -> str:
    base = _EXPLICATIONS.get(flux.reason, f"Génération non prouvée ({flux.reason}).")
    return f"{base} (modèle demandé : {model}, statut HTTP : {flux.http_code})"


async def _await_usage_log(
    *,
    settings: FirstTokenSettings,
    client: HttpClient,
    entetes: Mapping[str, str],
    context: ex.ExecutionContext,
    sleep: AsyncSleep,
    state: _RecipeState,
) -> str:
    """
    Point 10 : la génération a-t-elle été écrite au log d'usage ?

    Bornée par `usage_timeout_s` sur l'horloge injectée. Une génération non
    facturée est un défaut de facturation silencieux : l'absence d'entrée fait
    échouer la recette, elle ne produit pas un simple avertissement.
    """
    limite = context.monotonic() + settings.usage_timeout_s
    while True:
        reponse = await _call(
            client, "GET",
            f"{settings.admin_url}/admin/usage?username={state.username}&limit=10",
            headers=entetes, timeout=settings.admin_timeout_s,
        )
        if reponse.status == 200:
            state.usage_entries = _count_usage(reponse.body, state.model)
            if state.usage_entries > 0:
                state.add("usage_log", STAGE_PASS, "usage_logged",
                          f"{state.usage_entries} entrée(s) de log d'usage imputée(s) à {state.model}.")
                return REASON_OK
        if context.monotonic() >= limite:
            break
        await sleep(settings.usage_poll_interval_s)

    state.add("usage_log", STAGE_FAIL, REASON_NO_USAGE_LOG,
              "Aucune entrée de log d'usage après la génération : la consommation n'est pas "
              "facturée, alors que des tokens ont bien été produits.")
    return REASON_NO_USAGE_LOG


def _count_usage(document: Any, model: str) -> int:
    """Entrées du log d'usage imputées au modèle exercé, avec des tokens."""
    if not isinstance(document, list):
        return 0
    total = 0
    for entree in document:
        if not isinstance(entree, dict):
            continue
        if model and str(entree.get("model") or "") != model:
            continue
        try:
            if int(entree.get("total_tokens") or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        total += 1
    return total


async def _cleanup_identity(
    *,
    settings: FirstTokenSettings,
    client: HttpClient,
    admin_secret: str,
    context: ex.ExecutionContext,
    state: _RecipeState,
) -> None:
    """
    Point 11 : révoquer la clé, puis **anonymiser** l'utilisateur (DEC-001).

    Deux tolérances délibérées, et deux seulement : un `404` sur la révocation
    (clé déjà retirée) et un `200 already_anonymized` sur l'utilisateur. Tout
    autre statut laisse `identity_cleaned` à faux, et la recette le dit — un
    opérateur doit pouvoir retirer l'identité à la main.
    """
    if not state.identity_created:
        state.identity_cleaned = True
        return

    entetes = _admin_headers(admin_secret)
    clef_ok = True
    if state.key_prefix:
        revoquee = await _call(
            client, "DELETE", f"{settings.admin_url}/admin/keys/{state.key_prefix}",
            headers=entetes, timeout=settings.admin_timeout_s,
        )
        clef_ok = revoquee.status in (200, 404)

    retire = await _call(
        client, "DELETE", f"{settings.admin_url}/admin/users/{state.username}",
        headers=entetes, timeout=settings.admin_timeout_s,
    )
    corps = retire.body if isinstance(retire.body, dict) else {}
    # DEC-001 : la route ANONYMISE et répond 200. Un 204 « supprimé » signalerait
    # que le contrat a changé sous nos pieds, et n'est donc PAS accepté.
    utilisateur_ok = retire.status == 200 and corps.get("status") in ("anonymized", "already_anonymized")

    state.identity_cleaned = bool(clef_ok and utilisateur_ok)
    if state.identity_cleaned:
        state.add("identity_cleanup", STAGE_PASS, "identity_cleaned",
                  "Clé révoquée et utilisateur éphémère anonymisé (DEC-001).")
    else:
        state.add("identity_cleanup", STAGE_FAIL, "identity_residual",
                  f"Identité éphémère NON retirée (clé : {'ok' if clef_ok else 'échec'}, "
                  f"utilisateur : {retire.status}). Retirez-la à la main : "
                  f"DELETE {settings.admin_url}/admin/users/{state.username}")
    context.journaliser(f"recette du premier token — nettoyage : {state.identity_cleaned}")


# ── Exécuteur et enregistrement ───────────────────────────────────────────────

def make_smoke_test_executor(
    *,
    settings: FirstTokenSettings,
    client: HttpClient,
    admin_secret: str,
    sleep: AsyncSleep = _no_sleep,
    identity_suffix: Callable[[], str] = _default_identity_suffix,
) -> ex.StepExecutor:
    """
    Fabrique l'exécuteur de `schema.ACTION_SMOKE_TEST`.

    Le contrat fixe la signature `(step, context)` : tout le reste — transport,
    secret d'administration, sommeil, tirage d'identité — est capturé ici. C'est
    ce qui permet aux tests de n'ouvrir aucune socket et de n'attendre aucune
    seconde réelle.
    """

    async def executer(step: schema.PlanStep, context: ex.ExecutionContext) -> ex.StepResult:
        rapport = await run_first_token_recipe(
            settings=settings, client=client, admin_secret=admin_secret,
            context=context, sleep=sleep, identity_suffix=identity_suffix,
        )
        if context.dry_run:
            return ex.StepResult.for_step(
                step, status=ex.STEP_WOULD_APPLY,
                summary=(
                    "recette du premier token simulée : aucune requête émise, aucune identité "
                    f"créée — {len(rapport.stages)} étapes seraient exercées sur {settings.base_url}"
                ),
                evidence=rapport.to_dict(),
            )
        if rapport.served:
            flux = rapport.stream
            return ex.StepResult.for_step(
                step, status=ex.STEP_DONE,
                summary=(
                    f"premier token servi par {rapport.model} : en-têtes à {flux.headers_ms} ms, "
                    f"premier delta utile à {flux.ttft_ms} ms, {flux.content_chunks} chunk(s) de "
                    f"contenu, usage journalisé"
                ),
                duration_ms=max(flux.total_ms, 0),
                evidence=rapport.to_dict(),
                findings=rapport.findings(),
            )
        return ex.StepResult.for_step(
            step, status=ex.STEP_FAILED,
            summary=f"recette du premier token non prouvée sur {settings.base_url}",
            evidence=rapport.to_dict(),
            findings=rapport.findings(),
            error=f"cause : {rapport.reason}",
        )

    return executer


def register_executors(
    registry: ex.ExecutorRegistry,
    *,
    settings: FirstTokenSettings,
    client: HttpClient,
    admin_secret: str,
    sleep: AsyncSleep = _no_sleep,
    identity_suffix: Callable[[], str] = _default_identity_suffix,
) -> None:
    """
    Branche l'exécuteur de `ACTION_SMOKE_TEST` dans un registre.

    Volontairement séparé de celui de `warmup` : les deux modules ne s'importent
    pas l'un l'autre, exactement comme les producteurs de la vague 5. Un
    orchestrateur qui veut les deux appelle les deux fonctions.
    """
    registry.register(
        schema.ACTION_SMOKE_TEST,
        make_smoke_test_executor(
            settings=settings, client=client, admin_secret=admin_secret,
            sleep=sleep, identity_suffix=identity_suffix,
        ),
    )


__all__ = [
    "BUFFERING_GAP_TOLERANCE_MS",
    "BUFFERING_MIN_FIRST_DELTA_MS",
    "FirstTokenError",
    "FirstTokenSettings",
    "HttpClient",
    "HttpResponse",
    "LEVEL_NONE",
    "LEVEL_SERVING",
    "LEVEL_STRUCTURAL",
    "PROOF_KIND",
    "PROOF_NOT_SERVED",
    "PROOF_SERVED",
    "PROOF_VERSION",
    "READINESS_LEVELS",
    "RecipeReport",
    "RecipeStage",
    "StreamOutcome",
    "StreamedResponse",
    "analyse_sse_stream",
    "make_smoke_test_executor",
    "proof_authorizes",
    "register_executors",
    "run_first_token_recipe",
]
