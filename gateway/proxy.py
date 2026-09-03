"""
Proxy OpenAI-compatible vers llama-server.

Endpoints gérés :
  POST /v1/chat/completions   — streaming SSE + non-streaming
  POST /v1/completions        — legacy completions
  POST /v1/completion         — endpoint natif llama.cpp (prompt string, sans chat template)
  POST /completion            — alias direct pour les scripts llama.cpp existants
  POST /v1/tokenize           — tokenisation d'un texte
  POST /v1/detokenize         — reconstruction texte depuis token IDs
  GET  /v1/models             — liste dynamique depuis le registre

Design :
- On extrait le champ "model" du body JSON pour router vers le bon llama-server
- Si "model" est absent, on utilise le modèle par défaut configuré
- On injecte l'Authorization interne (clé gateway ↔ llama-server)
- On log l'usage en fire-and-forget après chaque requête terminée
- Pour le streaming : on désactive tout buffering nginx/uvicorn via les headers

Proxy transparent — paramètres llama.cpp natifs :
  Le body JSON est forwardé tel quel vers llama-server. Tous les paramètres de sampling
  avancés sont supportés sans configuration particulière, que ce soit via /v1/chat/completions
  (superset OpenAI) ou /completion (endpoint natif) :
  mirostat, mirostat_tau, mirostat_eta, dry_multiplier, dry_base, dry_allowed_length,
  repeat_last_n, repeat_penalty, top_k, min_p, tfs_z, typical_p,
  xtc_probability, xtc_threshold, ignore_eos, n_predict, seed, etc.

Point critique SSE :
  nginx doit avoir proxy_buffering off et X-Accel-Buffering: no
  pour que les chunks arrivent en temps réel chez le client.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import AsyncGenerator, Callable

import anyio  # backend asyncio de starlette/httpcore — dépendance déjà verrouillée, pas un ajout
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

import database as db
from background import fire_and_forget
from config import settings
from model_manager import CapacityQueueFull, CapacityQueueTimeout, ModelManager
from server_manager import ServerManager
from telemetry import TTFT_SECONDS

log = logging.getLogger(__name__)

# Timeout total pour une génération (10 minutes)
_INFERENCE_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=5.0)


# ── Client HTTP partagé (chemin chaud) ────────────────────────────────────────
#
# Un unique httpx.AsyncClient par processus proxifie toutes les requêtes vers
# les sous-processus llama-server locaux. Le pool de connexions keep-alive évite
# un handshake TCP par requête vers 127.0.0.1:<port_llama>. Le client est créé
# au démarrage (lifespan) via init_http_client() et fermé au shutdown via
# aclose_http_client() — JAMAIS par requête (ce serait rouvrir le pool à chaque
# appel, exactement le gaspillage qu'on élimine).
#
# En test (pas de lifespan monté), get_http_client() initialise paresseusement
# un client par défaut ; les tests peuvent aussi injecter un client mocké via
# set_http_client() (MockTransport).
_http_client: httpx.AsyncClient | None = None


def _build_http_client() -> httpx.AsyncClient:
    """Construit le client partagé avec un pool dimensionné sur la config."""
    limits = httpx.Limits(
        # 0 dans la config = illimité → None côté httpx.Limits.
        max_connections=settings.httpx_max_connections or None,
        max_keepalive_connections=settings.httpx_max_keepalive or None,
        keepalive_expiry=settings.httpx_keepalive_expiry,
    )
    return httpx.AsyncClient(timeout=_INFERENCE_TIMEOUT, limits=limits)


def init_http_client() -> httpx.AsyncClient:
    """
    Initialise le client HTTP partagé. Appelé par le lifespan de main.py au
    démarrage. Idempotent : ne recrée pas un client déjà initialisé.
    """
    global _http_client
    if _http_client is None:
        _http_client = _build_http_client()
    return _http_client


async def aclose_http_client() -> None:
    """Ferme proprement le client partagé. Appelé par le lifespan au shutdown."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Injecte un client (tests : MockTransport). None réinitialise l'état."""
    global _http_client
    _http_client = client


def get_http_client() -> httpx.AsyncClient:
    """
    Retourne le client partagé, en l'initialisant paresseusement si le lifespan
    n'a pas été monté (chemin des tests). Ne ferme jamais ce client par requête.
    """
    global _http_client
    if _http_client is None:
        _http_client = _build_http_client()
    return _http_client


