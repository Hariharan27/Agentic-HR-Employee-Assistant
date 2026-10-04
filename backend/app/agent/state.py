from typing import Any, TypedDict


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
    pending_summary: str | None
    llm_calls: int
    onboarding_context: dict[str, str]
    sensitive_response: bool
