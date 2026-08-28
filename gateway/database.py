"""
Couche base de données — SQLite avec WAL mode.
Suffisant pour une centaine d'utilisateurs universitaires.
On ne stocke jamais les clés API en clair, seulement leur hash SHA-256.

Le schéma est versionné par `PRAGMA user_version` et amené à niveau par le
moteur de migration de ce module (section « Migrations versionnées ») : voir
`docs/architecture.md` pour la procédure d'ajout d'une migration et le
comportement opérateur en cas d'échec.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import stat
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Awaitable, Callable, Sequence

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)

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
    notes                 TEXT,
    -- Horodatage d'anonymisation (COR-002 / DEC-001). NULL = utilisateur
    -- ordinaire. Non NULL = données personnelles effacées définitivement.
    -- Distingue un compte anonymisé d'un compte simplement désactivé, et rend
    -- l'opération idempotente. Déclarée en dernière position, la place
    -- qu'`ALTER TABLE ADD COLUMN` lui donne sur une base migrée, ce qui garde
    -- une base neuve et une base migrée strictement identiques.
    anonymized_at         TEXT
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

# PRAGMAs de robustesse appliqués à CHAQUE connexion (pas seulement à l'init) :
# - busy_timeout : attend au lieu d'échouer immédiatement en « database is locked ».
# - wal_autocheckpoint / journal_size_limit : bornent la croissance du WAL.
# Ce sont des réglages de session, légers, compatibles WAL (aucun changement de mode).
_SESSION_PRAGMAS = """
PRAGMA foreign_keys        = ON;
PRAGMA busy_timeout        = 5000;
PRAGMA wal_autocheckpoint  = 1000;
PRAGMA journal_size_limit  = 67108864;
"""


# ── Connexion ─────────────────────────────────────────────────────────────────

async def _apply_session_pragmas(db: aiosqlite.Connection) -> None:
    """Applique les PRAGMAs de session/robustesse sur une connexion ouverte."""
    for pragma in _SESSION_PRAGMAS.strip().splitlines():
        pragma = pragma.strip()
        if pragma:
            await db.execute(pragma)


@asynccontextmanager
async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Context manager : connexion aiosqlite avec Row factory."""
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        await _apply_session_pragmas(db)
        yield db


# ── Migrations versionnées (OPS-006) ──────────────────────────────────────────
#
# Un `CREATE TABLE IF NOT EXISTS` n'atteint JAMAIS une base déjà créée : tout
# changement de contrainte (clé étrangère, NOT NULL, UNIQUE…) exige une
# migration explicite. Le versionnement repose sur `PRAGMA user_version` :
#
#   user_version = 0  → base neuve, ou base déployée avant ce mécanisme ;
#   user_version = N  → les migrations 1..N ont été appliquées.
#
# Invariants du moteur :
#   - une migration s'applique entièrement ou pas du tout, et `user_version`
#     n'avance qu'avec elle (même transaction) ;
#   - une sauvegarde est prise avant la première migration réellement
#     applicable, jamais si la base est déjà à jour ;
#   - une base plus récente que le code est refusée (rollback applicatif) au
#     lieu d'être « migrée » à l'aveugle ;
#   - toute erreur remonte : fail-closed, le service ne démarre pas sur une
#     base à moitié migrée.


class MigrationError(RuntimeError):
    """Échec de migration du schéma SQLite. Doit empêcher le démarrage du service."""


@dataclass(frozen=True)
class Migration:
    """
    Une étape de migration, déclarative et ordonnée.

    version
        `user_version` atteint après application. Strictement croissant.
    description
        Résumé opérationnel, journalisé. Ne doit contenir aucune donnée
        personnelle ni secret.
    statements
        Instructions SQL exécutées dans l'ordre, une par appel `execute()`.
        On n'utilise volontairement PAS `executescript()` : sqlite3 valide
        implicitement la transaction en cours avant de l'exécuter, ce qui
        casserait l'atomicité de la migration.
    apply
        Fonction Python exécutée après `statements`, dans la MÊME transaction.
        Nécessaire dès qu'une migration doit inspecter la base ou appliquer le
        motif « create table new → copy → drop old → rename » que SQLite impose
        pour changer une contrainte (cf. `_rebuild_table`).
    check_foreign_keys
        Lance `PRAGMA foreign_key_check` avant le COMMIT et échoue s'il reste
        des violations. À activer pour toute migration qui recrée une table.
        Laissé à False par défaut : une base historique peut porter des
        violations préexistantes, sans lien avec la migration en cours, et le
        démarrage du service ne doit pas en dépendre.
    """

    version: int
    description: str
    statements: tuple[str, ...] = ()
    apply: Callable[[aiosqlite.Connection], Awaitable[None]] | None = None
    check_foreign_keys: bool = False


