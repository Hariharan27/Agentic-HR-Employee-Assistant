from typing import Any

from pydantic import BaseModel, Field, field_validator


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("message")
    @classmethod
    def message_must_contain_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message must contain text")
        return normalized


class ChatResponse(BaseModel):
    session_id: str
    message: str
    domain: str
    intent: str | None = None
    sources: list[dict[str, Any]] = Field(default_factory=list)
    pending_action: str | None = None
