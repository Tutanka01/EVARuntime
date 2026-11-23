"""
Tests de régression COR-002 — anonymisation des utilisateurs (politique DEC-001).

Défaut corrigé : `usage_log.user_id` référence `users(id)` SANS
`ON DELETE CASCADE`, alors que `PRAGMA foreign_keys = ON` est appliqué à chaque
connexion. Un `DELETE FROM users` échouait donc en `IntegrityError` dès que
l'utilisateur avait servi une requête, et `DELETE /admin/users/{username}`
répondait 500 pour tout utilisateur réel.

Politique retenue : la ligne `users` est CONSERVÉE, les données personnelles sont
effacées, le compte est désactivé et les clés sont révoquées. L'historique
`usage_log` reste intact et agrégeable — la contrainte de clé étrangère n'est
donc jamais violée et n'a pas eu à changer.

Fixtures reprises de `test_admin_routes.py` (client/admin_headers/temp_db) et de
`test_migrations.py` (base « legacy » en `user_version = 0`).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from fastapi.testclient import TestClient

import database as db
import main
from config import settings


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.admin_secret}"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Base SQLite jetable sur fichier (le ':memory:' des tests ne persiste pas)."""
    db_file = tmp_path / "anonymization_test.db"
    monkeypatch.setattr(settings, "db_path", db_file)
    return db_file


# Schéma d'avant COR-002 : identique au schéma déployé, sans `anonymized_at`.
# Reproduit une base réellement en production au moment de la migration.
LEGACY_SCHEMA_WITHOUT_ANONYMIZED_AT = """
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
CREATE INDEX IF NOT EXISTS idx_keys_user      ON api_keys(user_id);
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _seed_user_with_keys_and_usage(
    username: str = "alice",
    email: str | None = "alice@univ-pau.fr",
    notes: str | None = "Doctorante, encadrée par M. Dupont",
    n_keys: int = 2,
) -> tuple[dict, list[str]]:
    """Utilisateur complet : PII, plusieurs clés utilisables, usage journalisé."""
    user = await db.create_user(username=username, email=email, notes=notes)
    raw_keys: list[str] = []
    for i in range(n_keys):
        raw, key_row = await db.create_api_key(user["id"], name=f"portable-de-{username}-{i}")
        raw_keys.append(raw)
        await db.log_usage(user["id"], key_row["id"], "m", 100, 50, 12, 200, f"req-{username}-{i}")
    return user, raw_keys


async def _read_user_row(user_id: int) -> dict:
    """Relit la ligne brute — on vérifie l'état réel, pas un code de retour."""
    async with db.get_db() as conn:
        row = await (await conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        )).fetchone()
    return dict(row)


async def _usage_rows(user_id: int) -> list[dict]:
    async with db.get_db() as conn:
        rows = await (await conn.execute(
            "SELECT * FROM usage_log WHERE user_id = ? ORDER BY id", (user_id,)
        )).fetchall()
    return [dict(r) for r in rows]


async def _key_rows(user_id: int) -> list[dict]:
    async with db.get_db() as conn:
        rows = await (await conn.execute(
            "SELECT * FROM api_keys WHERE user_id = ? ORDER BY id", (user_id,)
        )).fetchall()
    return [dict(r) for r in rows]