def _split_statements(script: str) -> tuple[str, ...]:
    """
    Découpe un script DDL en instructions individuelles, exécutables une à une.

    Le schéma de ce module ne contient ni trigger ni littéral comportant un
    « ; » : le découpage naïf est donc sûr. Les lignes de commentaire sont
    retirées AVANT le découpage — un « ; » de ponctuation dans un commentaire
    français tronquerait sinon l'instruction qui l'entoure.
    """
    without_comments = "\n".join(
        line for line in script.splitlines() if not line.strip().startswith("--")
    )
    return tuple(
        chunk.strip() for chunk in without_comments.split(";") if chunk.strip()
    )


async def _rebuild_table(
    db: aiosqlite.Connection,
    table: str,
    create_new_sql: str,
    copy_columns: Sequence[str],
    index_statements: Sequence[str] = (),
) -> None:
    """
    Recrée `table` avec une nouvelle définition — seul moyen de changer une
    contrainte en SQLite (`ALTER TABLE` ne sait pas modifier une clé étrangère).

    Séquence appliquée : création de `<table>__new` → copie des `copy_columns`
    → `DROP` de l'ancienne table → renommage → recréation des index.

    `create_new_sql` doit créer la table sous le nom `<table>__new`. Les index
    de l'ancienne table disparaissent avec le `DROP` : `index_statements` doit
    les redéclarer.

    Les clés étrangères sont déjà désactivées par le moteur de migration
    (`PRAGMA foreign_keys = OFF`, positionné hors transaction) : sans cela le
    `DROP TABLE` casserait les références. Déclarer `check_foreign_keys=True`
    sur la migration pour faire vérifier l'intégrité avant le COMMIT.

    Les identifiants passés ici sont des littéraux du code, jamais des entrées
    utilisateur : l'interpolation de `table` dans le SQL est volontaire et sûre.
    """
    new_table = f"{table}__new"
    columns = ", ".join(copy_columns)
    await db.execute(create_new_sql)
    await db.execute(
        f"INSERT INTO {new_table} ({columns}) SELECT {columns} FROM {table}"
    )
    await db.execute(f"DROP TABLE {table}")
    await db.execute(f"ALTER TABLE {new_table} RENAME TO {table}")
    for statement in index_statements:
        await db.execute(statement)


async def _ensure_anonymized_at(db: aiosqlite.Connection) -> None:
    """
    Garantit la présence de `users.anonymized_at` (COR-002).

    La colonne fait partie de `_SCHEMA` : une base neuve la reçoit déjà par la
    migration de baseline, tandis qu'une base déployée avant COR-002 ne l'a pas.
    On inspecte donc l'état réel via `PRAGMA table_info` plutôt que de le
    supposer, et l'`ALTER TABLE` n'est émis que si la colonne manque — sans quoi
    SQLite rejetterait un « duplicate column name » sur base neuve.

    Aucune recréation de table n'est nécessaire : la politique DEC-001 conserve
    la ligne `users`, donc la clé étrangère `usage_log.user_id → users(id)`
    n'est jamais violée et sa contrainte reste inchangée.
    """
    rows = await (await db.execute("PRAGMA table_info(users)")).fetchall()
    if any(row["name"] == "anonymized_at" for row in rows):
        logger.debug("Colonne users.anonymized_at déjà présente.")
        return
    await db.execute("ALTER TABLE users ADD COLUMN anonymized_at TEXT")


# Liste ordonnée des migrations. Ajouter une entrée en fin de tuple, jamais
# réécrire une entrée déjà livrée (une base en production l'a déjà appliquée).
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        description="Baseline : tables users/api_keys/usage_log et index de reporting",
        # Toutes les instructions sont en `IF NOT EXISTS` : sur une base
        # préexistante (user_version = 0) cette migration ne fait qu'estampiller
        # la version, sans toucher aux données.
        statements=_split_statements(_SCHEMA),
    ),
    Migration(
        version=2,
        description="Colonne users.anonymized_at (politique d'anonymisation DEC-001)",
        apply=_ensure_anonymized_at,
    ),
)

