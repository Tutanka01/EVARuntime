"""
Couche base de données — SQLite avec WAL mode.
Suffisant pour une centaine d'utilisateurs universitaires.
On ne stocke jamais les clés API en clair, seulement leur hash SHA-256.
"""
from __future__ import annotations

import hashlib
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

import aiosqlite

from config import settings

# ── Schéma ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    username              TEXT    UNIQUE NOT NULL,
    email                 TEXT    UNIQUE,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    is_active             INTEGER NOT NULL DEFAULT 1,
    rpm_limit             INTEGER NOT NULL DEFAULT 20,
    monthly_token_limit   INTEGER NOT NULL DEFAULT 0,
    notes                 TEXT
);

CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- On ne stocke que le hash SHA-256, jamais la clé brute
    key_hash    TEXT    UNIQUE NOT NULL,
    -- 8 premiers caractères pour identification humaine (préfixe, pas secret)
    key_prefix  TEXT    NOT NULL,
    name        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    last_used   TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1,
    expires_at  TEXT
);

CREATE TABLE IF NOT EXISTS usage_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    api_key_id          INTEGER REFERENCES api_keys(id),
    timestamp           TEXT    NOT NULL DEFAULT (datetime('now')),
    model               TEXT    NOT NULL,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    duration_ms         INTEGER,
    status_code         INTEGER,
    request_id          TEXT
);

-- Index pour requêtes de rapport fréquentes
CREATE INDEX IF NOT EXISTS idx_usage_user_time   ON usage_log(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_timestamp   ON usage_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_keys_hash         ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_keys_user         ON api_keys(user_id);
"""

_PRAGMAS = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA cache_size   = -65536;
PRAGMA foreign_keys = ON;
PRAGMA temp_store   = MEMORY;
"""


# ── Connexion ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Context manager : connexion aiosqlite avec Row factory."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        yield db


async def init_db() -> None:
    """Crée les tables et applique les pragmas. Idempotent."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        for pragma in _PRAGMAS.strip().splitlines():
            pragma = pragma.strip()
            if pragma:
                await db.execute(pragma)
        await db.executescript(_SCHEMA)
        await db.commit()


# ── Helpers clés API ──────────────────────────────────────────────────────────

def generate_api_key() -> tuple[str, str, str]:
    """
    Génère une clé API.
    Retourne (raw_key, key_hash, key_prefix).
    Stocker uniquement key_hash et key_prefix, jamais raw_key.
    """
    raw = "llmgw-" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    # Le préfixe inclut "llmgw-" + 8 chars pour identification visuelle
    key_prefix = raw[:14]
    return raw, key_hash, key_prefix


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


# ── Helpers utilisateurs ──────────────────────────────────────────────────────

async def create_user(
    username: str,
    email: str | None = None,
    rpm_limit: int | None = None,
    monthly_token_limit: int | None = None,
    notes: str | None = None,
) -> dict:
    rpm = rpm_limit if rpm_limit is not None else settings.default_rpm_limit
    mtl = monthly_token_limit if monthly_token_limit is not None else settings.default_monthly_token_limit
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO users (username, email, rpm_limit, monthly_token_limit, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, email, rpm, mtl, notes),
        )
        await db.commit()
        row = await (await db.execute(
            "SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)
        )).fetchone()
        return dict(row)


async def get_user_by_username(username: str) -> dict | None:
    async with get_db() as db:
        row = await (await db.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        )).fetchone()
        return dict(row) if row else None


async def list_users() -> list[dict]:
    async with get_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        )).fetchall()
        return [dict(r) for r in rows]


async def update_user(user_id: int, **fields: object) -> dict | None:
    allowed = {"email", "is_active", "rpm_limit", "monthly_token_limit", "notes"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return None
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    async with get_db() as db:
        await db.execute(
            f"UPDATE users SET {set_clause} WHERE id = ?",
            (*updates.values(), user_id),
        )
        await db.commit()
        row = await (await db.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        )).fetchone()
        return dict(row) if row else None


# ── Helpers clés ──────────────────────────────────────────────────────────────

async def create_api_key(
    user_id: int,
    name: str | None = None,
    expires_at: str | None = None,
) -> tuple[str, dict]:
    """
    Retourne (raw_key, key_row).
    raw_key doit être montré à l'utilisateur UNE SEULE FOIS puis oublié.
    """
    raw, key_hash, key_prefix = generate_api_key()
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO api_keys (user_id, key_hash, key_prefix, name, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, key_hash, key_prefix, name, expires_at),
        )
        await db.commit()
        row = await (await db.execute(
            "SELECT * FROM api_keys WHERE id = ?", (cursor.lastrowid,)
        )).fetchone()
        return raw, dict(row)


async def lookup_key(raw_key: str) -> dict | None:
    """
    Lookup complet user+key depuis la clé brute.
    Retourne None si invalide, révoquée ou expirée.
    """
    key_hash = hash_key(raw_key)
    async with get_db() as db:
        row = await (await db.execute(
            """
            SELECT
                u.id             AS user_id,
                u.username,
                u.email,
                u.is_active      AS user_active,
                u.rpm_limit,
                u.monthly_token_limit,
                k.id             AS key_id,
                k.key_prefix,
                k.name           AS key_name,
                k.is_active      AS key_active,
                k.expires_at
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
            WHERE k.key_hash = ?
              AND k.is_active = 1
              AND u.is_active = 1
            """,
            (key_hash,),
        )).fetchone()

    if not row:
        return None

    row = dict(row)
    if row["expires_at"]:
        if row["expires_at"] < datetime.now(timezone.utc).isoformat():
            return None

    return row


async def revoke_key(key_prefix: str) -> bool:
    """Révoque toutes les clés ayant ce préfixe. Retourne True si au moins une révoquée."""
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE api_keys SET is_active = 0 WHERE key_prefix LIKE ?",
            (key_prefix + "%",),
        )
        await db.commit()
        return cursor.rowcount > 0


async def list_keys_for_user(user_id: int) -> list[dict]:
    async with get_db() as db:
        rows = await (await db.execute(
            """
            SELECT id, user_id, key_prefix, name, created_at, last_used, is_active, expires_at
            FROM api_keys
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        )).fetchall()
        return [dict(r) for r in rows]


