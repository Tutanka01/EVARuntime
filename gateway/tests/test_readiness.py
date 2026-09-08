"""
Tests de régression COR-005 — readiness structurelle stricte de `/ready`.

Défaut corrigé : `/ready` se contentait de « un modèle est ready OU il reste de
la VRAM » ; une gateway sans binaire `llama-server` et sans aucun GGUF présent
répondait 200. Comme `update.sh` décide du rollback uniquement sur `/ready`, une
version incapable de servir était acceptée puis déployée.

Ces tests couvrent les trois niveaux (liveness / structural / serving), les modes
local et cluster, la non-fuite d'information dans le corps public, et le fait que
les contrôles restent des `stat` (un GGUF factice vide suffit — aucun hash).

Style repris de `test_metrics_prometheus.py` (TestClient + monkeypatch) et de
`test_model_integrity.py` (`_bare_registry` pour fabriquer des ModelDefinition
sans toucher au YAML de production).
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
import readiness
from config import Settings
from model_registry import ModelDefinition, ModelRegistry


# ── Fixtures et utilitaires ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_readiness_cache():
    """Le cache des contrôles système est un état de module : on l'isole."""
    readiness.clear_cache()
    yield
    readiness.clear_cache()


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _bare_registry() -> ModelRegistry:
    """ModelRegistry sans _load() — juste pour appeler _parse_entry."""
    reg = ModelRegistry.__new__(ModelRegistry)
    reg._allowed_dirs = []
    return reg


def _model(path: Path, model_id: str = "m1", **extra) -> ModelDefinition:
    return _bare_registry()._parse_entry(
        {"id": model_id, "path": str(path), "vram_gb": 5.0, **extra}
    )


class _FakeRegistry:
    def __init__(self, models: list[ModelDefinition]) -> None:
        self._models = list(models)

    def list_all(self) -> list[ModelDefinition]:
        return list(self._models)

    def list_enabled(self) -> list[ModelDefinition]:
        return [m for m in self._models if m.enabled]


class _FakeManager:
    """Interface partagée local/cluster consommée par readiness : registry + status()."""

    def __init__(self, models: list[ModelDefinition], status: dict) -> None:
        self.registry = _FakeRegistry(models)
        self._status = status
        self.status_calls = 0

    def status(self) -> dict:
        self.status_calls += 1
        return self._status


def _local_status(
    *, ready_ids: tuple[str, ...] = (), available_gb: float = 48.0
) -> dict:
    return {
        "vram_budget": {"total_gb": 48.0, "used_gb": 0.0, "available_gb": available_gb},
        "models": [
            {"id": "m1", "state": "ready" if "m1" in ready_ids else "unloaded"}
        ],
        "capacity_queue": {},
    }


def _cluster_status(*, nodes_online: int = 2, available_gb: float = 90.0) -> dict:
    return {
        "vram_budget": {
            "total_gb": 96.0,
            "used_gb": 0.0,
            "available_gb": available_gb,
            "nodes": 2,
            "nodes_online": nodes_online,
        },
        "models": [{"id": "m1", "state": "unloaded"}],
    }


def _gguf(tmp_path: Path, name: str = "m1.gguf") -> Path:
    """GGUF factice VIDE : prouve que le contrôle ne lit ni ne hashe le fichier."""
    path = tmp_path / name
    path.write_bytes(b"")
    return path


def _binary(tmp_path: Path, executable: bool = True, name: str = "llama-server") -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)
    return path


def _healthy_settings(tmp_path: Path, **overrides) -> Settings:
    """Configuration locale saine : tous les artefacts existent réellement."""
    models_yaml = tmp_path / "models.yaml"
    models_yaml.write_text("models: []\n", encoding="utf-8")
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    db_dir = tmp_path / "db"
    db_dir.mkdir(exist_ok=True)
    base = {
        "cluster_mode": "local",
        "models_config_path": models_yaml,
        "llama_server_bin": _binary(tmp_path),
        "db_path": db_dir / "gateway.db",
        "log_dir": log_dir,
        "admin_secret": "readiness-test-admin-secret-32-chars-min",
        "internal_api_key": "readiness-test-internal-key-32-chars-min",
        "agent_secret": "readiness-test-agent-secret-32-chars-min",
    }
    base.update(overrides)
    return Settings(**base)


