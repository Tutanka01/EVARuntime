from __future__ import annotations

import pytest
from pydantic import ValidationError

from config import Settings


VALID = "x" * 40  # 40 chars, ne commence pas par CHANGE_ME


@pytest.mark.parametrize(
    "placeholder",
    [
        "CHANGE_ME_INTERNAL_STUDENT_EDGE_KEY",
        "CHANGE_ME_AUDIT_HMAC_SECRET",
        # Placeholder livré dans deploy/env.example : >= 32 caractères,
        # il doit être rejeté malgré sa longueur suffisante.
        "CHANGE_ME_LONG_RANDOM_SECRET_AT_LEAST_32_CHARS",
        "CHANGE_ME",
    ],
)
def test_placeholder_secrets_rejetes(placeholder: str) -> None:
    with pytest.raises(ValidationError):
        Settings(audit_hmac_secret=placeholder)
    with pytest.raises(ValidationError):
        Settings(upstream_api_key=placeholder)


def test_secret_trop_court_rejete() -> None:
    with pytest.raises(ValidationError):
        Settings(audit_hmac_secret="court")


def test_secret_valide_accepte() -> None:
    s = Settings(upstream_api_key=VALID, audit_hmac_secret=VALID)
    assert s.audit_hmac_secret == VALID
    assert s.upstream_api_key == VALID
