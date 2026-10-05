"""Leave Agent: the shared tool loop configured with the typed leave tools."""

from __future__ import annotations

import re
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
        intent: str | None = None,
    ) -> AgentRunState:
        result = super().invoke(
            session_id=session_id,
            user_message=user_message,
            conversation=conversation,
            active_plan=leave_plan,
            llm_calls=llm_calls,
            intent=intent,
        )
        result["leave_plan"] = result.get("active_plan")  # type: ignore[typeddict-unknown-key]
        return result

    def today(self) -> date:
        return self.tools.leave.today()

    def describe_active_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {key: plan.get(key) for key in ("plan_id", "leave_type", "requested_dates", "eligible", "summary")}

    # ------------------------------------------------------------------ deterministic presentation

    def fallback_message(self, state: AgentRunState) -> str:
        return "Please provide the leave type and the start date and end date."

    @staticmethod
    def balance_text(data: dict[str, Any]) -> str | None:
        balances = data.get("balances", [])
        if not balances:
            return None
        lines = [
            f"{entry['leave_type'].title()} {entry['available_days']} days available "
            f"(total {entry['total_days']}, used {entry['used_days']}, pending {entry['pending_days']})"
            for entry in balances
        ]
        if len(lines) == 1:
            return f"Your leave balance: {lines[0]}."
        return "Your leave balance:\n" + "\n".join(f"- {line}" for line in lines)

    @staticmethod
    def requests_text(tool: str, data: dict[str, Any]) -> str:
        requests = data.get("requests", [])
        managed = tool == "get_managed_leave_requests"
        if not requests:
            return "There are no pending leave requests." if managed else "You do not have any leave requests."
        heading = "Pending leave approvals:" if managed else "Your recent leave requests:"
        return heading + "\n" + "\n".join(
            f"- Request ID #{entry['request_id']}: {entry['leave_type'].title()} {entry['start_date']} to "
            f"{entry['end_date']}, {entry['working_days']} working day(s), {entry['status'].title()}"
            + (f" ({entry['employee_name']})" if managed and entry.get("employee_name") else "")
            for entry in requests
        )

    @staticmethod
    def plan_text(data: dict[str, Any]) -> str:
        return str(data["summary"]) + (" Would you like me to apply it?" if data.get("eligible") else "")

    def present(self, item: dict[str, Any]) -> str | None:
        """Fixed wording for a successful read-only result, or None if it has no fixed form."""
        tool, data = item.get("tool"), item.get("result", {})
        if tool in {"build_leave_plan", "shift_leave_plan"} and data.get("summary"):
            return self.plan_text(data)
        if tool == "get_leave_balance":
            return self.balance_text(data)
        if tool in {"get_my_leave_requests", "get_managed_leave_requests"}:
            return self.requests_text(str(tool), data)
        if tool == "calculate_leave_days":
            return f"That range contains {data.get('working_days')} working leave day(s)."
        if tool == "get_holidays":
            names = data.get("names") or {}
            if not names:
                return "There are no configured holidays in that range."
            return "Configured holidays in that range:\n" + "\n".join(
                f"- {day} ({name})" for day, name in names.items()
            )
        return None

    def render_from_results(self, state: AgentRunState) -> str | None:
        """Build a reply only from tool results already returned in this turn."""
        for item in reversed(state.get("tool_results", [])):
            if item.get("status") != "success":
                return self.friendly_failure(str(item.get("tool")), item.get("result", {}))
            text = self.present(item)
            if text:
                return text
        return None

    # ------------------------------------------------------------------ finalisation

    ID_INTENTS = {"cancel_leave_request", "leave_request_history", "approve_leave_request", "reject_leave_request"}
    PLAN_INTENTS = {"apply_leave", "leave_eligibility"}
    _TYPES = (
        (re.compile(r"\b(?:casual|casula|casaul|cl)\b", re.I), "CASUAL"),
        (re.compile(r"\b(?:sick|sl)\b", re.I), "SICK"),
        (re.compile(r"\b(?:earned|el)\b", re.I), "EARNED"),
    )
    _OTHER_TYPES = re.compile(r"\b(?:privilege|pl|maternity|paternity|adoption|lwp|comp[ -]?off)\b", re.I)
    _OTHER_PERSON = re.compile(
        r"\b[a-z]{1,2}\d{3,}\b|\b(?:colleague|teammate|someone else|other employee|another employee|my manager)\b"
        r"|\bemployee\s+\w+'s\b",
        re.I,
    )
    _BARE_CANCEL = re.compile(r"^\s*(?:cancel|stop|discard|never ?mind|no)\W*$", re.I)

    @staticmethod
    def covers(message: str, phrases: list[str]) -> bool:
        """Whether the model's answer states every key fact (ignoring bold and dash variants)."""
        normalized = re.sub(r"\s+", " ", message.replace("**", "").replace("\u2011", "-").replace("\u2010", "-")).casefold()
        return bool(message.strip()) and all(phrase.casefold() in normalized for phrase in phrases)

    def balance_stated(self, message: str, data: dict[str, Any]) -> bool:
        balances = data.get("balances", [])
        return bool(balances) and all(
            self.covers(message, [entry["leave_type"].lower(), str(entry["available_days"]), "available"])
            for entry in balances
        )

    def plan_stated(self, message: str, data: dict[str, Any]) -> bool:
        if not data.get("eligible") or data.get("split_with"):
            return False
        days = str(data.get("total_working_days"))
        phrases = [f"{days} working day", "eligible", *(item["date"] for item in data.get("excluded_days", []))]
        return self.covers(message, phrases) and "not eligible" not in message.casefold()

    @classmethod
    def mentioned_type(cls, text: str) -> str | None:
        found = [(match.start(), kind) for pattern, kind in cls._TYPES for match in pattern.finditer(text)]
        return max(found)[1] if found else None

    @staticmethod
    def _latest(results: list[dict[str, Any]], *tools: str) -> dict[str, Any] | None:
        for item in reversed(results):
            if item.get("tool") in tools:
                return item
        return None

    def finalize(self, state: AgentRunState, message: str) -> AgentRunState | None:
        intent = state.get("intent")
        text = state.get("user_message", "")
        results = state.get("tool_results", [])
        active = state.get("active_plan")

        if intent == "cancel_leave_request" and self._BARE_CANCEL.match(text) and active:
            return {"response": "Okay, I cancelled that leave plan. No changes were made.", "active_plan": None}

        if intent in self.ID_INTENTS and not re.search(r"\d", text):
            listing = self._latest(results, "get_my_leave_requests", "get_managed_leave_requests")
            reply = "Please provide the request ID."
            if listing and listing.get("status") == "success" and listing["result"].get("requests"):
                reply += "\n\n" + self.requests_text(str(listing["tool"]), listing["result"])
            return {"response": reply}

        if intent == "leave_balance" and not self._OTHER_TYPES.search(text):
            item = self._latest(results, "get_leave_balance")
            prefix = ""
            if item is None or item.get("status") != "success":
                # The model asked back, declined or passed a bad argument: the caller's own balance
                # is always answerable, so fetch it directly.
                state = self.execute_inline(state, "get_leave_balance", {"leave_type": self.mentioned_type(text)})
                item = self._latest(state.get("tool_results", []), "get_leave_balance")
                if self._OTHER_PERSON.search(text):
                    prefix = "I can only show your own leave balance. "
                else:
                    message = ""  # never keep a reply that skipped or misused the balance tool
            if item and item.get("status") == "success":
                if not prefix and self.repairable(state, message):
                    return None  # let the model correct its own numbers first
                if not prefix and self.balance_stated(message, item["result"]):
                    return None  # the model's own wording states every balance; keep it
                rendered = self.balance_text(item["result"])
                if rendered:
                    return {**state, "response": prefix + rendered}
            return None

        listing_tool = {"leave_requests": "get_my_leave_requests", "manager_leave_requests": "get_managed_leave_requests"}.get(
            intent or ""
        )
        if listing_tool:
            item = self._latest(results, listing_tool)
            if item is None:
                state = self.execute_inline(state, listing_tool, {})
                item = self._latest(state.get("tool_results", []), listing_tool)
            if item and item.get("status") == "success":
                return {**state, "response": self.requests_text(listing_tool, item["result"])}
            return None

        if intent in self.PLAN_INTENTS and not self._OTHER_TYPES.search(text):
            return self._finalize_plan(state, str(intent), message)

        if intent == "calculate_leave_days" and not self._latest(results, "calculate_leave_days"):
            # The model asked back instead of counting: resolve the stated range and count it.
            state = self.execute_inline(state, "resolve_dates", {"text": text[:300]})
            resolved = self._latest(state.get("tool_results", []), "resolve_dates")
            if resolved and resolved.get("status") != "success":
                return {**state, "response": self.friendly_failure("resolve_dates", resolved.get("result", {}))}
            dates = [item["date"] for item in (resolved or {}).get("result", {}).get("dates", [])]
            if not dates:
                return {**state, "response": "Please provide the start date and end date to count."}
            state = self.execute_inline(state, "calculate_leave_days", {"start_date": dates[0], "end_date": dates[-1]})
            results = state.get("tool_results", [])
        last = results[-1] if results else None
        if last and last.get("status") == "success" and last.get("tool") in {"calculate_leave_days", "get_holidays"}:
            return {**state, "response": self.present(last)}
        if last and last.get("status") != "success" and intent == "calculate_leave_days":
            return {**state, "response": self.friendly_failure(str(last.get("tool")), last.get("result", {}))}
        return None

    def _finalize_plan(self, state: AgentRunState, intent: str, message: str) -> AgentRunState | None:
        results = state.get("tool_results", [])
        if self._latest(results, "prepare_leave_application"):
            # A prepare attempt already failed; its reason is the answer.
            return None
        plan_item = self._latest(results, "build_leave_plan", "shift_leave_plan")
        if plan_item is not None and plan_item.get("status") != "success":
            return None
        if plan_item is None:
            if re.search(r"\b(?:rest|remaining|split|cover|combine)\b", state.get("user_message", ""), re.I):
                return None  # a split choice needs the model's build with split_with
            state, plan_item, ask = self._complete_plan(state, intent)
            if ask:
                return {**state, "response": ask}
            if plan_item is None or plan_item.get("status") != "success":
                return None if plan_item is None else {**state, "response": self.render_from_results(state)}
        data = plan_item["result"]
        if (
            intent == "apply_leave"
            and data.get("eligible")
            and data.get("plan_id")
            and plan_item.get("tool") != "shift_leave_plan"
        ):
            prepared = self.execute_inline(state, "prepare_leave_application", {"plan_id": data["plan_id"]})
            if prepared.get("response"):
                return prepared
            return {**prepared, "response": self.render_from_results(prepared) or self.plan_text(data)}
        if plan_item in state.get("tool_results", []) and self.plan_stated(message, data):
            return None  # the model's explanation states the plan's key facts; keep it
        return {**state, "response": self.plan_text(data)}

    def _complete_plan(
        self, state: AgentRunState, intent: str
    ) -> tuple[AgentRunState, dict[str, Any] | None, str | None]:
        """Finish a plan the model stopped short of, or ask exactly for what is missing."""
        text = state.get("user_message", "")
        active = state.get("active_plan") if isinstance(state.get("active_plan"), dict) else None
        resolved = self._latest(state.get("tool_results", []), "resolve_dates")
        if resolved is None:
            state = self.execute_inline(state, "resolve_dates", {"text": text[:300]})
            resolved = self._latest(state.get("tool_results", []), "resolve_dates")
        if resolved and resolved.get("status") != "success":
            return state, None, self.friendly_failure("resolve_dates", resolved.get("result", {}))
        data = resolved.get("result", {}) if resolved else {}
        dates = [item["date"] for item in data.get("dates", [])]
        question = data.get("question")
        if question and not str(question).startswith("Which day or days in"):
            question = None  # the generic "which dates" prompt; ask with the fixed wording instead
        type_now = self.mentioned_type(text)

        if not dates and not type_now and active and active.get("plan_id"):
            # "apply it" / "go ahead": the active plan is the subject.
            item = {"tool": "build_leave_plan", "status": "success", "result": active}
            return state, item, None

        leave_type = type_now or (active or {}).get("leave_type") or self._earlier_type(state)
        if not dates and not question:
            if type_now and active and active.get("requested_dates"):
                dates = list(active["requested_dates"])
            else:
                dates = self._earlier_dates(state)
        if leave_type and dates:
            state = self.execute_inline(state, "build_leave_plan", {"leave_type": leave_type, "dates": dates})
            return state, self._latest(state.get("tool_results", []), "build_leave_plan"), None
        if question and not dates:
            ask = str(question)
            if not leave_type:
                ask += " Please provide the leave type too: casual, sick or earned."
            return state, None, ask
        if not leave_type and not dates:
            return state, None, "Please provide the leave type (casual, sick or earned) and the start date and end date."
        if not leave_type:
            span = dates[0] if len(dates) == 1 else f"{dates[0]} to {dates[-1]}"
            return state, None, f"Please provide the leave type for {span}: casual, sick or earned."
        return state, None, f"Please provide the start date and end date for your {str(leave_type).title()} leave."

    _ASKING = re.compile(r"^(?:please provide|which day or days)", re.I)

    def _earlier_user_messages(self, state: AgentRunState) -> list[str]:
        """User messages that were answered with a request for missing leave details, newest first.

        Only an unbroken chain of "Please provide ..." exchanges counts, so details from a finished
        or cancelled request never leak into a new one.
        """
        current = state.get("user_message", "")
        conversation = list(state.get("conversation", []))
        if conversation and conversation[-1].get("role") == "user" and conversation[-1].get("content") == current:
            conversation = conversation[:-1]
        collected: list[str] = []
        index = len(conversation) - 1
        while index >= 1 and len(collected) < 3:
            reply, asked = conversation[index], conversation[index - 1]
            if reply.get("role") != "assistant" or asked.get("role") != "user":
                break
            if not self._ASKING.match(str(reply.get("content", "")).strip()):
                break
            collected.append(str(asked.get("content", "")))
            index -= 2
        return collected

    def _earlier_type(self, state: AgentRunState) -> str | None:
        for message in self._earlier_user_messages(state):
            found = self.mentioned_type(message)
            if found:
                return found
        return None

    def _earlier_dates(self, state: AgentRunState) -> list[str]:
        for message in self._earlier_user_messages(state):
            execution = self.tools.execute("resolve_dates", {"text": message[:300]}, session_id=state["session_id"])
            if execution.ok and execution.data.get("dates"):
                return [item["date"] for item in execution.data["dates"]]
        return []

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