def _wire(monkeypatch, manager: _FakeManager, config: Settings) -> None:
    monkeypatch.setattr(main, "model_manager", manager)
    monkeypatch.setattr(main, "settings", config)


# ── Configuration saine → 200 ─────────────────────────────────────────────────

def test_ready_200_when_structurally_sound(client, monkeypatch, tmp_path):
    cfg = _healthy_settings(tmp_path)
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["level"] == "structural"
    assert body["levels"] == {"liveness": True, "structural": True, "serving": False}
    assert body["mode"] == "local"
    assert "reason" not in body


def test_ready_level_serving_when_a_model_is_loaded(client, monkeypatch, tmp_path):
    """La serving readiness est EXPOSÉE (utile à COR-006) mais ne gate pas /ready."""
    cfg = _healthy_settings(tmp_path)
    manager = _FakeManager(
        [_model(_gguf(tmp_path))], _local_status(ready_ids=("m1",))
    )
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["level"] == "serving"
    assert body["levels"]["serving"] is True
    assert body["models_ready"] == ["m1"]


# ── Binaire llama-server ──────────────────────────────────────────────────────

def test_ready_503_when_llama_binary_missing(client, monkeypatch, tmp_path):
    """DÉFAUT COR-005 : sans binaire, l'ancienne sonde répondait 200."""
    cfg = _healthy_settings(tmp_path, llama_server_bin=tmp_path / "absent-binary")
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert body["reason"] == "llama_server_missing"
    assert body["checks"]["llama_server_binary"] == "fail"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignore les bits de permission")
def test_ready_503_when_llama_binary_not_executable(client, monkeypatch, tmp_path):
    # Fichier distinct du binaire sain fabriqué par _healthy_settings.
    cfg = _healthy_settings(
        tmp_path,
        llama_server_bin=_binary(tmp_path, executable=False, name="llama-server-noexec"),
    )
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "llama_server_not_executable"


# ── Fichiers de modèles ───────────────────────────────────────────────────────

