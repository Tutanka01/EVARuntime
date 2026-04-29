from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Role
    content: str | list[dict[str, Any]] | None = None
    name: str | None = Field(default=None, max_length=64)
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class NormalizedRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    max_tokens: int
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    repeat_penalty: float | None = None
    seed: int | None = None
    stop: str | list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    user: str


class OpenAIError(BaseModel):
    error: dict[str, str]

