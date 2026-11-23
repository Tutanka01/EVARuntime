"""
Tests du moteur de migration SQLite versionné (OPS-006).

Couvre : base neuve, base « legacy » en `user_version = 0`, idempotence de
`init_db()`, refus d'une base plus récente que le code, échec transactionnel
sans corruption, politique de sauvegarde préalable, et recréation de table
(motif attendu par COR-002).

Base temporaire (tmp_path) + monkeypatch de settings.db_path, comme
`test_database_hardening.py`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import database as db
from config import settings


# Schéma tel qu'il existait AVANT OPS-006 : créé par `CREATE TABLE IF NOT
# EXISTS`, donc sans `user_version`, et sans l'index `idx_keys_user` ajouté plus
# tard — une base réellement déployée peut se trouver dans cet état.
LEGACY_SCHEMA = """
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
    key_hash    TEXT    UNIQUE NOT NULL,
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

CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_log(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_keys_hash      ON api_keys(key_hash);
"""


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Pointe settings.db_path vers un fichier SQLite jetable (schéma non créé)."""
    db_file = tmp_path / "gateway_test.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    return db_file


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_legacy_db(path: Path) -> None:
    """Fabrique une base au schéma d'avant OPS-006, avec des données réelles."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO users (username, email, rpm_limit) VALUES ('legacy', 'l@x', 7)"
        )
        conn.execute(
            "INSERT INTO usage_log (user_id, model, total_tokens) VALUES (1, 'm', 42)"
        )
        conn.commit()
        # Une base d'avant OPS-006 n'a jamais estampillé user_version.
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        conn.close()


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()


def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.pre-migration.*"))


# ── Déclaration des migrations ────────────────────────────────────────────────

def test_migrations_are_ordered_unique_and_start_at_one() -> None:
    """Les versions déclarées sont uniques, croissantes et démarrent à 1."""
    versions = [m.version for m in db.MIGRATIONS]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert versions[0] == 1
    assert db.SCHEMA_VERSION == versions[-1]


# ── Base neuve ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_new_db_reaches_target_version_with_full_schema(temp_db) -> None:
    """Base neuve : schéma complet et user_version = version max connue."""
    await db.init_db()

    assert _user_version(temp_db) == db.SCHEMA_VERSION
    assert {"users", "api_keys", "usage_log"} <= _table_names(temp_db)

    async with db.get_db() as conn:
        rows = await (await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
        )).fetchall()
    names = {r["name"] for r in rows}
    assert {
        "idx_usage_user_time_tokens", "idx_usage_timestamp", "idx_keys_hash", "idx_keys_user",
    } <= names
    # Préfixe strict de l'index couvrant : supprimé par la migration 3.
    assert "idx_usage_user_time" not in names


@pytest.mark.anyio
async def test_new_db_is_writable_after_migration(temp_db) -> None:
    """Le schéma migré est fonctionnel (insertion réelle via les helpers)."""
    await db.init_db()
    user = await db.create_user(username="alice", email="alice@x")
    await db.log_usage(user["id"], None, "m", 10, 5, 12, 200, "req-1")
    assert await db.tokens_used_last_30_days(user["id"]) == 15


# ── Base legacy (user_version = 0) ────────────────────────────────────────────

@pytest.mark.anyio
async def test_legacy_db_is_migrated_and_data_preserved(temp_db) -> None:
    """Base legacy : estampillée à la version cible, données intactes."""
    _make_legacy_db(temp_db)

    await db.init_db()

    assert _user_version(temp_db) == db.SCHEMA_VERSION

    user = await db.get_user_by_username("legacy")
    assert user is not None
    assert user["email"] == "l@x"
    assert user["rpm_limit"] == 7

    async with db.get_db() as conn:
        row = await (await conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS t FROM usage_log"
        )).fetchone()
        assert row["t"] == 42

        indexes = await (await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'api_keys'"
        )).fetchall()
    # L'index absent de la base legacy est créé par la migration de baseline.
    assert "idx_keys_user" in {r["name"] for r in indexes}


@pytest.mark.anyio
async def test_foreign_keys_are_on_after_migration(temp_db) -> None:
    """Les FK sont désactivées PENDANT la migration, jamais après."""
    _make_legacy_db(temp_db)
    await db.init_db()
    async with db.get_db() as conn:
        row = await (await conn.execute("PRAGMA foreign_keys")).fetchone()
        assert int(row[0]) == 1


# ── Idempotence ───────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_init_db_twice_migrates_once(temp_db) -> None:
    """Deux appels consécutifs : pas d'erreur, une seule migration effective."""
    _make_legacy_db(temp_db)

    await db.init_db()
    assert len(_backups(temp_db)) == 1

    await db.init_db()
    assert _user_version(temp_db) == db.SCHEMA_VERSION
    # Aucune seconde sauvegarde : la deuxième passe n'a rien migré.
    assert len(_backups(temp_db)) == 1


@pytest.mark.anyio
async def test_init_db_repeated_on_new_db_is_stable(temp_db) -> None:
    """init_db() répété sur une base neuve reste à la version cible."""
    await db.init_db()
    await db.init_db()
    await db.init_db()
    assert _user_version(temp_db) == db.SCHEMA_VERSION


@pytest.mark.anyio
async def test_migration_already_applied_is_skipped_under_lock(temp_db) -> None:
    """
    Relecture de user_version sous verrou : une migration déjà appliquée par un
    autre processus n'est pas rejouée (cas service + CLI démarrés en parallèle).
    """
    await db.init_db()

    executed: list[str] = []

    async def _apply(conn) -> None:  # pragma: no cover — ne doit pas être appelé
        executed.append("python")

    already_applied = db.Migration(
        version=db.SCHEMA_VERSION,
        description="migration deja appliquee",
        statements=("CREATE TABLE canary_race (x INTEGER)",),
        apply=_apply,
    )

    async with db.get_db() as conn:
        await db._apply_migration(conn, already_applied)

    assert executed == []
    assert "canary_race" not in _table_names(temp_db)
    assert _user_version(temp_db) == db.SCHEMA_VERSION


# ── Sauvegarde préalable ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_backup_created_when_migration_applies(temp_db) -> None:
    """Migration réelle sur base existante → sauvegarde nommée avec la version source."""
    _make_legacy_db(temp_db)

    await db.init_db()

    backups = _backups(temp_db)
    assert len(backups) == 1
    backup = backups[0]
    assert ".pre-migration.v0." in backup.name

    # La sauvegarde est l'état AVANT migration : données présentes, version 0.
    conn = sqlite3.connect(backup)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    # Jamais plus largement accessible que la base elle-même.
    backup_mode = backup.stat().st_mode & 0o777
    db_mode = temp_db.stat().st_mode & 0o777
    assert backup_mode & 0o077 == 0
    assert backup_mode & ~db_mode == 0


@pytest.mark.anyio
async def test_no_backup_when_db_is_up_to_date(temp_db) -> None:
    """Aucune copie inutile : ni pour une base neuve, ni pour une base à jour."""
    await db.init_db()
    assert _backups(temp_db) == []

    await db.init_db()
    assert _backups(temp_db) == []


# ── Base plus récente que le code ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_db_newer_than_code_is_refused(temp_db) -> None:
    """user_version > version max connue → erreur explicite, base intacte."""
    await db.init_db()
    future = db.SCHEMA_VERSION + 5
    conn = sqlite3.connect(temp_db)
    try:
        conn.execute(f"PRAGMA user_version = {future}")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(db.MigrationError) as excinfo:
        await db.init_db()

    message = str(excinfo.value)
    assert str(future) in message
    assert "rollback applicatif" in message
    # Ni dégradation de version, ni sauvegarde parasite.
    assert _user_version(temp_db) == future
    assert _backups(temp_db) == []


# ── Échec de migration ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_failing_migration_rolls_back_and_keeps_version(temp_db, monkeypatch) -> None:
    """Migration qui lève → transaction annulée, user_version inchangée."""
    await db.init_db()
    before = _user_version(temp_db)

    async def _boom(conn) -> None:
        # Écriture volontaire AVANT l'échec : elle doit disparaître au rollback.
        await conn.execute("CREATE TABLE canary_python (x INTEGER)")
        raise RuntimeError("échec simulé de migration")

    failing = db.Migration(
        version=before + 1,
        description="migration factice qui echoue",
        statements=("CREATE TABLE canary_sql (x INTEGER)",),
        apply=_boom,
    )
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + (failing,))

    with pytest.raises(db.MigrationError) as excinfo:
        await db.init_db()
    assert "échec simulé de migration" in str(excinfo.value)

    assert _user_version(temp_db) == before
    tables = _table_names(temp_db)
    assert "canary_sql" not in tables
    assert "canary_python" not in tables

    conn = sqlite3.connect(temp_db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()

    # La base reste exploitable une fois la migration fautive retirée.
    monkeypatch.setattr(db, "MIGRATIONS", tuple(m for m in db.MIGRATIONS if m is not failing))
    await db.init_db()
    assert _user_version(temp_db) == db.SCHEMA_VERSION


@pytest.mark.anyio
async def test_failing_migration_keeps_previous_migrations_applied(temp_db, monkeypatch) -> None:
    """Une migration en échec n'annule pas celles qui ont déjà été validées."""
    _make_legacy_db(temp_db)

    async def _boom(conn) -> None:
        raise RuntimeError("deuxieme etape en echec")

    failing = db.Migration(
        version=db.SCHEMA_VERSION + 1,
        description="deuxieme migration factice",
        apply=_boom,
    )
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + (failing,))

    with pytest.raises(db.MigrationError):
        await db.init_db()

    # La baseline (version SCHEMA_VERSION) est validée, la suivante non.
    assert _user_version(temp_db) == db.SCHEMA_VERSION