def test_ready_503_when_enabled_model_gguf_missing(client, monkeypatch, tmp_path):
    """DÉFAUT COR-005 : GGUF d'un modèle activé absent → l'ancienne sonde disait prêt."""
    cfg = _healthy_settings(tmp_path)
    manager = _FakeManager([_model(tmp_path / "jamais-telecharge.gguf")], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["reason"] == "model_file_missing"
    assert body["checks"]["model_files"] == "fail"


def test_disabled_model_with_missing_gguf_does_not_break_readiness(
    client, monkeypatch, tmp_path
):
    """Fail-closed mais pas fragile : seuls les modèles ACTIVÉS sont contrôlés."""
    cfg = _healthy_settings(tmp_path)
    models = [
        _model(_gguf(tmp_path), model_id="m1"),
        _model(tmp_path / "retire-volontairement.gguf", model_id="m2", enabled=False),
    ]
    manager = _FakeManager(models, _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["checks"]["model_files"] == "pass"


def test_empty_gguf_file_is_enough_no_content_read_no_hash(client, monkeypatch, tmp_path):
    """
    Le contrôle est un `stat`, jamais une lecture ni un hash : un fichier de
    0 octet satisfait la présence. Vérifie aussi qu'un sha256 déclaré (qui ne
    correspond évidemment pas à un fichier vide) n'est PAS vérifié par /ready.
    """
    cfg = _healthy_settings(tmp_path)
    gguf = _gguf(tmp_path)
    assert gguf.stat().st_size == 0
    model = _model(gguf, sha256="a" * 64)
    manager = _FakeManager([model], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["checks"]["model_files"] == "pass"


def test_ready_503_when_vision_mmproj_missing(client, monkeypatch, tmp_path):
    cfg = _healthy_settings(tmp_path)
    model = _model(
        _gguf(tmp_path),
        capabilities=["text_generation", "vision"],
        mmproj_path=str(tmp_path / "absent-mmproj.gguf"),
        mmproj_sha256="0" * 64,
    )
    manager = _FakeManager([model], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "mmproj_missing"


# ── Modèles activés ───────────────────────────────────────────────────────────

def test_ready_503_when_no_enabled_model(client, monkeypatch, tmp_path):
    cfg = _healthy_settings(tmp_path)
    manager = _FakeManager(
        [_model(_gguf(tmp_path), enabled=False)], _local_status()
    )
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["reason"] == "no_enabled_model"
    assert body["checks"]["enabled_models"] == "fail"
    # Les contrôles dépendants sont ignorés, pas mis en échec en cascade.
    assert body["checks"]["model_files"] == "skip"


def test_ready_503_when_registry_empty(client, monkeypatch, tmp_path):
    cfg = _healthy_settings(tmp_path)
    manager = _FakeManager([], _local_status())
    _wire(monkeypatch, manager, cfg)

    assert client.get("/ready").status_code == 503


# ── Configuration et base de données ──────────────────────────────────────────

def test_ready_503_when_models_config_missing(client, monkeypatch, tmp_path):
    cfg = _healthy_settings(tmp_path, models_config_path=tmp_path / "absent.yaml")
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "models_config_missing"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignore les bits de permission")
def test_ready_503_when_database_dir_not_writable(client, monkeypatch, tmp_path):
    readonly = tmp_path / "readonly-db"
    readonly.mkdir()
    (readonly / "gateway.db").write_bytes(b"")
    readonly.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        cfg = _healthy_settings(tmp_path, db_path=readonly / "gateway.db")
        manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())
        _wire(monkeypatch, manager, cfg)

        resp = client.get("/ready")
        assert resp.status_code == 503
        assert resp.json()["reason"] == "database_dir_not_writable"
    finally:
        readonly.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignore les bits de permission")
def test_ready_503_when_database_file_not_writable(client, monkeypatch, tmp_path):
    db_dir = tmp_path / "db2"
    db_dir.mkdir()
    db_file = db_dir / "gateway.db"
    db_file.write_bytes(b"")
    db_file.chmod(stat.S_IRUSR)

    cfg = _healthy_settings(tmp_path, db_path=db_file)
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "database_not_writable"


def test_ready_503_when_database_dir_missing(client, monkeypatch, tmp_path):
    cfg = _healthy_settings(tmp_path, db_path=tmp_path / "absent-dir" / "gateway.db")
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "database_dir_missing"


def test_sqlite_memory_db_is_accepted(client, monkeypatch, tmp_path):
    cfg = _healthy_settings(tmp_path, db_path=Path(":memory:"))
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["checks"]["database"] == "pass"


# ── Contrôles non bloquants (fail-closed sans fragilité) ──────────────────────

def test_missing_log_dir_warns_but_stays_ready(client, monkeypatch, tmp_path):
    cfg = _healthy_settings(tmp_path, log_dir=tmp_path / "absent-logs")
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["checks"]["log_dir"] == "warn"


def test_placeholder_secrets_warn_but_stay_ready(client, monkeypatch, tmp_path):
    """Un secret placeholder est un défaut réel — signalé, jamais bloquant."""
    cfg = _healthy_settings(
        tmp_path,
        admin_secret="CHANGE_ME_ADMIN_SECRET",
        internal_api_key="CHANGE_ME_INTERNAL_KEY",
    )
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["checks"]["secrets"] == "warn"


# ── Budget VRAM ───────────────────────────────────────────────────────────────

def test_ready_503_when_no_enabled_model_fits_vram_budget(client, monkeypatch, tmp_path):
    cfg = _healthy_settings(tmp_path, total_vram_gb=8.0)
    model = _bare_registry()._parse_entry(
        {"id": "m1", "path": str(_gguf(tmp_path)), "vram_gb": 400.0}
    )
    manager = _FakeManager([model], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "no_model_fits_vram_budget"


# ── Contrat historique préservé (capacité de service) ─────────────────────────

def test_ready_503_when_no_model_ready_and_no_capacity(client, monkeypatch, tmp_path):
    """Le contrat d'avant COR-005 reste valable : saturation totale → 503."""
    cfg = _healthy_settings(tmp_path)
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status(available_gb=0.0))
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "no_model_ready_and_no_capacity"


def test_ready_200_when_saturated_but_a_model_is_loaded(client, monkeypatch, tmp_path):
    cfg = _healthy_settings(tmp_path)
    manager = _FakeManager(
        [_model(_gguf(tmp_path))], _local_status(ready_ids=("m1",), available_gb=0.0)
    )
    _wire(monkeypatch, manager, cfg)

    assert client.get("/ready").status_code == 200


def test_ready_503_when_manager_status_raises(client, monkeypatch, tmp_path):
    cfg = _healthy_settings(tmp_path)

    class _Broken(_FakeManager):
        def status(self) -> dict:
            raise RuntimeError("nvidia-smi indisponible /var/lib/secret")

    manager = _Broken([_model(_gguf(tmp_path))], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["reason"] == "status_unavailable"
    # Le message de l'exception ne doit jamais fuiter dans le corps public.
    assert "secret" not in resp.text.lower()


# ── Mode cluster ──────────────────────────────────────────────────────────────

def _cluster_settings(tmp_path: Path, **overrides) -> Settings:
    nodes_yaml = tmp_path / "nodes.yaml"
    nodes_yaml.write_text("nodes: []\n", encoding="utf-8")
    return _healthy_settings(
        tmp_path,
        cluster_mode="cluster",
        cluster_nodes_path=overrides.pop("cluster_nodes_path", nodes_yaml),
        **overrides,
    )


def test_cluster_ready_even_without_local_binary_and_gguf(client, monkeypatch, tmp_path):
    """
    En cluster, binaire et GGUF vivent sur les nœuds : l'orchestrateur ne doit
    pas exiger leur présence locale (cohérent avec _validate_inference_runtime).
    """
    cfg = _cluster_settings(tmp_path, llama_server_bin=tmp_path / "pas-de-binaire-ici")
    manager = _FakeManager(
        [_model(tmp_path / "gguf-sur-le-noeud.gguf")], _cluster_status(nodes_online=2)
    )
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "cluster"
    assert body["nodes_online"] == 2
    assert body["checks"]["llama_server_binary"] == "skip"
    assert body["checks"]["model_files"] == "skip"
    assert body["checks"]["vram_budget_fit"] == "skip"
    assert body["checks"]["cluster_nodes_online"] == "pass"


def test_cluster_503_when_all_nodes_offline(client, monkeypatch, tmp_path):
    cfg = _cluster_settings(tmp_path)
    manager = _FakeManager(
        [_model(tmp_path / "gguf-sur-le-noeud.gguf")], _cluster_status(
            nodes_online=0, available_gb=0.0
        )
    )
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "all_nodes_offline"


def test_cluster_503_when_nodes_inventory_missing(client, monkeypatch, tmp_path):
    """Readiness structurelle cluster : l'inventaire des nœuds doit être lisible."""
    cfg = _cluster_settings(tmp_path, cluster_nodes_path=tmp_path / "absent-nodes.yaml")
    manager = _FakeManager(
        [_model(tmp_path / "gguf-sur-le-noeud.gguf")], _cluster_status()
    )
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "cluster_nodes_config_missing"


def test_cluster_503_when_no_enabled_model(client, monkeypatch, tmp_path):
    cfg = _cluster_settings(tmp_path)
    manager = _FakeManager([], _cluster_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "no_enabled_model"


# ── Non-fuite d'information ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    "broken",
    ["binary", "gguf", "config", "nodes"],
)
def test_public_body_leaks_no_path_no_secret(client, monkeypatch, tmp_path, broken):
    if broken == "nodes":
        cfg = _cluster_settings(tmp_path, cluster_nodes_path=tmp_path / "absent.yaml")
        status = _cluster_status()
    else:
        overrides = {}
        if broken == "binary":
            overrides["llama_server_bin"] = tmp_path / "absent-binary"
        if broken == "config":
            overrides["models_config_path"] = tmp_path / "absent.yaml"
        cfg = _healthy_settings(tmp_path, **overrides)
        status = _local_status()

    gguf = tmp_path / "absent.gguf" if broken == "gguf" else _gguf(tmp_path)
    manager = _FakeManager([_model(gguf)], status)
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready")
    assert resp.status_code == 503
    text = resp.text
    # Aucun chemin de fichier, aucun secret, aucune URL de nœud.
    assert str(tmp_path) not in text
    assert ".gguf" not in text
    assert "/" not in text.replace("\\/", "")
    for secret in (cfg.admin_secret, cfg.internal_api_key, cfg.agent_secret):
        assert secret not in text
    # Le corps reste néanmoins actionnable : nom de contrôle + code stable.
    body = resp.json()
    assert body["reason"]
    assert "fail" in body["checks"].values()


def test_detailed_body_only_for_admin_secret_holder(client, monkeypatch, tmp_path):
    cfg = _healthy_settings(tmp_path, llama_server_bin=tmp_path / "absent-binary")
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())
    _wire(monkeypatch, manager, cfg)

    public = client.get("/ready")
    assert public.status_code == 503
    assert "details" not in public.json()

    for bad in ("Bearer mauvais-secret", "Basic " + cfg.admin_secret, cfg.admin_secret):
        resp = client.get("/ready", headers={"Authorization": bad})
        assert "details" not in resp.json(), bad

    privileged = client.get(
        "/ready", headers={"Authorization": f"Bearer {cfg.admin_secret}"}
    )
    assert privileged.status_code == 503
    details = {d["name"]: d for d in privileged.json()["details"]}
    # L'appelant privilégié obtient le message actionnable, chemin inclus.
    assert "absent-binary" in details["llama_server_binary"]["message"]
    assert details["llama_server_binary"]["critical"] is True


def test_placeholder_admin_secret_grants_no_privilege(client, monkeypatch, tmp_path):
    """Fail-closed : un ADMIN_SECRET d'exemple ne privilégie personne."""
    cfg = _healthy_settings(
        tmp_path,
        admin_secret="CHANGE_ME_ADMIN_SECRET",
        llama_server_bin=tmp_path / "absent-binary",
    )
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())
    _wire(monkeypatch, manager, cfg)

    resp = client.get("/ready", headers={"Authorization": "Bearer CHANGE_ME_ADMIN_SECRET"})
    assert resp.status_code == 503
    assert "details" not in resp.json()


# ── Coût : cache et invalidation ──────────────────────────────────────────────

def test_filesystem_probe_is_cached_across_calls(client, monkeypatch, tmp_path):
    """/ready est sondée souvent : les stat ne doivent pas être refaits à chaque appel."""
    cfg = _healthy_settings(tmp_path)
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())
    _wire(monkeypatch, manager, cfg)

    calls: list[int] = []
    real_probe = readiness._probe_filesystem

    def counting_probe(config, enabled_models):
        calls.append(1)
        return real_probe(config, enabled_models)

    monkeypatch.setattr(readiness, "_probe_filesystem", counting_probe)

    for _ in range(5):
        assert client.get("/ready").status_code == 200
    assert len(calls) == 1, "la sonde système devrait être mémorisée"
    # L'état de service, lui, est relu à chaque appel (donnée en mémoire).
    assert manager.status_calls == 5


def test_cache_is_invalidated_when_enabled_models_change(client, monkeypatch, tmp_path):
    """Une mutation admin du registre ne doit pas être masquée par le cache."""
    cfg = _healthy_settings(tmp_path)
    good = _model(_gguf(tmp_path), model_id="m1")
    manager = _FakeManager([good], _local_status())
    _wire(monkeypatch, manager, cfg)

    assert client.get("/ready").status_code == 200

    # Le nouveau modèle activé n'a pas de GGUF : détecté immédiatement.
    manager.registry._models.append(
        _model(tmp_path / "nouveau-absent.gguf", model_id="m2")
    )
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "model_file_missing"


def test_zero_ttl_disables_cache(client, monkeypatch, tmp_path):
    cfg = _healthy_settings(tmp_path, readiness_cache_ttl_seconds=0.0)
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())
    _wire(monkeypatch, manager, cfg)

    calls: list[int] = []
    real_probe = readiness._probe_filesystem

    def counting_probe(config, enabled_models):
        calls.append(1)
        return real_probe(config, enabled_models)

    monkeypatch.setattr(readiness, "_probe_filesystem", counting_probe)

    for _ in range(3):
        assert client.get("/ready").status_code == 200
    assert len(calls) == 3


