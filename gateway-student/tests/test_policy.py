from __future__ import annotations

import pytest
from fastapi import HTTPException

from policy import normalize_chat_body, prompt_char_count
from config import settings


USER = {"user_id": 123, "key_prefix": "llmstu-test"}
VALID_MODEL = settings.allowed_models[0]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_normalizes_allowed_request() -> None:
    body = {
        "model": VALID_MODEL,
        "messages": [{"role": "user", "content": "Bonjour"}],
        "max_tokens": 4096,
        "ignore_eos": True,
    }
    normalized = normalize_chat_body(body, USER)
    assert normalized["max_tokens"] == settings.max_completion_tokens
    assert normalized["user"] == "student:123"
    assert "ignore_eos" not in normalized


def test_user_field_injected_from_gateway() -> None:
    body = {
        "model": VALID_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "user": "evil_override",
    }
    normalized = normalize_chat_body(body, USER)
    assert normalized["user"] == "student:123"


def test_stream_defaults_to_false() -> None:
    body = {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}]}
    normalized = normalize_chat_body(body, USER)
    assert normalized["stream"] is False


def test_stream_true_preserved() -> None:
    body = {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True}
    normalized = normalize_chat_body(body, USER)
    assert normalized["stream"] is True


def test_temperature_clamped_high() -> None:
    body = {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "temperature": 999.0}
    normalized = normalize_chat_body(body, USER)
    assert normalized["temperature"] == 2.0


def test_temperature_clamped_negative() -> None:
    body = {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "temperature": -1.0}
    normalized = normalize_chat_body(body, USER)
    assert normalized["temperature"] == 0.0


def test_top_p_clamped() -> None:
    body = {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "top_p": 5.0}
    normalized = normalize_chat_body(body, USER)
    assert normalized["top_p"] == 1.0


def test_max_tokens_capped_at_limit() -> None:
    body = {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 99999}
    normalized = normalize_chat_body(body, USER)
    assert normalized["max_tokens"] == settings.max_completion_tokens


def test_max_tokens_minimum_one() -> None:
    body = {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 0}
    normalized = normalize_chat_body(body, USER)
    assert normalized["max_tokens"] == 1


def test_n_predict_alias_for_max_tokens() -> None:
    body = {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "n_predict": 500}
    normalized = normalize_chat_body(body, USER)
    assert normalized["max_tokens"] == 500
    assert "n_predict" not in normalized


# ---------------------------------------------------------------------------
# Model allowlist
# ---------------------------------------------------------------------------

