from typing import Any, Literal, TypedDict


LeaveDraftStatus = Literal[
    "COLLECTING_DETAILS",
    "READY_FOR_VALIDATION",
    "VALIDATED",
    "AWAITING_CONFIRMATION",
    "SUBMITTED",
    "CANCELLED",
    "EXPIRED",
    "REJECTED",
]


LEAVE_DRAFT_STATUSES = {
    "COLLECTING_DETAILS",
    "READY_FOR_VALIDATION",
    "VALIDATED",
    "AWAITING_CONFIRMATION",
    "SUBMITTED",
    "CANCELLED",
    "EXPIRED",
    "REJECTED",
}


class LeaveDraft(TypedDict, total=False):
    """Canonical, persisted state for a multi-turn leave request."""

    mode: Literal["apply_leave"]
    status: LeaveDraftStatus
    leave_type: str
    start_date: str
    end_date: str
    reason: str


class AgentState(TypedDict, total=False):
    session_id: str
    user_id: int
    employee_id: int
    role: str
    user_message: str
    messages: list[dict[str, str]]
    active_domain: str | None
    pending_action: Any | None
    route: Any
    response: str
    sources: list[dict[str, object]]
    agent_activity: list[dict[str, str]]
    pending_summary: str | None
    llm_calls: int
    leave_context: dict[str, str]
    onboarding_context: dict[str, str]
    parking_context: dict[str, str]
    sensitive_response: bool
