"""
Tests de durcissement SQLite : PRAGMAs de robustesse et purge de rétention.
Base temporaire (tmp_path) + monkeypatch de settings.db_path.
"""
from __future__ import annotations

import pytest

import database as db
from config import settings


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Pointe settings.db_path vers un fichier SQLite jetable et initialise le schéma."""
    db_file = tmp_path / "gateway_test.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    return db_file


@pytest.mark.anyio
async def test_busy_timeout_applied(temp_db) -> None:
    """busy_timeout est bien positionné (> 0) sur une connexion get_db()."""
    await db.init_db()
    async with db.get_db() as conn:
        row = await (await conn.execute("PRAGMA busy_timeout")).fetchone()
        assert int(row[0]) == 5000


@pytest.mark.anyio
async def test_wal_pragmas_applied(temp_db) -> None:
    """journal_size_limit et wal_autocheckpoint sont appliqués."""
    await db.init_db()
    async with db.get_db() as conn:
        size_limit = await (await conn.execute("PRAGMA journal_size_limit")).fetchone()
        autockpt = await (await conn.execute("PRAGMA wal_autocheckpoint")).fetchone()
        assert int(size_limit[0]) == 67108864
        assert int(autockpt[0]) == 1000


@pytest.mark.anyio
async def test_purge_usage_removes_old_keeps_recent(temp_db) -> None:
    """purge_usage_older_than supprime l'ancien et conserve le récent."""
    await db.init_db()
    user = await db.create_user(username="alice")
    uid = user["id"]

    async with db.get_db() as conn:
        # Ligne ancienne (100 jours) et ligne récente (1 jour).
        await conn.execute(
            """
            INSERT INTO usage_log (user_id, model, timestamp, total_tokens)
            VALUES (?, 'm', datetime('now', '-100 days'), 10)
            """,
            (uid,),
        )
        await conn.execute(
            """
            INSERT INTO usage_log (user_id, model, timestamp, total_tokens)
            VALUES (?, 'm', datetime('now', '-1 days'), 20)
            """,
            (uid,),
        )
        await conn.commit()

    deleted = await db.purge_usage_older_than(30)
    assert deleted == 1

    async with db.get_db() as conn:
        row = await (await conn.execute("SELECT COUNT(*) AS n FROM usage_log")).fetchone()
        assert row["n"] == 1
