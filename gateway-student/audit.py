from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from typing import Any

from config import settings


audit_log = logging.getLogger("audit")


def client_ip_hash(client_ip: str | None) -> str | None:
    if not client_ip:
        return None
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    secret = f"{settings.audit_hmac_secret}:{today}".encode()
    return hmac.new(secret, client_ip.encode(), hashlib.sha256).hexdigest()[:16]


def emit(event: dict[str, Any]) -> None:
    audit_log.info(json.dumps(event, ensure_ascii=False, separators=(",", ":")))

