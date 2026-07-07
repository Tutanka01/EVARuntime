from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    is_active INTEGER NOT NULL DEFAULT 1,
    rpm_limit INTEGER NOT NULL,
    daily_token_limit INTEGER NOT NULL,
    hourly_token_limit INTEGER NOT NULL DEFAULT 0,
    concurrent_stream_limit INTEGER NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash TEXT UNIQUE NOT NULL,
    key_prefix TEXT NOT NULL,
    name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    api_key_id INTEGER REFERENCES api_keys(id),
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    status_code INTEGER,
    request_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_keys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_log(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_log(timestamp);
"""

# Migrations appliquées après la création du schema initial.
_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN hourly_token_limit INTEGER NOT NULL DEFAULT 0",
]


# PRAGMAs de robustesse appliqués à CHAQUE connexion (pas seulement à l'init) :
# - busy_timeout : attend au lieu d'échouer immédiatement en « database is locked ».
# - wal_autocheckpoint / journal_size_limit : bornent la croissance du WAL.
# Réglages de session légers, compatibles WAL (aucun changement de mode).
_SESSION_PRAGMAS = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA wal_autocheckpoint = 1000",
    "PRAGMA journal_size_limit = 67108864",
)


async def _apply_session_pragmas(db: aiosqlite.Connection) -> None:
    """Applique les PRAGMAs de session/robustesse sur une connexion ouverte."""
    for pragma in _SESSION_PRAGMAS:
        await db.execute(pragma)


@asynccontextmanager
async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        await _apply_session_pragmas(db)
        yield db


async def init_db() -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA synchronous = NORMAL")
        await _apply_session_pragmas(db)
        await db.executescript(SCHEMA)
        for migration in _MIGRATIONS:
            try:
                await db.execute(migration)
            except sqlite3.OperationalError as exc:
                # On n'ignore QUE l'ajout d'une colonne déjà existante ; toute
                # autre erreur de migration doit remonter.
                if "duplicate column" not in str(exc).lower():
                    raise
                logger.debug("Migration ignorée (colonne déjà existante) : %s", exc)
        await db.commit()


# ---------------------------------------------------------------------------
# Clés API
# ---------------------------------------------------------------------------

def generate_api_key() -> tuple[str, str, str]:
    raw = "llmstu-" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    key_prefix = raw[:15]
    return raw, key_hash, key_prefix


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# CRUD utilisateurs
# ---------------------------------------------------------------------------

async def create_user(
    username: str,
    email: str | None = None,
    rpm_limit: int | None = None,
    daily_token_limit: int | None = None,
    hourly_token_limit: int | None = None,
    concurrent_stream_limit: int | None = None,
    notes: str | None = None,
) -> dict:
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO users
                (username, email, rpm_limit, daily_token_limit, hourly_token_limit,
                 concurrent_stream_limit, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                email,
                rpm_limit or settings.default_rpm_limit,
                daily_token_limit or settings.default_daily_token_limit,
                hourly_token_limit if hourly_token_limit is not None else settings.default_hourly_token_limit,
                concurrent_stream_limit or settings.default_concurrent_stream_limit,
                notes,
            ),
        )
        await db.commit()
        return await get_user(cursor.lastrowid)


async def get_user(user_id: int) -> dict:
    async with get_db() as db:
        row = await (await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))).fetchone()
        if not row:
            raise LookupError(f"Étudiant introuvable: {user_id}")
        return dict(row)


async def get_user_by_username(username: str) -> dict | None:
    async with get_db() as db:
        row = await (await db.execute("SELECT * FROM users WHERE username = ?", (username,))).fetchone()
        return dict(row) if row else None


async def list_users() -> list[dict]:
    async with get_db() as db:
        rows = await (await db.execute(
            """
            SELECT u.*,
                   MAX(k.last_used) AS last_api_call,
                   COUNT(k.id) AS key_count
            FROM users u
            LEFT JOIN api_keys k ON k.user_id = u.id AND k.is_active = 1
            GROUP BY u.id
            ORDER BY u.username
            """
        )).fetchall()
        return [dict(row) for row in rows]


async def update_user_quotas(
    user_id: int,
    rpm_limit: int | None = None,
    daily_token_limit: int | None = None,
    hourly_token_limit: int | None = None,
    concurrent_stream_limit: int | None = None,
) -> dict:
    updates: dict[str, int] = {}
    if rpm_limit is not None:
        updates["rpm_limit"] = rpm_limit
    if daily_token_limit is not None:
        updates["daily_token_limit"] = daily_token_limit
    if hourly_token_limit is not None:
        updates["hourly_token_limit"] = hourly_token_limit
    if concurrent_stream_limit is not None:
        updates["concurrent_stream_limit"] = concurrent_stream_limit
    if not updates:
        return await get_user(user_id)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [user_id]
    async with get_db() as db:
        await db.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
        await db.commit()
    return await get_user(user_id)


async def set_user_active(user_id: int, is_active: bool) -> None:
    async with get_db() as db:
        await db.execute("UPDATE users SET is_active = ? WHERE id = ?", (int(is_active), user_id))
        await db.commit()


async def delete_user(user_id: int) -> None:
    """Suppression RGPD — cascade sur les clés et les logs."""
    async with get_db() as db:
        await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await db.commit()


# ---------------------------------------------------------------------------
# Clés API — CRUD
# ---------------------------------------------------------------------------

async def create_api_key(user_id: int, name: str | None, expires_at: str) -> tuple[str, dict]:
    raw, key_hash, key_prefix = generate_api_key()
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO api_keys (user_id, key_hash, key_prefix, name, expires_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, key_hash, key_prefix, name, expires_at),
        )
        await db.commit()
        row = await (await db.execute("SELECT * FROM api_keys WHERE id = ?", (cursor.lastrowid,))).fetchone()
        return raw, dict(row)


async def lookup_key(raw_key: str) -> dict | None:
    key_hash = hash_key(raw_key)
    async with get_db() as db:
        row = await (await db.execute(
            """
            SELECT
                u.id AS user_id, u.username, u.email, u.is_active AS user_active,
                u.rpm_limit, u.daily_token_limit, u.hourly_token_limit,
                u.concurrent_stream_limit,
                k.id AS key_id, k.key_prefix, k.name AS key_name, k.expires_at
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
            WHERE k.key_hash = ? AND k.is_active = 1 AND u.is_active = 1
            """,
            (key_hash,),
        )).fetchone()

    if not row:
        return None
    result = dict(row)
    try:
        expires_str = result["expires_at"]
        expires_dt = datetime.fromisoformat(expires_str)
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=timezone.utc)
        if expires_dt < datetime.now(timezone.utc):
            return None
    except (ValueError, TypeError):
        return None
    return result


async def touch_key_last_used(key_id: int) -> None:
    async with get_db() as db:
        await db.execute("UPDATE api_keys SET last_used = datetime('now') WHERE id = ?", (key_id,))
        await db.commit()


async def revoke_key(key_prefix: str) -> bool:
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE api_keys SET is_active = 0 WHERE key_prefix LIKE ?",
            (key_prefix + "%",),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_user_keys(user_id: int) -> list[dict]:
    async with get_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Usage & quotas
# ---------------------------------------------------------------------------

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
                (user_id, api_key_id, model, prompt_tokens, completion_tokens,
                 total_tokens, duration_ms, status_code, request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, key_id, model, prompt_tokens, completion_tokens,
                prompt_tokens + completion_tokens, duration_ms, status_code, request_id,
            ),
        )
        await db.commit()


async def tokens_used_today(user_id: int) -> int:
    async with get_db() as db:
        row = await (await db.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS total
            FROM usage_log
            WHERE user_id = ? AND date(timestamp) = date('now')
            """,
            (user_id,),
        )).fetchone()
        return int(row["total"] or 0)