async def delete_user(user_id: int) -> bool:
    """Supprime définitivement un utilisateur et toutes ses clés (CASCADE). Retourne True si supprimé."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await db.commit()
        return cursor.rowcount > 0


async def touch_key_last_used(key_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE api_keys SET last_used = datetime('now') WHERE id = ?",
            (key_id,),
        )
        await db.commit()


# ── Helpers usage ─────────────────────────────────────────────────────────────

async def log_usage(
    user_id: int,
    key_id: int | None,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    duration_ms: int,
    status_code: int,
    request_id: str,
) -> None:
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO usage_log
                (user_id, api_key_id, model,
                 prompt_tokens, completion_tokens, total_tokens,
                 duration_ms, status_code, request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, key_id, model,
                prompt_tokens, completion_tokens, prompt_tokens + completion_tokens,
                duration_ms, status_code, request_id,
            ),
        )
        await db.commit()


async def get_usage_report(
    user_id: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 1000,
) -> list[dict]:
    conditions: list[str] = []
    params: list[object] = []

    if user_id is not None:
        conditions.append("l.user_id = ?")
        params.append(user_id)
    if from_date:
        conditions.append("l.timestamp >= ?")
        params.append(from_date)
    if to_date:
        conditions.append("l.timestamp <= ?")
        params.append(to_date)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    async with get_db() as db:
        rows = await (await db.execute(
            f"""
            SELECT
                l.id, l.timestamp, l.model,
                l.prompt_tokens, l.completion_tokens, l.total_tokens,
                l.duration_ms, l.status_code, l.request_id,
                u.username
            FROM usage_log l
            JOIN users u ON u.id = l.user_id
            {where}
            ORDER BY l.timestamp DESC
            LIMIT ?
            """,
            params,
        )).fetchall()
        return [dict(r) for r in rows]


async def get_usage_summary(
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Agrégat par utilisateur pour le reporting mensuel."""
    conditions: list[str] = []
    params: list[object] = []

    if from_date:
        conditions.append("l.timestamp >= ?")
        params.append(from_date)
    if to_date:
        conditions.append("l.timestamp <= ?")
        params.append(to_date)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with get_db() as db:
        rows = await (await db.execute(
            f"""
            SELECT
                u.username,
                COUNT(*)              AS request_count,
                SUM(l.prompt_tokens)  AS total_prompt_tokens,
                SUM(l.completion_tokens) AS total_completion_tokens,
                SUM(l.total_tokens)   AS total_tokens,
                AVG(l.duration_ms)    AS avg_duration_ms,
                MIN(l.timestamp)      AS first_request,
                MAX(l.timestamp)      AS last_request
            FROM usage_log l
            JOIN users u ON u.id = l.user_id
            {where}
            GROUP BY u.id, u.username
            ORDER BY total_tokens DESC
            """,
            params,
        )).fetchall()
        return [dict(r) for r in rows]