# ── Recréation de table (ergonomie attendue par COR-002) ──────────────────────

@pytest.mark.anyio
async def test_rebuild_table_migration_changes_foreign_key(temp_db, monkeypatch) -> None:
    """
    Une migration peut recréer `usage_log` pour changer sa contrainte de clé
    étrangère : données préservées, index recréés, nouvelle contrainte active.

    C'est exactement le cas d'usage de COR-002 ; ce test verrouille l'ergonomie.
    """
    await db.init_db()
    user = await db.create_user(username="alice")
    await db.log_usage(user["id"], None, "m", 10, 5, 12, 200, "req-1")

    async def _apply(conn) -> None:
        await db._rebuild_table(
            conn,
            table="usage_log",
            create_new_sql="""
                CREATE TABLE usage_log__new (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id             INTEGER NOT NULL
                                        REFERENCES users(id) ON DELETE CASCADE,
                    api_key_id          INTEGER
                                        REFERENCES api_keys(id) ON DELETE SET NULL,
                    timestamp           TEXT    NOT NULL DEFAULT (datetime('now')),
                    model               TEXT    NOT NULL,
                    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
                    completion_tokens   INTEGER NOT NULL DEFAULT 0,
                    total_tokens        INTEGER NOT NULL DEFAULT 0,
                    duration_ms         INTEGER,
                    status_code         INTEGER,
                    request_id          TEXT
                )
            """,
            copy_columns=(
                "id", "user_id", "api_key_id", "timestamp", "model",
                "prompt_tokens", "completion_tokens", "total_tokens",
                "duration_ms", "status_code", "request_id",
            ),
            index_statements=(
                "CREATE INDEX idx_usage_user_time ON usage_log(user_id, timestamp)",
                "CREATE INDEX idx_usage_timestamp ON usage_log(timestamp)",
            ),
        )

    migration = db.Migration(
        version=db.SCHEMA_VERSION + 1,
        description="recreation de usage_log avec ON DELETE CASCADE",
        apply=_apply,
        check_foreign_keys=True,
    )
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + (migration,))

    await db.init_db()
    assert _user_version(temp_db) == migration.version

    async with db.get_db() as conn:
        row = await (await conn.execute("SELECT COUNT(*) AS n FROM usage_log")).fetchone()
        assert row["n"] == 1

        indexes = await (await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'usage_log'"
        )).fetchall()
        assert {"idx_usage_user_time", "idx_usage_timestamp"} <= {r["name"] for r in indexes}

        # La nouvelle contrainte est réellement appliquée : le CASCADE fonctionne.
        await conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        await conn.commit()
        row = await (await conn.execute("SELECT COUNT(*) AS n FROM usage_log")).fetchone()
        assert row["n"] == 0


