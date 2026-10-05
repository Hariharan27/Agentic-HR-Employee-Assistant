from dataclasses import dataclass, field
from typing import Any, Protocol

from app.llm.models import ModelTier


@dataclass(frozen=True, slots=True)
class LLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMToolTurn:
    """One model turn in native tool-calling mode: text, tool calls, or both."""

    content: str | None
    tool_calls: list[LLMToolCall] = field(default_factory=list)


class LLMGateway(Protocol):
    def complete(
        self,
        tier: ModelTier,
        *,
        system: str,
        user: str,
        json_mode: bool = False,
    ) -> str: ...


class ToolCallingLLMGateway(LLMGateway, Protocol):
    def complete_with_tools(
        self,
        tier: ModelTier,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMToolTurn: ...
