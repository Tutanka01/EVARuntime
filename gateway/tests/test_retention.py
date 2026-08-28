"""
Tests de la rétention RGPD (audit 2026-08-28, ISSUE 3) :

  - purge bornée des sauvegardes `*.pre-migration.*.bak` produites par le
    moteur de migration — sans elle, une copie complète de la base (users,
    api_keys, usage_log) survivait indéfiniment à `anonymize_user`, vidant la
    garantie DEC-001 ;
  - rétention automatique de `usage_log` (`delete_usage_older_than`) ;
  - passe orchestrée `run_retention_pass` appelée par le lifespan.

La base est temporaire sur fichier + monkeypatch de `settings.db_path`,
comme `test_migrations.py`.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import database as db
from config import Settings, settings


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Base SQLite jetable sur fichier + pointage de settings.db_path."""
    db_file = tmp_path / "retention_test.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    return db_file


async def _init_and_seed(ages_days: list[int]) -> None:
    """Schéma à jour + un utilisateur + une ligne d'usage par âge donné."""
    await db.init_db()
    user = await db.create_user(username="alice", email="alice@univ-pau.fr")
    for i, days in enumerate(ages_days):
        stamp = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%d %H:%M:%S")
        async with db.get_db() as conn:
            await conn.execute(
                "INSERT INTO usage_log (user_id, model, total_tokens, timestamp) "
                "VALUES (?, 'm', 42, ?)",
                (user["id"], stamp),
            )
            await conn.commit()


async def _usage_count() -> int:
    async with db.get_db() as conn:
        row = await (await conn.execute("SELECT COUNT(*) FROM usage_log")).fetchone()
    return int(row[0])


# ── Purge des sauvegardes pre-migration ───────────────────────────────────────

def _cree_bak(dossier: Path, nom: str, age_secondes: int) -> Path:
    """Sauvegarde factice avec un mtime contrôlé (l'ancienneté décide de la purge)."""
    p = dossier / nom
    p.write_bytes(b"sqlite-backup-canari")
    stamp = datetime.now().timestamp() - age_secondes
    os.utime(p, (stamp, stamp))
    return p


def test_purge_garde_les_plus_recentes_et_rien_d_autre(tmp_path):
    """keep=2 sur 5 sauvegardes : les 2 plus récentes restent, les decoys aussi."""
    for i in range(5):
        _cree_bak(tmp_path, f"gateway.db.pre-migration.v1.{i}.bak", 10_000 * (i + 1))
    # Decoys qui NE doivent JAMAIS être touchés : autres familles de .bak,
    # archives du script de sauvegarde, base elle-même.
    decoys = [
        _cree_bak(tmp_path, "models.yaml.pre-admin.20260828T000000Z.bak", 1),
        _cree_bak(tmp_path, "models.yaml.pre-bootstrap.20260828T000000Z.bak", 1),
        _cree_bak(tmp_path, "gateway-20260101-000000.db", 1),
        _cree_bak(tmp_path, "autre-base.db.pre-migration.v1.old.bak", 1),
    ]

    purged = db.purge_pre_migration_backups(tmp_path / "gateway.db", keep=2)

    assert purged == 3
    restantes = sorted(p.name for p in tmp_path.glob("gateway.db.pre-migration.*.bak"))
    # Les deux plus récentes (âges 10 000 et 20 000 s) sont conservées.
    assert restantes == [
        "gateway.db.pre-migration.v1.0.bak",
        "gateway.db.pre-migration.v1.1.bak",
    ]
    for decoy in decoys:
        assert decoy.exists(), f"decoy purgé par erreur : {decoy.name}"


def test_purge_ne_descend_jamais_sous_une_sauvegarde(tmp_path):
    """
    keep=0 est borné à 1 : la procédure de rollback documentée
    (docs/architecture.md, « Migrations versionnées ») repose sur la
    sauvegarde la plus récente.
    """
    _cree_bak(tmp_path, "gateway.db.pre-migration.v1.1.bak", 100)
    _cree_bak(tmp_path, "gateway.db.pre-migration.v1.2.bak", 50)

    purged = db.purge_pre_migration_backups(tmp_path / "gateway.db", keep=0)

    assert purged == 1
    assert len(list(tmp_path.glob("gateway.db.pre-migration.*.bak"))) == 1


def test_purge_sans_sauvegarde_est_sans_effet(tmp_path):
    assert db.purge_pre_migration_backups(tmp_path / "gateway.db", keep=2) == 0


def test_migration_purge_les_sauvegardes_heritees(tmp_path, monkeypatch):
    """
    Bout en bout : une base legacy + 3 vieilles sauvegardes → la migration
    (v0 → courante) produit UNE nouvelle sauvegarde, puis la purge ramène le
    total à migration_backups_to_keep.
    """
    db_file = tmp_path / "gateway_legacy.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    monkeypatch.setattr(settings, "migration_backups_to_keep", 2)

    conn = sqlite3.connect(db_file)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT);"
        "INSERT INTO users (username) VALUES ('legacy');"
    )
    conn.commit()
    conn.close()
    for i in range(3):
        _cree_bak(
            tmp_path, f"gateway_legacy.db.pre-migration.v0.{i}.bak", 10_000 * (i + 1)
        )

    asyncio.run(db.init_db())

    restantes = list(tmp_path.glob("gateway_legacy.db.pre-migration.*.bak"))
    assert len(restantes) == 2, (
        f"attendu 2 sauvegardes après purge, trouvé : {sorted(p.name for p in restantes)}"
    )