# ── API réutilisable par `evaruntime doctor` (AUT-012) ────────────────────────

@pytest.mark.anyio
async def test_evaluate_readiness_returns_structured_result(tmp_path):
    cfg = _healthy_settings(tmp_path, llama_server_bin=tmp_path / "absent-binary")
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())

    report = await readiness.evaluate_readiness(manager, config=cfg, use_cache=False)

    assert report.structural_ok is False
    assert report.serving_ok is False
    assert report.level == "none"
    assert report.mode == "local"
    assert report.reason == "llama_server_missing"
    # Chaque contrôle porte un nom, un statut, un code et un message actionnable.
    for check in report.checks:
        assert check.name and check.status and check.code and check.message
        assert check.status in ("pass", "fail", "warn", "skip")
    assert [c.name for c in report.failed] == ["llama_server_binary"]
    assert report.checks[0].name == "models_config"
    assert report.checks[-1].name == "serving_capacity"


@pytest.mark.anyio
async def test_evaluate_readiness_use_cache_false_always_probes(tmp_path, monkeypatch):
    cfg = _healthy_settings(tmp_path)
    manager = _FakeManager([_model(_gguf(tmp_path))], _local_status())

    calls: list[int] = []
    real_probe = readiness._probe_filesystem
    monkeypatch.setattr(
        readiness,
        "_probe_filesystem",
        lambda c, m: (calls.append(1), real_probe(c, m))[1],
    )

    for _ in range(3):
        await readiness.evaluate_readiness(manager, config=cfg, use_cache=False)
    assert len(calls) == 3


