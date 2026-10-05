from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError, model_validator

from app.agent.leave_tools import LeaveToolExecution, LeaveToolExecutor
from app.core.config import Settings
from app.llm.ports import LLMGateway


logger = logging.getLogger("app.leave_agent")


LEAVE_AGENT_BASE_PROMPT = """You are the tool-calling Leave Agent for an HR assistant.
Choose tools dynamically from the supplied tool definitions. Tool results are the only source of
truth for dates, balances, holidays, working days, eligibility, requests, status, approvals and
policy. Never invent those facts and never do calendar arithmetic yourself. The authenticated
employee is injected by the application and must never appear in tool arguments.

Leave dates and applications:
- When the employee mentions dates, call resolve_dates with their date words copied as written.
  "Tuesday and Sunday" are two separate days, not a range; trust the tool's shape.
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

LEAVE_AGENT_JSON_PROTOCOL = """Return exactly one JSON object in one of these forms:
{"action":"tool","tool_calls":[{"name":"tool_name","arguments":{...}}]}
{"action":"final","message":"grounded response or a concise request for missing information"}
"""

# Kept for compatibility with callers and tests that identify the Leave Agent by its prompt.
LEAVE_AGENT_SYSTEM_PROMPT = LEAVE_AGENT_BASE_PROMPT + "\n" + LEAVE_AGENT_JSON_PROTOCOL

_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")


class LeaveAgentToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)


class LeaveAgentDecision(BaseModel):
    # Unknown keys from the model are ignored rather than failing the whole turn.
    model_config = ConfigDict(extra="ignore")

    action: Literal["tool", "final"]
    tool_calls: list[LeaveAgentToolCall] = Field(default_factory=list, max_length=4)
    message: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_action(self) -> "LeaveAgentDecision":
        if self.action == "tool" and not self.tool_calls:
            raise ValueError("A tool action requires at least one tool call")
        if self.action == "final" and not (self.message and self.message.strip()):
            raise ValueError("A final action requires a message")
        return self


class LeaveAgentState(TypedDict, total=False):
    session_id: str
    user_message: str
    conversation: list[dict[str, str]]
    leave_plan: dict[str, Any] | None
    tool_results: list[dict[str, Any]]
    pending_tool_calls: list[dict[str, Any]]
    seen_tool_calls: list[str]
    agent_activity: list[dict[str, str]]
    sources: list[dict[str, object]]
    response: str
    pending_summary: str | None
    iterations: int
    tool_call_count: int
    llm_calls: int
    final_status: str
    # Corrections shown to the model on its next turn (invalid output, repeats, ungrounded facts).
    feedback: list[str]
    repairs: int
    repeated_calls: int
    # Native tool-calling transcript after the context message (assistant + tool messages).
    transcript: list[dict[str, Any]]


class LeaveAgent:
    """Bounded LangGraph loop that delegates all facts and mutations to typed tools.

    The model decides what to do next; Python executes tools, feeds every result or error back,
    checks that the final answer only states facts found in tool results, and builds any fallback
    reply from tool results alone.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        llm: LLMGateway,
        tools: LeaveToolExecutor,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.tools = tools
        self.native = bool(settings.leave_agent_native_tools) and hasattr(llm, "complete_with_tools")
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(LeaveAgentState)
        graph.add_node("leave_agent", self._agent_node)
        graph.add_node("leave_tools", self._tool_node)
        graph.add_edge(START, "leave_agent")
        graph.add_conditional_edges(
            "leave_agent",
            lambda state: "end" if state.get("response")
            else "tools" if state.get("pending_tool_calls")
            else "agent",
            {"tools": "leave_tools", "end": END, "agent": "leave_agent"},
        )
        graph.add_conditional_edges(
            "leave_tools",
            lambda state: "end" if state.get("response") else "agent",
            {"agent": "leave_agent", "end": END},
        )
        return graph.compile()

    def invoke(
        self,
        *,
        session_id: str,
        user_message: str,
        conversation: list[dict[str, str]],
        leave_plan: dict[str, Any] | None,
        llm_calls: int,
    ) -> LeaveAgentState:
        initial: LeaveAgentState = {
            "session_id": session_id,
            "user_message": user_message,
            "conversation": conversation[-8:],
            "leave_plan": leave_plan,
            "tool_results": [],
            "pending_tool_calls": [],
            "seen_tool_calls": [],
            "agent_activity": [],
            "sources": [],
            "iterations": 0,
            "tool_call_count": 0,
            "llm_calls": llm_calls,
            "final_status": "running",
            "feedback": [],
            "repairs": 0,
            "repeated_calls": 0,
            "transcript": [],
        }
        result = self.graph.invoke(initial, {"recursion_limit": 60})
        logger.info(
            "leave_agent_completed",
            extra={
                "session_id": session_id,
                "agent": "leave_agent",
                "iterations": result.get("iterations", 0),
                "tool_call_count": result.get("tool_call_count", 0),
                "status": result.get("final_status", "unknown"),
                "native_tools": self.native,
            },
        )
        return result

    # ------------------------------------------------------------------ model turn

    def _agent_node(self, state: LeaveAgentState) -> LeaveAgentState:
        iteration = state.get("iterations", 0) + 1
        if iteration > self.settings.leave_agent_max_iterations:
            return self._limit_response(state, iteration)
        if state.get("llm_calls", 0) >= self.settings.llm_max_calls_per_request:
            return self._limit_response(state, iteration)

        started = time.monotonic()
        llm_calls = state.get("llm_calls", 0) + 1
        transcript_add: list[dict[str, Any]] = []
        try:
            if self.native:
                decision, assistant_message = self._native_turn(state)
                transcript_add.append(assistant_message)
            else:
                raw = self.llm.complete(
                    "standard", system=LEAVE_AGENT_SYSTEM_PROMPT, user=self._agent_input(state), json_mode=True
                )
                decision = self._parse_decision(raw)
        except Exception as exc:
            logger.warning(
                "leave_agent_model_failed",
                extra={"session_id": state["session_id"], "iteration": iteration, "error_type": type(exc).__name__},
                exc_info=True,
            )
            return self._repair_or_fallback(
                state,
                iteration,
                llm_calls,
                f"Your previous reply could not be used ({self._short_error(exc)}). "
                + ("Call a tool or answer in plain text." if self.native
                   else "Reply with exactly one JSON object in the specified form."),
                status="model_error",
            )
        finally:
            logger.info(
                "leave_agent_model_turn",
                extra={
                    "session_id": state["session_id"],
                    "agent": "leave_agent",
                    "iteration": iteration,
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                },
            )

        updates: LeaveAgentState = {
            "iterations": iteration,
            "llm_calls": llm_calls,
            "feedback": [],
            "transcript": [*state.get("transcript", []), *transcript_add],
        }
        if decision.action == "final":
            message = (decision.message or "").strip()
            ungrounded = self._ungrounded_values(message, state)
            if ungrounded:
                logger.warning(
                    "leave_agent_ungrounded_answer",
                    extra={"session_id": state["session_id"], "values": ungrounded[:10]},
                )
                repaired = self._repair_or_fallback(
                    state,
                    iteration,
                    llm_calls,
                    "Your answer stated values that are not in the tool results or the conversation: "
                    + ", ".join(ungrounded[:10])
                    + ". Answer again using only facts from the tool results, or call a tool to get them.",
                    status="ungrounded_answer",
                )
                repaired["transcript"] = updates["transcript"]
                return repaired
            updates.update(response=self._clean_reply(message), pending_tool_calls=[], final_status="completed")
            return updates
        updates["pending_tool_calls"] = [item.model_dump() for item in decision.tool_calls]
        if self.native:
            # Carry the provider's tool-call ids so each result answers its own call.
            native_calls = transcript_add[0].get("tool_calls", [])
            for call, native in zip(updates["pending_tool_calls"], native_calls):
                call["id"] = native.get("id")
        return updates

    def _native_turn(self, state: LeaveAgentState) -> tuple[LeaveAgentDecision, dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "user", "content": self._context_message(state)}]
        messages.extend(state.get("transcript", []))
        for note in state.get("feedback", []):
            messages.append({"role": "user", "content": note})
        turn = self.llm.complete_with_tools(  # type: ignore[attr-defined]
            "standard",
            system=LEAVE_AGENT_BASE_PROMPT,
            messages=messages,
            tools=self._native_tool_definitions(),
        )
        if turn.tool_calls:
            decision = LeaveAgentDecision(
                action="tool",
                tool_calls=[LeaveAgentToolCall(name=item.name, arguments=item.arguments) for item in turn.tool_calls[:4]],
            )
            assistant = {
                "role": "assistant",
                "content": turn.content or "",
                "tool_calls": [
                    {
                        "id": item.id,
                        "type": "function",
                        "function": {"name": item.name, "arguments": json.dumps(item.arguments, default=str)},
                    }
                    for item in turn.tool_calls[:4]
                ],
            }
            return decision, assistant
        if not turn.content:
            raise ValueError("The model returned neither text nor tool calls")
        return LeaveAgentDecision(action="final", message=turn.content), {"role": "assistant", "content": turn.content}

    def _native_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item["description"],
                    "parameters": item["arguments_schema"],
                },
            }
            for item in self.tools.definitions()
        ]

    def _repair_or_fallback(
        self, state: LeaveAgentState, iteration: int, llm_calls: int, note: str, *, status: str
    ) -> LeaveAgentState:
        repairs = state.get("repairs", 0)
        if repairs < self.settings.leave_agent_max_repairs:
            return {
                "iterations": iteration,
                "llm_calls": llm_calls,
                "repairs": repairs + 1,
                "feedback": [note],
                "pending_tool_calls": [],
            }
        rendered = self._render_from_results(state)
        return {
            "response": rendered
            or "I couldn't complete that Leave request safely. Please tell me the leave type and the dates again.",
            "pending_tool_calls": [],
            "iterations": iteration,
            "llm_calls": llm_calls,
            "final_status": "completed_fallback" if rendered else status,
        }

    @staticmethod
    def _short_error(exc: Exception) -> str:
        text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return text[:200]

    @staticmethod
    def _clean_reply(message: str) -> str:
        """Keep only the Markdown the chat UI renders (**bold**, "- " bullets)."""
        cleaned = message.replace("\u202f", " ").replace("\u00a0", " ")
        cleaned = re.sub(r"__(.+?)__", r"**\1**", cleaned, flags=re.S)
        cleaned = re.sub(r"^\s{0,3}#{1,6}\s+", "", cleaned, flags=re.M)
        return re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    # ------------------------------------------------------------------ grounding

    @classmethod
    def _numbers(cls, text: str) -> set[str]:
        values = set()
        for token in _NUMBER.findall(text):
            try:
                number = float(token)
            except ValueError:
                continue
            values.add(str(int(number)) if number.is_integer() else str(number))
        return values

    def _ungrounded_values(self, message: str, state: LeaveAgentState) -> list[str]:
        """Numbers in the answer that appear in no tool result, the conversation or today's date."""
        stated = self._numbers(message)
        if not stated:
            return []
        today = self.tools.leave.today()
        allowed_text = " ".join(
            [
                json.dumps(state.get("tool_results", []), default=str),
                json.dumps(state.get("leave_plan") or {}, default=str),
                state.get("user_message", ""),
                " ".join(item.get("content", "") for item in state.get("conversation", [])),
                today.isoformat(),
            ]
        )
        allowed = self._numbers(allowed_text)
        return sorted(stated - allowed, key=lambda value: float(value))

    # ------------------------------------------------------------------ fallbacks

    def _render_from_results(self, state: LeaveAgentState) -> str | None:
        """Build a reply only from tool results already returned in this turn."""
        results = state.get("tool_results", [])
        for item in reversed(results):
            tool, data, ok = item.get("tool"), item.get("result", {}), item.get("status") == "success"
            if tool in {"build_leave_plan", "shift_leave_plan"} and ok and data.get("summary"):
                text = str(data["summary"])
                return text + (" Would you like me to apply it?" if data.get("eligible") else "")
            if not ok:
                return self._friendly_failure(str(tool), data)
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
    def _friendly_failure(tool: str, data: dict[str, Any]) -> str:
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

    def _limit_response(self, state: LeaveAgentState, iteration: int) -> LeaveAgentState:
        rendered = self._render_from_results(state)
        return {
            "response": (
                f"{rendered}\n\n(I stopped early to stay within safe limits; ask again if you need more.)"
                if rendered
                else "I stopped the Leave workflow because it reached the safe execution limit. Please ask again more specifically."
            ),
            "pending_tool_calls": [],
            "iterations": iteration,
            "final_status": "iteration_limit",
        }

    # ------------------------------------------------------------------ tools

    def _tool_node(self, state: LeaveAgentState) -> LeaveAgentState:
        results = list(state.get("tool_results", []))
        activities = list(state.get("agent_activity", []))
        sources = list(state.get("sources", []))
        transcript = list(state.get("transcript", []))
        plan = state.get("leave_plan")
        seen = set(state.get("seen_tool_calls", []))
        count = state.get("tool_call_count", 0)
        repeated = state.get("repeated_calls", 0)
        feedback: list[str] = []

        def snapshot(**extra: Any) -> LeaveAgentState:
            return {
                "pending_tool_calls": [],
                "tool_results": results,
                "agent_activity": activities,
                "sources": sources,
                "leave_plan": plan,
                "seen_tool_calls": sorted(seen),
                "tool_call_count": count,
                "repeated_calls": repeated,
                "transcript": transcript,
                "feedback": feedback,
                **extra,
            }

        for call in state.get("pending_tool_calls", []):
            call_id = call.get("id")
            if count >= self.settings.leave_agent_max_tool_calls:
                rendered = self._render_from_results({**state, "tool_results": results})
                return snapshot(
                    response=(rendered + "\n\n(I stopped early to stay within safe limits.)") if rendered
                    else "I stopped the Leave workflow because it reached the safe tool-call limit. Please ask again more specifically.",
                    final_status="tool_limit",
                )
            signature = json.dumps({"name": call["name"], "arguments": call.get("arguments", {})}, sort_keys=True, default=str)
            if signature in seen:
                repeated += 1
                if repeated > 1:
                    rendered = self._render_from_results({**state, "tool_results": results})
                    return snapshot(
                        response=rendered
                        or "I stopped the Leave workflow because the same tool request was repeated without new information. Please rephrase your request.",
                        final_status="repeated_tool_call",
                    )
                note = f"{call['name']} was already called with these arguments; use its earlier result instead of calling it again."
                feedback.append(note)
                if call_id:
                    transcript.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"error": note})})
                continue
            seen.add(signature)
            started = time.monotonic()
            execution = self.tools.execute(
                call["name"], call.get("arguments", {}), session_id=state["session_id"], active_plan=plan
            )
            count += 1
            payload = self._result_payload(execution)
            results.append(payload)
            if call_id:
                transcript.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(payload, default=str)})
            activities.append(
                {"tool": execution.tool, "label": execution.label, "status": "success" if execution.ok else "error"}
            )
            sources = self._merge_sources(sources, execution.sources)
            if execution.plan is not None:
                plan = execution.plan
            logger.info(
                "leave_agent_tool_executed",
                extra={
                    "session_id": state["session_id"],
                    "agent": "leave_agent",
                    "tool": execution.tool,
                    "tool_status": "success" if execution.ok else "error",
                    "iteration": state.get("iterations", 0),
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                },
            )
            if execution.pending_summary:
                return snapshot(
                    response=f"{execution.pending_summary}. Reply yes to confirm or cancel.",
                    pending_summary=execution.pending_summary,
                    final_status="awaiting_confirmation",
                )
            # A failed tool result goes back to the model, which can fix its arguments,
            # try another tool or explain the factual reason to the employee.

        return snapshot()

    # ------------------------------------------------------------------ prompt

    def _context_message(self, state: LeaveAgentState) -> str:
        history = "\n".join(
            f"{item['role']}: {item['content'][:1000]}" for item in state.get("conversation", [])
        )
        today = self.tools.leave.today()
        plan = state.get("leave_plan")
        active_plan = (
            {key: plan.get(key) for key in ("plan_id", "leave_type", "requested_dates", "eligible", "summary")}
            if isinstance(plan, dict)
            else None
        )
        return (
            f"Current date: {today.isoformat()} ({today.strftime('%A')})\n"
            f"Authenticated role: {self.tools.actor.role}\n"
            f"Active leave plan: {json.dumps(active_plan, sort_keys=True, default=str)}\n"
            f"Recent conversation:\n{history or '(none)'}\n"
            f"Current user message: {state['user_message']}"
        )

    def _agent_input(self, state: LeaveAgentState) -> str:
        feedback = state.get("feedback", [])
        return (
            f"{self._context_message(state)}\n\n"
            f"Available tools:\n{json.dumps(self.tools.definitions(), default=str)}\n\n"
            + ("Correction:\n" + "\n".join(feedback) + "\n\n" if feedback else "")
            + f"Tool results so far:\n{json.dumps(state.get('tool_results', []), default=str)}"
        )

    @staticmethod
    def _parse_decision(raw: str) -> LeaveAgentDecision:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].lstrip()
        last_error: Exception | None = None
        for start, character in enumerate(cleaned):
            if character != "{":
                continue
            try:
                payload, _ = json.JSONDecoder().raw_decode(cleaned[start:])
                return LeaveAgentDecision.model_validate(payload)
            except (ValueError, json.JSONDecodeError, PydanticValidationError) as exc:
                last_error = exc
                continue
        raise ValueError(
            "The Leave Agent returned invalid structured output"
            + (f": {LeaveAgent._short_error(last_error)}" if last_error else "")
        )

    @staticmethod
    def _result_payload(execution: LeaveToolExecution) -> dict[str, Any]:
        return {"tool": execution.tool, "status": "success" if execution.ok else "error", "result": execution.data}

    @staticmethod
    def _merge_sources(
        current: list[dict[str, object]], additions: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        result = list(current)
        for source in additions:
            if source not in result:
                result.append(source)
        return result