# ── Migration 3 : index couvrant du quota ─────────────────────────────────────

_QUOTA_SQL = (
    "SELECT COALESCE(SUM(total_tokens), 0) FROM usage_log "
    "WHERE user_id = ? AND timestamp >= datetime('now', '-30 days')"
)


def _schema_objects(path: Path) -> list[tuple]:
    """Objets du schéma (hors tables internes), pour comparer deux bases."""
    conn = sqlite3.connect(path)
    try:
        return sorted(
            conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
        )
    finally:
        conn.close()


def _usage_indexes(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'usage_log' AND name LIKE 'idx_%'"
            )
        }
    finally:
        conn.close()


async def _init_at_version(monkeypatch, version: int) -> None:
    """Amène la base à `version` exactement, comme un déploiement antérieur."""
    full = db.MIGRATIONS
    monkeypatch.setattr(db, "MIGRATIONS", tuple(m for m in full if m.version <= version))
    try:
        await db.init_db()
    finally:
        monkeypatch.setattr(db, "MIGRATIONS", full)


def test_migration_3_is_declared_last_and_is_pure_sql() -> None:
    migration = db.MIGRATIONS[2]
    assert migration.version == 3
    assert migration.apply is None
    # Une instruction par entrée (jamais de script) : `execute()` un par un.
    assert all(";" not in statement for statement in migration.statements)