# Version de schéma attendue par ce code.
SCHEMA_VERSION = MIGRATIONS[-1].version


def _target_version() -> int:
    """Version cible = version de la dernière migration déclarée."""
    return MIGRATIONS[-1].version if MIGRATIONS else 0


async def _read_user_version(db: aiosqlite.Connection) -> int:
    row = await (await db.execute("PRAGMA user_version")).fetchone()
    return int(row[0]) if row else 0


async def _write_user_version(db: aiosqlite.Connection, version: int) -> None:
    # Un PRAGMA n'accepte pas de paramètre lié ; la valeur est un entier validé
    # issu des migrations déclarées dans ce module, jamais d'une entrée externe.
    await db.execute(f"PRAGMA user_version = {int(version)}")


def _backup_path(db_path: Path, from_version: int) -> Path:
    """Chemin de sauvegarde dérivé de la base, incluant la version d'origine."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return db_path.with_name(
        f"{db_path.name}.pre-migration.v{from_version}.{stamp}.bak"
    )


async def _backup_database(
    db: aiosqlite.Connection, db_path: Path, from_version: int
) -> Path:
    """
    Sauvegarde cohérente de la base via l'API de sauvegarde SQLite.

    On n'utilise pas une copie de fichier : en mode WAL, le fichier principal
    seul est incomplet. `Connection.backup()` produit une image cohérente,
    WAL inclus.

    Le fichier est créé en 0600 AVANT toute écriture (O_EXCL : on n'écrase
    jamais une sauvegarde existante), puis restreint à ce que la base autorise
    déjà — une sauvegarde ne doit jamais être plus largement accessible que
    l'original.
    """
    target = _backup_path(db_path, from_version)
    os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    try:
        async with aiosqlite.connect(target) as dest:
            await db.backup(dest)
        # Restriction appliquée après l'écriture : un mode source en lecture
        # seule (0400) empêcherait sinon d'écrire la sauvegarde.
        os.chmod(target, stat.S_IMODE(db_path.stat().st_mode) & 0o600)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def purge_pre_migration_backups(db_path: Path, keep: int = 2) -> int:
    """
    Borne la rétention des sauvegardes `*.pre-migration.*.bak` (RGPD / OPS-002).

    Ces sauvegardes contiennent une copie complète de la base (users, api_keys,
    usage_log) : sans purge, elles survivent indéfiniment à `anonymize_user`
    et DEC-001 ne garantit plus rien — une copie personnelle datée traîne
    à côté de la base. La purge ne touche QUE le motif
    `<nom_db>.pre-migration.` : jamais les sauvegardes `.pre-admin.` /
    `.pre-bootstrap.` du registre, jamais les archives du script de sauvegarde.

    Les `keep` sauvegardes les plus récentes sont conservées (au moins une,
    même si `keep` vaut moins : la procédure de rollback documentée repose sur
    la plus récente). Retourne le nombre de fichiers supprimés.
    """
    keep = max(1, keep)
    prefix = f"{db_path.name}.pre-migration."
    candidates = sorted(
        (p for p in db_path.parent.glob(f"{prefix}*.bak") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for stale in candidates[keep:]:
        try:
            stale.unlink()
            removed += 1
        except OSError:
            # Best-effort : un fichier retenu (permission, antivirus) ne doit
            # pas empêcher le démarrage ; il sera retenté à la prochaine passe.
            logger.warning("Purge impossible de la sauvegarde %s.", stale)
    return removed


async def _apply_migration(db: aiosqlite.Connection, migration: Migration) -> None:
    """
    Applique une migration de façon atomique : SQL, hook Python et
    `user_version` sont validés ensemble ou annulés ensemble.
    """
    logger.info(
        "Migration SQLite %d — %s", migration.version, migration.description
    )
    await db.execute("BEGIN IMMEDIATE")
    try:
        # Relecture sous verrou d'écriture : un autre processus (service et CLI
        # démarrés en parallèle) a pu appliquer la migration entre la lecture
        # initiale de `user_version` et l'obtention du verrou. Sans ce contrôle,
        # une migration de recréation de table serait rejouée.
        if await _read_user_version(db) >= migration.version:
            await db.execute("COMMIT")
            logger.info(
                "Migration SQLite %d déjà appliquée par un autre processus, ignorée.",
                migration.version,
            )
            return
        for statement in migration.statements:
            await db.execute(statement)
        if migration.apply is not None:
            await migration.apply(db)
        if migration.check_foreign_keys:
            violations = await (
                await db.execute("PRAGMA foreign_key_check")
            ).fetchall()
            if violations:
                raise MigrationError(
                    f"Migration {migration.version} : {len(violations)} violation(s) "
                    "de clé étrangère détectée(s) avant validation."
                )
        await _write_user_version(db, migration.version)
        await db.execute("COMMIT")
    except BaseException as exc:
        try:
            await db.execute("ROLLBACK")
        except Exception:  # pragma: no cover — transaction déjà refermée
            logger.warning("ROLLBACK impossible après échec de migration.")
        logger.error(
            "Migration SQLite %d échouée, transaction annulée : %s",
            migration.version,
            exc,
        )
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(
            f"Migration {migration.version} ({migration.description}) échouée : {exc}"
        ) from exc
    logger.info(
        "Migration SQLite %d appliquée (user_version = %d).",
        migration.version,
        migration.version,
    )


async def _migrate(db_path: Path) -> None:
    """
    Amène la base au niveau `SCHEMA_VERSION`.

    Idempotent : si la base est déjà à jour, aucune transaction de migration
    n'est ouverte et aucune sauvegarde n'est produite.
    """
    # L'état d'existence est évalué AVANT la connexion : aiosqlite crée le
    # fichier à l'ouverture, ce qui rendrait le test inopérant ensuite.
    pre_existing = db_path.is_file() and db_path.stat().st_size > 0

    # isolation_level=None : contrôle manuel des transactions. Par défaut
    # sqlite3 ouvre et valide des transactions implicites qui interfèrent avec
    # les BEGIN/COMMIT explicites du moteur de migration.
    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        db.row_factory = aiosqlite.Row
        for pragma in _PRAGMAS.strip().splitlines():
            pragma = pragma.strip()
            if pragma:
                await db.execute(pragma)
        await _apply_session_pragmas(db)

        current = await _read_user_version(db)
        target = _target_version()

        if current > target:
            raise MigrationError(
                f"Base SQLite en version de schéma {current}, code compatible "
                f"jusqu'à {target} : rollback applicatif détecté. Redéployer une "
                "version du code au moins égale au schéma, ou restaurer une "
                "sauvegarde antérieure. Aucune modification effectuée."
            )

        pending = [m for m in MIGRATIONS if m.version > current]
        if not pending:
            logger.debug(
                "Schéma SQLite déjà en version %d, aucune migration à appliquer.",
                current,
            )
            return

        if pre_existing:
            backup = await _backup_database(db, db_path, current)
            logger.info(
                "Sauvegarde avant migration (version %d) : %s", current, backup
            )
        else:
            logger.info(
                "Base SQLite absente ou vide : création du schéma en version %d.",
                target,
            )

        # `PRAGMA foreign_keys` est silencieusement IGNORÉ dans une transaction :
        # il doit donc être basculé ici, hors transaction, pour toute la série de
        # migrations. C'est une nécessité du motif de recréation de table
        # (`_rebuild_table`), dont le `DROP TABLE` casserait sinon les
        # références. L'état est restauré à ON quoi qu'il arrive.
        await db.execute("PRAGMA foreign_keys = OFF")
        try:
            for migration in pending:
                await _apply_migration(db, migration)
        finally:
            await db.execute("PRAGMA foreign_keys = ON")

        # Borne la rétention des sauvegardes produites (RGPD / OPS-002) —
        # uniquement après un succès complet : en cas d'échec, toutes les
        # sauvegardes de la série restent disponibles pour le rollback.
        purged = purge_pre_migration_backups(
            db_path, settings.migration_backups_to_keep
        )
        if purged:
            logger.info(
                "Rétention des sauvegardes pre-migration : %d fichier(s) purgé(s).",
                purged,
            )


async def init_db() -> None:
    """
    Crée ou migre le schéma et applique les pragmas. Idempotent.

    Fail-closed : toute erreur de migration remonte sous forme de
    `MigrationError` et empêche le démarrage plutôt que de laisser le service
    tourner sur une base à moitié migrée.
    """
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    await _migrate(settings.db_path)


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

# Préfixe du pseudonyme attribué par `anonymize_user()`. Le « : » est refusé par
# le motif de `UserCreate` (`^[a-zA-Z0-9_.-]+$`) : l'API admin ne peut donc pas
# créer un nom entrant en collision avec un compte anonymisé.
ANONYMIZED_USERNAME_PREFIX = "anonymized-user:"


def is_anonymized(user: dict) -> bool:
    """True si la ligne utilisateur porte un horodatage d'anonymisation."""
    return bool(user.get("anonymized_at"))


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
        # Parsing réel plutôt que comparaison lexicale : les formats ISO 8601
        # ("2026-01-01", "2026-01-01 12:00:00", avec/sans timezone) ne se
        # comparent pas correctement en tant que chaînes. Fail-closed si le
        # format est invalide.
        try:
            expires = datetime.fromisoformat(str(row["expires_at"]))
        except ValueError:
            return None
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
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