def _make_legacy_db(path: Path) -> None:
    """Base d'avant COR-002, avec utilisateurs, clés et usage réels."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA_WITHOUT_ANONYMIZED_AT)
        conn.execute(
            "INSERT INTO users (username, email, notes, rpm_limit) "
            "VALUES ('legacy-alice', 'legacy@univ-pau.fr', 'notes historiques', 7)"
        )
        conn.execute(
            "INSERT INTO api_keys (user_id, key_hash, key_prefix, name) "
            "VALUES (1, 'deadbeef', 'llmgw-legacy', 'poste-legacy')"
        )
        conn.execute(
            "INSERT INTO usage_log (user_id, model, total_tokens) VALUES (1, 'm', 42)"
        )
        conn.commit()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        # La colonne cible n'existe pas encore : c'est ce que la migration ajoute.
        columns = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        assert "anonymized_at" not in columns
    finally:
        conn.close()


def _columns(path: Path, table: str) -> dict[str, str]:
    """{nom de colonne: type déclaré} — comparaison de schéma indépendante du DDL."""
    conn = sqlite3.connect(path)
    try:
        return {row[1]: row[2] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _foreign_keys(path: Path, table: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return [
            (row[2], row[3], row[4], row[5], row[6])  # table, from, to, on_update, on_delete
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        ]
    finally:
        conn.close()


# ── 1. Reproduction du défaut ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_raw_delete_still_violates_foreign_key(temp_db) -> None:
    """
    Reproduction du défaut d'origine : un `DELETE FROM users` échoue toujours en
    `IntegrityError` dès qu'une ligne `usage_log` référence l'utilisateur.

    C'est précisément la raison pour laquelle `delete_user()` a été remplacé par
    `anonymize_user()` : la contrainte n'a PAS été relâchée en `ON DELETE CASCADE`
    (perte de traçabilité de facturation).
    """
    await db.init_db()
    user, _ = await _seed_user_with_keys_and_usage()

    with pytest.raises(aiosqlite.IntegrityError, match="FOREIGN KEY"):
        async with db.get_db() as conn:
            await conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
            await conn.commit()


@pytest.mark.anyio
async def test_delete_without_usage_was_the_only_case_that_worked(temp_db) -> None:
    """Sans usage le DELETE passait — d'où un défaut invisible en test naïf."""
    await db.init_db()
    user = await db.create_user(username="sans-usage")
    async with db.get_db() as conn:
        cursor = await conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        await conn.commit()
    assert cursor.rowcount == 1


@pytest.mark.anyio
async def test_anonymize_succeeds_where_delete_failed(temp_db) -> None:
    """Le nouveau chemin réussit exactement là où l'ancien levait IntegrityError."""
    await db.init_db()
    user, _ = await _seed_user_with_keys_and_usage()

    result = await db.anonymize_user(user["id"])

    assert result is not None
    assert result["already_anonymized"] is False
    assert result["anonymized_at"]


