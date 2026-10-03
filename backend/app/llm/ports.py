from typing import Protocol

from app.llm.models import ModelTier


class LLMGateway(Protocol):
    def complete(
        self,
        tier: ModelTier,
        *,
        system: str,
        user: str,
        json_mode: bool = False,
    ) -> str: ...
