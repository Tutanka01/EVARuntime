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
  GET /admin/metrics/prometheus    — exposition texte format Prometheus
"""
from __future__ import annotations

import logging
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

import database as db
from auth import require_admin
from model_manager import model_manager
from server_manager import ModelState

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/metrics", tags=["metrics"])

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


# ── Génération format d'exposition Prometheus (à la main, sans dépendance) ─────

def _esc_label(value: str) -> str:
    """
    Échappe une valeur de label selon le format d'exposition Prometheus :
    backslash, guillemet double et retour à la ligne. Cf.
    https://prometheus.io/docs/instrumenting/exposition_formats/
    """
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _fmt_value(value: float | int) -> str:
    """Formate une valeur numérique Prometheus (entiers sans .0, NaN toléré)."""
    if isinstance(value, bool):  # bool est un int en Python — normaliser
        return "1" if value else "0"
    f = float(value)
    if f.is_integer():
        return str(int(f))
    return repr(f)


class _PromWriter:
    """
    Accumulateur de lignes d'exposition Prometheus.

    Émet `# HELP` / `# TYPE` une seule fois par métrique, puis les échantillons.
    Robuste : `sample()` ignore silencieusement les valeurs None (source
    indisponible) plutôt que d'émettre une ligne invalide.
    """

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._declared: set[str] = set()

    def declare(self, name: str, mtype: str, help_text: str) -> None:
        if name in self._declared:
            return
        self._declared.add(name)
        self._lines.append(f"# HELP {name} {help_text}")
        self._lines.append(f"# TYPE {name} {mtype}")

    def sample(
        self,
        name: str,
        value: float | int | None,
        labels: dict[str, str] | None = None,
    ) -> None:
        if value is None:
            return
        if labels:
            rendered = ",".join(
                f'{k}="{_esc_label(v)}"' for k, v in labels.items()
            )
            self._lines.append(f"{name}{{{rendered}}} {_fmt_value(value)}")
        else:
            self._lines.append(f"{name} {_fmt_value(value)}")

    def render(self) -> str:
        # Une ligne vide finale est recommandée par le format d'exposition.
        return "\n".join(self._lines) + "\n"


async def _usage_by_model() -> list[dict]:
    """
    Agrégat requêtes + tokens par (modèle, statut) sur les dernières 24h.
    Requête scopée ici (lecture seule) pour peupler eva_requests_total /
    eva_tokens_total sans modifier database.py. Robuste : renvoie [] si la
    table est vide ou en cas d'erreur d'accès (jamais d'exception propagée).
    """
    try:
        async with db.get_db() as conn:
            rows = await (await conn.execute(
                """
                SELECT
                    model,
                    COALESCE(CAST(status_code AS TEXT), 'unknown') AS status,
                    COUNT(*)                            AS requests,
                    COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens
                FROM usage_log
                WHERE timestamp >= datetime('now', '-24 hours')
                GROUP BY model, status
                """
            )).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        log.exception("Agrégation usage par modèle (prometheus) indisponible")
        return []


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


def _shape_llama_metrics(raw: dict[str, float]) -> dict:
    """Projette les métriques Prometheus brutes llama-server sur nos clés JSON."""
    return {
        "kv_cache_usage_ratio": raw.get("llamacpp:kv_cache_usage_ratio"),
        "kv_cache_tokens": raw.get("llamacpp:kv_cache_tokens"),
        "requests_processing": raw.get("llamacpp:requests_processing"),
        "requests_deferred": raw.get("llamacpp:requests_deferred"),
        "tokens_per_second": raw.get("llamacpp:tokens_per_second"),
        "prompt_tokens_total": raw.get("llamacpp:prompt_tokens_total"),
        "tokens_predicted_total": raw.get("llamacpp:tokens_predicted_total"),
    }


async def _collect_llama_metrics_local() -> dict[str, dict]:
    """
    Collecte les métriques llama-server des modèles READY en mode local.
    Retourne {model_id: {clé: valeur|None}}. Vide en mode cluster (pas de
    _managers) ou si aucun modèle chargé. Jamais d'exception propagée.
    """
    result: dict[str, dict] = {}

    # Uniquement en mode local : en mode cluster, le manager n'a pas de pool
    # de sous-processus local (_managers).
    managers = getattr(model_manager, "_managers", None)
    if not managers:
        return {}

    ready_managers = [
        (mid, mgr)
        for mid, mgr in managers.items()
        if mgr.state == ModelState.READY
    ]
    if not ready_managers:
        return {}

    async with httpx.AsyncClient(timeout=3.0) as client:
        for model_id, mgr in ready_managers:
            try:
                resp = await client.get(
                    mgr.llama_url("/metrics"),
                    headers=mgr.auth_headers(),
                )
                if resp.status_code != 200:
                    continue
                result[model_id] = _shape_llama_metrics(_parse_prometheus(resp.text))
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
                pass
            except Exception:
                log.exception("Erreur métriques llama pour '%s'", model_id)

    return result


async def _collect_llama_metrics() -> dict[str, dict]:
    """
    Collecte les métriques llama-server, local OU cluster.

    - Local : interroge directement les sous-processus READY.
    - Cluster : délègue au ClusterManager (agrégation par nœud) si celui-ci
      expose `collect_llama_metrics()`. Sinon renvoie {} proprement.

    En cluster, les entrées sont taguées par node_id (via le champ "node" du
    dict retourné par le manager) pour éviter les collisions de model_id entre
    nœuds.
    """
    collector = getattr(model_manager, "collect_llama_metrics", None)
    if collector is not None:
        try:
            return await collector()
        except Exception:
            log.exception("Collecte métriques llama cluster indisponible")
            return {}
    return await _collect_llama_metrics_local()


@router.get("/llama")
async def metrics_llama(
    _: None = Depends(require_admin),
) -> dict:
    """
    Métriques temps réel de llama-server (format Prometheus → JSON).
    Interroge tous les modèles actuellement chargés (état READY).
    Retourne {} si aucun modèle n'est chargé ou en cas d'erreur.

    En mode cluster, l'agrégation est déléguée au ClusterManager et les entrées
    portent un champ "node" (node_id) — {} si l'agrégation n'est pas disponible.

    Métriques clés exposées par modèle :
      llamacpp:kv_cache_usage_ratio   — occupation du cache KV (0–1)
      llamacpp:requests_processing    — requêtes en cours d'inférence
      llamacpp:requests_deferred      — requêtes en attente de slot
      llamacpp:tokens_per_second      — vitesse de génération (tokens/s)
      llamacpp:prompt_tokens_total    — tokens prompt traités depuis démarrage
      llamacpp:tokens_predicted_total — tokens générés depuis démarrage
    """
    return await _collect_llama_metrics()


# ── Exposition Prometheus texte ────────────────────────────────────────────────

@router.get("/prometheus", response_class=PlainTextResponse)
async def metrics_prometheus(
    _: None = Depends(require_admin),
) -> PlainTextResponse:
    """
    Exposition au format texte Prometheus (version 0.0.4), généré à la main.

    Additif : ne remplace PAS les endpoints JSON existants. Robuste par
    construction — chaque source indisponible (aucun modèle, pas de nvidia-smi,
    mode cluster, DB vide) est silencieusement omise ou émise à 0, jamais 500.
    Ne divulgue AUCUN contenu de prompt (seulement des compteurs agrégés).
    """
    w = _PromWriter()

    # ── Requêtes & tokens par modèle (usage_log 24h) ──────────────────────────
    w.declare(
        "eva_requests_total", "counter",
        "Nombre de requêtes par modèle et code de statut (fenêtre 24h).",
    )
    w.declare(
        "eva_tokens_total", "counter",
        "Nombre de tokens par modèle et type (prompt|completion, fenêtre 24h).",
    )
    usage_rows = await _usage_by_model()
    for row in usage_rows:
        model = row.get("model") or "unknown"
        status = str(row.get("status") or "unknown")
        w.sample(
            "eva_requests_total",
            row.get("requests", 0),
            {"model": model, "status": status},
        )
    # Tokens agrégés par modèle (indépendants du statut) — regrouper d'abord.
    tokens_by_model: dict[str, dict[str, int]] = {}
    for row in usage_rows:
        model = row.get("model") or "unknown"
        acc = tokens_by_model.setdefault(model, {"prompt": 0, "completion": 0})
        acc["prompt"] += int(row.get("prompt_tokens", 0) or 0)
        acc["completion"] += int(row.get("completion_tokens", 0) or 0)
    for model, acc in tokens_by_model.items():
        w.sample("eva_tokens_total", acc["prompt"], {"model": model, "type": "prompt"})
        w.sample(
            "eva_tokens_total", acc["completion"],
            {"model": model, "type": "completion"},
        )

    # ── Latence (percentiles déjà calculés côté overview) ─────────────────────
    w.declare(
        "eva_request_latency_seconds", "gauge",
        "Percentiles de latence des requêtes (secondes, fenêtre 7j).",
    )
    try:
        samples = await db.get_latency_samples(period_hours=168)
        samples.sort()
        for q, p in (("0.5", 0.50), ("0.95", 0.95), ("0.99", 0.99)):
            ms = _percentile(samples, p)
            if ms is not None:
                w.sample(
                    "eva_request_latency_seconds", ms / 1000.0, {"quantile": q}
                )
    except Exception:
        log.exception("Percentiles latence (prometheus) indisponibles")

    # ── VRAM & modèles chargés (status() — même format local/cluster) ─────────
    try:
        status = model_manager.status()
    except Exception:
        log.exception("status() indisponible pour l'exposition prometheus")
        status = {"vram_budget": {}, "models": []}

    budget = status.get("vram_budget") or {}
    w.declare("eva_vram_used_gb", "gauge", "VRAM utilisée estimée (GB).")
    w.declare("eva_vram_total_gb", "gauge", "VRAM totale du budget (GB).")
    w.declare("eva_vram_available_gb", "gauge", "VRAM disponible estimée (GB).")
    w.sample("eva_vram_used_gb", budget.get("used_gb"))
    w.sample("eva_vram_total_gb", budget.get("total_gb"))
    w.sample("eva_vram_available_gb", budget.get("available_gb"))

    models = status.get("models") or []
    ready = sum(1 for m in models if m.get("state") == "ready")
    w.declare("eva_models_loaded", "gauge", "Nombre de modèles à l'état ready.")
    w.sample("eva_models_loaded", ready)

    # ── Métriques llama.cpp par modèle (local ou cluster) ─────────────────────
    llama = await _collect_llama_metrics()
    if llama:
        w.declare(
            "eva_llama_kv_cache_usage_ratio", "gauge",
            "Occupation du cache KV llama-server (0–1).",
        )
        w.declare(
            "eva_llama_tokens_per_second", "gauge",
            "Vitesse de génération llama-server (tokens/s).",
        )
        w.declare(
            "eva_llama_requests_processing", "gauge",
            "Requêtes en cours d'inférence sur llama-server.",
        )
        w.declare(
            "eva_llama_requests_deferred", "gauge",
            "Requêtes en attente de slot sur llama-server.",
        )
        for model_id, m in llama.items():
            if not isinstance(m, dict):
                continue
            labels = {"model": model_id}
            # Le collecteur cluster peut ajouter un node_id — le propager en label.
            node = m.get("node")
            if node:
                labels["node"] = str(node)
            w.sample(
                "eva_llama_kv_cache_usage_ratio",
                m.get("kv_cache_usage_ratio"), labels,
            )
            w.sample(
                "eva_llama_tokens_per_second", m.get("tokens_per_second"), labels
            )
            w.sample(
                "eva_llama_requests_processing",
                m.get("requests_processing"), labels,
            )
            w.sample(
                "eva_llama_requests_deferred", m.get("requests_deferred"), labels
            )

    return PlainTextResponse(
        content=w.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