def test_rejects_non_allowlisted_model() -> None:
    with pytest.raises(HTTPException) as exc:
        normalize_chat_body(
            {"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]},
            USER,
        )
    assert exc.value.status_code == 400


def test_rejects_ssrf_attempt_via_model() -> None:
    with pytest.raises(HTTPException) as exc:
        normalize_chat_body(
            {"model": "http://evil.com/pwn", "messages": [{"role": "user", "content": "x"}]},
            USER,
        )
    assert exc.value.status_code == 400


def test_rejects_llama70b_not_in_allowlist() -> None:
    with pytest.raises(HTTPException) as exc:
        normalize_chat_body(
            {"model": "llama-3.1-70b-instruct", "messages": [{"role": "user", "content": "x"}]},
            USER,
        )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Message validation
# ---------------------------------------------------------------------------

def test_rejects_empty_messages_list() -> None:
    with pytest.raises(HTTPException) as exc:
        normalize_chat_body({"model": VALID_MODEL, "messages": []}, USER)
    assert exc.value.status_code == 400


def test_rejects_missing_messages() -> None:
    with pytest.raises(HTTPException) as exc:
        normalize_chat_body({"model": VALID_MODEL}, USER)
    assert exc.value.status_code == 400


def test_rejects_messages_not_a_list() -> None:
    with pytest.raises(HTTPException) as exc:
        normalize_chat_body({"model": VALID_MODEL, "messages": "oops"}, USER)
    assert exc.value.status_code == 400


def test_rejects_too_many_messages() -> None:
    msgs = [{"role": "user", "content": "x"}] * (settings.max_messages + 1)
    with pytest.raises(HTTPException) as exc:
        normalize_chat_body({"model": VALID_MODEL, "messages": msgs}, USER)
    assert exc.value.status_code == 400


def test_rejects_oversized_message() -> None:
    with pytest.raises(HTTPException) as exc:
        normalize_chat_body(
            {"model": VALID_MODEL, "messages": [{"role": "user", "content": "x" * (settings.max_message_chars + 1)}]},
            USER,
        )
    assert exc.value.status_code == 400


def test_rejects_oversized_total_prompt() -> None:
    chunk = "x" * (settings.max_message_chars - 1)
    msgs = [{"role": "user", "content": chunk}] * 5
    with pytest.raises(HTTPException) as exc:
        normalize_chat_body({"model": VALID_MODEL, "messages": msgs}, USER)
    assert exc.value.status_code == 400


def test_rejects_invalid_role() -> None:
    with pytest.raises(HTTPException) as exc:
        normalize_chat_body(
            {"model": VALID_MODEL, "messages": [{"role": "admin", "content": "x"}]},
            USER,
        )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Dangerous field stripping
# ---------------------------------------------------------------------------

def test_strips_ignore_eos() -> None:
    body = {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "ignore_eos": True}
    normalized = normalize_chat_body(body, USER)
    assert "ignore_eos" not in normalized


def test_strips_cache_prompt() -> None:
    body = {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "cache_prompt": True}
    normalized = normalize_chat_body(body, USER)
    assert "cache_prompt" not in normalized


def test_strips_system_prompt() -> None:
    body = {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "system_prompt": "evil"}
    normalized = normalize_chat_body(body, USER)
    assert "system_prompt" not in normalized


def test_strips_mirostat_fields() -> None:
    body = {
        "model": VALID_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "mirostat": 2,
        "mirostat_tau": 5.0,
        "mirostat_eta": 0.1,
    }
    normalized = normalize_chat_body(body, USER)
    assert "mirostat" not in normalized
    assert "mirostat_tau" not in normalized
    assert "mirostat_eta" not in normalized


def test_strips_extra_message_fields() -> None:
    """Extra fields inside message objects must not pass through (extra='ignore')."""
    body = {
        "model": VALID_MODEL,
        "messages": [{"role": "user", "content": "hi", "id_slot": 99, "cache_prompt": True}],
    }
    normalized = normalize_chat_body(body, USER)
    msg = normalized["messages"][0]
    assert "id_slot" not in msg
    assert "cache_prompt" not in msg


# ---------------------------------------------------------------------------
# Tools validation
# ---------------------------------------------------------------------------

def test_tools_within_size_limit_pass() -> None:
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "Gets weather"}}]
    body = {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "tools": tools}
    normalized = normalize_chat_body(body, USER)
    assert "tools" in normalized


def test_rejects_oversized_tools() -> None:
    huge_tool = [{"type": "function", "function": {"name": "x", "description": "y" * settings.max_tools_bytes}}]
    with pytest.raises(HTTPException) as exc:
        normalize_chat_body({"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "tools": huge_tool}, USER)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Numeric bounds
# ---------------------------------------------------------------------------

def test_invalid_temperature_string_raises() -> None:
    with pytest.raises(HTTPException) as exc:
        normalize_chat_body(
            {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "temperature": "hot"},
            USER,
        )
    assert exc.value.status_code == 400


def test_invalid_max_tokens_string_raises() -> None:
    with pytest.raises(HTTPException) as exc:
        normalize_chat_body(
            {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": "beaucoup"},
            USER,
        )
    assert exc.value.status_code == 400


def test_top_k_clamped() -> None:
    body = {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hi"}], "top_k": 9999}
    normalized = normalize_chat_body(body, USER)
    assert normalized["top_k"] == 200


# ---------------------------------------------------------------------------
# prompt_char_count helper
# ---------------------------------------------------------------------------

def test_prompt_char_count() -> None:
    body = {"model": VALID_MODEL, "messages": [{"role": "user", "content": "hello"}]}
    normalized = normalize_chat_body(body, USER)
    assert prompt_char_count(normalized) == 5


def test_prompt_char_count_multipart() -> None:
    body = {
        "model": VALID_MODEL,
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Answer this."},
        ],
    }
    normalized = normalize_chat_body(body, USER)
    assert prompt_char_count(normalized) == len("You are helpful.") + len("Answer this.")


# ---------------------------------------------------------------------------
# Body type guard
# ---------------------------------------------------------------------------

def test_rejects_non_dict_body() -> None:
    with pytest.raises(HTTPException) as exc:
        normalize_chat_body(["not", "a", "dict"], USER)  # type: ignore[arg-type]
    assert exc.value.status_code == 400
