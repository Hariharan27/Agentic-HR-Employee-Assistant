import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError as PydanticValidationError

from app.agent.state import AgentState
from app.application.leave.service import LeaveService
from app.application.pending.service import PendingActionCoordinator
from app.core.config import Settings
from app.core.exceptions import LLMServiceError
from app.core.security import AuthenticatedUser
from app.domain.pending.entities import ConfirmationDecision
from app.infrastructure.repositories.conversation import SQLAlchemyConversationRepository
from app.llm.models import ModelTier, RouteDecision
from app.llm.ports import LLMGateway
from app.rag.service import PolicyKnowledgeService


ROUTER_SYSTEM_PROMPT = """You route requests for an HR employee assistant.
Return exactly one JSON object with these fields:
- domain: leave, policy, onboarding, parking, or general
- intent: policy_question, leave_balance, leave_eligibility, apply_leave, leave_requests,
  calculate_leave_days, holidays, onboarding, parking, or general
- confidence: number from 0 to 1
- leave_type: CASUAL, SICK, PRIVILEGE, or null
- start_date: YYYY-MM-DD or null
- end_date: YYYY-MM-DD or null
- reason: string or null

Policy or rules questions use policy/policy_question. Personal balance, eligibility, calculation,
application, holidays, and request history use leave. Never invent missing dates or fields.
When the user clearly asks about one specific date, set both start_date and end_date to that date.
Questions about general entitlements or rules use policy_question. Questions asking whether the
current employee can use a named leave type on a date use leave_eligibility, even on weekends or
holidays. If the employee does not explicitly name a leave type, leave_type must be null.
Questions about "my balance", days "available to me", or days "I have" use leave_balance, not
policy_question. Company requirements, standards, security rules, and the code of conduct use
policy/policy_question and must be answered from policy documents.
Return JSON only."""


@dataclass(frozen=True, slots=True)
class ChatResult:
    session_id: str
    message: str
    domain: str
    intent: str | None
    sources: list[dict[str, object]]
    pending_action: str | None = None


class HRAssistantOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        actor: AuthenticatedUser,
        conversations: SQLAlchemyConversationRepository,
        leave: LeaveService,
        pending: PendingActionCoordinator,
        policies: PolicyKnowledgeService,
        llm: LLMGateway,
    ):
        self.settings = settings
        self.actor = actor
        self.conversations = conversations
        self.leave = leave
        self.pending = pending
        self.policies = policies
        self.llm = llm
        self.graph = self._build_graph()

    def chat(self, session_id: str, message: str) -> ChatResult:
        stored = self.conversations.load_or_create(session_id, self.actor.user_id)
        history = self._safe_history(stored.get("messages"))
        state: AgentState = {
            "session_id": session_id,
            "user_id": self.actor.user_id,
            "employee_id": self.actor.employee_id,
            "role": self.actor.role,
            "user_message": message.strip(),
            "messages": history,
            "active_domain": self._safe_domain(stored.get("active_domain")),
            "sources": [],
            "llm_calls": 0,
        }
        result = self.graph.invoke(state)
        response = result["response"]
        updated_history = [
            *history,
            {"role": "user", "content": state["user_message"]},
            {"role": "assistant", "content": response},
        ][-20:]
        self.conversations.save(
            session_id,
            self.actor.user_id,
            {"messages": updated_history, "active_domain": result.get("active_domain")},
        )
        route = result.get("route")
        domain = route.domain if isinstance(route, RouteDecision) else result.get("active_domain") or "general"
        return ChatResult(
            session_id=session_id,
            message=response,
            domain=domain,
            intent=route.intent if isinstance(route, RouteDecision) else None,
            sources=result.get("sources", []),
            pending_action=result.get("pending_summary"),
        )

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("resolve_pending_action", self._resolve_pending_action)
        graph.add_node("confirmation", self._handle_confirmation)
        graph.add_node("router", self._route)
        graph.add_node("policy", self._handle_policy)
        graph.add_node("leave", self._handle_leave)
        graph.add_node("unsupported", self._handle_unsupported_domain)
        graph.add_node("general", self._handle_general)
        graph.add_edge(START, "resolve_pending_action")
        graph.add_conditional_edges(
            "resolve_pending_action",
            lambda state: "confirmation" if state.get("pending_action") else "router",
            {"confirmation": "confirmation", "router": "router"},
        )
        graph.add_conditional_edges(
            "router",
            self._route_destination,
            {
                "policy": "policy",
                "leave": "leave",
                "unsupported": "unsupported",
                "general": "general",
            },
        )
        for node in ("confirmation", "policy", "leave", "unsupported", "general"):
            graph.add_edge(node, END)
        return graph.compile()

    def _resolve_pending_action(self, state: AgentState) -> AgentState:
        action = self.pending.get(self.actor, state["session_id"])
        return {"pending_action": action, "pending_summary": action.summary if action else None}

    def _handle_confirmation(self, state: AgentState) -> AgentState:
        decision = ConfirmationDecision.from_message(state["user_message"])
        action = state["pending_action"]
        if decision is ConfirmationDecision.UNKNOWN:
            return {
                "response": f"An action is waiting for confirmation: {action.summary}. Reply yes to confirm or cancel.",
                "active_domain": "leave",
            }
        if decision is ConfirmationDecision.CANCEL:
            self.pending.cancel(self.actor, state["session_id"])
            return {
                "response": "The pending action has been cancelled. No changes were made.",
                "active_domain": "leave",
                "pending_summary": None,
            }
        created = self.pending.confirm(self.actor, state["session_id"])
        return {
            "response": (
                f"Your {created.leave_type.value.title()} leave request for "
                f"{self._number(created.working_days)} working day(s) was submitted successfully "
                f"with request ID {created.id}."
            ),
            "active_domain": "leave",
            "pending_summary": None,
        }

    def _route(self, state: AgentState) -> AgentState:
        today = datetime.now(ZoneInfo(self.settings.app_timezone)).date().isoformat()
        prompt = self._routing_input(state, today)
        raw, calls = self._complete(state, "router", ROUTER_SYSTEM_PROMPT, prompt, json_mode=True)
        try:
            decision = self._parse_route(raw)
        except LLMServiceError:
            decision = None
        if decision is None or decision.confidence < self.settings.complex_escalation_threshold:
            raw, calls = self._complete(
                {**state, "llm_calls": calls},
                "complex",
                ROUTER_SYSTEM_PROMPT,
                prompt,
                json_mode=False,
            )
            decision = self._parse_route(raw)
        decision = self._apply_routing_guards(decision, state["user_message"])
        return {"route": decision, "active_domain": decision.domain, "llm_calls": calls}

    @staticmethod
    def _route_destination(state: AgentState) -> str:
        domain = state["route"].domain
        if domain in {"policy", "leave", "general"}:
            return domain
        return "unsupported"

    def _handle_policy(self, state: AgentState) -> AgentState:
        context = self.policies.search(state["user_message"])
        system = (
            "Answer the employee's HR policy question using only the supplied policy context. "
            "Do not add rules that are absent. If passages conflict, say so. Be concise and cite "
            "the document and page in the answer."
        )
        raw, calls = self._complete(
            state,
            "standard",
            system,
            f"Question:\n{state['user_message']}\n\nPolicy context:\n{context.text}",
        )
        return {
            "response": raw,
            "sources": context.sources,
            "active_domain": "policy",
            "llm_calls": calls,
        }

    def _handle_leave(self, state: AgentState) -> AgentState:
        route: RouteDecision = state["route"]
        if route.intent == "leave_balance":
            balances = self.leave.get_leave_balance(self.actor, route.leave_type)
            lines = [
                f"{balance.leave_type.value.title()}: {self._number(balance.available_days)} available "
                f"({self._number(balance.pending_days)} pending)"
                for balance in balances
            ]
            return {"response": "Your leave balance:\n" + "\n".join(lines), "active_domain": "leave"}
        if route.intent == "leave_requests":
            requests = self.leave.get_my_leave_requests(self.actor)
            if not requests:
                return {"response": "You do not have any leave requests.", "active_domain": "leave"}
            lines = [
                f"#{item.id}: {item.leave_type.value.title()} {item.start_date} to {item.end_date} "
                f"— {self._number(item.working_days)} day(s), {item.status.value.title()}"
                for item in requests[:10]
            ]
            return {"response": "Your recent leave requests:\n" + "\n".join(lines), "active_domain": "leave"}
        if route.intent in {"leave_eligibility", "apply_leave", "calculate_leave_days", "holidays"}:
            missing = self._missing_leave_fields(route)
            if missing:
                return {"response": f"Please provide {', '.join(missing)}.", "active_domain": "leave"}
        if route.intent == "holidays":
            holidays = sorted(self.leave.get_holidays(route.start_date, route.end_date))
            message = "No configured holidays fall in that range."
            if holidays:
                message = "Configured holidays in that range: " + ", ".join(map(str, holidays)) + "."
            return {"response": message, "active_domain": "leave"}
        if route.intent == "calculate_leave_days":
            days = self.leave.calculate_leave_days(route.start_date, route.end_date)
            return {"response": f"That range contains {self._number(days)} working leave day(s).",
                    "active_domain": "leave"}
        if route.intent in {"leave_eligibility", "apply_leave"}:
            eligibility = self.leave.check_leave_eligibility(
                self.actor, route.leave_type, route.start_date, route.end_date
            )
            if not eligibility.eligible:
                return {"response": f"You are not eligible for this request: {eligibility.reason}.",
                        "active_domain": "leave"}
            if route.intent == "leave_eligibility":
                return {
                    "response": (
                        f"You are eligible. The request uses {self._number(eligibility.working_days)} "
                        f"working day(s), and you have {self._number(eligibility.available_days)} available."
                    ),
                    "active_domain": "leave",
                }
            summary = (
                f"Apply for {self._number(eligibility.working_days)} working day(s) of "
                f"{route.leave_type.title()} leave from {route.start_date} to {route.end_date}"
            )
            action = self.pending.propose(
                self.actor,
                state["session_id"],
                "apply_leave",
                {
                    "leave_type": route.leave_type,
                    "start_date": route.start_date.isoformat(),
                    "end_date": route.end_date.isoformat(),
                    "reason": route.reason,
                },
                summary,
            )
            return {
                "response": f"{action.summary}. Reply yes to confirm or cancel.",
                "active_domain": "leave",
                "pending_summary": action.summary,
            }
        return {
            "response": "I can help with leave balance, eligibility, applications, holidays, and request history.",
            "active_domain": "leave",
        }

    @staticmethod
    def _handle_unsupported_domain(state: AgentState) -> AgentState:
        domain = state["route"].domain
        return {
            "response": f"The {domain} workflow is planned for a later phase and is not available yet.",
            "active_domain": domain,
        }

    def _handle_general(self, state: AgentState) -> AgentState:
        raw, calls = self._complete(
            state,
            "standard",
            "You are a concise HR assistant. Explain that you currently support leave and policy questions. "
            "Do not claim to execute onboarding, parking, payroll, or other unavailable workflows.",
            self._history_text(state),
        )
        return {"response": raw, "active_domain": "general", "llm_calls": calls}

    def _complete(
        self,
        state: AgentState,
        tier: ModelTier,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
    ) -> tuple[str, int]:
        calls = state.get("llm_calls", 0)
        if calls >= self.settings.llm_max_calls_per_request:
            raise LLMServiceError("The model call budget for this request was exhausted")
        return self.llm.complete(tier, system=system, user=user, json_mode=json_mode), calls + 1

    def _routing_input(self, state: AgentState, today: str) -> str:
        return f"Today's date is {today}.\n{self._history_text(state)}"

    @staticmethod
    def _history_text(state: AgentState) -> str:
        history = state.get("messages", [])[-8:]
        lines = [f"{item['role']}: {item['content'][:1000]}" for item in history]
        lines.append(f"user: {state['user_message']}")
        return "Conversation:\n" + "\n".join(lines)

    @staticmethod
    def _parse_route(raw: str) -> RouteDecision:
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
                return RouteDecision.model_validate(payload)
            except (ValueError, json.JSONDecodeError, PydanticValidationError) as exc:
                last_error = exc
        raise LLMServiceError("The routing model returned invalid structured output") from last_error

    @staticmethod
    def _apply_routing_guards(decision: RouteDecision, message: str) -> RouteDecision:
        """Enforce high-value routing and extraction invariants after probabilistic classification."""
        normalized = message.casefold()
        patterns = {
            "CASUAL": r"\b(casual(?:\s+leave)?|cl)\b",
            "SICK": r"\b(sick(?:\s+leave)?|sl)\b",
            "PRIVILEGE": r"\b(privilege(?:\s+leave)?|earned(?:\s+leave)?|pl|el)\b",
        }
        explicit_types = [leave_type for leave_type, pattern in patterns.items() if re.search(pattern, message, re.I)]
        explicit_type = explicit_types[0] if len(explicit_types) == 1 else None

        personal = bool(re.search(r"\b(i|my|me)\b", normalized))
        balance_signal = bool(
            re.search(r"\b(balance|available\s+to\s+me|do\s+i\s+have|i\s+have)\b", normalized)
        )
        apply_signal = (
            bool(re.search(r"\b(apply|submit|request)\b", normalized))
            and (personal or normalized.lstrip().startswith(("apply ", "submit ", "request ")))
            and ("leave" in normalized or explicit_type is not None)
        )
        eligibility_signal = personal and bool(re.search(r"\b(eligible|can\s+i|could\s+i|may\s+i)\b", normalized))
        policy_signal = any(
            phrase in normalized
            for phrase in (
                "policy", "according to", "requirement", "code of conduct", "standard",
                "carry forward", "carried forward", "encash", "entitlement", "approved leave",
                "notice period", "resignation notice", "serving notice",
            )
        )
        unsupported_action_signal = any(
            phrase in normalized
            for phrase in ("expense", "payroll", "bank account", "laptop", "performance review", "insurance")
        )

        updates: dict[str, object] = {}
        if unsupported_action_signal:
            updates.update(
                domain="general", intent="general", confidence=max(decision.confidence, 0.95),
                leave_type=None, start_date=None, end_date=None,
            )
        elif apply_signal:
            updates.update(domain="leave", intent="apply_leave", confidence=max(decision.confidence, 0.95))
        elif policy_signal:
            # A question about an organisation-wide rule can naturally use
            # "can I".  Prefer the explicit policy cue over that generic
            # eligibility wording so it is answered from the policy corpus.
            updates.update(
                domain="policy", intent="policy_question", confidence=max(decision.confidence, 0.95),
                leave_type=None, start_date=None, end_date=None,
            )
        elif eligibility_signal:
            updates.update(domain="leave", intent="leave_eligibility", confidence=max(decision.confidence, 0.95))
        elif balance_signal and personal:
            updates.update(domain="leave", intent="leave_balance", confidence=max(decision.confidence, 0.95))

        guarded_intent = updates.get("intent", decision.intent)
        if guarded_intent in {"apply_leave", "leave_eligibility", "leave_balance"}:
            updates["leave_type"] = explicit_type

        if guarded_intent in {"apply_leave", "leave_eligibility", "calculate_leave_days", "holidays"}:
            iso_dates = [date.fromisoformat(value) for value in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", message)]
            if len(iso_dates) == 1:
                updates.update(start_date=iso_dates[0], end_date=iso_dates[0])
            elif len(iso_dates) >= 2:
                updates.update(start_date=iso_dates[0], end_date=iso_dates[1])

        return decision.model_copy(update=updates) if updates else decision

    @staticmethod
    def _missing_leave_fields(route: RouteDecision) -> list[str]:
        missing: list[str] = []
        if route.intent in {"leave_eligibility", "apply_leave"} and not route.leave_type:
            missing.append("the leave type")
        if route.start_date is None:
            missing.append("a start date")
        if route.end_date is None:
            missing.append("an end date")
        return missing

    @staticmethod
    def _number(value: Decimal) -> str:
        return format(value.normalize(), "f")

    @staticmethod
    def _safe_history(value: object) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        return [
            {"role": item["role"], "content": item["content"]}
            for item in value
            if isinstance(item, dict)
            and item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
        ][-20:]

    @staticmethod
    def _safe_domain(value: object) -> str | None:
        return value if value in {"leave", "policy", "onboarding", "parking", "general"} else None
