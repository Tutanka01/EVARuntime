"""
Tests de durcissement SQLite (student) : PRAGMAs de robustesse, index
idx_keys_user et purge de rétention. Base temporaire + monkeypatch db_path.
"""
from __future__ import annotations

import pytest

import database as db
from config import settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Pointe settings.db_path vers un fichier SQLite jetable et initialise le schéma."""
    db_file = tmp_path / "students_test.db"
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
async def test_idx_keys_user_exists(temp_db) -> None:
    """L'index idx_keys_user sur api_keys(user_id) existe après init_db."""
    await db.init_db()
    async with db.get_db() as conn:
        rows = await (await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'api_keys'"
        )).fetchall()
        names = {r["name"] for r in rows}
        assert "idx_keys_user" in names


@pytest.mark.anyio
async def test_purge_usage_removes_old_keeps_recent(temp_db) -> None:
    """purge_usage_older_than supprime l'ancien et conserve le récent."""
    await db.init_db()
    user = await db.create_user(username="etu1")
    uid = user["id"]

    async with db.get_db() as conn:
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
