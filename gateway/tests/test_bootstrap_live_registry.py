"""Activation bootstrap live : mémoire provisoire, disque fail-closed."""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace

import pytest
import yaml
from fastapi import HTTPException

import admin
from model_manager import LocalModelManager
from model_registry import ModelRegistry
from schemas import BootstrapModelSync, ModelEntryUpdate


class FakeManager:
    supports_unload_force = True

    def __init__(self, registry: ModelRegistry) -> None:
        self.registry = registry
        self.loaded = False
        self.blocked: set[str] = set()
        self.fail_unload = False
        self.target_enabled_at_unload: bool | None = None
        self.unload_calls = 0

    def is_model_loaded(self, model_id: str) -> bool:
        return self.loaded

    def block_bootstrap_admission(self, model_id: str) -> None:
        self.blocked.add(model_id)

    def unblock_bootstrap_admission(self, model_id: str) -> None:
        self.blocked.discard(model_id)

    async def unload_model(self, model_id: str, *, force: bool = False) -> None:
        self.unload_calls += 1
        model = self.registry.get(model_id)
        self.target_enabled_at_unload = None if model is None else model.enabled
        if self.fail_unload:
            raise RuntimeError("backend n'a pas confirmé le déchargement")
        self.loaded = False


def _entry(model_id: str, *, enabled: bool, vram_gb: float = 2.0) -> dict:
    return {
        "id": model_id,
        "path": f"/models/{model_id}.gguf",
        "description": model_id,
        "vram_gb": vram_gb,
        "enabled": enabled,
        "capabilities": ["text_generation"],
    }


