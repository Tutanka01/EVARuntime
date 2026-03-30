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
