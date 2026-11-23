"""
Chemin chaud SQLite : PRAGMAs par connexion et connexions partagées.

Couvre :
  - les PRAGMAs de session (synchronous, cache_size, temp_store, busy_timeout,
    foreign_keys, secure_delete) appliqués à CHAQUE connexion, dédiée comme
    partagée — ils n'étaient posés que sur la connexion de migration ;
  - la réutilisation des connexions partagées de `lookup_key`,
    `tokens_used_last_30_days`, `touch_key_last_used` et `log_usage`, leur
    indexation par (chemin, boucle), et l'absence de transaction laissée
    ouverte entre deux appels ;
  - la visibilité immédiate d'une révocation écrite par un autre processus
    (aucun cache d'authentification) ;
  - une écriture bloquée sur le verrou ne retarde pas l'authentification ;
  - un processus court (CLI, `asyncio.run`) qui ne ferme jamais la connexion
    partagée sort quand même ;
  - le lifespan ferme les connexions APRÈS le flush des tâches d'arrière-plan.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import database as db
from config import settings

GATEWAY_DIR = Path(__file__).resolve().parents[1]

EXPECTED_SESSION_PRAGMAS = {
    "synchronous": 1,        # NORMAL
    "cache_size": -65536,
    "temp_store": 2,         # MEMORY
    "busy_timeout": 5000,
    "foreign_keys": 1,
    "secure_delete": 1,
}


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "hot_path.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    return db_file


async def _seed() -> tuple[str, dict, dict]:
    await db.init_db()
    user = await db.create_user("hot", monthly_token_limit=1000)
    raw, key = await db.create_api_key(user["id"])
    return raw, user, key


async def _pragmas(conn) -> dict[str, int]:
    values = {}
    for name in EXPECTED_SESSION_PRAGMAS:
        rows = await conn.execute_fetchall(f"PRAGMA {name}")
        values[name] = int(rows[0][0])
    return values


def _thread_of(conn) -> threading.Thread:
    return getattr(conn, "_thread", conn)


# ── PRAGMAs de session ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_dedicated_connection_gets_performance_pragmas(temp_db) -> None:
    await db.init_db()
    async with db.get_db() as conn:
        assert await _pragmas(conn) == EXPECTED_SESSION_PRAGMAS


@pytest.mark.anyio
async def test_shared_connections_get_session_pragmas(temp_db) -> None:
    await db.init_db()
    await db.open_shared_db()
    try:
        for role in db._SHARED_ROLES:
            conn = await db._shared_db(role)
            assert await _pragmas(conn) == EXPECTED_SESSION_PRAGMAS, role
            assert conn.isolation_level is None, "autocommit attendu"
    finally:
        await db.close_shared_db()


# ── Réutilisation et isolation des connexions partagées ──────────────────────

@pytest.mark.anyio
async def test_hot_path_reuses_one_connection_per_role(temp_db) -> None:
    raw, user, key = await _seed()
    try:
        assert await db.lookup_key(raw) is not None
        read = await db._shared_db("read")
        await db.touch_key_last_used(key["id"])
        write = await db._shared_db("write")
        await db.log_usage(user["id"], key["id"], "m", 1, 2, 3, 200, "r1")
        assert await db.tokens_used_last_30_days(user["id"]) == 3
        quota = await db._shared_db("quota")
        assert await db.lookup_key(raw) is not None
        assert await db._shared_db("read") is read
        assert await db._shared_db("write") is write
        assert await db._shared_db("quota") is quota
        assert len({id(read), id(write), id(quota)}) == 3
    finally:
        await db.close_shared_db()


def _checkpoint_truncate(path) -> int:
    """
    `wal_checkpoint(TRUNCATE)` depuis une autre connexion, sans attente :
    renvoie 1 (busy) si un lecteur garde un instantané antérieur aux dernières
    trames du WAL — exactement ce que laisserait une lecture non consommée.
    """
    conn = sqlite3.connect(path, timeout=0)
    try:
        return int(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0])
    finally:
        conn.close()


def _external_write(path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE users SET notes = 'x' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.anyio
async def test_hot_path_leaves_no_read_snapshot_open(temp_db) -> None:
    """
    Aucune lecture partagée ne garde d'instantané ouvert entre deux appels :
    sinon une révocation écrite ensuite resterait invisible, et le WAL ne
    pourrait plus être tronqué. `in_transaction` ne voit pas un instantané de
    lecture : on vérifie la propriété elle-même, par un checkpoint TRUNCATE.
    """
    raw, user, key = await _seed()
    try:
        # Contrôle positif : un curseur non consommé sur la connexion de
        # lecture garde son instantané et bloque le checkpoint.
        read = await db._shared_db("read")
        cursor = await read.execute("SELECT id FROM api_keys UNION ALL SELECT id FROM users")
        assert await cursor.fetchone() is not None
        _external_write(temp_db)
        assert _checkpoint_truncate(temp_db) == 1, "le test doit voir un instantané ouvert"
        await cursor.close()
        assert _checkpoint_truncate(temp_db) == 0

        await asyncio.gather(
            db.lookup_key(raw),
            db.tokens_used_last_30_days(user["id"]),
            db.touch_key_last_used(key["id"]),
            db.log_usage(user["id"], key["id"], "m", 1, 2, 3, 200, "r1"),
            db.lookup_key(raw),
        )
        _external_write(temp_db)
        assert _checkpoint_truncate(temp_db) == 0
    finally:
        await db.close_shared_db()


@pytest.mark.anyio
async def test_revocation_by_another_process_is_visible_on_next_lookup(temp_db) -> None:
    """La CLI écrit depuis un autre processus : la clé révoquée est refusée tout de suite."""
    raw, _user, key = await _seed()
    try:
        assert await db.lookup_key(raw) is not None  # connexion partagée ouverte

        other = sqlite3.connect(temp_db)
        try:
            other.execute("UPDATE api_keys SET is_active = 0 WHERE id = ?", (key["id"],))
            other.commit()
        finally:
            other.close()

        assert await db.lookup_key(raw) is None
    finally:
        await db.close_shared_db()


async def _occupy(role: str) -> tuple[asyncio.Future, threading.Event]:
    """
    Occupe le thread de la connexion partagée `role` jusqu'à `gate.set()` :
    une fonction SQL qui bloque sur un Event. Simule, sans dépendre de la
    vitesse de la machine, une somme de quota longue ou une écriture en
    attente du verrou.
    """
    conn = await db._shared_db(role)
    gate = threading.Event()
    entered = threading.Event()

    def _block() -> int:
        entered.set()
        gate.wait(30)
        return 0

    await conn.create_function("block_until_released", 0, _block)
    busy = asyncio.ensure_future(conn.execute_fetchall("SELECT block_until_released()"))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    return busy, gate


@pytest.mark.anyio
@pytest.mark.parametrize("role", ["quota", "write"])
async def test_busy_quota_or_write_connection_does_not_delay_authentication(
    temp_db, role,
) -> None:
    """
    Le thread de la connexion « quota » (somme sur 30 jours) ou « write »
    (écriture en attente du verrou) est occupé : `lookup_key` répond quand
    même, AVANT la libération — il n'est pas en file derrière. Avec une
    connexion commune, il attendrait la libération (ordre, pas chronomètre).
    """
    raw, user, _key = await _seed()
    busy, gate = await _occupy(role)
    try:
        lookup = asyncio.ensure_future(db.lookup_key(raw))
        done, _ = await asyncio.wait({lookup, busy}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        assert lookup in done, "l'authentification attendait la connexion occupée"
        assert not busy.done()
        assert lookup.result() is not None
    finally:
        gate.set()
        await busy
        await db.close_shared_db()


@pytest.mark.anyio
async def test_quota_does_not_queue_behind_authentication_role(temp_db) -> None:
    """Symétrique : la connexion de lecture occupée ne bloque pas le quota."""
    _raw, user, _key = await _seed()
    busy, gate = await _occupy("read")
    try:
        quota = asyncio.ensure_future(db.tokens_used_last_30_days(user["id"]))
        done, _ = await asyncio.wait({quota, busy}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        assert quota in done and not busy.done()
        assert quota.result() == 0
    finally:
        gate.set()
        await busy
        await db.close_shared_db()


@pytest.mark.anyio
async def test_shared_connection_follows_db_path(temp_db, tmp_path, monkeypatch) -> None:
    """Changer `settings.db_path` (les tests le font) ne réutilise pas l'ancienne base."""
    raw, _user, _key = await _seed()
    try:
        first = await db._shared_db("read")
        assert await db.lookup_key(raw) is not None

        monkeypatch.setattr(settings, "db_path", tmp_path / "other.db")
        await db.init_db()
        assert await db.lookup_key(raw) is None
        assert await db._shared_db("read") is not first
        _thread_of(first).join(timeout=5)
        assert not _thread_of(first).is_alive(), "l'ancienne connexion est fermée"
    finally:
        await db.close_shared_db()