async def anonymize_user(user_id: int) -> dict | None:
    """
    Anonymise un utilisateur — chemin du droit à l'effacement RGPD (COR-002).

    Politique DEC-001 : la ligne `users` est CONSERVÉE, ses données personnelles
    sont effacées, le compte est désactivé et toutes ses clés sont révoquées.

    Pourquoi pas un `DELETE` : `usage_log.user_id` référence `users(id)` sans
    `ON DELETE CASCADE`, et `PRAGMA foreign_keys = ON` est appliqué à chaque
    connexion — un `DELETE FROM users` échouait donc en `IntegrityError` dès que
    l'utilisateur avait servi une seule requête. `ON DELETE CASCADE` ferait
    perdre la traçabilité de facturation et `SET NULL` casserait les jointures
    des rapports : les deux ont été écartés.

    Effacé      : `username` (remplacé par un pseudonyme stable), `email`,
                  `notes`, et le champ libre `api_keys.name`.
    Conservé    : `id`, `created_at`, les lignes `usage_log` et les métadonnées
                  non identifiantes des clés (préfixe, dates, hash) — l'historique
                  agrégé reste exploitable pour la facturation et l'audit.
    Irréversible: aucune donnée effacée n'est récupérable.

    Idempotent : un second appel ne réécrit pas `anonymized_at` et n'échoue pas.
    Retourne None si l'utilisateur n'existe pas (le caller décide du 404).

    Le pseudonyme dérive de l'`id` (clé primaire AUTOINCREMENT, jamais réattribuée)
    et contient un « : », caractère refusé par le motif de `UserCreate` : deux
    anonymisations ne peuvent pas entrer en collision sur la contrainte UNIQUE, et
    l'ancien nom redevient disponible pour un utilisateur recréé plus tard.
    """
    async with get_db() as db:
        row = await (await db.execute(
            "SELECT id, anonymized_at FROM users WHERE id = ?", (user_id,)
        )).fetchone()
        if row is None:
            return None

        # `WHERE anonymized_at IS NULL` : le premier horodatage est préservé sans
        # relecture préalable, donc sans fenêtre de course avec un second appel.
        cursor = await db.execute(
            """
            UPDATE users
               SET username      = ?,
                   email         = NULL,
                   notes         = NULL,
                   is_active     = 0,
                   anonymized_at = datetime('now')
             WHERE id = ?
               AND anonymized_at IS NULL
            """,
            (f"{ANONYMIZED_USERNAME_PREFIX}{user_id}", user_id),
        )
        already_anonymized = cursor.rowcount == 0

        # Révocation des clés : inconditionnelle et idempotente. Une clé déjà
        # révoquée le reste, et le champ libre `name` est effacé dans tous les cas
        # (il peut porter un nom de personne ou de machine).
        revoked = await db.execute(
            "UPDATE api_keys SET is_active = 0 WHERE user_id = ? AND is_active = 1",
            (user_id,),
        )
        revoked_now = revoked.rowcount
        await db.execute(
            "UPDATE api_keys SET name = NULL WHERE user_id = ? AND name IS NOT NULL",
            (user_id,),
        )
        await db.commit()

        final = await (await db.execute(
            "SELECT username, anonymized_at FROM users WHERE id = ?", (user_id,)
        )).fetchone()
        keys_total = await (await db.execute(
            "SELECT COUNT(*) AS n FROM api_keys WHERE user_id = ?", (user_id,)
        )).fetchone()

    return {
        "user_id": user_id,
        "username": final["username"],
        "anonymized_at": final["anonymized_at"],
        "already_anonymized": already_anonymized,
        "keys_revoked": revoked_now,
        "keys_total": int(keys_total["n"] or 0),
    }