def test_route_delete_user_with_usage_returns_200_not_500(
    client, admin_headers, temp_db,
) -> None:
    """
    Régression du symptôme opérateur : la route répondait 500 pour tout
    utilisateur ayant servi au moins une requête. Elle répond désormais 200.
    """
    import asyncio

    asyncio.run(db.init_db())
    asyncio.run(_seed_user_with_keys_and_usage(username="bob", email="bob@univ-pau.fr"))

    response = client.delete("/admin/users/bob", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "anonymized"
    assert body["keys_total"] == 2
    assert body["keys_revoked"] == 2
    # La réponse dit explicitement ce qu'elle fait : ni l'API ni l'opérateur ne
    # peuvent croire à une suppression de ligne.
    assert "username" in body["erased_fields"]
    assert "usage_log" in body["retained"]
    assert "anonymis" in body["message"].lower()


@pytest.mark.anyio
async def test_legacy_delete_user_helper_is_gone(temp_db) -> None:
    """
    `delete_user()` a été retiré : sa docstring promettait une « suppression
    définitive […] CASCADE » qui n'a jamais existé, et l'appeler produisait
    systématiquement une IntegrityError sur un utilisateur réel.
    """
    assert not hasattr(db, "delete_user")
    assert hasattr(db, "anonymize_user")


# ── 2. Effacement effectif des données personnelles ───────────────────────────

@pytest.mark.anyio
async def test_personal_data_is_actually_erased_in_the_row(temp_db) -> None:
    """Vérification sur la ligne relue, pas sur le code de retour."""
    await db.init_db()
    user, _ = await _seed_user_with_keys_and_usage(
        username="carol", email="carol@univ-pau.fr", notes="adresse personnelle",
    )

    await db.anonymize_user(user["id"])

    row = await _read_user_row(user["id"])
    assert row["username"] == f"{db.ANONYMIZED_USERNAME_PREFIX}{user['id']}"
    assert "carol" not in row["username"]
    assert row["email"] is None
    assert row["notes"] is None
    assert row["is_active"] == 0
    assert row["anonymized_at"] is not None
    # Conservé : identifiants techniques non ré-identifiants.
    assert row["id"] == user["id"]
    assert row["created_at"] == user["created_at"]


@pytest.mark.anyio
async def test_api_key_free_text_name_is_erased(temp_db) -> None:
    """`api_keys.name` est un champ libre : il peut porter un nom de personne."""
    await db.init_db()
    user, _ = await _seed_user_with_keys_and_usage(username="dave")

    await db.anonymize_user(user["id"])

    keys = await _key_rows(user["id"])
    assert keys
    assert all(k["name"] is None for k in keys)
    # Le préfixe (aléatoire, non identifiant) reste, pour tracer usage_log.api_key_id.
    assert all(k["key_prefix"] for k in keys)


@pytest.mark.anyio
async def test_anonymized_user_is_distinguishable_from_merely_disabled(temp_db) -> None:
    """
    Sans horodatage dédié l'opération ne serait ni auditable ni idempotente :
    un compte désactivé et un compte anonymisé seraient indiscernables.
    """
    await db.init_db()
    disabled = await db.create_user(username="eve")
    await db.update_user(disabled["id"], is_active=False)
    anonymized, _ = await _seed_user_with_keys_and_usage(username="frank")
    await db.anonymize_user(anonymized["id"])

    disabled_row = await _read_user_row(disabled["id"])
    anonymized_row = await _read_user_row(anonymized["id"])

    assert disabled_row["is_active"] == 0 and anonymized_row["is_active"] == 0
    assert disabled_row["anonymized_at"] is None
    assert anonymized_row["anonymized_at"] is not None
    assert db.is_anonymized(anonymized_row) is True
    assert db.is_anonymized(disabled_row) is False


# ── 3. Révocation des clés ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_all_keys_are_revoked_and_unusable_for_auth(temp_db) -> None:
    """Aucune clé ne doit rester utilisable pour s'authentifier."""
    await db.init_db()
    user, raw_keys = await _seed_user_with_keys_and_usage(username="grace", n_keys=3)

    # Avant : les clés authentifient bien.
    for raw in raw_keys:
        assert await db.lookup_key(raw) is not None

    result = await db.anonymize_user(user["id"])
    assert result["keys_revoked"] == 3
    assert result["keys_total"] == 3

    keys = await _key_rows(user["id"])
    assert all(k["is_active"] == 0 for k in keys)
    # Après : plus aucune ne résout — clé révoquée ET compte désactivé.
    for raw in raw_keys:
        assert await db.lookup_key(raw) is None


@pytest.mark.anyio
async def test_already_revoked_key_is_not_double_counted(temp_db) -> None:
    """`keys_revoked` compte les clés effectivement révoquées par cet appel."""
    await db.init_db()
    user, raw_keys = await _seed_user_with_keys_and_usage(username="heidi", n_keys=2)
    keys = await _key_rows(user["id"])
    await db.revoke_key(keys[0]["key_prefix"])

    result = await db.anonymize_user(user["id"])

    assert result["keys_revoked"] == 1
    assert result["keys_total"] == 2
    assert all(k["is_active"] == 0 for k in await _key_rows(user["id"]))


# ── 4. Préservation de l'historique d'usage ───────────────────────────────────

@pytest.mark.anyio
async def test_usage_log_rows_and_aggregates_are_unchanged(temp_db) -> None:
    """
    Tout l'intérêt de DEC-001 : l'historique agrégé survit à l'effacement.
    Les lignes `usage_log` sont inchangées, ligne par ligne.
    """
    await db.init_db()
    user, _ = await _seed_user_with_keys_and_usage(username="ivan", n_keys=2)

    before_rows = await _usage_rows(user["id"])
    before_tokens = await db.tokens_used_last_30_days(user["id"])
    assert before_tokens == 300  # 2 × (100 + 50)

    await db.anonymize_user(user["id"])

    after_rows = await _usage_rows(user["id"])
    assert after_rows == before_rows
    assert await db.tokens_used_last_30_days(user["id"]) == before_tokens


@pytest.mark.anyio
async def test_usage_foreign_key_constraint_is_left_unchanged(temp_db) -> None:
    """
    La politique DEC-001 conserve la ligne `users` : la clé étrangère n'est jamais
    violée, donc `usage_log` n'a PAS été recréée et garde sa contrainte d'origine
    (aucune action ON DELETE). Ce test verrouille cette décision de conception.
    """
    await db.init_db()
    fks = _foreign_keys(temp_db, "usage_log")
    users_fk = [fk for fk in fks if fk[0] == "users"]
    assert users_fk, "usage_log doit toujours référencer users(id)"
    assert users_fk[0][1] == "user_id"
    assert users_fk[0][4] == "NO ACTION"


# ── 5. Idempotence ────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_anonymizing_twice_preserves_initial_timestamp(temp_db) -> None:
    """Deux anonymisations : aucune erreur, horodatage initial préservé."""
    await db.init_db()
    user, _ = await _seed_user_with_keys_and_usage(username="judy")

    first = await db.anonymize_user(user["id"])
    assert first["already_anonymized"] is False

    second = await db.anonymize_user(user["id"])
    assert second["already_anonymized"] is True
    assert second["anonymized_at"] == first["anonymized_at"]
    assert second["username"] == first["username"]
    # Aucune clé à révoquer une seconde fois.
    assert second["keys_revoked"] == 0


def test_route_is_idempotent_and_reports_already_anonymized(
    client, admin_headers, temp_db,
) -> None:
    """Le second appel HTTP est un 200 explicite, pas un 404 ni un 500."""
    import asyncio

    asyncio.run(db.init_db())
    asyncio.run(_seed_user_with_keys_and_usage(username="ken"))

    first = client.delete("/admin/users/ken", headers=admin_headers)
    assert first.status_code == 200
    pseudonym = first.json()["anonymized_username"]

    # Le nom d'origine n'existe plus : on repasse par le pseudonyme.
    second = client.delete(f"/admin/users/{pseudonym}", headers=admin_headers)
    assert second.status_code == 200
    assert second.json()["status"] == "already_anonymized"
    assert second.json()["anonymized_at"] == first.json()["anonymized_at"]


# ── 6. Unicité et réutilisation du nom ────────────────────────────────────────

@pytest.mark.anyio
async def test_two_users_anonymized_without_unique_collision(temp_db) -> None:
    """Deux anonymisations successives : aucun conflit UNIQUE sur username/email."""
    await db.init_db()
    first, _ = await _seed_user_with_keys_and_usage(username="u1", email="u1@x")
    second, _ = await _seed_user_with_keys_and_usage(username="u2", email="u2@x")

    r1 = await db.anonymize_user(first["id"])
    r2 = await db.anonymize_user(second["id"])

    assert r1["username"] != r2["username"]
    rows = [await _read_user_row(first["id"]), await _read_user_row(second["id"])]
    # SQLite autorise plusieurs NULL dans une contrainte UNIQUE — vérifié, pas supposé.
    assert all(r["email"] is None for r in rows)
    assert len({r["username"] for r in rows}) == 2


@pytest.mark.anyio
async def test_many_users_anonymized_keep_distinct_pseudonyms(temp_db) -> None:
    """Le pseudonyme dérive de l'id (AUTOINCREMENT) : jamais deux fois le même."""
    await db.init_db()
    ids = []
    for i in range(5):
        user, _ = await _seed_user_with_keys_and_usage(
            username=f"bulk{i}", email=f"bulk{i}@x", n_keys=1,
        )
        ids.append(user["id"])
    for uid in ids:
        assert await db.anonymize_user(uid) is not None

    async with db.get_db() as conn:
        rows = await (await conn.execute(
            "SELECT username FROM users WHERE anonymized_at IS NOT NULL"
        )).fetchall()
    names = [r["username"] for r in rows]
    assert len(names) == 5
    assert len(set(names)) == 5


@pytest.mark.anyio
async def test_recreating_user_with_former_username_succeeds(temp_db) -> None:
    """Cas réel : un étudiant part, est anonymisé, puis revient sous le même nom."""
    await db.init_db()
    original, _ = await _seed_user_with_keys_and_usage(
        username="returning", email="returning@univ-pau.fr",
    )
    await db.anonymize_user(original["id"])

    recreated = await db.create_user(username="returning", email="returning@univ-pau.fr")

    assert recreated["id"] != original["id"]
    assert recreated["is_active"] == 1
    assert recreated["anonymized_at"] is None
    # L'ancien compte reste distinct et anonymisé, son usage lui reste attaché.
    assert (await _read_user_row(original["id"]))["anonymized_at"] is not None
    assert len(await _usage_rows(original["id"])) == 2
    assert await _usage_rows(recreated["id"]) == []


@pytest.mark.anyio
async def test_pseudonym_cannot_be_created_through_the_admin_api(temp_db) -> None:
    """
    Le pseudonyme contient un « : », refusé par le motif de `UserCreate` :
    l'API admin ne peut pas fabriquer un nom entrant en collision.
    """
    from pydantic import ValidationError

    from schemas import UserCreate

    with pytest.raises(ValidationError):
        UserCreate(username=f"{db.ANONYMIZED_USERNAME_PREFIX}1")


# ── 7. Utilisateur inexistant ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_anonymize_unknown_user_returns_none(temp_db) -> None:
    """Erreur exploitable par le caller, pas d'exception non gérée."""
    await db.init_db()
    assert await db.anonymize_user(999_999) is None


def test_route_unknown_user_returns_404_not_500(client, admin_headers, temp_db) -> None:
    import asyncio

    asyncio.run(db.init_db())

    response = client.delete("/admin/users/no-such-user", headers=admin_headers)

    assert response.status_code == 404
    assert "introuvable" in response.json()["detail"]


# ── 8. Rapports, statistiques et liste d'utilisateurs ─────────────────────────

@pytest.mark.anyio
async def test_usage_report_still_joins_and_shows_pseudonym(temp_db) -> None:
    """
    Décision : les rapports d'usage CONTINUENT d'inclure l'utilisateur anonymisé,
    sous son pseudonyme. C'est l'objectif de DEC-001 — la jointure `usage_log ⋈
    users` n'est pas cassée, contrairement à un `SET NULL`.
    """
    await db.init_db()
    user, _ = await _seed_user_with_keys_and_usage(username="lena", n_keys=2)

    before = await db.get_usage_report()
    assert len(before) == 2
    assert {r["username"] for r in before} == {"lena"}

    await db.anonymize_user(user["id"])

    after = await db.get_usage_report()
    assert len(after) == len(before)
    pseudonym = f"{db.ANONYMIZED_USERNAME_PREFIX}{user['id']}"
    assert {r["username"] for r in after} == {pseudonym}
    assert all("lena" not in r["username"] for r in after)


@pytest.mark.anyio
async def test_usage_summary_totals_are_preserved(temp_db) -> None:
    """Les agrégats de facturation sont identiques avant et après anonymisation."""
    await db.init_db()
    user, _ = await _seed_user_with_keys_and_usage(username="mia", n_keys=2)

    before = await db.get_usage_summary()
    assert len(before) == 1
    before_entry = before[0]

    await db.anonymize_user(user["id"])

    after = await db.get_usage_summary()
    assert len(after) == 1
    after_entry = after[0]
    assert after_entry["username"] != before_entry["username"]
    for field in (
        "request_count", "total_prompt_tokens",
        "total_completion_tokens", "total_tokens",
    ):
        assert after_entry[field] == before_entry[field]


@pytest.mark.anyio
async def test_anonymized_user_stays_listed_but_not_counted_as_active(temp_db) -> None:
    """
    Décision : un utilisateur anonymisé reste VISIBLE partout où un compte
    désactivé l'est déjà (liste admin, stats par utilisateur du dashboard), et
    n'est JAMAIS compté comme utilisateur actif.
    """
    await db.init_db()
    # Compte actif témoin : référencé par son nom dans les assertions.
    await db.create_user(username="nina")
    anonymized, _ = await _seed_user_with_keys_and_usage(username="oscar")
    await db.anonymize_user(anonymized["id"])

    users = await db.list_users()
    assert len(users) == 2
    pseudonym = f"{db.ANONYMIZED_USERNAME_PREFIX}{anonymized['id']}"
    assert {u["username"] for u in users} == {"nina", pseudonym}

    overview = await db.get_overview_stats()
    assert overview["total_users"] == 1  # seul `nina` est actif

    period = await db.get_user_period_stats(period_days=30)
    by_name = {r["username"]: r for r in period}
    assert pseudonym in by_name
    assert by_name[pseudonym]["is_active"] == 0
    assert by_name[pseudonym]["total_tokens"] == 300


def test_route_get_users_exposes_anonymized_at(client, admin_headers, temp_db) -> None:
    """Un opérateur distingue anonymisé / désactivé via l'API, sans lire la base."""
    import asyncio

    asyncio.run(db.init_db())
    asyncio.run(_seed_user_with_keys_and_usage(username="pat"))
    client.delete("/admin/users/pat", headers=admin_headers)

    listing = client.get("/admin/users", headers=admin_headers)
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    assert rows[0]["anonymized_at"] is not None
    assert rows[0]["email"] is None
    assert rows[0]["notes"] is None
    assert rows[0]["is_active"] is False


def test_route_get_former_username_returns_404(client, admin_headers, temp_db) -> None:
    """L'ancien nom ne résout plus : il n'est plus dans la base."""
    import asyncio

    asyncio.run(db.init_db())
    asyncio.run(_seed_user_with_keys_and_usage(username="quinn"))

    assert client.delete("/admin/users/quinn", headers=admin_headers).status_code == 200
    assert client.get("/admin/users/quinn", headers=admin_headers).status_code == 404


# ── 9. Aucune donnée personnelle dans les journaux ────────────────────────────

@pytest.mark.anyio
async def test_anonymization_logs_no_personal_data(temp_db, caplog) -> None:
    """
    L'opération ne doit pas recréer la donnée ailleurs : ni le nom, ni l'e-mail,
    ni les notes effacées n'apparaissent dans les journaux (§14 / AGENTS.md).
    """
    await db.init_db()
    user, _ = await _seed_user_with_keys_and_usage(
        username="secret-name",
        email="secret.mail@univ-pau.fr",
        notes="note tres personnelle",
    )

    with caplog.at_level(logging.DEBUG):
        await db.anonymize_user(user["id"])

    captured = caplog.text
    assert "secret-name" not in captured
    assert "secret.mail@univ-pau.fr" not in captured
    assert "note tres personnelle" not in captured


def test_route_logs_no_personal_data(client, admin_headers, temp_db, caplog) -> None:
    """
    Même exigence sur le chemin HTTP : la route journalisait le nom d'utilisateur
    effacé (`"Admin : utilisateur '%s' supprimé"`), ce qui recréait la donnée
    dans les logs. Elle ne trace plus que des identifiants techniques.

    L'assertion porte sur les journaux applicatifs de `admin` et `database`, les
    seuls que cet item possède. Le middleware d'accès de `main.py` journalise
    `request.url.path` pour TOUTES les routes — donc le nom d'utilisateur pour
    `GET/PATCH/DELETE /admin/users/{username}` comme pour la création de clés.
    C'est une fuite préexistante et transverse, hors périmètre de COR-002.
    """
    import asyncio

    asyncio.run(db.init_db())
    asyncio.run(_seed_user_with_keys_and_usage(
        username="rita-secret", email="rita.secret@univ-pau.fr", notes="pii libre",
    ))

    owned_loggers = {"admin", "database"}
    with caplog.at_level(logging.DEBUG):
        response = client.delete("/admin/users/rita-secret", headers=admin_headers)
    assert response.status_code == 200

    captured = "\n".join(
        record.getMessage() for record in caplog.records if record.name in owned_loggers
    )
    assert "rita-secret" not in captured
    assert "rita.secret@univ-pau.fr" not in captured
    assert "pii libre" not in captured
    # La trace d'audit existe malgré tout, sur des identifiants techniques.
    assert "anonymis" in captured.lower()


# ── 10. Commande CLI ──────────────────────────────────────────────────────────

@pytest.fixture
def cli_runner():
    from typer.testing import CliRunner

    return CliRunner()


def test_cli_anonymize_user_with_keys_and_usage(cli_runner, temp_db) -> None:
    """Critère d'acceptation via le CLI : clés + usage, anonymisation réussie."""
    import asyncio

    import cli

    asyncio.run(db.init_db())
    user, raw_keys = asyncio.run(_seed_user_with_keys_and_usage(username="cli-user"))

    result = cli_runner.invoke(cli.app, ["anonymize-user", "cli-user", "--yes"])

    assert result.exit_code == 0, result.output
    assert "anonymisé" in result.output
    # La sortie est explicite sur l'effet réel de la commande.
    assert "Effacé" in result.output
    assert "Conservé" in result.output

    row = asyncio.run(_read_user_row(user["id"]))
    assert row["email"] is None and row["notes"] is None
    assert row["anonymized_at"] is not None
    assert asyncio.run(db.lookup_key(raw_keys[0])) is None
    assert len(asyncio.run(_usage_rows(user["id"]))) == 2


def test_cli_anonymize_unknown_user_exits_with_clear_message(cli_runner, temp_db) -> None:
    """Utilisateur inexistant : message clair et code de sortie 1, pas de traceback."""
    import asyncio

    import cli

    asyncio.run(db.init_db())

    result = cli_runner.invoke(cli.app, ["anonymize-user", "ghost", "--yes"])

    assert result.exit_code == 1
    assert "introuvable" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_cli_anonymize_twice_reports_already_anonymized(cli_runner, temp_db) -> None:
    """Second passage : signalé explicitement, sans erreur."""
    import asyncio

    import cli

    asyncio.run(db.init_db())
    user, _ = asyncio.run(_seed_user_with_keys_and_usage(username="cli-twice"))
    pseudonym = f"{db.ANONYMIZED_USERNAME_PREFIX}{user['id']}"

    first = cli_runner.invoke(cli.app, ["anonymize-user", "cli-twice", "--yes"])
    assert first.exit_code == 0
    second = cli_runner.invoke(cli.app, ["anonymize-user", pseudonym, "--yes"])

    assert second.exit_code == 0
    assert "Déjà anonymisé" in second.output


def test_cli_anonymize_aborts_without_confirmation(cli_runner, temp_db) -> None:
    """Sans --yes, un refus interactif laisse le compte intact."""
    import asyncio

    import cli

    asyncio.run(db.init_db())
    user, _ = asyncio.run(_seed_user_with_keys_and_usage(username="cli-abort"))

    result = cli_runner.invoke(cli.app, ["anonymize-user", "cli-abort"], input="n\n")

    assert result.exit_code == 0
    assert "Annulé" in result.output
    row = asyncio.run(_read_user_row(user["id"]))
    assert row["username"] == "cli-abort"
    assert row["anonymized_at"] is None


# ── 11. Migration depuis une base legacy ──────────────────────────────────────

@pytest.mark.anyio
async def test_legacy_db_is_migrated_and_data_preserved(temp_db) -> None:
    """Base d'avant COR-002 : colonne ajoutée, données existantes intactes."""
    _make_legacy_db(temp_db)

    await db.init_db()

    assert "anonymized_at" in _columns(temp_db, "users")
    user = await db.get_user_by_username("legacy-alice")
    assert user is not None
    assert user["email"] == "legacy@univ-pau.fr"
    assert user["notes"] == "notes historiques"
    assert user["rpm_limit"] == 7
    # Une ligne préexistante n'est PAS considérée comme anonymisée.
    assert user["anonymized_at"] is None
    assert len(await _usage_rows(user["id"])) == 1


@pytest.mark.anyio
async def test_anonymization_works_after_migration_from_legacy(temp_db) -> None:
    """Critère d'acceptation sur une base réelle migrée, clés et usage inclus."""
    _make_legacy_db(temp_db)
    await db.init_db()

    user = await db.get_user_by_username("legacy-alice")
    result = await db.anonymize_user(user["id"])

    assert result["already_anonymized"] is False
    assert result["keys_revoked"] == 1
    row = await _read_user_row(user["id"])
    assert row["email"] is None and row["notes"] is None and row["is_active"] == 0
    assert row["username"] == f"{db.ANONYMIZED_USERNAME_PREFIX}{user['id']}"
    # L'historique legacy est conservé.
    async with db.get_db() as conn:
        total = await (await conn.execute(
            "SELECT COALESCE(SUM(total_tokens), 0) AS t FROM usage_log"
        )).fetchone()
    assert total["t"] == 42


@pytest.mark.anyio
async def test_migration_is_declared_at_version_two(temp_db) -> None:
    """La migration COR-002 est la version 2 du gateway principal."""
    versions = {m.version: m for m in db.MIGRATIONS}
    assert db.SCHEMA_VERSION >= 2
    assert "anonymized_at" in versions[2].description
    # Ajoutée après la baseline, qui n'est pas réécrite ; les migrations
    # suivantes (3 : index du quota) viennent après elle.
    assert [m.version for m in db.MIGRATIONS][:2] == [1, 2]
    assert versions[2].apply is db._ensure_anonymized_at


@pytest.mark.anyio
async def test_fresh_and_migrated_schemas_are_identical(tmp_path, monkeypatch) -> None:
    """
    Cohérence `_SCHEMA` / migrations : une base neuve et une base migrée depuis
    l'ancienne version doivent aboutir au MÊME schéma.
    """
    fresh = tmp_path / "fresh.db"
    migrated = tmp_path / "migrated.db"

    monkeypatch.setattr(settings, "db_path", fresh)
    await db.init_db()

    _make_legacy_db(migrated)
    monkeypatch.setattr(settings, "db_path", migrated)
    await db.init_db()

    for table in ("users", "api_keys", "usage_log"):
        assert _columns(fresh, table) == _columns(migrated, table), table
        assert _foreign_keys(fresh, table) == _foreign_keys(migrated, table), table

    conn_fresh = sqlite3.connect(fresh)
    conn_migrated = sqlite3.connect(migrated)
    try:
        query = (
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name LIKE 'idx_%' ORDER BY name"
        )
        assert (
            [r[0] for r in conn_fresh.execute(query)]
            == [r[0] for r in conn_migrated.execute(query)]
        )
        assert (
            conn_fresh.execute("PRAGMA user_version").fetchone()[0]
            == conn_migrated.execute("PRAGMA user_version").fetchone()[0]
            == db.SCHEMA_VERSION
        )
    finally:
        conn_fresh.close()
        conn_migrated.close()
