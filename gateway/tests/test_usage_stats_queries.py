"""
Requêtes d'agrégat du dashboard : `get_overview_stats` borné à 7 jours.

Le dashboard rafraîchit l'aperçu toutes les 30 s par onglet ouvert. La requête
parcourait toute la table `usage_log` (70 ms sur 200 000 lignes) alors que sa
fenêtre la plus large est de 7 jours. On vérifie que les résultats sont
inchangés par rapport à la requête non bornée et que le plan passe par l'index
de timestamp au lieu d'un parcours complet.
"""
from __future__ import annotations

import sqlite3
import unittest.mock
from contextlib import asynccontextmanager

import pytest

import database as db
from config import settings

# Requête d'origine (avant borne), conservée comme référence de résultat.
_UNBOUNDED_OVERVIEW_SQL = """
SELECT
    COUNT(CASE WHEN date(timestamp) = date('now') THEN 1 END) AS requests_today,
    COALESCE(SUM(CASE WHEN date(timestamp) = date('now') THEN total_tokens END), 0) AS tokens_today,
    COALESCE(SUM(CASE WHEN date(timestamp) = date('now') THEN prompt_tokens END), 0) AS prompt_tokens_today,
    COALESCE(SUM(CASE WHEN date(timestamp) = date('now') THEN completion_tokens END), 0) AS completion_tokens_today,
    COUNT(CASE WHEN date(timestamp) = date('now', '-1 day') THEN 1 END) AS requests_yesterday,
    COALESCE(SUM(CASE WHEN date(timestamp) = date('now', '-1 day') THEN total_tokens END), 0) AS tokens_yesterday,
    COUNT(CASE WHEN timestamp >= datetime('now', '-24 hours') THEN 1 END) AS requests_24h,
    COALESCE(SUM(CASE WHEN timestamp >= datetime('now', '-24 hours') THEN total_tokens END), 0) AS tokens_24h,
    COUNT(CASE WHEN timestamp >= datetime('now', '-24 hours')
        AND status_code IS NOT NULL AND status_code != 200 THEN 1 END) AS errors_24h,
    AVG(CASE WHEN timestamp >= datetime('now', '-24 hours')
        AND duration_ms IS NOT NULL THEN duration_ms END) AS avg_latency_24h_ms,
    COUNT(DISTINCT CASE WHEN timestamp >= datetime('now', '-7 days') THEN user_id END) AS active_users_7d
FROM usage_log
"""

# (décalage, user, tokens prompt, tokens complétion, statut, durée)
_ROWS = [
    ("-5 minutes", 1, 10, 20, 200, 100),
    ("-2 hours", 2, 1, 2, 500, 300),
    ("-23 hours", 1, 3, 4, 200, None),
    ("-25 hours", 3, 5, 6, 429, 50),
    ("-36 hours", 2, 7, 8, 200, 70),
    ("-3 days", 4, 9, 9, 200, 10),
    ("-167 hours", 5, 1, 1, 200, 10),
    ("-169 hours", 6, 100, 100, 200, 10),
    ("-30 days", 7, 1000, 1000, 500, 10),
    ("-400 days", 8, 5000, 5000, 200, 10),
]


@pytest.fixture
def populated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "stats.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    return db_file


def _populate(path) -> None:
    conn = sqlite3.connect(path)
    try:
        for uid in range(1, 9):
            conn.execute("INSERT INTO users (id, username) VALUES (?, ?)", (uid, f"u{uid}"))
        for offset, uid, prompt, completion, status, duration in _ROWS:
            conn.execute(
                "INSERT INTO usage_log (user_id, timestamp, model, prompt_tokens, "
                "completion_tokens, total_tokens, duration_ms, status_code) "
                "VALUES (?, datetime('now', ?), 'm', ?, ?, ?, ?, ?)",
                (uid, offset, prompt, completion, prompt + completion, duration, status),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.anyio
async def test_overview_stats_match_unbounded_query(populated_db) -> None:
    await db.init_db()
    _populate(populated_db)

    stats = await db.get_overview_stats()

    conn = sqlite3.connect(populated_db)
    conn.row_factory = sqlite3.Row
    try:
        reference = dict(conn.execute(_UNBOUNDED_OVERVIEW_SQL).fetchone())
    finally:
        conn.close()
    for key, value in reference.items():
        assert stats[key] == value, key
    # Contrôle positif : la fixture exerce bien chaque fenêtre.
    assert stats["requests_24h"] == 3
    assert stats["errors_24h"] == 1
    assert stats["active_users_7d"] == 5
    assert stats["requests_today"] + stats["requests_yesterday"] >= 3


@pytest.mark.anyio
async def test_overview_stats_do_not_scan_the_whole_table(populated_db) -> None:
    """Le prédicat `timestamp >=` est sargable : recherche par index, pas SCAN."""
    await db.init_db()
    captured: list[str] = []

    real_get_db = db.get_db

    @asynccontextmanager
    async def _capturing_get_db():
        async with real_get_db() as conn:
            await conn.set_trace_callback(captured.append)
            yield conn

    with unittest.mock.patch.object(db, "get_db", _capturing_get_db):
        await db.get_overview_stats()

    overview_sql = next(sql for sql in captured if "active_users_7d" in sql)
    conn = sqlite3.connect(populated_db)
    try:
        plan = " ".join(row[-1] for row in conn.execute(f"EXPLAIN QUERY PLAN {overview_sql}"))
    finally:
        conn.close()
    assert "SEARCH usage_log USING" in plan and "idx_usage_timestamp" in plan, plan
    assert "SCAN usage_log" not in plan, plan
