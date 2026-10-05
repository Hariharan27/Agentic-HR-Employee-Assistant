"""Onboarding Agent: the shared tool loop configured with the typed onboarding tools."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.agent.onboarding_tools import OnboardingToolExecutor
from app.agent.runtime import AgentRunState, ToolAgent
from app.core.config import Settings
from app.domain.onboarding.draft import FIELDS, LABELS, missing_fields
from app.llm.ports import LLMGateway

ONBOARDING_AGENT_BASE_PROMPT = """You are the tool-calling Onboarding Agent for an HR assistant.
Tool results are the only source of truth for candidates, managers, plans, requests and statuses.
The authenticated user and role are injected by the application; never put identity in arguments.

Creating onboarding (Manager or HR):
- Call update_onboarding_draft with every candidate value the user stated in THIS message, copied
  as written (joining_date as their date words). Never pass a value the user did not state, and
  never guess an email, date, manager, location or employment type.
- If the draft still has missing fields, ask for them using their labels, starting with
  "Please provide", e.g. "Please provide the employee name, email and designation." The user can
  also use the onboarding form shown below your reply.
- If a value is invalid or "not stated by user", say which and ask for it again.
- When nothing is missing, call build_onboarding_plan, then prepare_onboarding with its plan_id in
  the same turn so the user sees the plan with Confirm and Cancel.
Status: get_onboarding_status needs a request ID or the employee name or email from the user; if
none was given, reply "Please provide the employee name, email, or onboarding request ID."
HR administrator reviews: list_onboarding_approvals for the queue. Approving or rejecting needs an
explicit request ID from the user ("Please provide the onboarding request ID."); rejecting also
needs the user's reason ("Please provide a reason for rejecting the onboarding request.").
Prepare tools only create a pending confirmation; never claim a request was created, approved or
rejected. Never show or ask for passwords or credentials. Keep replies short; you may use **bold**
and "- " bullets; no headings or tables. Do not mention tools, plan ids or JSON.
"""


class OnboardingAgent(ToolAgent):
    active_plan_label = "Current onboarding draft"
    agent_name = "onboarding_agent"

    def __init__(self, *, settings: Settings, llm: LLMGateway, tools: OnboardingToolExecutor) -> None:
        super().__init__(settings=settings, llm=llm, tools=tools, base_prompt=ONBOARDING_AGENT_BASE_PROMPT)

    def today(self) -> date:
        return self.tools.onboarding.today()

    def describe_active_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "fields": {LABELS[name]: plan[name] for name in FIELDS if plan.get(name)},
            "missing": missing_fields(plan),
            "plan_id": plan.get("plan_id"),
        }

    def render_from_results(self, state: AgentRunState) -> str | None:
        for item in reversed(state.get("tool_results", [])):
            tool, data, ok = item.get("tool"), item.get("result", {}), item.get("status") == "success"
            if not ok:
                return self.friendly_failure(str(tool), data)
            if tool == "update_onboarding_draft":
                if data.get("invalid"):
                    return "Please check: " + "; ".join(f"{key}: {value}" for key, value in data["invalid"].items()) + "."
                if data.get("missing"):
                    return "Please provide the " + ", ".join(data["missing"]) + "."
            if tool == "build_onboarding_plan" and data.get("summary"):
                return str(data["summary"])
            if tool == "get_onboarding_status":
                lines = [f"{task['title']}: {task['status'].replace('_', ' ').title()}" for task in data.get("tasks", [])]
                return (
                    f"{data['name']} — {data['designation']}\nJoining: {data['joining_date']}\n"
                    f"Status: {data['status'].replace('_', ' ').title()} "
                    f"({data['completed_tasks']}/{data['total_tasks']} completed)\n" + "\n".join(lines)
                )
            if tool == "list_onboarding_approvals":
                requests = data.get("requests", [])
                if not requests:
                    return "There are no onboarding requests pending approval."
                return "Pending onboarding approvals:\n" + "\n".join(
                    f"#{entry['request_id']}: {entry['name']} — {entry['designation']}, joining {entry['joining_date']}"
                    for entry in requests
                )
        return None

    def finalize(self, state: AgentRunState, message: str) -> AgentRunState | None:
        intent = state.get("intent")
        results = state.get("tool_results", [])
        if intent == "onboarding_approvals":
            listed = next((item for item in reversed(results) if item.get("tool") == "list_onboarding_approvals"), None)
            if listed is None:
                state = self.execute_inline(state, "list_onboarding_approvals", {})
            rendered = self.render_from_results(state)
            return {**state, "response": rendered} if rendered else None
        if intent != "start_onboarding":
            return None
        if any(item.get("tool") == "prepare_onboarding" for item in results):
            return None
        if any(item.get("status") != "success" for item in results):
            return None
        draft = state.get("active_plan") or {}
        if missing_fields(draft):
            return {**state, "response": self.missing_details_reply(draft, results)}
        if not results:
            return None
        last = results[-1]
        complete = last.get("tool") in {"update_onboarding_draft", "build_onboarding_plan"} and not missing_fields(draft)
        if last.get("tool") == "update_onboarding_draft":
            complete = complete and not last["result"].get("invalid") and not last["result"].get("not_stated_by_user")
        if not complete:
            return None
        # The draft is complete: finish build and prepare so the user sees Confirm and Cancel.
        if last.get("tool") != "build_onboarding_plan":
            state = self.execute_inline(state, "build_onboarding_plan", {})
            last = state["tool_results"][-1]
            if last.get("status") != "success":
                return {**state, "response": self.render_from_results(state)}
        prepared = self.execute_inline(state, "prepare_onboarding", {"plan_id": last["result"]["plan_id"]})
        if prepared.get("response"):
            return prepared
        return {**prepared, "response": self.render_from_results(prepared)}

    @staticmethod
    def missing_details_reply(draft: dict[str, Any], results: list[dict[str, Any]]) -> str:
        """Always ask for every missing detail by label and point at the form, whatever the model wrote."""
        missing = missing_fields(draft)
        parts: list[str] = []
        last_update = next(
            (item for item in reversed(results) if item.get("tool") == "update_onboarding_draft"), None
        )
        invalid = (last_update or {}).get("result", {}).get("invalid") or {}
        if invalid:
            parts.append("Please check: " + "; ".join(f"{key}: {value}" for key, value in invalid.items()) + ".")
        if len(missing) > 1:
            needed = ", ".join(missing[:-1]) + " and " + missing[-1]
        else:
            needed = missing[0]
        parts.append(
            f"Please provide the {needed}. You can type them here or fill in the onboarding form below."
        )
        have = [f"{LABELS[name]}: {draft[name]}" for name in FIELDS if draft.get(name)]
        if have:
            parts.append("So far I have " + "; ".join(have) + ".")
        return " ".join(parts)

    @staticmethod
    def friendly_failure(tool: str, data: dict[str, Any]) -> str:
        details = data.get("reason") or data.get("error")
        return f"I could not complete that onboarding step: {details}." if details else "I could not complete that onboarding step."