@pytest.mark.anyio
async def test_migration_3_replaces_quota_index_on_existing_db(temp_db, monkeypatch) -> None:
    """Base en version 2 avec données : index couvrant créé, ancien supprimé, sauvegarde prise."""
    await _init_at_version(monkeypatch, 2)
    assert _user_version(temp_db) == 2
    assert "idx_usage_user_time" in _usage_indexes(temp_db)
    user = await db.create_user(username="quota")
    await db.log_usage(user["id"], None, "m", 10, 5, 12, 200, "req-1")
    await db.close_shared_db()

    await db.init_db()

    assert _user_version(temp_db) == 3
    indexes = _usage_indexes(temp_db)
    assert "idx_usage_user_time_tokens" in indexes
    assert "idx_usage_user_time" not in indexes
    backups = _backups(temp_db)
    assert len(backups) == 1 and ".pre-migration.v2." in backups[0].name
    assert await db.tokens_used_last_30_days(user["id"]) == 15
    await db.close_shared_db()


@pytest.mark.anyio
async def test_migration_3_is_idempotent(temp_db, monkeypatch) -> None:
    await _init_at_version(monkeypatch, 2)
    await db.init_db()
    await db.init_db()
    assert _user_version(temp_db) == 3
    assert len(_backups(temp_db)) == 1


@pytest.mark.anyio
async def test_fresh_and_migrated_databases_have_identical_schema(
    tmp_path, monkeypatch,
) -> None:
    """Base neuve, base migrée depuis v2 et base legacy aboutissent au même schéma."""
    fresh = tmp_path / "fresh.db"
    from_v2 = tmp_path / "from_v2.db"
    legacy = tmp_path / "legacy.db"

    monkeypatch.setattr(settings, "db_path", fresh)
    await db.init_db()

    monkeypatch.setattr(settings, "db_path", from_v2)
    await _init_at_version(monkeypatch, 2)
    await db.init_db()

    _make_legacy_db(legacy)
    monkeypatch.setattr(settings, "db_path", legacy)
    await db.init_db()

    assert _schema_objects(fresh) == _schema_objects(from_v2)
    # La base legacy garde le texte SQL de ses propres CREATE (espacement,
    # colonne ajoutée par ALTER) : on compare ses objets, pas leur texte.
    assert [o[:3] for o in _schema_objects(legacy)] == [o[:3] for o in _schema_objects(fresh)]


@pytest.mark.anyio
async def test_quota_query_is_answered_from_the_covering_index(temp_db) -> None:
    """Le SUM du quota ne lit plus la table : plan « USING COVERING INDEX »."""
    await db.init_db()
    conn = sqlite3.connect(temp_db)
    try:
        plan = " ".join(
            row[-1] for row in conn.execute(f"EXPLAIN QUERY PLAN {_QUOTA_SQL}", (1,))
        )
    finally:
        conn.close()
    assert "COVERING INDEX idx_usage_user_time_tokens" in plan, plan
