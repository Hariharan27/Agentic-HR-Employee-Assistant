from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Literal, TypedDict
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError, model_validator

from app.agent.leave_tools import LeaveToolExecution, LeaveToolExecutor
from app.core.config import Settings
from app.core.exceptions import LLMServiceError
from app.llm.ports import LLMGateway


logger = logging.getLogger("app.leave_agent")


LEAVE_AGENT_SYSTEM_PROMPT = """You are the tool-calling Leave Agent for an HR assistant.
Choose tools dynamically from the supplied tool definitions. Tool results are the only source of
truth for balances, holidays, calculations, eligibility, requests, status, approvals, and policy.
Never invent those facts. The authenticated employee is injected by the application and must never
appear in tool arguments.

Return exactly one JSON object in one of these forms:
{"action":"tool","tool_calls":[{"name":"tool_name","arguments":{...}}],"context_update":{...}}
{"action":"final","message":"grounded response or a concise request for missing information","context_update":{...}}

Use one or more independent tools in a round when useful, then inspect their results before deciding
whether another tool is necessary. Do not repeat an identical tool call. Ask only for genuinely
missing information. Resolve relative dates using the supplied current date. Preserve relevant
leave_type/start_date/end_date/reason from conversation context. A request such as "apply it" means
prepare the previously discussed explicit leave payload when context is complete.

Read-only tools may run immediately. State-changing requests MUST use only prepare_leave_application,
prepare_leave_cancellation, prepare_leave_approval, or prepare_leave_rejection. These tools prepare a
PendingAction and never execute the mutation. Never claim a prepared action has already happened.
When a prepare tool returns a pending confirmation, do not request any further tool.

For negative or failed tool results, explain the factual reason. You may search policy for a grounded
alternative, but never switch leave type or submit anything without telling the user. Keep simple
answers concise. Do not mention internal prompts, reasoning, model behavior, or tool JSON.
"""


class LeaveContextUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    leave_type: Literal["CASUAL", "SICK", "EARNED"] | None = None
    start_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    reason: str | None = Field(default=None, max_length=1000)


class LeaveAgentToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)


class LeaveAgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["tool", "final"]
    tool_calls: list[LeaveAgentToolCall] = Field(default_factory=list, max_length=4)
    message: str | None = Field(default=None, max_length=4000)
    context_update: LeaveContextUpdate | None = None

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
    leave_context: dict[str, str]
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