# ── Dashboard analytics ───────────────────────────────────────────────────────

async def get_overview_stats() -> dict:
    """
    Agrégats KPI pour le dashboard :
    - Compteurs aujourd'hui et hier (pour Δ%)
    - Compteurs sur les dernières 24h
    - Utilisateurs actifs sur 7 jours
    - Taux d'erreur sur 24h
    """
    async with get_db() as db:
        row = await (await db.execute(
            """
            SELECT
                -- Aujourd'hui (UTC)
                COUNT(CASE WHEN date(timestamp) = date('now') THEN 1 END)
                    AS requests_today,
                COALESCE(SUM(CASE WHEN date(timestamp) = date('now')
                    THEN total_tokens END), 0)
                    AS tokens_today,
                COALESCE(SUM(CASE WHEN date(timestamp) = date('now')
                    THEN prompt_tokens END), 0)
                    AS prompt_tokens_today,
                COALESCE(SUM(CASE WHEN date(timestamp) = date('now')
                    THEN completion_tokens END), 0)
                    AS completion_tokens_today,

                -- Hier (pour calcul Δ%)
                COUNT(CASE WHEN date(timestamp) = date('now', '-1 day') THEN 1 END)
                    AS requests_yesterday,
                COALESCE(SUM(CASE WHEN date(timestamp) = date('now', '-1 day')
                    THEN total_tokens END), 0)
                    AS tokens_yesterday,

                -- 24 dernières heures
                COUNT(CASE WHEN timestamp >= datetime('now', '-24 hours') THEN 1 END)
                    AS requests_24h,
                COALESCE(SUM(CASE WHEN timestamp >= datetime('now', '-24 hours')
                    THEN total_tokens END), 0)
                    AS tokens_24h,

                -- Erreurs 24h (status != 200)
                COUNT(CASE WHEN timestamp >= datetime('now', '-24 hours')
                    AND status_code IS NOT NULL AND status_code != 200 THEN 1 END)
                    AS errors_24h,

                -- Latence moyenne 24h
                AVG(CASE WHEN timestamp >= datetime('now', '-24 hours')
                    AND duration_ms IS NOT NULL THEN duration_ms END)
                    AS avg_latency_24h_ms,

                -- Total utilisateurs ayant fait au moins une requête en 7j
                COUNT(DISTINCT CASE WHEN timestamp >= datetime('now', '-7 days')
                    THEN user_id END)
                    AS active_users_7d
            FROM usage_log
            """
        )).fetchone()

        total_users_row = await (await db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_active = 1"
        )).fetchone()

    result = dict(row)
    result["total_users"] = total_users_row["n"] if total_users_row else 0

    # Taux d'erreur 24h
    r24h = result["requests_24h"] or 0
    result["error_rate_24h"] = (
        round(result["errors_24h"] / r24h, 4) if r24h > 0 else 0.0
    )
    return result