async def tokens_used_last_minutes(user_id: int, minutes: int) -> int:
    async with get_db() as db:
        row = await (await db.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS total
            FROM usage_log
            WHERE user_id = ? AND timestamp >= datetime('now', ? || ' minutes')
            """,
            (user_id, f"-{minutes}"),
        )).fetchone()
        return int(row["total"] or 0)


async def purge_usage_older_than(days: int) -> int:
    """
    Purge de rétention MANUELLE (opt-in) des entrées `usage_log` plus anciennes
    que `days` jours, puis `VACUUM` complet pour restituer l'espace disque.

    Le `timestamp` est stocké en UTC au format SQLite (`datetime('now')`), donc la
    comparaison à `datetime('now', '-N days')` est correcte. Retourne le nombre de
    lignes supprimées. Aucune suppression automatique n'est déclenchée ailleurs ;
    à exécuter hors ligne (VACUUM verrouille la base).
    """
    if days < 0:
        raise ValueError("days doit être >= 0")
    async with get_db() as db:
        cursor = await db.execute(
            "DELETE FROM usage_log WHERE timestamp < datetime('now', ? || ' days')",
            (f"-{days}",),
        )
        deleted = cursor.rowcount
        await db.commit()
        # VACUUM complet (pas de PRAGMA incremental_vacuum : auto_vacuum n'est pas
        # activé, et on ne le change pas sur une base existante).
        await db.execute("VACUUM")
        return deleted


# ---------------------------------------------------------------------------
# Admin : stats et rapports
# ---------------------------------------------------------------------------

async def get_global_stats() -> dict:
    """Stats du jour et des 7 derniers jours."""
    async with get_db() as db:
        today = dict(await (await db.execute(
            """
            SELECT
                COUNT(*) AS requests,
                COALESCE(SUM(prompt_tokens), 0)      AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0)  AS completion_tokens,
                COALESCE(SUM(total_tokens), 0)       AS total_tokens,
                COUNT(DISTINCT user_id)              AS active_users,
                COALESCE(ROUND(AVG(duration_ms)), 0) AS avg_duration_ms
            FROM usage_log
            WHERE date(timestamp) = date('now')
            """
        )).fetchone())

        week = dict(await (await db.execute(
            """
            SELECT
                COUNT(*) AS requests,
                COALESCE(SUM(total_tokens), 0)  AS total_tokens,
                COUNT(DISTINCT user_id)         AS active_users
            FROM usage_log
            WHERE timestamp >= datetime('now', '-7 days')
            """
        )).fetchone())

        models = [
            dict(r) for r in await (await db.execute(
                """
                SELECT model,
                       COUNT(*) AS requests,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM usage_log
                WHERE date(timestamp) = date('now')
                GROUP BY model
                ORDER BY requests DESC
                """
            )).fetchall()
        ]

        total_active = dict(await (await db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_active = 1"
        )).fetchone())

        return {
            "today": today,
            "week": week,
            "models_today": models,
            "total_active_users": int(total_active["n"] or 0),
        }


async def get_usage_report(days: int = 7, user_id: int | None = None) -> list[dict]:
    """Classement par tokens sur N jours, filtrable par utilisateur."""
    where_user = "AND u.id = ?" if user_id is not None else ""
    params: list = [f"-{days} days"]
    if user_id is not None:
        params.append(user_id)
    async with get_db() as db:
        rows = await (await db.execute(
            f"""
            SELECT
                u.id           AS user_id,
                u.username,
                u.email,
                COUNT(*)                             AS request_count,
                COALESCE(SUM(l.prompt_tokens), 0)    AS prompt_tokens,
                COALESCE(SUM(l.completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(l.total_tokens), 0)     AS total_tokens,
                COALESCE(ROUND(AVG(l.duration_ms)), 0) AS avg_duration_ms,
                MAX(l.timestamp)                     AS last_seen
            FROM usage_log l
            JOIN users u ON u.id = l.user_id
            WHERE l.timestamp >= datetime('now', ?)
              {where_user}
            GROUP BY u.id, u.username
            ORDER BY total_tokens DESC
            """,
            params,
        )).fetchall()
        return [dict(row) for row in rows]


async def get_expiring_keys(within_days: int = 30) -> list[dict]:
    """Clés actives qui expirent dans les N prochains jours."""
    async with get_db() as db:
        rows = await (await db.execute(
            """
            SELECT k.*, u.username, u.email
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
            WHERE k.is_active = 1
              AND datetime(k.expires_at) >= datetime('now')
              AND datetime(k.expires_at) <= datetime('now', ? || ' days')
            ORDER BY k.expires_at
            """,
            (f"+{within_days}",),
        )).fetchall()
        return [dict(row) for row in rows]


async def get_all_keys_overview() -> list[dict]:
    """Vue admin : toutes les clés avec état et étudiant."""
    async with get_db() as db:
        rows = await (await db.execute(
            """
            SELECT k.*, u.username, u.email
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
            ORDER BY u.username, k.expires_at
            """
        )).fetchall()
        return [dict(row) for row in rows]