@pytest.mark.anyio
async def test_evaluate_readiness_survives_broken_registry(tmp_path):
    cfg = _healthy_settings(tmp_path)

    class _BrokenRegistry:
        def list_enabled(self):
            raise OSError("models.yaml illisible")

    class _Manager:
        registry = _BrokenRegistry()

        def status(self):
            return _local_status()

    report = await readiness.evaluate_readiness(_Manager(), config=cfg)
    assert report.structural_ok is False
    assert report.reason == "registry_unavailable"


def test_caller_is_privileged_rejects_malformed_headers(tmp_path):
    cfg = _healthy_settings(tmp_path)
    assert readiness.caller_is_privileged(f"Bearer {cfg.admin_secret}", cfg) is True
    for bad in (None, "", "Bearer", "Bearer ", "bearer wrong", cfg.admin_secret):
        assert readiness.caller_is_privileged(bad, cfg) is False


def test_public_body_never_contains_messages(tmp_path):
    """Garde-fou d'API : `public_body()` ne doit jamais sérialiser un message."""
    report = readiness.ReadinessReport(
        mode="local",
        checks=(
            readiness.CheckResult(
                "model_files", "fail", "model_file_missing",
                "[m1] Fichier GGUF introuvable : /models/secret-path.gguf",
            ),
        ),
        models_ready=(),
        vram_available_gb=0.0,
    )
    body = report.public_body()
    assert "secret-path" not in repr(body)
    assert body["checks"] == {"model_files": "fail"}
    assert body["reason"] == "model_file_missing"
    assert body["level"] == "none"
