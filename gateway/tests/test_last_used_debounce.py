"""
`last_used` n'est réécrit qu'une fois par clé et par minute.

Avant : chaque requête authentifiée planifiait un `UPDATE api_keys`, soit une
transaction SQLite par appel en concurrence avec `log_usage` pour l'unique
verrou d'écriture. L'authentification elle-même interroge toujours la base
(aucun cache) : une révocation reste effective à la requête suivante.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

import auth


@pytest.fixture
def harness(monkeypatch):
    """Base simulée : compte les lookups et les écritures de last_used."""
    state = {"now": 1000.0, "lookups": 0, "touches": [], "revoked": set()}

    async def _lookup(raw_key: str):
        state["lookups"] += 1
        key_id = int(raw_key.rsplit("-", 1)[1])
        if key_id in state["revoked"]:
            return None
        return {"user_id": 1, "key_id": key_id}

    async def _touch(key_id: int) -> None:
        state["touches"].append(key_id)

    def _fire_and_forget(coro, name=None):
        # Exécution immédiate et déterministe de la coroutine de touch.
        try:
            coro.send(None)
        except StopIteration:
            pass

    monkeypatch.setattr(auth.db, "lookup_key", _lookup)
    monkeypatch.setattr(auth.db, "touch_key_last_used", _touch)
    monkeypatch.setattr(auth, "fire_and_forget", _fire_and_forget)
    # Horloge propre au module : `time.monotonic` du processus (horloge de la
    # boucle asyncio) reste intacte. La table est vidée par conftest.py.
    monkeypatch.setattr(auth, "_clock", lambda: state["now"])
    return state


def _creds(key_id: int) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=f"llmgw-test-{key_id}")


@pytest.mark.anyio
async def test_last_used_written_once_per_key_per_window(harness) -> None:
    for _ in range(5):
        await auth.get_current_user(_creds(7))
    assert harness["touches"] == [7]
    assert harness["lookups"] == 5, "chaque requête interroge la base"

    await auth.get_current_user(_creds(8))
    assert harness["touches"] == [7, 8], "fenêtre indépendante par clé"

    harness["now"] += auth.LAST_USED_DEBOUNCE_SECONDS - 0.1
    await auth.get_current_user(_creds(7))
    assert harness["touches"] == [7, 8]

    harness["now"] += 0.2
    await auth.get_current_user(_creds(7))
    assert harness["touches"] == [7, 8, 7]


@pytest.mark.anyio
async def test_revocation_is_not_hidden_by_debounce(harness) -> None:
    await auth.get_current_user(_creds(9))
    harness["revoked"].add(9)
    with pytest.raises(HTTPException) as excinfo:
        await auth.get_current_user(_creds(9))
    assert excinfo.value.status_code == 401


def test_debounce_table_purges_expired_keys(harness, monkeypatch) -> None:
    monkeypatch.setattr(auth, "_LAST_USED_MAX_ENTRIES", 3)
    for key_id in range(3):
        assert auth._should_touch_last_used(key_id)
    harness["now"] += auth.LAST_USED_DEBOUNCE_SECONDS + 1
    assert auth._should_touch_last_used(99)
    assert list(auth._last_used_written) == [99]


def test_debounce_table_bound_holds_when_every_key_is_recent(harness, monkeypatch) -> None:
    """
    Toutes les clés récentes : la borne tient quand même, en évinçant la plus
    ancienne. Avant : rien n'était purgé, la table dépassait sa borne, et
    chaque nouvelle clé déclenchait un parcours complet.
    """
    monkeypatch.setattr(auth, "_LAST_USED_MAX_ENTRIES", 100)
    for key_id in range(1000):
        harness["now"] += 0.001
        assert auth._should_touch_last_used(key_id)
        assert len(auth._last_used_written) <= 100
    assert list(auth._last_used_written) == list(range(900, 1000))
    # Une clé évincée encore récente est réécrite une fois de plus : sans risque.
    assert auth._should_touch_last_used(0)


def test_debounce_eviction_is_constant_time(harness, monkeypatch) -> None:
    """L'éviction ne parcourt pas la table : seules les têtes retirées sont visitées."""
    monkeypatch.setattr(auth, "_LAST_USED_MAX_ENTRIES", 50_000)
    visited = 0
    table = auth._last_used_written

    class _CountingTable(type(table)):
        def __iter__(self):
            nonlocal visited
            for key in super().__iter__():
                visited += 1
                yield key

    counting = _CountingTable()
    monkeypatch.setattr(auth, "_last_used_written", counting)
    for key_id in range(60_000):
        harness["now"] += 0.0001
        auth._should_touch_last_used(key_id)
    assert len(counting) == 50_000
    # Une visite par appel (tête conservée) + une par éviction : linéaire, pas quadratique.
    assert visited <= 2 * 60_000
