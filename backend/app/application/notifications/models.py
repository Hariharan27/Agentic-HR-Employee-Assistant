from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InboundEmail:
    """Provider-independent representation of an incoming email."""

    sender: str
    subject: str
    body: str