def _write(path, entries: list[dict]) -> str:
    path.write_text(yaml.safe_dump({"models": entries}), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def bootstrap_runtime(tmp_path, monkeypatch):
    cfg = tmp_path / "models.yaml"
    other = _entry("other", enabled=True)
    _write(cfg, [other])
    registry = ModelRegistry(cfg)
    disabled = _entry("candidate", enabled=False)
    digest = _write(cfg, [other, disabled])
    manager = FakeManager(registry)
    monkeypatch.setattr(admin, "model_manager", manager)
    yield manager, cfg, other, disabled, digest

    for key, state in list(admin._bootstrap_sync_states.items()):
        if key[0] == id(manager):
            admin._cancel_bootstrap_watchdog(state)
            admin._bootstrap_sync_states.pop(key, None)
            admin._bootstrap_sync_locks.pop(key, None)


def _activate(digest: str, **updates) -> BootstrapModelSync:
    payload = {
        "action": "activate",
        "digest": digest,
        "vram_gb": 3.5,
        "lease_seconds": 30,
    }
    payload.update(updates)
    return BootstrapModelSync(**payload)


@pytest.mark.anyio
async def test_absent_target_activate_then_rollback_is_memory_only(bootstrap_runtime):
    manager, cfg, _, _, digest = bootstrap_runtime
    disk_before = cfg.read_bytes()

    result = await admin.bootstrap_sync_model("candidate", _activate(digest), None)

    assert result["phase"] == "active"
    assert manager.registry.get("candidate").enabled is True
    assert manager.registry.get("candidate").vram_gb == 3.5
    assert cfg.read_bytes() == disk_before

    manager.loaded = True
    rolled_back = await admin.bootstrap_sync_model(
        "candidate",
        BootstrapModelSync(action="rollback", digest=digest),
        None,
    )

    assert rolled_back["phase"] == "rolled_back"
    assert manager.registry.get("candidate").enabled is False
    assert manager.target_enabled_at_unload is False
    assert manager.loaded is False
    assert "candidate" not in manager.blocked
    assert cfg.read_bytes() == disk_before


@pytest.mark.anyio
async def test_rollback_unload_failure_stays_fail_closed(bootstrap_runtime):
    manager, _, _, _, digest = bootstrap_runtime
    await admin.bootstrap_sync_model("candidate", _activate(digest), None)
    manager.loaded = True
    manager.fail_unload = True

    with pytest.raises(HTTPException) as caught:
        await admin.bootstrap_sync_model(
            "candidate",
            BootstrapModelSync(action="rollback", digest=digest),
            None,
        )

    assert caught.value.status_code == 503
    assert manager.registry.get("candidate").enabled is False
    assert "candidate" in manager.blocked
    state = admin._bootstrap_sync_states[(id(manager), "candidate")]
    assert state.phase == "rolling_back"


@pytest.mark.anyio
async def test_confirm_requires_exact_enabled_disk_snapshot(bootstrap_runtime):
    manager, cfg, other, disabled, digest = bootstrap_runtime
    await admin.bootstrap_sync_model("candidate", _activate(digest), None)
    enabled = {**disabled, "enabled": True, "vram_gb": 3.5}
    final_digest = _write(cfg, [other, enabled])

    result = await admin.bootstrap_sync_model(
        "candidate",
        BootstrapModelSync(action="confirm", digest=final_digest),
        None,
    )

    assert result["phase"] == "confirmed"
    assert manager.registry.get("candidate").enabled is True
    assert manager.registry.get("candidate").vram_gb == 3.5


@pytest.mark.anyio
async def test_confirm_response_loss_allows_compensating_rollback(bootstrap_runtime):
    manager, cfg, other, disabled, activate_digest = bootstrap_runtime
    await admin.bootstrap_sync_model("candidate", _activate(activate_digest), None)
    enabled = {**disabled, "enabled": True, "vram_gb": 3.5}
    confirm_digest = _write(cfg, [other, enabled])
    await admin.bootstrap_sync_model(
        "candidate",
        BootstrapModelSync(action="confirm", digest=confirm_digest),
        None,
    )

    # Le client n'a pas reçu la réponse confirm : il compense le disque vers
    # disabled puis appelle rollback avec le nouveau snapshot.
    rollback_digest = _write(cfg, [other, disabled])
    manager.loaded = True
    result = await admin.bootstrap_sync_model(
        "candidate",
        BootstrapModelSync(action="rollback", digest=rollback_digest),
        None,
    )

    assert result["phase"] == "rolled_back"
    assert manager.registry.get("candidate").enabled is False
    assert manager.loaded is False


@pytest.mark.anyio
async def test_false_digest_and_non_target_mutation_are_rejected(bootstrap_runtime):
    manager, cfg, other, disabled, digest = bootstrap_runtime
    with pytest.raises(HTTPException, match="Digest"):
        await admin.bootstrap_sync_model("candidate", _activate("0" * 64), None)

    changed = {**other, "description": "mutation concurrente"}
    changed_digest = _write(cfg, [changed, disabled])
    with pytest.raises(HTTPException, match="non ciblée"):
        await admin.bootstrap_sync_model("candidate", _activate(changed_digest), None)

    assert manager.registry.get("candidate") is None
    assert digest != changed_digest


@pytest.mark.anyio
async def test_provisional_state_refuses_normal_admin_mutation(bootstrap_runtime):
    _, _, _, _, digest = bootstrap_runtime
    await admin.bootstrap_sync_model("candidate", _activate(digest), None)

    with pytest.raises(HTTPException) as caught:
        await admin.update_model(
            "candidate", ModelEntryUpdate(description="interdit"), False, None,
        )
    assert caught.value.status_code == 409


@pytest.mark.anyio
async def test_update_rechecks_provisional_state_after_await(bootstrap_runtime):
    manager, _, _, _, digest = bootstrap_runtime
    snapshot = manager.registry.read_snapshot()
    manager.registry.publish_snapshot(snapshot)

    async def unload_then_activate(model_id: str, *, force: bool = False) -> None:
        await admin.bootstrap_sync_model(model_id, _activate(digest), None)

    manager.unload_model = unload_then_activate
    with pytest.raises(HTTPException) as caught:
        await admin.update_model(
            "candidate", ModelEntryUpdate(enabled=False), False, None,
        )

    assert caught.value.status_code == 409
    assert manager.registry.get("candidate").enabled is True


@pytest.mark.anyio
async def test_expired_lease_unload_failure_is_fail_closed_then_converges(
    bootstrap_runtime,
    monkeypatch,
):
    """
    Bail expiré pendant une requête plus longue que le drain admin.

    Tant que le déchargement est refusé, l'admission reste fermée et le registre
    verrouillé (fail-closed). Régression : le watchdog abandonnait après le
    premier refus et la transition restait `rolling_back` jusqu'au redémarrage.
    Il retente désormais avec un backoff plafonné et converge dès la fin de la
    requête en vol, sans applicateur.
    """
    manager, _, _, _, digest = bootstrap_runtime
    await admin.bootstrap_sync_model("candidate", _activate(digest), None)
    state = admin._bootstrap_sync_states[(id(manager), "candidate")]
    admin._cancel_bootstrap_watchdog(state)
    manager.loaded = True
    manager.fail_unload = True
    sleeps: list[float] = []
    refusals_seen = 0

    async def recording_sleep(delay: float) -> None:
        nonlocal refusals_seen
        sleeps.append(delay)
        if len(sleeps) == 1:
            return None  # le bail lui-même
        # Entre deux tentatives : toujours fail-closed.
        assert state.phase == "rolling_back"
        assert manager.registry.get("candidate").enabled is False
        assert "candidate" in manager.blocked
        with pytest.raises(HTTPException) as caught:
            admin._refuse_registry_mutation_during_bootstrap()
        assert caught.value.status_code == 409
        transitions = admin.bootstrap_transitions_status()
        assert transitions == [{
            "model_id": "candidate", "phase": "rolling_back",
            "lease_expired": True, "rollback_attempts": manager.unload_calls,
        }]
        refusals_seen += 1
        if len(sleeps) == 5:
            manager.fail_unload = False  # la requête en vol s'est terminée

    monkeypatch.setattr(admin, "_bootstrap_lease_sleep", recording_sleep)
    await admin._bootstrap_lease_watchdog(
        (id(manager), "candidate"), "candidate", state,
    )

    assert refusals_seen == 4
    assert sleeps[0] == state.lease_seconds
    assert sleeps[1:] == [1.0, 2.0, 4.0, 8.0]  # backoff exponentiel
    assert manager.unload_calls == 5
    assert state.phase == "rolled_back"
    assert state.terminal_digest == digest
    assert "candidate" not in manager.blocked
    assert manager.loaded is False
    assert admin.bootstrap_transitions_status() == []
    admin._refuse_registry_mutation_during_bootstrap()  # registre déverrouillé

    # Un rollback tardif de l'applicateur reste idempotent.
    late = await admin.bootstrap_sync_model(
        "candidate", BootstrapModelSync(action="rollback", digest=digest), None,
    )
    assert late["idempotent"] is True


def test_rollback_retry_backoff_is_capped():
    delay = admin._BOOTSTRAP_ROLLBACK_RETRY_INITIAL_SECONDS
    for _ in range(20):
        delay = min(delay * 2, admin._BOOTSTRAP_ROLLBACK_RETRY_MAX_SECONDS)
    assert delay == admin._BOOTSTRAP_ROLLBACK_RETRY_MAX_SECONDS <= 60


@pytest.mark.anyio
async def test_refused_explicit_rollback_converges_without_the_applier(
    bootstrap_runtime,
    monkeypatch,
):
    """Rollback explicite refusé puis applicateur disparu : la gateway termine seule."""
    manager, _, _, _, digest = bootstrap_runtime
    await admin.bootstrap_sync_model("candidate", _activate(digest), None)
    manager.loaded = True
    manager.fail_unload = True
    retry_sleeps: list[float] = []

    async def retry_sleep(delay: float) -> None:
        retry_sleeps.append(delay)
        manager.fail_unload = False

    monkeypatch.setattr(admin, "_bootstrap_lease_sleep", retry_sleep)
    with pytest.raises(HTTPException):
        await admin.bootstrap_sync_model(
            "candidate", BootstrapModelSync(action="rollback", digest=digest), None,
        )
    state = admin._bootstrap_sync_states[(id(manager), "candidate")]
    assert state.phase == "rolling_back" and "candidate" in manager.blocked
    assert state.watchdog is not None

    await state.watchdog

    assert retry_sleeps == [1.0]
    assert state.phase == "rolled_back"
    assert "candidate" not in manager.blocked
    assert manager.unload_calls == 2


@pytest.mark.anyio
async def test_admin_status_exposes_open_bootstrap_transition(bootstrap_runtime):
    manager, _, _, _, digest = bootstrap_runtime
    manager.status = lambda: {
        "vram_budget": {"total_gb": 1.0, "used_gb": 0.0, "available_gb": 1.0},
        "models": [],
    }
    await admin.bootstrap_sync_model("candidate", _activate(digest), None)

    status = await admin.get_status(None)

    assert status["bootstrap_transitions"] == [{
        "model_id": "candidate", "phase": "active",
        "lease_expired": False, "rollback_attempts": 0,
    }]


@pytest.mark.anyio
async def test_expired_lease_success_makes_late_rollback_idempotent(
    bootstrap_runtime,
    monkeypatch,
):
    manager, _, _, _, digest = bootstrap_runtime
    await admin.bootstrap_sync_model("candidate", _activate(digest), None)
    state = admin._bootstrap_sync_states[(id(manager), "candidate")]
    admin._cancel_bootstrap_watchdog(state)
    manager.loaded = True

    async def immediate_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(admin, "_bootstrap_lease_sleep", immediate_sleep)
    await admin._bootstrap_lease_watchdog(
        (id(manager), "candidate"), "candidate", state,
    )
    result = await admin.bootstrap_sync_model(
        "candidate",
        BootstrapModelSync(action="rollback", digest=digest),
        None,
    )

    assert state.phase == "rolled_back"
    assert result["idempotent"] is True
    assert manager.unload_calls == 1


@pytest.mark.anyio
async def test_terminal_confirm_does_not_block_a_future_bootstrap(bootstrap_runtime):
    manager, cfg, other, disabled, activate_digest = bootstrap_runtime
    await admin.bootstrap_sync_model("candidate", _activate(activate_digest), None)
    enabled = {**disabled, "enabled": True, "vram_gb": 3.5}
    confirm_digest = _write(cfg, [other, enabled])
    await admin.bootstrap_sync_model(
        "candidate",
        BootstrapModelSync(action="confirm", digest=confirm_digest),
        None,
    )

    # Cycle opérateur ultérieur : désactivation normale sur disque et en mémoire.
    disabled_again = replace(manager.registry.get("candidate"), enabled=False)
    disabled_digest = _write(cfg, [other, disabled_again.to_dict()])
    snapshot = manager.registry.read_snapshot()
    manager.registry.publish_snapshot(snapshot)

    result = await admin.bootstrap_sync_model(
        "candidate", _activate(disabled_digest, vram_gb=4.0), None,
    )
    assert result["phase"] == "active"
    assert result["idempotent"] is False
    assert manager.registry.get("candidate").vram_gb == 4.0


@pytest.mark.anyio
async def test_local_admission_rechecks_enabled_after_waiting_for_model_lock(
    tmp_path,
):
    cfg = tmp_path / "models.yaml"
    enabled = _entry("candidate", enabled=True)
    _write(cfg, [enabled])
    registry = ModelRegistry(cfg)
    manager = LocalModelManager(registry)
    lock = manager._model_locks.setdefault("candidate", asyncio.Lock())
    await lock.acquire()
    task = asyncio.create_task(manager.ensure_model_loaded("candidate"))
    await asyncio.sleep(0)

    disabled = replace(registry.get("candidate"), enabled=False)
    snapshot = registry.read_snapshot()
    registry.publish_snapshot(snapshot, overrides={"candidate": disabled})
    lock.release()

    with pytest.raises(PermissionError):
        await task


def test_bootstrap_schema_is_closed_and_strict() -> None:
    digest = "a" * 64
    with pytest.raises(ValueError):
        BootstrapModelSync(
            action="activate", digest=digest, vram_gb="3.5", lease_seconds=30,
        )
    with pytest.raises(ValueError):
        BootstrapModelSync(
            action="activate", digest=digest, vram_gb=3.5, lease_seconds=True,
        )
    with pytest.raises(ValueError):
        BootstrapModelSync(
            action="activate", digest=digest, vram_gb=3.5,
            lease_seconds=30, unexpected="nope",
        )
