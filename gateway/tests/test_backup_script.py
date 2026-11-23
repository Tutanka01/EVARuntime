"""
`gateway/deploy/llm-gateway-backup.sh` — source unique Linux/macOS (issue #29).

Les chemins par défaut sont choisis à l'exécution (`uname -s`). On simule la
plateforme par un faux `uname` en tête de PATH ; aucun chemin réel de l'hôte
n'est écrit. Un job launchd peut tourner sans HOME : DB_PATH/BACKUP_DIR
fournis doivent alors suffire, et l'absence de HOME n'est signalée que si le
défaut sert vraiment.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

BACKUP_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "llm-gateway-backup.sh"

pytestmark = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 absent")


def _env(tmp_path: Path, system: str, *, home: bool = True, **extra: str) -> dict[str, str]:
    fake_bin = tmp_path / f"fake-bin-{system}"
    fake_bin.mkdir(exist_ok=True)
    uname = fake_bin / "uname"
    uname.write_text(f"#!/bin/sh\necho {system}\n", encoding="utf-8")
    uname.chmod(0o755)
    env = {
        k: v for k, v in os.environ.items()
        if k not in {"HOME", "DB_PATH", "BACKUP_DIR", "BACKUP_RETENTION_DAYS"}
    }
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    if home:
        env["HOME"] = str(tmp_path / "home")
    env.update(extra)
    return env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(BACKUP_SCRIPT)], capture_output=True, text=True, timeout=60, env=env
    )


def _db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
    return path


def test_macos_sans_home_avec_surcharges_sauvegarde(tmp_path):
    db = _db(tmp_path / "data" / "gateway.db")
    backups = tmp_path / "backups"
    proc = _run(_env(tmp_path, "Darwin", home=False, DB_PATH=str(db), BACKUP_DIR=str(backups)))
    assert proc.returncode == 0, proc.stderr
    assert "unbound variable" not in proc.stderr
    assert len(list(backups.glob("gateway-*.db"))) == 1


def test_macos_sans_home_ni_surcharge_echoue_clairement(tmp_path):
    proc = _run(_env(tmp_path, "Darwin", home=False))
    assert proc.returncode == 1
    assert "HOME n'est pas défini" in proc.stderr
    assert "unbound variable" not in proc.stderr


def test_macos_sans_home_backup_dir_manquant_est_nomme(tmp_path):
    db = _db(tmp_path / "data" / "gateway.db")
    proc = _run(_env(tmp_path, "Darwin", home=False, DB_PATH=str(db)))
    assert proc.returncode == 1
    assert "BACKUP_DIR" in proc.stderr and "HOME n'est pas défini" in proc.stderr


def test_macos_avec_home_utilise_le_layout_utilisateur(tmp_path):
    proc = _run(_env(tmp_path, "Darwin"))
    expected = tmp_path / "home" / "Library" / "Application Support" / "evaruntime" / "data" / "gateway.db"
    assert proc.returncode == 1
    assert f"Base introuvable : {expected}" in proc.stderr


def test_linux_defauts_inchanges_meme_sans_home(tmp_path):
    """Les défauts Linux restent /var/lib/llm-gateway/{gateway.db,backups}."""
    proc = _run(_env(tmp_path, "Linux", home=False))
    assert proc.returncode == 1
    assert "Base introuvable : /var/lib/llm-gateway/gateway.db" in proc.stderr

    # BACKUP_DIR par défaut : un faux `mkdir` révèle le chemin sans rien créer.
    fake_mkdir = tmp_path / "fake-bin-Linux" / "mkdir"
    fake_mkdir.write_text('#!/bin/sh\necho "MKDIR $*" >&2\nexit 1\n', encoding="utf-8")
    fake_mkdir.chmod(0o755)
    db = tmp_path / "gateway.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
    proc = _run(_env(tmp_path, "Linux", home=False, DB_PATH=str(db)))
    assert "MKDIR -p /var/lib/llm-gateway/backups" in proc.stderr
