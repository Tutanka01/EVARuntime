from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from config import settings
from schemas import ChatMessage, NormalizedRequest


ALLOWED_FIELDS = {
    "model",
    "messages",
    "max_tokens",
    "n_predict",
    "stream",
    "temperature",
    "top_p",
    "top_k",
    "repeat_penalty",
    "seed",
    "stop",
    "tools",
    "tool_choice",
}


def normalize_chat_body(body: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise_openai_error(400, "Le corps JSON doit etre un objet.", "invalid_request_error")

    requested_model = str(body.get("model") or settings.default_model_id).strip()
    if requested_model not in settings.allowed_models:
        raise_openai_error(400, f"Modele non autorise: {requested_model}", "invalid_request_error")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise_openai_error(400, "Le champ messages doit etre une liste non vide.", "invalid_request_error")
    if len(raw_messages) > settings.max_messages:
        raise_openai_error(400, f"Trop de messages: maximum {settings.max_messages}.", "invalid_request_error")

    messages: list[ChatMessage] = []
    prompt_chars = 0
    try:
        for raw_message in raw_messages:
            message = ChatMessage.model_validate(raw_message)
            _validate_content_structure(message.content)
            message_chars = _content_size(message.content)
            if message_chars > settings.max_message_chars:
                raise_openai_error(400, "Un message depasse la taille maximale.", "invalid_request_error")
            prompt_chars += message_chars
            messages.append(message)
    except ValidationError:
        raise_openai_error(400, "Format de message invalide.", "invalid_request_error")

    if prompt_chars > settings.max_prompt_chars:
        raise_openai_error(400, "Prompt trop volumineux.", "invalid_request_error")

    max_tokens = body.get("max_tokens", body.get("n_predict", settings.max_completion_tokens))
    try:
        max_tokens_int = int(max_tokens)
    except (TypeError, ValueError):
        raise_openai_error(400, "max_tokens doit etre un entier.", "invalid_request_error")
    max_tokens_int = max(1, min(max_tokens_int, settings.max_completion_tokens))

    tools = body.get("tools")
    if tools is not None:
        tools_bytes = len(json.dumps(tools, ensure_ascii=False).encode("utf-8"))
        if tools_bytes > settings.max_tools_bytes:
            raise_openai_error(400, "La definition tools est trop volumineuse.", "invalid_request_error")

    if "stop" in body and body["stop"] is not None:
        _validate_stop(body["stop"])

    normalized: dict[str, Any] = {
        "model": requested_model,
        "messages": [message.model_dump(exclude_none=True) for message in messages],
        "max_tokens": max_tokens_int,
        "stream": bool(body.get("stream", False)),
        "user": f"student:{user['user_id']}",
    }

    _copy_float(body, normalized, "temperature", 0.0, 2.0)
    _copy_float(body, normalized, "top_p", 0.0, 1.0)
    _copy_int(body, normalized, "top_k", 0, 200)
    _copy_float(body, normalized, "repeat_penalty", 0.5, 2.0)
    _copy_int(body, normalized, "seed", -1, 2_147_483_647)

    for optional in ("stop", "tools", "tool_choice"):
        if optional in body and optional in ALLOWED_FIELDS:
            normalized[optional] = body[optional]

    return NormalizedRequest.model_validate(normalized).model_dump(exclude_none=True)


def prompt_char_count(normalized_body: dict[str, Any]) -> int:
    return sum(_content_size(message.get("content")) for message in normalized_body.get("messages", []))


def raise_openai_error(status_code: int, message: str, error_type: str) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"message": message, "type": error_type, "code": str(status_code)}},
    )


def _validate_content_structure(content: Any) -> None:
    """Valide la structure du contenu multimodal (allowlist-first).

    Le multimodal n'est pas supporté : on n'autorise QUE du texte. Un contenu
    liste doit être exclusivement composé d'items `{"type": "text", "text": str}`.
    Tout autre `type` ou toute clé du genre `image_url`/`input_audio` (vecteur
    SSRF si un modèle vision est actif côté admin) est rejeté en 400.
    """
    if content is None or isinstance(content, str):
        return
    if not isinstance(content, list):
        raise_openai_error(400, "Contenu de message invalide.", "invalid_request_error")
    for item in content:
        if not isinstance(item, dict):
            raise_openai_error(400, "Contenu de message invalide.", "invalid_request_error")
        if item.get("type") != "text":
            raise_openai_error(
                400,
                "Seul le contenu texte est autorise (multimodal non supporte).",
                "invalid_request_error",
            )
        if not isinstance(item.get("text"), str):
            raise_openai_error(400, "Un item texte doit contenir un champ text (chaine).", "invalid_request_error")
        # Allowlist stricte : aucune clé autre que type/text n'est admise.
        if set(item.keys()) - {"type", "text"}:
            raise_openai_error(400, "Champ non autorise dans le contenu du message.", "invalid_request_error")


def _validate_stop(stop: Any) -> None:
    """Borne le champ stop : au plus N séquences, chacune bornée en longueur."""
    sequences = stop if isinstance(stop, list) else [stop]
    if len(sequences) > settings.max_stop_sequences:
        raise_openai_error(
            400,
            f"Trop de sequences stop: maximum {settings.max_stop_sequences}.",
            "invalid_request_error",
        )
    for sequence in sequences:
        if not isinstance(sequence, str):
            raise_openai_error(400, "Chaque sequence stop doit etre une chaine.", "invalid_request_error")
        if len(sequence) > settings.max_stop_sequence_chars:
            raise_openai_error(
                400,
                f"Une sequence stop depasse {settings.max_stop_sequence_chars} caracteres.",
                "invalid_request_error",
            )


def _content_size(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    return len(json.dumps(content, ensure_ascii=False))


def _copy_float(source: dict[str, Any], target: dict[str, Any], key: str, minimum: float, maximum: float) -> None:
    if key not in source:
        return
    try:
        value = float(source[key])
    except (TypeError, ValueError):
        raise_openai_error(400, f"{key} doit etre numerique.", "invalid_request_error")
    target[key] = min(max(value, minimum), maximum)


def _copy_int(source: dict[str, Any], target: dict[str, Any], key: str, minimum: int, maximum: int) -> None:
    if key not in source:
        return
    try:
        value = int(source[key])
    except (TypeError, ValueError):
        raise_openai_error(400, f"{key} doit etre entier.", "invalid_request_error")
    target[key] = min(max(value, minimum), maximum)