# ── Rétention usage_log ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_delete_usage_older_than(temp_db):
    await _init_and_seed(ages_days=[10, 30, 400, 500])

    deleted = await db.delete_usage_older_than(365)

    assert deleted == 2
    assert await _usage_count() == 2


@pytest.mark.anyio
async def test_delete_usage_zero_jour_purge_tout_sauf_aujourdhui(temp_db):
    await _init_and_seed(ages_days=[0, 2])

    deleted = await db.delete_usage_older_than(1)

    assert deleted == 1
    assert await _usage_count() == 1


@pytest.mark.anyio
async def test_delete_usage_negatif_refuse(temp_db):
    await db.init_db()
    with pytest.raises(ValueError, match=">= 0"):
        await db.delete_usage_older_than(-1)


@pytest.mark.anyio
async def test_purge_usage_manuelle_supprime_et_fonctionne_hors_ligne(temp_db):
    """Le chemin CLI manuel (delete + VACUUM) reste opérationnel."""
    await _init_and_seed(ages_days=[400])

    deleted = await db.purge_usage_older_than(365)

    assert deleted == 1
    assert await _usage_count() == 0


# ── Passe orchestrée (lifespan) ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_run_retention_pass_combine_usage_et_backups(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "usage_retention_days", 365)
    monkeypatch.setattr(settings, "migration_backups_to_keep", 1)
    await _init_and_seed(ages_days=[10, 400, 500])
    for i in range(3):
        _cree_bak(temp_db.parent, f"{temp_db.name}.pre-migration.v1.{i}.bak", 10_000 * (i + 1))

    counts = await db.run_retention_pass(
        settings.usage_retention_days, settings.migration_backups_to_keep
    )

    assert counts == {"usage_deleted": 2, "backups_purged": 2}
    assert await _usage_count() == 1
    assert len(list(temp_db.parent.glob(f"{temp_db.name}.pre-migration.*.bak"))) == 1


@pytest.mark.anyio
async def test_run_retention_pass_desactive_usage_si_zero(temp_db, monkeypatch):
    """USAGE_RETENTION_DAYS=0 désactive la rétention usage_log (opt-out documenté)."""
    monkeypatch.setattr(settings, "usage_retention_days", 0)
    monkeypatch.setattr(settings, "migration_backups_to_keep", 5)
    await _init_and_seed(ages_days=[400])
    _cree_bak(temp_db.parent, f"{temp_db.name}.pre-migration.v1.1.bak", 100)

    counts = await db.run_retention_pass(
        settings.usage_retention_days, settings.migration_backups_to_keep
    )

    assert counts == {"usage_deleted": 0, "backups_purged": 0}
    assert await _usage_count() == 1  # la ligne ancienne est conservée


# ── Validation des réglages ───────────────────────────────────────────────────

def _settings(monkeypatch, **env: str) -> Settings:
    """Construit un Settings depuis de vraies variables d'environnement."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


@pytest.mark.parametrize("valeur", [-1, -365])
def test_usage_retention_days_negatif_refuse(monkeypatch, valeur):
    with pytest.raises(Exception, match="usage_retention_days"):
        _settings(
            monkeypatch,
            ADMIN_SECRET="secret-admin-de-test-1234567890abcd",
            INTERNAL_API_KEY="secret-interne-de-test-1234567890abcd",
            USAGE_RETENTION_DAYS=str(valeur),
        )


def test_migration_backups_to_keep_zero_refuse(monkeypatch):
    with pytest.raises(Exception, match="migration_backups_to_keep"):
        _settings(
            monkeypatch,
            ADMIN_SECRET="secret-admin-de-test-1234567890abcd",
            INTERNAL_API_KEY="secret-interne-de-test-1234567890abcd",
            MIGRATION_BACKUPS_TO_KEEP="0",
        )


@pytest.mark.parametrize(
    "champ, valeur, attendu",
    [
        ("USAGE_RETENTION_DAYS", "0", 0),  # 0 = désactivé, valeur légale
        ("USAGE_RETENTION_DAYS", "30", 30),
        ("MIGRATION_BACKUPS_TO_KEEP", "1", 1),
        ("MIGRATION_BACKUPS_TO_KEEP", "5", 5),
    ],
)
def test_reglages_retention_legaux(monkeypatch, champ, valeur, attendu):
    s = _settings(
        monkeypatch,
        ADMIN_SECRET="secret-admin-de-test-1234567890abcd",
        INTERNAL_API_KEY="secret-interne-de-test-1234567890abcd",
        **{champ: valeur},
    )
    cle = champ.lower()
    assert getattr(s, cle) == attendu
