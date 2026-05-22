from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# Le plugin pytest fourni par `anyio` exige un fixture `anyio_backend` —
# on force asyncio (le seul backend utilisé par la gateway) plutôt que de laisser
# anyio paramétrer tous les tests sur asyncio + trio.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"

# Valeurs minimales pour que `from gateway.config import settings` ne tente pas
# de joindre des fichiers de prod inexistants en environnement de test.
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key-at-least-32-chars-long")
os.environ.setdefault("ADMIN_SECRET", "test-admin-secret-at-least-32-chars-long")
os.environ.setdefault("AGENT_SECRET", "test-agent-secret-at-least-32-chars-long")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("LOG_DIR", str(PROJECT_ROOT / "tests" / "_logs"))
os.environ.setdefault("MODELS_CONFIG_PATH", str(PROJECT_ROOT / "models.yaml"))