async def get_timeseries(bucket: str = "hour", lookback_hours: int = 24) -> list[dict]:
    """
    Série temporelle agrégée par heure ou par jour.

    bucket : "hour" → GROUP BY heure  |  "day" → GROUP BY jour
    lookback_hours : fenêtre en heures (24, 168, 720…)
    """
    if bucket == "day":
        fmt = "%Y-%m-%d"
    else:
        fmt = "%Y-%m-%d %H:00"

    async with get_db() as db:
        rows = await (await db.execute(
            f"""
            SELECT
                strftime('{fmt}', timestamp)    AS bucket,
                COUNT(*)                         AS request_count,
                COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(total_tokens), 0)      AS total_tokens,
                COUNT(CASE WHEN status_code IS NOT NULL
                           AND status_code != 200 THEN 1 END) AS error_count,
                AVG(duration_ms)                 AS avg_latency_ms
            FROM usage_log
            WHERE timestamp >= datetime('now', ? || ' hours')
            GROUP BY bucket
            ORDER BY bucket ASC
            """,
            (f"-{lookback_hours}",),
        )).fetchall()
        return [dict(r) for r in rows]


async def get_user_period_stats(period_days: int = 30) -> list[dict]:
    """
    Statistiques par utilisateur sur une période donnée.
    Inclut les limites quota pour afficher les barres de progression.
    """
    async with get_db() as db:
        rows = await (await db.execute(
            """
            SELECT
                u.username,
                u.is_active,
                u.rpm_limit,
                u.monthly_token_limit,
                COUNT(l.id)                          AS request_count,
                COALESCE(SUM(l.total_tokens), 0)     AS total_tokens,
                COALESCE(SUM(l.prompt_tokens), 0)    AS prompt_tokens,
                COALESCE(SUM(l.completion_tokens), 0) AS completion_tokens,
                COUNT(CASE WHEN l.status_code IS NOT NULL
                           AND l.status_code != 200 THEN 1 END) AS error_count,
                AVG(l.duration_ms)                   AS avg_latency_ms,
                MAX(l.timestamp)                     AS last_request
            FROM users u
            LEFT JOIN usage_log l
                ON l.user_id = u.id
                AND l.timestamp >= datetime('now', ? || ' days')
            GROUP BY u.id, u.username
            ORDER BY total_tokens DESC
            """,
            (f"-{period_days}",),
        )).fetchall()
        return [dict(r) for r in rows]


async def get_status_code_stats(period_hours: int = 24) -> list[dict]:
    """Distribution des codes HTTP sur la période."""
    async with get_db() as db:
        rows = await (await db.execute(
            """
            SELECT
                COALESCE(CAST(status_code AS TEXT), 'unknown') AS status_code,
                COUNT(*) AS count
            FROM usage_log
            WHERE timestamp >= datetime('now', ? || ' hours')
            GROUP BY status_code
            ORDER BY count DESC
            """,
            (f"-{period_hours}",),
        )).fetchall()
        return [dict(r) for r in rows]


async def get_latency_samples(
    period_hours: int = 168,
    limit: int = 10_000,
) -> list[int]:
    """
    Retourne les durées brutes (ms) pour le calcul des percentiles en Python.
    On utilise l'index idx_usage_timestamp.
    """
    async with get_db() as db:
        rows = await (await db.execute(
            """
            SELECT duration_ms
            FROM usage_log
            WHERE timestamp >= datetime('now', ? || ' hours')
              AND duration_ms IS NOT NULL
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (f"-{period_hours}", limit),
        )).fetchall()
        return [r["duration_ms"] for r in rows]
