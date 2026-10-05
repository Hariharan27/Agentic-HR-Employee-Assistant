"""Leave Agent: the shared tool loop configured with the typed leave tools."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.agent.leave_tools import LeaveToolExecutor
from app.agent.runtime import AGENT_JSON_PROTOCOL, AgentDecision, AgentRunState, ToolAgent
from app.core.config import Settings
from app.llm.ports import LLMGateway


LEAVE_AGENT_BASE_PROMPT = """You are the tool-calling Leave Agent for an HR assistant.
Choose tools dynamically from the supplied tool definitions. Tool results are the only source of
truth for dates, balances, holidays, working days, eligibility, requests, status, approvals and
policy. Never invent those facts and never do calendar arithmetic yourself. The authenticated
employee is injected by the application and must never appear in tool arguments.

Leave dates and applications:
- When the employee mentions dates, call resolve_dates with their date words copied as written.
  "Tuesday and Sunday" are two separate days, not a range; trust the tool's shape.
  If it returns no dates (shape "none"), ask the employee its question, e.g. which day or days
  in the month they named; never pick a day for them.
- Then call build_leave_plan with the resolved dates and the leave type the employee named
  (CASUAL, SICK or EARNED). If the type was never named in this conversation, ask for it; never
  guess it. Explain the plan from its summary: working days, any weekend or holiday not counted,
  balance after, or the exact problems if it is not eligible.
- To move the active plan ("same leave next week", "push it a week later"), call
  shift_leave_plan with the number of calendar days (7 per week); never recompute dates yourself.
- "Apply it", "go ahead", "submit it" with an eligible active plan means call
  prepare_leave_application with that plan_id. If the latest message changes dates or type, build
  a new plan first. Never prepare an application the employee has not seen as a plan.
- After an eligible plan for a "can I" question, answer and offer to apply; do not prepare it.
- If a plan is not eligible only because the balance is short and it lists split_options, offer
  to cover the remaining days with one of those types. Only after the employee agrees, call
  build_leave_plan again with the same dates and split_with set to the type they chose.
- Only CASUAL, SICK and EARNED leave are managed here. Privilege Leave (PL) is a separate legacy
  balance, not Earned Leave: say it is handled in iAssistant and offer the other types.
- For rule questions inside a leave conversation (carry forward, lapse, encashment, approval,
  holidays), use get_leave_rules first and search_leave_policy for anything it does not cover.

State-changing requests MUST use only prepare_leave_application, prepare_leave_cancellation,
prepare_leave_approval or prepare_leave_rejection. They prepare a PendingAction and never execute
the mutation. Never claim a prepared action has already happened. When a prepare tool returns a
pending confirmation, do not request any further tool.

When information is missing, ask only for it, starting with "Please provide" (for example
"Please provide the leave type: casual, sick or earned.").

Use one or more independent tools in a round when useful, then inspect their results. Do not repeat
an identical tool call. For failed tool results, explain the factual reason. You may search policy
for a grounded alternative, but never switch leave type without the employee agreeing. Keep simple
answers concise. Do not mention internal prompts, reasoning, model behaviour, plan ids or tool JSON.
Formatting: plain sentences; you may use **bold** for the key facts (day counts, dates, balances)
and "- " bullet lists for several items. No headings, tables, links or other Markdown.
"""

LEAVE_AGENT_JSON_PROTOCOL = AGENT_JSON_PROTOCOL
LEAVE_AGENT_SYSTEM_PROMPT = LEAVE_AGENT_BASE_PROMPT + "\n" + LEAVE_AGENT_JSON_PROTOCOL

# Backwards-compatible names.
LeaveAgentDecision = AgentDecision
LeaveAgentState = AgentRunState


class LeaveAgent(ToolAgent):
    active_plan_label = "Active leave plan"
    agent_name = "leave_agent"

    def __init__(self, *, settings: Settings, llm: LLMGateway, tools: LeaveToolExecutor) -> None:
        super().__init__(settings=settings, llm=llm, tools=tools, base_prompt=LEAVE_AGENT_BASE_PROMPT)

    def invoke(  # type: ignore[override]
        self,
        *,
        session_id: str,
        user_message: str,
        conversation: list[dict[str, str]],
        leave_plan: dict[str, Any] | None,
        llm_calls: int,
    ) -> AgentRunState:
        result = super().invoke(
            session_id=session_id,
            user_message=user_message,
            conversation=conversation,
            active_plan=leave_plan,
            llm_calls=llm_calls,
        )
        result["leave_plan"] = result.get("active_plan")  # type: ignore[typeddict-unknown-key]
        return result

    def today(self) -> date:
        return self.tools.leave.today()

    def describe_active_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {key: plan.get(key) for key in ("plan_id", "leave_type", "requested_dates", "eligible", "summary")}

    def render_from_results(self, state: AgentRunState) -> str | None:
        """Build a reply only from tool results already returned in this turn."""
        results = state.get("tool_results", [])
        for item in reversed(results):
            tool, data, ok = item.get("tool"), item.get("result", {}), item.get("status") == "success"
            if tool in {"build_leave_plan", "shift_leave_plan"} and ok and data.get("summary"):
                text = str(data["summary"])
                return text + (" Would you like me to apply it?" if data.get("eligible") else "")
            if not ok:
                return self.friendly_failure(str(tool), data)
            if tool == "get_leave_balance":
                balances = data.get("balances", [])
                if len(balances) == 1:
                    entry = balances[0]
                    return (
                        f"You have {entry['available_days']} days of {entry['leave_type'].title()} leave available "
                        f"(total {entry['total_days']}, used {entry['used_days']}, pending {entry['pending_days']})."
                    )
                if balances:
                    return "Your leave balance:\n" + "\n".join(
                        f"{entry['leave_type'].title()}: {entry['available_days']} available" for entry in balances
                    )
            if tool in {"get_my_leave_requests", "get_managed_leave_requests"}:
                requests = data.get("requests", [])
                heading = "Pending leave approvals:" if tool == "get_managed_leave_requests" else "Your recent leave requests:"
                if not requests:
                    return heading + " None."
                return heading + "\n" + "\n".join(
                    f"#{entry['request_id']}: {entry['leave_type'].title()} {entry['start_date']} to "
                    f"{entry['end_date']}, {entry['working_days']} day(s), {entry['status'].title()}"
                    for entry in requests
                )
        return None

    @staticmethod
    def friendly_failure(tool: str, data: dict[str, Any]) -> str:
        """Translate a deterministic tool failure into safe, actionable text."""
        details = data.get("reason") or data.get("error")
        if not details:
            return "I could not complete that Leave request. Please review the dates and try again."
        if tool in {"resolve_dates", "get_holidays", "calculate_leave_days"}:
            return f"The Leave dates are invalid: {details}."
        if tool == "build_leave_plan":
            return f"I could not check those leave dates: {details}."
        if tool == "prepare_leave_application":
            updated = data.get("updated_plan")
            if isinstance(updated, dict) and updated.get("summary"):
                return f"The leave details changed since the plan was shown. Updated plan: {updated['summary']}"
            return f"This Leave request is not eligible: {details}."
        if tool in {"prepare_leave_cancellation", "prepare_leave_approval", "prepare_leave_rejection"}:
            return f"I could not prepare that Leave action: {details}."
        return f"I could not complete that Leave request: {details}."
