from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    session_id: str
    user_id: int
    employee_id: int
    role: str
    user_message: str
    messages: list[dict[str, str]]
    active_domain: str | None
    # The domain of the previous turn, before routing overwrote active_domain.
    previous_domain: str | None
    pending_action: Any | None
    route: Any
    response: str
    sources: list[dict[str, object]]
    agent_activity: list[dict[str, str]]
    pending_summary: str | None
    llm_calls: int
    # The active, validated leave plan (LeavePlan.to_dict()); replaces the old leave_context draft.
    leave_plan: dict[str, Any] | None
    onboarding_context: dict[str, str]
    parking_context: dict[str, str]
    parking_plan: dict[str, Any] | None
    onboarding_form: dict[str, str] | None
    sensitive_response: bool