def test_shared_connection_is_not_reused_across_event_loops(temp_db) -> None:
    """Chaque `asyncio.run` (CLI, tests) obtient sa connexion ; l'ancienne s'arrête."""
    async def _connection():
        await db.init_db()
        return await db._shared_db("read")

    first = asyncio.run(_connection())
    second = asyncio.run(_connection())
    try:
        assert first is not second
        _thread_of(first).join(timeout=5)
        assert not _thread_of(first).is_alive()
    finally:
        asyncio.run(db.close_shared_db())


@pytest.mark.anyio
async def test_shared_connection_thread_is_daemon(temp_db) -> None:
    """
    Sans lifespan, personne ne ferme la connexion partagée : son thread doit
    être démon, sinon l'interpréteur l'attend indéfiniment à la sortie.
    Verrouille aussi l'API interne d'aiosqlite dont dépend `_daemonize`.
    """
    await db.init_db()
    try:
        for role in db._SHARED_ROLES:
            thread = _thread_of(await db._shared_db(role))
            assert isinstance(thread, threading.Thread)
            assert thread.daemon, role
    finally:
        await db.close_shared_db()


def test_short_lived_process_exits_without_closing_shared_connection(temp_db) -> None:
    """
    Une commande `asyncio.run` (forme de cli.py) qui touche au chemin chaud
    sans fermer la connexion partagée doit se terminer promptement.
    """
    script = (
        "import asyncio, database as db\n"
        "async def main():\n"
        "    await db.init_db()\n"
        "    user = await db.create_user('cli')\n"
        "    raw, key = await db.create_api_key(user['id'])\n"
        "    assert await db.lookup_key(raw) is not None\n"
        "    await db.touch_key_last_used(key['id'])\n"
        "    await db.log_usage(user['id'], key['id'], 'm', 1, 2, 3, 200, 'r')\n"
        "    print(await db.tokens_used_last_30_days(user['id']))\n"
        "asyncio.run(main())\n"
    )
    env = {**os.environ, "DB_PATH": str(temp_db)}
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=GATEWAY_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "3"