async def _report_backend_failure(manager: ServerManager) -> None:
    """
    Informe le gestionnaire d'un échec du data-plane, s'il sait le traiter.

    ``ServerManager`` (mode local) n'expose volontairement pas ce hook. Le
    handle cluster l'utilise pour invalider immédiatement un placement devenu
    injoignable, sans attendre le prochain heartbeat. Le reporting reste
    best-effort : une erreur de bookkeeping ne doit jamais masquer l'erreur
    d'inférence d'origine renvoyée au client.
    """
    callback = getattr(manager, "report_backend_failure", None)
    if callback is None:
        return
    try:
        result = callback()
        if inspect.isawaitable(result):
            await result
    except Exception:
        log.exception(
            "Impossible de signaler l'échec du backend '%s'.",
            manager.model.id,
        )


# ── Stream upstream ouvert en pré-flight ─────────────────────────────────────

@dataclass
class _OpenedStream:
    """
    Connexion upstream ouverte (contexte httpx entré) mais pas encore lue.

    Le pré-flight de `proxy_request` ouvre le stream ici pour connaître le
    statut AVANT d'envoyer le premier octet SSE au client : un 4xx/5xx du
    backend est alors relayé comme une vraie réponse HTTP d'erreur, pas comme
    un flux 200 contenant un JSON d'erreur brut. Le générateur `_stream_proxy`
    devient propriétaire du contexte et le rend au pool dans son finally.
    """

    ctx: AbstractAsyncContextManager[httpx.Response]
    response: httpx.Response


async def _open_upstream_stream(
    manager: ServerManager,
    path: str,
    json_body: dict,
) -> _OpenedStream:
    """
    Ouvre le stream upstream sans consommer le corps : on entre dans le
    contexte httpx (requête émise, statut et headers reçus) et la lecture des
    chunks est laissée au générateur. Client partagé uniquement — on ne ferme
    JAMAIS ce client ici, seule la connexion empruntée est rendue au pool.
    """
    client = get_http_client()
    ctx = client.stream(
        "POST",
        manager.llama_url(path),
        json=json_body,
        headers=manager.auth_headers(),
    )
    response = await ctx.__aenter__()
    return _OpenedStream(ctx=ctx, response=response)


async def _discard_opened(opened: _OpenedStream) -> None:
    """Ferme silencieusement un stream upstream ouvert mais jamais consommé."""
    try:
        await opened.ctx.__aexit__(None, None, None)
    except Exception as exc:
        log.debug("Fermeture du stream upstream ignorée : %s", exc)


def _schedule_usage_log(
    *,
    user: dict,
    model_id: str,
    request_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    status_code: int,
    start_time: float,
) -> asyncio.Task:
    """
    Planifie la ligne d'usage terminale d'une requête streaming, en
    fire-and-forget. Appelée exactement UNE fois par requête : soit en
    pré-flight (échec avant tout octet envoyé), soit dans le finally du
    générateur — y compris sur déconnexion client (GeneratorExit), sinon la
    ligne était perdue : rien après le finally d'un générateur fermé ne
    s'exécute jamais.

    Retourne la tâche pour que l'appelant puisse l'attendre sous annulation
    (voir le finally de _stream_proxy).
    """
    return fire_and_forget(db.log_usage(
        user_id=user["user_id"],
        key_id=user["key_id"],
        model=model_id,
        prompt_tokens=int(prompt_tokens or 0),
        completion_tokens=int(completion_tokens or 0),
        duration_ms=int((time.monotonic() - start_time) * 1000),
        status_code=status_code,
        request_id=request_id,
    ), name="log_usage_stream")


def _resolve_model_id(body: dict, model_manager: ModelManager) -> str:
    """
    Résout l'ID du modèle à utiliser pour une requête.
    Priorité : champ "model" du body → default_model_id → premier modèle enabled.
    """
    requested = (body.get("model") or "").strip()
    if requested:
        return requested

    if settings.default_model_id:
        return settings.default_model_id

    first = model_manager.registry.first_enabled_id()
    if first:
        return first

    return ""


# ── Handler principal ─────────────────────────────────────────────────────────

