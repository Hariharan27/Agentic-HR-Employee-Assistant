from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class ConfirmationDecision(StrEnum):
    CONFIRM = "CONFIRM"
    CANCEL = "CANCEL"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_message(cls, message: str) -> "ConfirmationDecision":
        normalized = " ".join(message.strip().lower().split()).rstrip(".!?")
        if normalized in {"yes", "y", "confirm", "confirmed", "proceed", "go ahead", "do it"}:
            return cls.CONFIRM
        if normalized in {"no", "n", "cancel", "stop", "never mind", "nevermind"}:
            return cls.CANCEL
        return cls.UNKNOWN


@dataclass(frozen=True, slots=True)
class PendingActionData:
    id: int | None
    session_id: str
    user_id: int
    action_type: str
    arguments: dict[str, Any]
    summary: str
    expires_at: datetime