@pytest.mark.anyio
async def test_close_shared_db_is_idempotent_and_never_reopens_lazily(temp_db) -> None:
    """
    Après la fermeture, un appel retardataire de la même boucle passe par une
    connexion dédiée, refermée aussitôt : aucune connexion partagée n'est
    rouverte sans être jamais refermée. `open_shared_db` les rétablit.
    """
    raw, user, key = await _seed()
    first = await db._shared_db("read")
    await db.close_shared_db()
    await db.close_shared_db()
    assert db._shared == {}

    assert await db.lookup_key(raw) is not None
    await db.log_usage(user["id"], key["id"], "m", 1, 2, 3, 200, "straggler")
    assert await db.tokens_used_last_30_days(user["id"]) == 3
    assert db._shared == {}, "aucune réouverture paresseuse après fermeture"

    await db.open_shared_db()
    assert set(db._shared) == set(db._SHARED_ROLES)
    assert await db._shared_db("read") is not first
    await db.close_shared_db()


@pytest.mark.anyio
async def test_close_waits_for_operation_in_flight(temp_db) -> None:
    """
    Une ligne d'usage déjà en route sur la connexion d'écriture au moment de
    la fermeture est écrite, sans « Connection closed » : la fermeture attend
    la fin des opérations en cours.
    """
    _raw, user, key = await _seed()
    busy, gate = await _occupy("write")
    pending = asyncio.ensure_future(
        db.log_usage(user["id"], key["id"], "m", 4, 5, 9, 200, "in-flight")
    )
    for _ in range(5):
        await asyncio.sleep(0)
    closing = asyncio.ensure_future(db.close_shared_db())
    for _ in range(5):
        await asyncio.sleep(0)
    assert not closing.done(), "la fermeture attend l'opération en cours"

    gate.set()
    await busy
    await pending
    await closing
    assert await db.tokens_used_last_30_days(user["id"]) == 9


@pytest.mark.anyio
async def test_close_does_not_drop_an_operation_waiting_for_the_connection(temp_db) -> None:
    """
    Ligne d'usage partie alors que la connexion d'écriture s'ouvre encore :
    la fermeture ne doit ni annuler l'ouverture (ligne perdue SANS trace :
    une tâche annulée n'est pas journalisée par fire_and_forget), ni fermer
    la connexion sous elle.
    """
    _raw, user, key = await _seed()
    await db.close_shared_db()
    await db.open_shared_db()
    await db.close_shared_db()  # état de départ : aucune connexion partagée
    db._closed_loop = None      # …et la boucle n'est pas marquée fermée

    pending = asyncio.ensure_future(
        db.log_usage(user["id"], key["id"], "m", 2, 3, 5, 200, "opening")
    )
    await asyncio.sleep(0)  # la tâche attend maintenant l'ouverture
    assert db._shared["write"].users == 1
    assert not db._shared["write"].opening.done()

    await db.close_shared_db()
    await pending
    assert await db.tokens_used_last_30_days(user["id"]) == 5