async def proxy_request(
    request: Request,
    path: str,
    user: dict,
    model_manager: ModelManager,
) -> StreamingResponse | JSONResponse:
    """
    Point d'entrée générique.
    - Lit le body et résout le modèle cible
    - Assure que le modèle est chargé (charge si nécessaire, évinçe LRU si besoin)
    - Proxy la requête vers le bon llama-server
    - Log l'usage
    """
    request_start_time = time.monotonic()

    # ── Lire le body ──────────────────────────────────────────────────────────
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        return _openai_error(400, "Corps JSON invalide.", "invalid_request_error")

    if not isinstance(body, dict):
        return _openai_error(400, "Le corps JSON doit être un objet.", "invalid_request_error")
    if "model" in body and body["model"] is not None and not isinstance(body["model"], str):
        return _openai_error(400, "Le champ 'model' doit être une chaîne.", "invalid_request_error")

    # ── Résoudre le modèle ────────────────────────────────────────────────────
    model_id = _resolve_model_id(body, model_manager)
    if not model_id:
        return _openai_error(
            400,
            "Aucun modèle spécifié et aucun modèle activé dans le registre. "
            "Précisez le champ 'model' dans votre requête.",
            "invalid_request_error",
        )

    # ── Charger le modèle ─────────────────────────────────────────────────────
    try:
        manager = await model_manager.ensure_model_loaded(model_id)
    except LookupError as exc:
        return _openai_error(404, str(exc), "model_not_found")
    except PermissionError as exc:
        return _openai_error(403, str(exc), "model_disabled")
    except (CapacityQueueFull, CapacityQueueTimeout) as exc:
        return _openai_error(
            503,
            str(exc),
            "server_error",
            headers={"Retry-After": str(settings.capacity_queue_retry_after_seconds)},
        )
    except TimeoutError as exc:
        # Le message est sûr par construction (server_manager._wait_for_health) :
        # ni stderr, ni chemin, ni URL interne. On le journalise côté serveur
        # pour corrélation avec le détail technique (OPS-004).
        log.error("Chargement de '%s' impossible (timeout) : %s", model_id, exc)
        return _openai_error(503, str(exc), "server_error")
    except RuntimeError as exc:
        log.error("Chargement de '%s' impossible : %s", model_id, exc)
        return _openai_error(503, str(exc), "server_error")
    except Exception:
        log.exception("Erreur inattendue lors du chargement du modèle '%s'", model_id)
        return _openai_error(500, "Erreur interne du serveur.", "server_error")

    is_streaming = body.get("stream", False)
    request_id = str(uuid.uuid4())
    start_time = time.monotonic()

    if is_streaming:
        # Pré-flight : la connexion upstream est ouverte ICI, avant de renvoyer
        # la StreamingResponse. Un 4xx/5xx du backend (ou une erreur de
        # transport) est donc converti en vraie réponse HTTP AVANT le premier
        # octet SSE, au lieu d'être relayé comme un flux 200 contenant un JSON
        # d'erreur brut. Le générateur ne fait plus que relayer les chunks.
        #
        # Pin de garde : entre le return de cette fonction et le démarrage
        # effectif du générateur par Starlette, le modèle n'est pas encore
        # pinné par le stream et pourrait être évincé par une requête
        # concurrente. On pose donc un pin temporaire, relâché dès que le
        # générateur démarre (on_start) — ou par timer si le générateur ne
        # démarre jamais (déconnexion immédiate), pour ne pas bloquer
        # l'éviction indéfiniment.
        manager.pin()
        guard_released = False
        opened: _OpenedStream | None = None

        def _release_stream_guard() -> None:
            nonlocal guard_released
            if guard_released:
                return
            guard_released = True
            guard_timer.cancel()
            manager.unpin()

        def _close_unclaimed_stream() -> None:
            """Timer 30 s : le générateur n'a jamais démarré (jamais itéré).

            Le stream ouvert en pré-flight est alors orphelin — le générateur
            ne le réclamera jamais et son finally ne le fermera pas. On le
            rend au pool ici, sinon la connexion resterait ouverte jusqu'au
            GC. Quand le générateur a démarré, `_release_stream_guard` a déjà
            annulé ce timer et posé `guard_released` : le stream réclamé n'est
            jamais fermé ici (pas de double fermeture possible).
            """
            nonlocal opened
            if guard_released:
                return
            if opened is not None:
                orphan, opened = opened, None
                fire_and_forget(_discard_opened(orphan), name="discard_stream")
            _release_stream_guard()

        guard_timer = asyncio.get_running_loop().call_later(
            30.0, _close_unclaimed_stream
        )

        body_with_usage = {**body, "stream_options": {"include_usage": True}}

        try:
            opened = await _open_upstream_stream(manager, path, body_with_usage)
        except httpx.TimeoutException:
            _release_stream_guard()
            await _report_backend_failure(manager)
            _schedule_usage_log(
                user=user,
                model_id=manager.model.id,
                request_id=request_id,
                prompt_tokens=0,
                completion_tokens=0,
                status_code=504,
                start_time=request_start_time,
            )
            return _openai_error(
                504, "Timeout : le modèle n'a pas répondu à temps.", "server_error"
            )
        except httpx.RequestError as exc:
            _release_stream_guard()
            await _report_backend_failure(manager)
            log.error("Erreur de connexion à llama-server '%s' : %s", manager.model.id, exc)
            _schedule_usage_log(
                user=user,
                model_id=manager.model.id,
                request_id=request_id,
                prompt_tokens=0,
                completion_tokens=0,
                status_code=502,
                start_time=request_start_time,
            )
            return _openai_error(
                502, "Impossible de joindre le backend d'inférence.", "server_error"
            )
        except Exception:
            # Ni timeout ni erreur transport : panne inattendue du client HTTP
            # (client fermé au shutdown, StreamClosed…). On reflète le
            # catch-all du chargement de modèle ci-dessus plutôt que de
            # laisser fuir le pin de garde et la ligne d'usage dans un 500
            # framework nu.
            log.exception("Ouverture du stream impossible pour '%s'", manager.model.id)
            _release_stream_guard()
            _schedule_usage_log(
                user=user,
                model_id=manager.model.id,
                request_id=request_id,
                prompt_tokens=0,
                completion_tokens=0,
                status_code=500,
                start_time=request_start_time,
            )
            return _openai_error(500, "Erreur interne du serveur.", "server_error")

        upstream_status = opened.response.status_code
        if upstream_status >= 400:
            # Statut d'erreur reçu avant tout octet SSE : on lit le corps pour
            # décider du relais, on referme le contexte (finally), puis on
            # répond avec une vraie erreur HTTP (mêmes sémantiques que
            # _non_stream_proxy).
            raw_body: bytes | None
            try:
                raw_body = await opened.response.aread()
            except httpx.RequestError as exc:
                # Reset en pleine lecture du corps d'erreur : pas de relais
                # possible, on retombe sur l'enveloppe 502 générique.
                log.error(
                    "Lecture du corps d'erreur de llama-server '%s' interrompue : %s",
                    manager.model.id, exc,
                )
                raw_body = None
            finally:
                # Sur TOUS les chemins de cette branche : contexte rendu au
                # pool et pin de garde relâché — jamais de fuite, même si la
                # lecture du corps a échoué.
                await _discard_opened(opened)
                _release_stream_guard()
            try:
                data = json.loads(raw_body) if raw_body is not None else None
            except ValueError:
                data = None
            log.warning(
                "Stream refusé par llama-server '%s' (HTTP %d)",
                manager.model.id, upstream_status,
            )
            if isinstance(data, dict):
                # Cohérence avec _non_stream_proxy : l'ID public remplace
                # l'identifiant interne du backend, sans l'ajouter si absent.
                if "model" in data:
                    data["model"] = manager.model.id
                _schedule_usage_log(
                    user=user,
                    model_id=manager.model.id,
                    request_id=request_id,
                    prompt_tokens=0,
                    completion_tokens=0,
                    status_code=upstream_status,
                    start_time=request_start_time,
                )
                return JSONResponse(content=data, status_code=upstream_status)
            log.error(
                "Réponse non-JSON de llama-server '%s' (HTTP %d) sur un stream",
                manager.model.id, upstream_status,
            )
            _schedule_usage_log(
                user=user,
                model_id=manager.model.id,
                request_id=request_id,
                prompt_tokens=0,
                completion_tokens=0,
                status_code=502,
                start_time=request_start_time,
            )
            return _openai_error(502, "Réponse invalide du backend d'inférence.", "server_error")

        return StreamingResponse(
            _stream_proxy(
                user=user,
                request_id=request_id,
                start_time=start_time,
                manager=manager,
                opened=opened,
                on_start=_release_stream_guard,
                telemetry_start_time=request_start_time,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        manager.pin()
        try:
            return await _non_stream_proxy(path, body, user, request_id, start_time, manager)
        finally:
            manager.unpin()


# ── Proxy non-streaming ───────────────────────────────────────────────────────

async def _non_stream_proxy(
    path: str,
    body: dict,
    user: dict,
    request_id: str,
    start_time: float,
    manager: ServerManager,
) -> JSONResponse:
    try:
        # Client partagé : on emprunte une connexion keep-alive du pool. Ne
        # jamais fermer ce client ici — il vit toute la durée du processus.
        client = get_http_client()
        response = await client.post(
            manager.llama_url(path),
            json=body,
            headers=manager.auth_headers(),
        )
    except httpx.TimeoutException:
        await _report_backend_failure(manager)
        return _openai_error(504, "Timeout : le modèle n'a pas répondu à temps.", "server_error")
    except httpx.RequestError as exc:
        await _report_backend_failure(manager)
        log.error("Erreur de connexion à llama-server '%s' : %s", manager.model.id, exc)
        return _openai_error(502, "Impossible de joindre le backend d'inférence.", "server_error")

    duration_ms = int((time.monotonic() - start_time) * 1000)
    try:
        data = response.json()
    except ValueError:
        log.error(
            "Réponse non-JSON de llama-server '%s' (HTTP %d)",
            manager.model.id, response.status_code,
        )
        return _openai_error(502, "Réponse invalide du backend d'inférence.", "server_error")

    usage: dict = {}
    if isinstance(data, dict):
        data["model"] = manager.model.id

        # Supporte le format OpenAI {"usage": {...}} ET le format natif llama.cpp
        # /completion qui retourne {"tokens_predicted": N, "tokens_evaluated": M}.
        usage = data.get("usage") or {
            "prompt_tokens": data.get("tokens_evaluated", 0),
            "completion_tokens": data.get("tokens_predicted", 0),
        }

    fire_and_forget(db.log_usage(
        user_id=user["user_id"],
        key_id=user["key_id"],
        model=manager.model.id,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        duration_ms=duration_ms,
        status_code=response.status_code,
        request_id=request_id,
    ), name="log_usage")

    return JSONResponse(content=data, status_code=response.status_code)


# ── Proxy streaming SSE ───────────────────────────────────────────────────────

async def _stream_proxy(
    user: dict,
    request_id: str,
    start_time: float,
    manager: ServerManager,
    opened: _OpenedStream,
    *,
    on_start: Callable[[], None] | None = None,
    telemetry_start_time: float | None = None,
) -> AsyncGenerator[bytes, None]:
    """
    Générateur async qui pipe les chunks SSE de llama-server vers le client.

    La connexion upstream est déjà ouverte (pré-flight de `proxy_request`,
    via `_open_upstream_stream`) : ce générateur n'ouvre plus rien, il
    devient propriétaire de `opened` et le rend au pool dans son finally.

    Chaque événement est relayé dès sa réception, y compris lorsque la
    requête contient des tools. Les extensions du backend comme
    ``reasoning_content`` sont préservées : les réécrire ou les supprimer
    casserait les harness compatibles DeepSeek et fausserait le TTFT visible.

    Pin/unpin : manager.pin() est appelé en premier (avant tout yield) et
    manager.unpin() est garanti dans le finally, même en cas de déconnexion
    client (GeneratorExit) ou d'exception réseau. Cela protège le modèle
    contre une éviction LRU pendant toute la durée du stream.

    Le finally planifie aussi la ligne d'usage terminale, UNE seule fois par
    requête (499 = déconnexion client). Rien après le finally d'un générateur
    fermé ne s'exécutant jamais, ce log ne peut plus vivre en dehors.

    on_start : callback appelé dès que le pin du stream est posé — utilisé par
    proxy_request pour relâcher son pin de garde.
    """
    manager.pin()
    if on_start is not None:
        on_start()
    prompt_tokens = 0
    completion_tokens = 0
    status_code = 200
    terminal_logged = False
    ttft_recorded = False
    ttft_start = start_time if telemetry_start_time is None else telemetry_start_time

    def _record_ttft(chunk: dict) -> None:
        nonlocal ttft_recorded
        if ttft_recorded:
            return
        meaningful = any(
            bool(choice.get("delta", {}).get("content"))
            or bool(choice.get("delta", {}).get("reasoning_content"))
            or bool(choice.get("delta", {}).get("tool_calls"))
            for choice in chunk.get("choices", [])
        )
        if not meaningful:
            return
        TTFT_SECONDS.observe(
            max(0.0, time.monotonic() - ttft_start),
            model=manager.model.id,
            node=getattr(manager, "telemetry_node", "local"),
        )
        ttft_recorded = True

    try:
        response = opened.response
        async for line in response.aiter_lines():
            if not line:
                yield b"\n"
                continue

            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    chunk = json.loads(line[6:])
                    if "model" in chunk:
                        chunk["model"] = manager.model.id
                    if usage := chunk.get("usage"):
                        prompt_tokens = usage.get("prompt_tokens", 0)
                        completion_tokens = usage.get("completion_tokens", 0)
                    _record_ttft(chunk)
                    line = "data: " + json.dumps(chunk, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass

            yield (line + "\n\n").encode()

    except httpx.TimeoutException:
        await _report_backend_failure(manager)
        err = _sse_error("Timeout d'inférence dépassé.")
        status_code = 504
        yield err.encode()
    except httpx.RequestError as exc:
        await _report_backend_failure(manager)
        log.error("Erreur stream llama-server '%s' : %s", manager.model.id, exc)
        err = _sse_error("Erreur de connexion au backend d'inférence.")
        status_code = 502
        yield err.encode()
    except (GeneratorExit, asyncio.CancelledError):
        # Déconnexion client (aclose/annulation) : aucun yield ni await de
        # flux ici — le nettoyage vit uniquement dans le finally, qui doit
        # pouvoir s'exécuter jusqu'au bout sans rien re-yielder.
        status_code = 499
        raise
    except Exception:
        status_code = 500
        log.exception("Erreur inattendue dans le stream '%s'", manager.model.id)
        raise
    finally:
        # Garantie absolue : fermeture du contexte upstream (rendu au pool du
        # client partagé — jamais le client lui-même), unpin, puis UNE ligne
        # d'usage terminale, même si le client se déconnecte (GeneratorExit),
        # si une exception réseau survient, ou si le stream se termine
        # normalement.
        #
        # Bouclier anyio : sous uvicorn (asgi spec_version 2.3), Starlette
        # itère la réponse dans un task group et ANNULE la scope à la
        # déconnexion client. anyio re-livre alors CancelledError à CHAQUE
        # await de la scope (boucle call_soon tant que la scope reste
        # annulée) : sans bouclier, le premier await du finally échouait et
        # sautait unpin + la ligne 499 — fuite de _active_requests, modèle
        # plus jamais idle-évictable. La scope bouclier réparente la tâche
        # hors de portée de la re-livraison : le nettoyage est ininterruptible.
        with anyio.CancelScope(shield=True):
            try:
                await opened.ctx.__aexit__(None, None, None)
            except Exception as exc:
                log.debug("Fermeture du stream upstream ignorée : %s", exc)
            manager.unpin()
            if not terminal_logged:
                terminal_logged = True
                usage_task = _schedule_usage_log(
                    user=user,
                    model_id=manager.model.id,
                    request_id=request_id,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    status_code=status_code,
                    start_time=start_time,
                )
                # Sous annulation, la tâche fire-and-forget risque de ne jamais
                # tourner (loop en cours d'arrêt, task group en teardown) : on
                # l'attend ici, sous bouclier, UNIQUEMENT quand la tâche
                # courante est réellement annulée (cancelling() > 0). Chemin
                # normal : cancelling() == 0 → fire-and-forget pur, zéro
                # latence ajoutée. Une erreur DB ne doit jamais masquer la
                # GeneratorExit/CancelledError en cours de re-raise.
                if asyncio.current_task().cancelling() > 0:
                    try:
                        await usage_task
                    except Exception as exc:
                        log.debug(
                            "Ligne d'usage terminale non écrite (annulation) : %s",
                            exc,
                        )


# ── /v1/models ────────────────────────────────────────────────────────────────

def models_response(model_manager: ModelManager) -> JSONResponse:
    """
    Retourne la liste des modèles activés dans le registre.
    Compatible avec openai.models.list().
    """
    enabled_models = model_manager.registry.list_enabled()
    return JSONResponse(content={
        "object": "list",
        "data": [
            {
                "id": model.id,
                "object": "model",
                "created": 1704067200,
                "owned_by": "local-uppa",
                "description": model.description,
            }
            for model in enabled_models
        ],
    })


# ── Helpers ───────────────────────────────────────────────────────────────────

def _openai_error(
    status_code: int,
    message: str,
    error_type: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "code": str(status_code),
            }
        },
    )


def _sse_error(message: str) -> str:
    """Formate une erreur comme chunk SSE final."""
    payload = json.dumps({
        "error": {
            "message": message,
            "type": "server_error",
        }
    })
    return f"data: {payload}\n\ndata: [DONE]\n\n"
