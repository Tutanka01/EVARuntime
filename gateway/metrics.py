"""
Endpoints de métriques pour le dashboard admin.

Toutes les routes sont sous /admin/metrics/ et nécessitent le secret admin.
Elles ne sont accessibles que depuis le réseau campus (filtrage IP nginx).

Endpoints :
  GET /admin/metrics/overview      — KPIs temps réel
  GET /admin/metrics/timeseries    — série temporelle (requêtes / tokens)
  GET /admin/metrics/users         — stats par utilisateur avec quota
  GET /admin/metrics/status-codes  — distribution des codes HTTP
  GET /admin/metrics/llama         — métriques Prometheus llama-server → JSON
"""
from __future__ import annotations

import logging
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Query

import database as db
from auth import require_admin
from config import settings
from model_manager import model_manager
from server_manager import ModelState

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/metrics", tags=["metrics"])

_INTERNAL_HEADERS = {
    "Authorization": f"Bearer {settings.internal_api_key}",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _percentile(values: list[int], p: float) -> float | None:
    """Percentile d'une liste triée. p ∈ [0, 1]."""
    if not values:
        return None
    return float(values[max(0, int(p * len(values)) - 1)])


def _parse_prometheus(text: str) -> dict[str, float]:
    """
    Parse minimaliste du format texte Prometheus.
    Extrait les métriques scalaires (gauge/counter) de llama-server.
    Ignore les histogrammes (_bucket, _sum, _count avec labels).
    """
    result: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Sauter les lignes avec labels complexes ({...})
        if "{" in line:
            continue
        parts = line.split()
        if len(parts) == 2:
            try:
                result[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return result


def _period_to_hours(period: str) -> int:
    mapping = {"24h": 24, "7d": 168, "30d": 720}
    return mapping.get(period, 24)


def _period_to_days(period: str) -> int:
    mapping = {"7d": 7, "30d": 30, "90d": 90}
    return mapping.get(period, 30)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/overview")
async def metrics_overview(
    _: None = Depends(require_admin),
) -> dict:
    """
    KPIs principaux : requêtes aujourd'hui, tokens, latence, erreurs,
    utilisateurs actifs, statut du modèle.
    """
    overview = await db.get_overview_stats()

    # Percentiles latence (7 derniers jours pour avoir assez de données)
    latency_samples = await db.get_latency_samples(period_hours=168)
    latency_samples.sort()

    p50 = _percentile(latency_samples, 0.50)
    p95 = _percentile(latency_samples, 0.95)
    p99 = _percentile(latency_samples, 0.99)

    return {
        **overview,
        "latency_p50_ms": p50,
        "latency_p95_ms": p95,
        "latency_p99_ms": p99,
        "models": model_manager.status(),
    }


@router.get("/timeseries")
async def metrics_timeseries(
    period: Literal["24h", "7d", "30d"] = Query("24h"),
    _: None = Depends(require_admin),
) -> list[dict]:
    """
    Série temporelle des requêtes et tokens.
    - period=24h  → buckets horaires sur 24h
    - period=7d   → buckets journaliers sur 7 jours
    - period=30d  → buckets journaliers sur 30 jours
    """
    lookback_hours = _period_to_hours(period)
    bucket = "hour" if period == "24h" else "day"
    return await db.get_timeseries(bucket=bucket, lookback_hours=lookback_hours)


@router.get("/users")
async def metrics_users(
    period: Literal["7d", "30d", "90d"] = Query("30d"),
    _: None = Depends(require_admin),
) -> list[dict]:
    """Statistiques par utilisateur avec consommation quota."""
    period_days = _period_to_days(period)
    rows = await db.get_user_period_stats(period_days=period_days)

    # Calculer le % quota utilisé si une limite est définie.
    # On utilise tokens_30d (toujours sur 30 jours glissants) pour que la
    # comparaison avec monthly_token_limit reste correcte quelle que soit
    # la période d'affichage sélectionnée dans le dashboard.
    result = []
    for row in rows:
        entry = dict(row)
        limit = entry.get("monthly_token_limit", 0)
        if limit and limit > 0:
            entry["quota_used_pct"] = round(
                min(entry["tokens_30d"] / limit * 100, 100), 1
            )
        else:
            entry["quota_used_pct"] = None  # Illimité
        result.append(entry)

    return result


@router.get("/status-codes")
async def metrics_status_codes(
    period: Literal["24h", "7d", "30d"] = Query("24h"),
    _: None = Depends(require_admin),
) -> list[dict]:
    """Distribution des codes de statut HTTP sur la période."""
    hours = _period_to_hours(period)
    return await db.get_status_code_stats(period_hours=hours)


@router.get("/llama")
async def metrics_llama(
    _: None = Depends(require_admin),
) -> dict:
    """
    Métriques temps réel de llama-server (format Prometheus → JSON).
    Interroge tous les modèles actuellement chargés (état READY).
    Retourne {} si aucun modèle n'est chargé ou en cas d'erreur.

    Métriques clés exposées par modèle :
      llamacpp:kv_cache_usage_ratio   — occupation du cache KV (0–1)
      llamacpp:requests_processing    — requêtes en cours d'inférence
      llamacpp:requests_deferred      — requêtes en attente de slot
      llamacpp:tokens_per_second      — vitesse de génération (tokens/s)
      llamacpp:prompt_tokens_total    — tokens prompt traités depuis démarrage
      llamacpp:tokens_predicted_total — tokens générés depuis démarrage
    """
    result: dict = {}

    ready_managers = [
        (mid, mgr)
        for mid, mgr in model_manager._managers.items()
        if mgr.state == ModelState.READY
    ]

    if not ready_managers:
        return {}

    async with httpx.AsyncClient(timeout=3.0) as client:
        for model_id, mgr in ready_managers:
            try:
                resp = await client.get(
                    mgr.llama_url("/metrics"),
                    headers=_INTERNAL_HEADERS,
                )
                if resp.status_code != 200:
                    continue

                raw = _parse_prometheus(resp.text)

                def _get(key: str) -> float | None:
                    return raw.get(key)

                result[model_id] = {
                    "kv_cache_usage_ratio": _get("llamacpp:kv_cache_usage_ratio"),
                    "kv_cache_tokens": _get("llamacpp:kv_cache_tokens"),
                    "requests_processing": _get("llamacpp:requests_processing"),
                    "requests_deferred": _get("llamacpp:requests_deferred"),
                    "tokens_per_second": _get("llamacpp:tokens_per_second"),
                    "prompt_tokens_total": _get("llamacpp:prompt_tokens_total"),
                    "tokens_predicted_total": _get("llamacpp:tokens_predicted_total"),
                }
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
                pass
            except Exception:
                log.exception("Erreur métriques llama pour '%s'", model_id)

    return result
