from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmailMessage:
    to: tuple[str, ...]
    subject: str
    body: str


class EmailGateway(Protocol):
    """Port for delivering application email."""

    def send(self, message: EmailMessage) -> None:
        """Deliver an email message."""
        ...