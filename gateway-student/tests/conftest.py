from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Set required secrets before any module imports that instantiate Settings.
os.environ.setdefault("UPSTREAM_API_KEY", "test-secret-key-at-least-32-chars-long")
os.environ.setdefault("AUDIT_HMAC_SECRET", "test-audit-hmac-secret-at-least-32chars")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("UPSTREAM_CA_PATH", "")
os.environ.setdefault("UPSTREAM_CLIENT_CERT_PATH", "")
os.environ.setdefault("UPSTREAM_CLIENT_KEY_PATH", "")
os.environ.setdefault("AUDIT_LOG_PATH", str(PROJECT_ROOT / "tests" / "audit_test.jsonl"))
