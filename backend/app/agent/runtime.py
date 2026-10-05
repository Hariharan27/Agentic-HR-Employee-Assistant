"""Shared bounded tool-calling agent loop (leave, onboarding, parking).

The model decides what to do next; Python executes typed tools, feeds every result or error back,
checks that the final answer only states facts found in tool results, and builds any fallback reply
from tool results alone. Domain agents subclass ToolAgent and provide the prompt, the tools, how the
active working object (plan or draft) is described, and how results are rendered on fallback.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from typing import Any, Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError, model_validator

from app.core.config import Settings
from app.core.security import AuthenticatedUser
from app.llm.ports import LLMGateway


logger = logging.getLogger("app.agent_runtime")

AGENT_JSON_PROTOCOL = """Return exactly one JSON object in one of these forms:
{"action":"tool","tool_calls":[{"name":"tool_name","arguments":{...}}]}
{"action":"final","message":"grounded response or a concise request for missing information"}
"""


class ToolExecution(Protocol):
    tool: str
    ok: bool
    data: dict[str, Any]
    label: str
    sources: list[dict[str, object]]
    pending_summary: str | None
    plan: dict[str, Any] | None


class AgentTools(Protocol):
    actor: AuthenticatedUser

    def definitions(self) -> list[dict[str, Any]]: ...

    def execute(
        self, tool: str, arguments: dict[str, Any], *, session_id: str, active_plan: dict[str, Any] | None = None
    ) -> ToolExecution: ...


_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")


class AgentToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentDecision(BaseModel):
    # Unknown keys from the model are ignored rather than failing the whole turn.
    model_config = ConfigDict(extra="ignore")

    action: Literal["tool", "final"]
    tool_calls: list[AgentToolCall] = Field(default_factory=list, max_length=4)
    message: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_action(self) -> "AgentDecision":
        if self.action == "tool" and not self.tool_calls:
            raise ValueError("A tool action requires at least one tool call")
        if self.action == "final" and not (self.message and self.message.strip()):
            raise ValueError("A final action requires a message")
        return self


class AgentRunState(TypedDict, total=False):
    session_id: str
    user_message: str
    conversation: list[dict[str, str]]
    # The domain's active working object (a leave plan, an onboarding draft or plan).
    active_plan: dict[str, Any] | None
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


class ToolAgent:
    """Bounded LangGraph loop that delegates all facts and mutations to typed tools."""

    #: Shown to the model before the active working object, e.g. "Active leave plan".
    active_plan_label = "Active plan"
    #: Logged agent name.
    agent_name = "agent"

    def __init__(
        self,
        *,
        settings: Settings,
        llm: LLMGateway,
        tools: AgentTools,
        base_prompt: str,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.tools = tools
        self.base_prompt = base_prompt
        self.system_prompt = base_prompt + "\n" + AGENT_JSON_PROTOCOL
        self.native = bool(settings.leave_agent_native_tools) and hasattr(llm, "complete_with_tools")
        self.graph = self._build_graph()

    # ------------------------------------------------------------------ domain hooks

    def today(self) -> date:
        return date.today()

    def describe_active_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        return plan

    def render_from_results(self, state: AgentRunState) -> str | None:
        """Fallback reply built only from tool results; domains override for richer text."""
        for item in reversed(state.get("tool_results", [])):
            if item.get("status") != "success":
                return self.friendly_failure(str(item.get("tool")), item.get("result", {}))
        return None

    @staticmethod
    def friendly_failure(tool: str, data: dict[str, Any]) -> str:
        details = data.get("reason") or data.get("error")
        return f"I could not complete that request: {details}." if details else "I could not complete that request."

    def _build_graph(self):
        graph = StateGraph(AgentRunState)
        graph.add_node("agent", self._agent_node)
        graph.add_node("tools", self._tool_node)
        graph.add_edge(START, "agent")
        graph.add_conditional_edges(
            "agent",
            lambda state: "end" if state.get("response")
            else "tools" if state.get("pending_tool_calls")
            else "agent",
            {"tools": "tools", "end": END, "agent": "agent"},
        )
        graph.add_conditional_edges(
            "tools",
            lambda state: "end" if state.get("response") else "agent",
            {"agent": "agent", "end": END},
        )
        return graph.compile()

    def invoke(
        self,
        *,
        session_id: str,
        user_message: str,
        conversation: list[dict[str, str]],
        active_plan: dict[str, Any] | None,
        llm_calls: int,
    ) -> AgentRunState:
        initial: AgentRunState = {
            "session_id": session_id,
            "user_message": user_message,
            "conversation": conversation[-8:],
            "active_plan": active_plan,
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
            "agent_completed",
            extra={
                "session_id": session_id,
                "agent": self.agent_name,
                "iterations": result.get("iterations", 0),
                "tool_call_count": result.get("tool_call_count", 0),
                "status": result.get("final_status", "unknown"),
                "native_tools": self.native,
            },
        )
        return result

    # ------------------------------------------------------------------ model turn

    def _agent_node(self, state: AgentRunState) -> AgentRunState:
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
                    "standard", system=self.system_prompt, user=self._agent_input(state), json_mode=True
                )
                decision = self._parse_decision(raw)
        except Exception as exc:
            logger.warning(
                "agent_model_failed",
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
                "agent_model_turn",
                extra={
                    "session_id": state["session_id"],
                    "agent": self.agent_name,
                    "iteration": iteration,
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                },
            )

        updates: AgentRunState = {
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
                    "agent_ungrounded_answer",
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

    def _native_turn(self, state: AgentRunState) -> tuple[AgentDecision, dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "user", "content": self._context_message(state)}]
        messages.extend(state.get("transcript", []))
        for note in state.get("feedback", []):
            messages.append({"role": "user", "content": note})
        turn = self.llm.complete_with_tools(  # type: ignore[attr-defined]
            "standard",
            system=self.base_prompt,
            messages=messages,
            tools=self._native_tool_definitions(),
        )
        if turn.tool_calls:
            decision = AgentDecision(
                action="tool",
                tool_calls=[AgentToolCall(name=item.name, arguments=item.arguments) for item in turn.tool_calls[:4]],
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
        return AgentDecision(action="final", message=turn.content), {"role": "assistant", "content": turn.content}

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
        self, state: AgentRunState, iteration: int, llm_calls: int, note: str, *, status: str
    ) -> AgentRunState:
        repairs = state.get("repairs", 0)
        if repairs < self.settings.leave_agent_max_repairs:
            return {
                "iterations": iteration,
                "llm_calls": llm_calls,
                "repairs": repairs + 1,
                "feedback": [note],
                "pending_tool_calls": [],
            }
        rendered = self.render_from_results(state)
        return {
            "response": rendered
            or "I couldn't complete that request safely. Please tell me again what you need.",
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

    def _ungrounded_values(self, message: str, state: AgentRunState) -> list[str]:
        """Numbers in the answer that appear in no tool result, the conversation or today's date."""
        stated = self._numbers(message)
        if not stated:
            return []
        today = self.today()
        allowed_text = " ".join(
            [
                json.dumps(state.get("tool_results", []), default=str),
                json.dumps(state.get("active_plan") or {}, default=str),
                state.get("user_message", ""),
                " ".join(item.get("content", "") for item in state.get("conversation", [])),
                today.isoformat(),
            ]
        )
        allowed = self._numbers(allowed_text)
        return sorted(stated - allowed, key=lambda value: float(value))

    # ------------------------------------------------------------------ fallbacks

    def _limit_response(self, state: AgentRunState, iteration: int) -> AgentRunState:
        rendered = self.render_from_results(state)
        return {
            "response": (
                f"{rendered}\n\n(I stopped early to stay within safe limits; ask again if you need more.)"
                if rendered
                else "I stopped because the request reached the safe execution limit. Please ask again more specifically."
            ),
            "pending_tool_calls": [],
            "iterations": iteration,
            "final_status": "iteration_limit",
        }

    # ------------------------------------------------------------------ tools

    def _tool_node(self, state: AgentRunState) -> AgentRunState:
        results = list(state.get("tool_results", []))
        activities = list(state.get("agent_activity", []))
        sources = list(state.get("sources", []))
        transcript = list(state.get("transcript", []))
        plan = state.get("active_plan")
        seen = set(state.get("seen_tool_calls", []))
        count = state.get("tool_call_count", 0)
        repeated = state.get("repeated_calls", 0)
        feedback: list[str] = []

        def snapshot(**extra: Any) -> AgentRunState:
            return {
                "pending_tool_calls": [],
                "tool_results": results,
                "agent_activity": activities,
                "sources": sources,
                "active_plan": plan,
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
                rendered = self.render_from_results({**state, "tool_results": results})
                return snapshot(
                    response=(rendered + "\n\n(I stopped early to stay within safe limits.)") if rendered
                    else "I stopped because the request reached the safe tool-call limit. Please ask again more specifically.",
                    final_status="tool_limit",
                )
            signature = json.dumps({"name": call["name"], "arguments": call.get("arguments", {})}, sort_keys=True, default=str)
            if signature in seen:
                repeated += 1
                if repeated > 1:
                    rendered = self.render_from_results({**state, "tool_results": results})
                    return snapshot(
                        response=rendered
                        or "I stopped because the same tool request was repeated without new information. Please rephrase your request.",
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
                "agent_tool_executed",
                extra={
                    "session_id": state["session_id"],
                    "agent": self.agent_name,
                    "tool": execution.tool,
                    "tool_status": "success" if execution.ok else "error",
                    "iteration": state.get("iterations", 0),
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                },
            )
            if execution.pending_summary:
                return snapshot(
                    response=getattr(execution, "pending_response", None)
                    or f"{execution.pending_summary}. Reply yes to confirm or cancel.",
                    pending_summary=execution.pending_summary,
                    final_status="awaiting_confirmation",
                )
            # A failed tool result goes back to the model, which can fix its arguments,
            # try another tool or explain the factual reason to the employee.

        return snapshot()

    # ------------------------------------------------------------------ prompt

    def _context_message(self, state: AgentRunState) -> str:
        history = "\n".join(
            f"{item['role']}: {item['content'][:1000]}" for item in state.get("conversation", [])
        )
        today = self.today()
        plan = state.get("active_plan")
        described = self.describe_active_plan(plan) if isinstance(plan, dict) else None
        return (
            f"Current date: {today.isoformat()} ({today.strftime('%A')})\n"
            f"Authenticated role: {self.tools.actor.role}\n"
            f"{self.active_plan_label}: {json.dumps(described, sort_keys=True, default=str)}\n"
            f"Recent conversation:\n{history or '(none)'}\n"
            f"Current user message: {state['user_message']}"
        )

    def _agent_input(self, state: AgentRunState) -> str:
        feedback = state.get("feedback", [])
        return (
            f"{self._context_message(state)}\n\n"
            f"Available tools:\n{json.dumps(self.tools.definitions(), default=str)}\n\n"
            + ("Correction:\n" + "\n".join(feedback) + "\n\n" if feedback else "")
            + f"Tool results so far:\n{json.dumps(state.get('tool_results', []), default=str)}"
        )

    @staticmethod
    def _parse_decision(raw: str) -> AgentDecision:
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
                return AgentDecision.model_validate(payload)
            except (ValueError, json.JSONDecodeError, PydanticValidationError) as exc:
                last_error = exc
                continue
        raise ValueError(
            "The agent returned invalid structured output"
            + (f": {ToolAgent._short_error(last_error)}" if last_error else "")
        )

    @staticmethod
    def _result_payload(execution: ToolExecution) -> dict[str, Any]:
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