async def touch_key_last_used(key_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE api_keys SET last_used = datetime('now') WHERE id = ?",
            (key_id,),
        )
        await db.commit()


# ── Helpers usage ─────────────────────────────────────────────────────────────

async def tokens_used_last_30_days(user_id: int) -> int:
    """
    Total de tokens consommés sur les 30 derniers jours (fenêtre glissante).
    Utilisé pour appliquer monthly_token_limit — même fenêtre que le dashboard
    (tokens_30d dans get_user_period_stats). Requête couverte par idx_usage_user_time.
    """
    async with get_db() as db:
        row = await (await db.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS total
            FROM usage_log
            WHERE user_id = ?
              AND timestamp >= datetime('now', '-30 days')
            """,
            (user_id,),
        )).fetchone()
        return int(row["total"] or 0)


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


async def _delete_usage_rows(db: aiosqlite.Connection, days: int) -> int:
    """DELETE partagé des entrées usage_log plus anciennes que `days` jours."""
    cursor = await db.execute(
        "DELETE FROM usage_log WHERE timestamp < datetime('now', ? || ' days')",
        (f"-{days}",),
    )
    deleted = cursor.rowcount
    await db.commit()
    return deleted


async def delete_usage_older_than(days: int) -> int:
    """
    Suppression LIVE des entrées `usage_log` plus anciennes que `days` jours.

    Contrairement à `purge_usage_older_than`, aucun `VACUUM` : la requête
    s'exécute pendant que la gateway sert (WAL, transaction courte). L'espace
    disque n'est rendu que par la prochaine purge manuelle hors ligne.
    Retourne le nombre de lignes supprimées.
    """
    if days < 0:
        raise ValueError("days doit être >= 0")
    async with get_db() as db:
        return await _delete_usage_rows(db, days)


async def purge_usage_older_than(days: int) -> int:
    """
    Purge de rétention MANUELLE (opt-in) des entrées `usage_log` plus anciennes
    que `days` jours, puis `VACUUM` complet pour restituer l'espace disque.

    Le `timestamp` est stocké en UTC au format SQLite (`datetime('now')`), donc la
    comparaison à `datetime('now', '-N days')` est correcte. Retourne le nombre de
    lignes supprimées. À exécuter hors ligne (VACUUM verrouille la base) — la
    rétention automatique quotidienne passe par `delete_usage_older_than`,
    qui ne fait pas de VACUUM. Le VACUUM s'exécute sur la MÊME connexion que
    le DELETE : une seconde connexion porte encore des statements ouverts.
    """
    if days < 0:
        raise ValueError("days doit être >= 0")
    async with get_db() as db:
        deleted = await _delete_usage_rows(db, days)
        # VACUUM complet (pas de PRAGMA incremental_vacuum : auto_vacuum n'est pas
        # activé, et on ne le change pas sur une base existante).
        await db.execute("VACUUM")
        return deleted


async def run_retention_pass(usage_days: int, bak_keep: int) -> dict[str, int]:
    """
    Une passe de rétention RGPD, appelée par le lifespan de la gateway au
    démarrage puis toutes les 24 h : usage_log au-delà de `usage_days` jours
    (0 = désactivé) et sauvegardes pre-migration au-delà des `bak_keep` plus
    récentes.

    Vivre-sûr : suppression seule, jamais de VACUUM (la purge des .bak est
    sync FS — déportée hors de la boucle d'événements).
    """
    deleted_usage = 0
    if usage_days > 0:
        deleted_usage = await delete_usage_older_than(usage_days)
    purged_backups = await asyncio.to_thread(
        purge_pre_migration_backups, settings.db_path, bak_keep
    )
    return {"usage_deleted": deleted_usage, "backups_purged": purged_backups}


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

    - total_tokens / prompt_tokens / completion_tokens / request_count :
      agrégés sur la fenêtre glissante `period_days`.
    - tokens_30d : toujours calculé sur les 30 derniers jours, utilisé
      pour comparer correctement au monthly_token_limit quelle que soit
      la période d'affichage sélectionnée.
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
                MAX(l.timestamp)                     AS last_request,
                -- Toujours sur 30 jours fixes pour le calcul de quota mensuel
                COALESCE((
                    SELECT SUM(l2.total_tokens)
                    FROM usage_log l2
                    WHERE l2.user_id = u.id
                      AND l2.timestamp >= datetime('now', '-30 days')
                ), 0)                                AS tokens_30d
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