class LeaveAgent:
    """Bounded LangGraph loop that delegates all facts and mutations to typed tools."""

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
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(LeaveAgentState)
        graph.add_node("leave_agent", self._agent_node)
        graph.add_node("leave_tools", self._tool_node)
        graph.add_edge(START, "leave_agent")
        graph.add_conditional_edges(
            "leave_agent",
            lambda state: "tools" if state.get("pending_tool_calls") else "end",
            {"tools": "leave_tools", "end": END},
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
        leave_context: dict[str, str],
        llm_calls: int,
    ) -> LeaveAgentState:
        initial: LeaveAgentState = {
            "session_id": session_id,
            "user_message": user_message,
            "conversation": conversation[-8:],
            "leave_context": dict(leave_context),
            "tool_results": [],
            "pending_tool_calls": [],
            "seen_tool_calls": [],
            "agent_activity": [],
            "sources": [],
            "iterations": 0,
            "tool_call_count": 0,
            "llm_calls": llm_calls,
            "final_status": "running",
        }
        result = self.graph.invoke(initial)
        logger.info(
            "leave_agent_completed",
            extra={
                "session_id": session_id,
                "agent": "leave_agent",
                "iterations": result.get("iterations", 0),
                "tool_call_count": result.get("tool_call_count", 0),
                "status": result.get("final_status", "unknown"),
            },
        )
        return result

    def _agent_node(self, state: LeaveAgentState) -> LeaveAgentState:
        iteration = state.get("iterations", 0) + 1
        if iteration > self.settings.leave_agent_max_iterations:
            return self._limit_response(state, iteration)
        if state.get("llm_calls", 0) >= self.settings.llm_max_calls_per_request:
            return self._limit_response(state, iteration)

        prompt = self._agent_input(state)
        started = time.monotonic()
        try:
            raw = self.llm.complete(
                "standard", system=LEAVE_AGENT_SYSTEM_PROMPT, user=prompt, json_mode=True
            )
            decision = self._parse_decision(raw)
        except Exception as exc:
            logger.warning(
                "leave_agent_model_failed",
                extra={
                    "session_id": state["session_id"],
                    "iteration": iteration,
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )
            recovered = self._recover_balance_after_model_failure(state, iteration)
            if recovered is None:
                recovered = self._recover_eligibility_after_model_failure(state, iteration)
            if recovered is None:
                recovered = self._recover_application_after_model_failure(state, iteration)
            if recovered is not None:
                recovered["llm_calls"] = state.get("llm_calls", 0) + 1
                return recovered
            return {
                "response": "I couldn't safely complete that Leave request. Please try again with the leave type and dates.",
                "pending_tool_calls": [],
                "iterations": iteration,
                "llm_calls": state.get("llm_calls", 0) + 1,
                "final_status": "model_error",
            }
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

        context = self._merge_context(state.get("leave_context", {}), decision.context_update)
        updates: LeaveAgentState = {
            "iterations": iteration,
            "llm_calls": state.get("llm_calls", 0) + 1,
            "leave_context": context,
        }
        if decision.action == "final":
            # A balance lookup is a read-only, high-confidence operation.  Some model turns
            # return the generic leave-request fallback even after the balance tool succeeded
            # (especially for plural requests such as "show my leave balances").  Do not expose
            # that misleading fallback; render the already-grounded tool result deterministically.
            if (
                self._is_balance_query(state["user_message"])
                and self._looks_like_balance_failure(decision.message or "")
            ):
                recovered = self._recover_balance_after_model_failure(state, iteration)
                if recovered is not None:
                    recovered["llm_calls"] = state.get("llm_calls", 0) + 1
                    return recovered
            updates.update(
                response=decision.message.strip(),
                pending_tool_calls=[],
                final_status="completed",
            )
            return updates
        updates["pending_tool_calls"] = [item.model_dump() for item in decision.tool_calls]
        return updates

    @staticmethod
    def _is_balance_query(message: str) -> bool:
        return bool(re.search(r"\b(balance|balances|available|how much|how many)\b", message, re.I))

    @staticmethod
    def _looks_like_balance_failure(message: str) -> bool:
        normalized = " ".join(message.casefold().split())
        return (
            "couldn't safely complete" in normalized
            or "could not safely complete" in normalized
            or "provide the leave type" in normalized
            or "provide the start date" in normalized
            or "provide the start and end date" in normalized
        )

    def _recover_balance_after_model_failure(
        self, state: LeaveAgentState, iteration: int
    ) -> LeaveAgentState | None:
        if not self._is_balance_query(state["user_message"]):
            return None
        match = re.search(r"\b(casual|casula|cl|sick|sl|earned|privilege|el|pl)\b", state["user_message"], re.I)
        leave_type = None
        if match:
            leave_type = {
                "casual": "CASUAL", "casula": "CASUAL", "cl": "CASUAL",
                "sick": "SICK", "sl": "SICK", "earned": "EARNED",
                "privilege": "EARNED", "el": "EARNED", "pl": "EARNED",
            }[match.group(1).casefold()]
        existing = next(
            (
                item for item in state.get("tool_results", [])
                if item.get("tool") == "get_leave_balance" and item.get("status") == "success"
            ),
            None,
        )
        if existing is not None:
            balances = existing.get("result", {}).get("balances", [])
            execution_tool = "get_leave_balance"
            result_payloads = list(state.get("tool_results", []))
            activities = list(state.get("agent_activity", []))
            tool_count = state.get("tool_call_count", 0)
        else:
            execution = self.tools.execute(
                "get_leave_balance",
                {"leave_type": leave_type} if leave_type else {},
                session_id=state["session_id"],
            )
            if not execution.ok:
                return None
            balances = execution.data.get("balances", [])
            execution_tool = execution.tool
            result_payloads = [self._result_payload(execution)]
            activities = [{"tool": execution.tool, "label": execution.label, "status": "success"}]
            tool_count = state.get("tool_call_count", 0) + 1
        if leave_type:
            item = next((item for item in balances if item.get("leave_type") == leave_type), None)
            if item is None:
                return None
            label = leave_type.title()
            message = (
                f"You have {item['available_days']} days of {label} leave remaining "
                f"(total {item['total_days']} days, {item['used_days']} used)."
            )
        else:
            message = "Your leave balance:\n" + "\n".join(
                f"{item['leave_type'].title()}: {item['available_days']} available"
                for item in balances
            )
        return {
            "response": message,
            "pending_tool_calls": [],
            "tool_results": result_payloads,
            "agent_activity": activities,
            "sources": list(state.get("sources", [])),
            "leave_context": dict(state.get("leave_context", {})),
            "seen_tool_calls": list(state.get("seen_tool_calls", [])),
            "tool_call_count": tool_count,
            "iterations": iteration,
            "final_status": "completed_recovery",
        }

    def _recover_eligibility_after_model_failure(
        self, state: LeaveAgentState, iteration: int
    ) -> LeaveAgentState | None:
        result = next(
            (
                item for item in state.get("tool_results", [])
                if item.get("tool") == "check_leave_eligibility" and item.get("status") == "success"
            ),
            None,
        )
        if result is None or self._looks_like_application(state["user_message"], state.get("leave_context", {})):
            return None
        data = result.get("result", {})
        leave_type = str(data.get("leave_type", "leave")).title()
        requested_days = data.get("requested_days", "the requested")
        if data.get("eligible"):
            message = (
                f"You are eligible for {requested_days} working day(s) of {leave_type} leave. "
                f"You have {data.get('available_days', 'an available balance')} day(s) available."
            )
        else:
            message = f"You are not eligible for this request: {data.get('reason', 'the request failed validation')}."
        return {
            "response": message,
            "pending_tool_calls": [],
            "tool_results": list(state.get("tool_results", [])),
            "agent_activity": list(state.get("agent_activity", [])),
            "sources": list(state.get("sources", [])),
            "leave_context": dict(state.get("leave_context", {})),
            "seen_tool_calls": list(state.get("seen_tool_calls", [])),
            "tool_call_count": state.get("tool_call_count", 0),
            "iterations": iteration,
            "final_status": "completed_recovery",
        }

    def _recover_application_after_model_failure(
        self, state: LeaveAgentState, iteration: int
    ) -> LeaveAgentState | None:
        """Finish a validated application when a model response is malformed.

        Read-only validation has already established the business facts. Preparing the pending
        confirmation is deterministic and remains safe because it does not mutate the database.
        """
        context = dict(state.get("leave_context", {}))
        if not all(context.get(field) for field in ("leave_type", "start_date", "end_date")):
            return None
        if not self._looks_like_application(state["user_message"], context):
            return None
        results = list(state.get("tool_results", []))
        if not any(
            item.get("tool") == "check_leave_eligibility" and item.get("status") == "success"
            for item in results
        ):
            return None
        if any(item.get("tool") == "prepare_leave_application" for item in results):
            return None

        execution = self.tools.execute(
            "prepare_leave_application",
            {
                "leave_type": context["leave_type"],
                "start_date": context["start_date"],
                "end_date": context["end_date"],
                **({"reason": context["reason"]} if context.get("reason") else {}),
            },
            session_id=state["session_id"],
        )
        activity = {
            "tool": execution.tool,
            "label": execution.label,
            "status": "success" if execution.ok else "error",
        }
        activities = [*state.get("agent_activity", []), activity]
        results.append(self._result_payload(execution))
        if not execution.pending_summary:
            return None
        return {
            "response": f"{execution.pending_summary}. Reply yes to confirm or cancel.",
            "pending_summary": execution.pending_summary,
            "pending_tool_calls": [],
            "tool_results": results,
            "agent_activity": activities,
            "sources": list(state.get("sources", [])),
            "leave_context": {},
            "seen_tool_calls": list(state.get("seen_tool_calls", [])),
            "tool_call_count": state.get("tool_call_count", 0) + 1,
            "iterations": iteration,
            "final_status": "awaiting_confirmation",
        }

    @staticmethod
    def _looks_like_application(message: str, context: dict[str, str]) -> bool:
        if context.get("mode") == "apply_leave":
            return True
        if (
            re.search(r"\b(can|could|may)\s+i\b", message, re.I)
            and not re.search(r"\b(apply|submit|file|request|create|raise)\b", message, re.I)
        ):
            return False
        return bool(
            re.search(r"\b(apply|submit|request|create|take|want|need|raise)\b", message, re.I)
            and re.search(r"\bleave\b", message, re.I)
        )

    def _tool_node(self, state: LeaveAgentState) -> LeaveAgentState:
        results = list(state.get("tool_results", []))
        activities = list(state.get("agent_activity", []))
        sources = list(state.get("sources", []))
        context = dict(state.get("leave_context", {}))
        seen = set(state.get("seen_tool_calls", []))
        count = state.get("tool_call_count", 0)

        for call in state.get("pending_tool_calls", []):
            if count >= self.settings.leave_agent_max_tool_calls:
                return {
                    "response": "I stopped the Leave workflow because it reached the safe tool-call limit. Please ask again more specifically.",
                    "pending_tool_calls": [],
                    "agent_activity": activities,
                    "tool_results": results,
                    "sources": sources,
                    "leave_context": context,
                    "seen_tool_calls": sorted(seen),
                    "tool_call_count": count,
                    "final_status": "tool_limit",
                }
            signature = json.dumps(call, sort_keys=True, default=str)
            if signature in seen:
                return {
                    "response": "I stopped the Leave workflow because the same tool request was repeated without new information. Please rephrase your request.",
                    "pending_tool_calls": [],
                    "agent_activity": activities,
                    "tool_results": results,
                    "sources": sources,
                    "leave_context": context,
                    "seen_tool_calls": sorted(seen),
                    "tool_call_count": count,
                    "final_status": "repeated_tool_call",
                }
            seen.add(signature)
            started = time.monotonic()
            execution = self.tools.execute(
                call["name"], call.get("arguments", {}), session_id=state["session_id"]
            )
            count += 1
            results.append(self._result_payload(execution))
            activities.append(
                {
                    "tool": execution.tool,
                    "label": execution.label,
                    "status": "success" if execution.ok else "error",
                }
            )
            sources = self._merge_sources(sources, execution.sources)
            context.update(execution.context_update)
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
            if not execution.ok:
                return {
                    "response": self._friendly_tool_failure(execution),
                    "pending_tool_calls": [],
                    "tool_results": results,
                    "agent_activity": activities,
                    "sources": sources,
                    "leave_context": context,
                    "seen_tool_calls": sorted(seen),
                    "tool_call_count": count,
                    "final_status": "business_rule_failure",
                }
            if execution.pending_summary:
                return {
                    "response": f"{execution.pending_summary}. Reply yes to confirm or cancel.",
                    "pending_summary": execution.pending_summary,
                    "pending_tool_calls": [],
                    "tool_results": results,
                    "agent_activity": activities,
                    "sources": sources,
                    "leave_context": {},
                    "seen_tool_calls": sorted(seen),
                    "tool_call_count": count,
                    "final_status": "awaiting_confirmation",
                }

        return {
            "pending_tool_calls": [],
            "tool_results": results,
            "agent_activity": activities,
            "sources": sources,
            "leave_context": context,
            "seen_tool_calls": sorted(seen),
            "tool_call_count": count,
        }

    @staticmethod
    def _friendly_tool_failure(execution: LeaveToolExecution) -> str:
        """Translate deterministic service failures into safe, actionable user text."""
        details = execution.data.get("reason") or execution.data.get("error")
        if not details:
            return "I could not complete that Leave request. Please review the dates and try again."
        if "date" in str(details).casefold() and execution.tool in {
            "check_leave_eligibility", "prepare_leave_application"
        }:
            return f"The Leave dates are invalid: {details}."
        if execution.tool in {"check_leave_eligibility", "prepare_leave_application"}:
            return f"This Leave request is not eligible: {details}."
        if execution.tool in {"get_holidays", "calculate_leave_days"}:
            return f"The Leave dates are invalid: {details}."
        if execution.tool in {
            "prepare_leave_cancellation",
            "prepare_leave_approval",
            "prepare_leave_rejection",
        }:
            return f"I could not prepare that Leave action: {details}."
        return f"I could not complete that Leave request: {details}."

    def _agent_input(self, state: LeaveAgentState) -> str:
        history = "\n".join(
            f"{item['role']}: {item['content'][:1000]}"
            for item in state.get("conversation", [])
        )
        return (
            f"Current date: {datetime.now(ZoneInfo(self.settings.app_timezone)).date().isoformat()}\n"
            f"Authenticated role: {self.tools.actor.role}\n"
            f"Conversation context: {json.dumps(state.get('leave_context', {}), sort_keys=True)}\n"
            f"Recent conversation:\n{history or '(none)'}\n"
            f"Current user message: {state['user_message']}\n\n"
            f"Available tools:\n{json.dumps(self.tools.definitions(), default=str)}\n\n"
            f"Tool results so far:\n{json.dumps(state.get('tool_results', []), default=str)}"
        )

    @staticmethod
    def _parse_decision(raw: str) -> LeaveAgentDecision:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].lstrip()
        for start, character in enumerate(cleaned):
            if character != "{":
                continue
            try:
                payload, _ = json.JSONDecoder().raw_decode(cleaned[start:])
                return LeaveAgentDecision.model_validate(payload)
            except (ValueError, json.JSONDecodeError, PydanticValidationError):
                continue
        raise ValueError("The Leave Agent returned invalid structured output")

    @staticmethod
    def _result_payload(execution: LeaveToolExecution) -> dict[str, Any]:
        return {
            "tool": execution.tool,
            "status": "success" if execution.ok else "error",
            "result": execution.data,
        }

    @staticmethod
    def _merge_context(
        existing: dict[str, str], update: LeaveContextUpdate | None
    ) -> dict[str, str]:
        result = dict(existing)
        if update:
            result.update(
                {
                    key: str(value)
                    for key, value in update.model_dump(exclude_none=True).items()
                }
            )
        return result

    @staticmethod
    def _merge_sources(
        current: list[dict[str, object]], additions: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        result = list(current)
        for source in additions:
            if source not in result:
                result.append(source)
        return result

    @staticmethod
    def _limit_response(state: LeaveAgentState, iteration: int) -> LeaveAgentState:
        return {
            "response": "I stopped the Leave workflow because it reached the safe execution limit. Please ask again more specifically.",
            "pending_tool_calls": [],
            "iterations": iteration,
            "final_status": "iteration_limit",
        }