@pytest.mark.anyio
async def test_close_timeout_is_logged(temp_db, caplog) -> None:
    """Une opération qui ne se termine pas n'est jamais perdue en silence."""
    _raw, user, key = await _seed()
    busy, gate = await _occupy("write")
    pending = asyncio.ensure_future(
        db.log_usage(user["id"], key["id"], "m", 1, 1, 2, 200, "stuck")
    )
    for _ in range(5):
        await asyncio.sleep(0)
    try:
        closing = asyncio.ensure_future(db.close_shared_db(timeout=0.05))

        async def _warned() -> None:
            while not any("encore en cours" in r.getMessage() for r in caplog.records):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(_warned(), timeout=10)
    finally:
        gate.set()
        await busy
        await pending  # déjà en file avant la fermeture : écrite quand même
        await closing
    assert await db.tokens_used_last_30_days(user["id"]) == 2


@pytest.mark.anyio
async def test_open_failure_surfaces_the_sqlite_error_even_if_stop_is_unavailable(
    temp_db, monkeypatch,
) -> None:
    """
    aiosqlite < 0.22 n'a pas `Connection.stop()` : l'arrêt de nettoyage ne
    doit jamais masquer l'erreur SQLite d'origine.
    """
    import aiosqlite

    await db.init_db()

    async def _failing_pragmas(conn) -> None:
        raise sqlite3.OperationalError("pragma refusé")

    monkeypatch.setattr(db, "_apply_session_pragmas", _failing_pragmas)
    monkeypatch.delattr(aiosqlite.Connection, "stop")
    with pytest.raises(sqlite3.OperationalError, match="pragma refusé"):
        await db.lookup_key("llmgw-absent")
    assert "read" not in db._shared


def test_aiosqlite_floor_matches_the_api_used() -> None:
    """`Connection.stop()` et `Connection._thread` : aiosqlite >= 0.22."""
    import aiosqlite

    requirement = next(
        line for line in (GATEWAY_DIR / "requirements.txt").read_text().splitlines()
        if line.startswith("aiosqlite")
    )
    assert requirement.split()[0] == "aiosqlite>=0.22.0"
    assert hasattr(aiosqlite.Connection, "stop")


@pytest.mark.anyio
async def test_failed_open_is_not_pinned(tmp_path, monkeypatch) -> None:
    """Une ouverture en échec (répertoire absent) est retentée au prochain appel."""
    target = tmp_path / "later" / "gateway.db"
    monkeypatch.setattr(settings, "db_path", target)
    with pytest.raises(sqlite3.OperationalError):
        await db.lookup_key("llmgw-absent")
    assert "read" not in db._shared

    await db.init_db()  # crée le répertoire et le schéma
    assert await db.lookup_key("llmgw-absent") is None
    await db.close_shared_db()


@pytest.mark.anyio
async def test_anonymization_still_runs_beside_shared_connections(temp_db) -> None:
    """Le travail transactionnel garde sa connexion dédiée et cohabite."""
    raw, user, _key = await _seed()
    try:
        assert await db.lookup_key(raw) is not None
        result = await db.anonymize_user(user["id"])
        assert result is not None and result["keys_revoked"] == 1
        assert await db.lookup_key(raw) is None
    finally:
        await db.close_shared_db()


# ── Lifespan ──────────────────────────────────────────────────────────────────

def test_lifespan_closes_shared_db_after_background_flush(temp_db, monkeypatch) -> None:
    """Les dernières lignes d'usage passent par la connexion partagée : fermeture APRÈS drain."""
    from fastapi.testclient import TestClient

    import main

    calls: list[str] = []
    real_drain = main.drain_pending
    real_close = db.close_shared_db
    real_open = db.open_shared_db

    async def _drain(deadline: float) -> int:
        calls.append("drain_pending")
        return await real_drain(deadline)

    async def _close() -> None:
        calls.append("close_shared_db")
        await real_close()

    async def _open() -> None:
        calls.append("open_shared_db")
        await real_open()

    monkeypatch.setattr(main, "drain_pending", _drain)
    monkeypatch.setattr(db, "close_shared_db", _close)
    monkeypatch.setattr(db, "open_shared_db", _open)

    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        assert set(db._shared) == set(db._SHARED_ROLES)

    assert calls == ["open_shared_db", "drain_pending", "close_shared_db"]
    assert db._shared == {}
