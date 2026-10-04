import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError as PydanticValidationError

from app.agent.state import AgentState
from app.application.leave.service import LeaveService
from app.application.onboarding.service import OnboardingService
from app.application.parking.service import ParkingService
from app.application.pending.service import PendingActionCoordinator
from app.core.config import Settings
from app.core.exceptions import LLMServiceError, ParkingUnavailableError
from app.core.security import AuthenticatedUser, require_role
from app.domain.onboarding.entities import TASK_TITLES, OnboardingCandidate
from app.domain.pending.entities import ConfirmationDecision
from app.infrastructure.repositories.conversation import SQLAlchemyConversationRepository
from app.llm.models import ModelTier, RouteDecision
from app.llm.ports import LLMGateway
from app.rag.service import PolicyKnowledgeService


ROUTER_SYSTEM_PROMPT = """You route requests for an HR employee assistant.
Return exactly one JSON object with these fields:
- domain: leave, policy, onboarding, parking, or general
- intent: policy_question, leave_balance, leave_eligibility, apply_leave, leave_requests,
  manager_leave_requests, approve_leave_request, reject_leave_request, cancel_leave_request,
  leave_request_history, calculate_leave_days, holidays, start_onboarding, onboarding_status,
  onboarding_approvals, approve_onboarding, reject_onboarding, parking_vehicle,
  parking_availability, reserve_parking, parking_reservations, cancel_parking,
  join_parking_waitlist, parking_admin_reservations, check_in_parking,
  admin_cancel_parking, mark_parking_no_show, override_parking_no_show,
  complete_parking, parking, or general
- confidence: number from 0 to 1
- leave_type: CASUAL, SICK, PRIVILEGE, or null
- start_date: YYYY-MM-DD or null
- end_date: YYYY-MM-DD or null
- reason: string or null
- request_id: positive integer or null
- employee_name: string or null
- employee_email: string or null
- designation: string or null
- department: string or null
- reporting_manager: string or null
- joining_date: YYYY-MM-DD or null
- location: string or null
- employment_type: string or null
- parking_date: YYYY-MM-DD or null

Policy or rules questions use policy/policy_question. Personal balance, eligibility, calculation,
application, holidays, and request history use leave. Never invent missing dates or fields.
Manager approval queues and approval/rejection actions use their dedicated intents. An employee
cancelling their own submitted request uses cancel_leave_request. Extract a request ID only when
the user explicitly provides it. Rejection reasons belong in reason. Use leave_requests for a list
of the employee's recent requests. Use leave_request_history only for the audit trail of one
specific request ID.
When the user clearly asks about one specific date, set both start_date and end_date to that date.
Questions about general entitlements or rules use policy_question. Questions asking whether the
current employee can use a named leave type on a date use leave_eligibility, even on weekends or
holidays. If the employee does not explicitly name a leave type, leave_type must be null.
Questions about "my balance", days "available to me", or days "I have" use leave_balance, not
policy_question. Company requirements, standards, security rules, and the code of conduct use
policy/policy_question and must be answered from policy documents.
Use onboarding/start_onboarding when a Manager or HR user wants to onboard a new employee or is
answering missing onboarding questions. Use onboarding/onboarding_status for an onboarding status
question. Use onboarding/onboarding_approvals for the HR administrator's pending onboarding queue,
and the dedicated approve_onboarding or reject_onboarding intents for reviewing one onboarding
request. Extract only onboarding values explicitly stated by the user; never guess a name, email,
role, department, manager, date, location, or employment type. Existing values are supplied
separately and should not be repeated as newly extracted fields.
Use parking_vehicle for the employee's registered vehicle. Use parking_availability when the user
asks whether parking is available, reserve_parking when they ask to book, parking_reservations to
view their booking, cancel_parking to cancel it, and join_parking_waitlist for a full-day waitlist.
Parking operations apply only to the authenticated employee. Extract parking_date only when the
user explicitly supplies a date or relative date such as tomorrow.
Parking Administrator requests to view all reservations, verify arrival, perform a late
cancellation, mark a no-show, correct a no-show, or complete a visit use the dedicated administrator
intents. Put an explicitly supplied parking reservation ID in request_id and an audit explanation
in reason. Never infer an administrator action from an ordinary employee request.
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
        onboarding: OnboardingService,
        parking: ParkingService,
        pending: PendingActionCoordinator,
        policies: PolicyKnowledgeService,
        llm: LLMGateway,
    ):
        self.settings = settings
        self.actor = actor
        self.conversations = conversations
        self.leave = leave
        self.onboarding = onboarding
        self.parking = parking
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
            "leave_context": self._safe_leave_context(stored.get("leave_context")),
            "onboarding_context": self._safe_onboarding_context(
                stored.get("onboarding_context")
            ),
            "parking_context": self._safe_parking_context(stored.get("parking_context")),
            "sources": [],
            "llm_calls": 0,
        }
        result = self.graph.invoke(state)
        response = result["response"]
        stored_response = (
            "Onboarding was approved and one-time credentials were shown to the HR administrator."
            if result.get("sensitive_response")
            else response
        )
        updated_history = [
            *history,
            {"role": "user", "content": state["user_message"]},
            {"role": "assistant", "content": stored_response},
        ][-20:]
        self.conversations.save(
            session_id,
            self.actor.user_id,
            {
                "messages": updated_history,
                "active_domain": result.get("active_domain"),
                "leave_context": result.get("leave_context", {}),
                "onboarding_context": result.get("onboarding_context", {}),
                "parking_context": result.get("parking_context", state.get("parking_context", {})),
            },
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
        graph.add_node("onboarding", self._handle_onboarding)
        graph.add_node("parking", self._handle_parking)
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
                "onboarding": "onboarding",
                "parking": "parking",
                "unsupported": "unsupported",
                "general": "general",
            },
        )
        for node in (
            "confirmation", "policy", "leave", "onboarding", "parking", "unsupported", "general"
        ):
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
                "active_domain": self._action_domain(action.action_type),
            }
        if decision is ConfirmationDecision.CANCEL:
            self.pending.cancel(self.actor, state["session_id"])
            return {
                "response": "The pending action has been cancelled. No changes were made.",
                "active_domain": self._action_domain(action.action_type),
                "pending_summary": None,
                "leave_context": {},
                "onboarding_context": {},
                "parking_context": {},
            }
        created = self.pending.confirm(self.actor, state["session_id"])
        action_type = action.action_type
        if action_type == "approve_leave_request":
            response = f"Leave request #{created.id} was approved successfully."
        elif action_type == "reject_leave_request":
            response = f"Leave request #{created.id} was rejected successfully."
        elif action_type == "cancel_leave_request":
            response = f"Your leave request #{created.id} was cancelled successfully."
        elif action_type == "create_onboarding":
            response = (
                f"Onboarding request #{created.id} for {created.candidate.name} was created "
                f"successfully and is pending HR administrator approval. "
                f"It includes {created.total_tasks} provisioning tasks."
            )
        elif action_type == "approve_onboarding":
            response = (
                f"Onboarding request #{created.request.id} was approved and the employee account is active.\n"
                f"Employee code: {created.employee_code}\n"
                f"Username: {created.username}\n"
                f"Temporary password: {created.temporary_password}\n"
                "Share these credentials securely. This password is shown only once."
            )
        elif action_type == "reject_onboarding":
            response = f"Onboarding request #{created.id} was rejected successfully."
        elif action_type == "reserve_parking":
            response = (
                f"Parking slot {created.slot.code} is reserved for {created.reservation_date}. "
                f"Reservation ID: {created.id}."
            )
        elif action_type == "cancel_parking":
            response = (
                f"Your parking reservation #{created.id} for {created.reservation_date} "
                "was cancelled successfully."
            )
        elif action_type == "join_parking_waitlist":
            response = f"You were added to the parking waitlist for {created.requested_date}."
        elif action_type == "check_in_parking":
            response = (
                f"Parking reservation #{created.id} for {created.employee_name or created.employee_code} "
                "was checked in successfully."
            )
        elif action_type == "admin_cancel_parking":
            response = f"Parking reservation #{created.id} was cancelled by the Parking Administrator."
        elif action_type == "mark_parking_no_show":
            response = f"Parking reservation #{created.id} was marked as a no-show."
        elif action_type == "override_parking_no_show":
            response = f"The no-show on parking reservation #{created.id} was corrected successfully."
        elif action_type == "complete_parking":
            response = f"Parking reservation #{created.id} was marked completed."
        else:
            response = (
                f"Your {created.leave_type.value.title()} leave request for "
                f"{self._number(created.working_days)} working day(s) was submitted successfully "
                f"with request ID {created.id}."
            )
        result = {
            "response": response,
            "active_domain": self._action_domain(action_type),
            "pending_summary": None,
            "leave_context": {},
            "onboarding_context": {},
            "parking_context": {},
        }
        if action_type == "approve_onboarding":
            result["sensitive_response"] = True
        return result

    @staticmethod
    def _action_domain(action_type: str) -> str:
        if action_type in {
            "reserve_parking",
            "cancel_parking",
            "join_parking_waitlist",
            "check_in_parking",
            "admin_cancel_parking",
            "mark_parking_no_show",
            "override_parking_no_show",
            "complete_parking",
        }:
            return "parking"
        return "onboarding" if action_type in {
            "create_onboarding", "approve_onboarding", "reject_onboarding"
        } else "leave"

    def _route(self, state: AgentState) -> AgentState:
        security_route = self._security_route(state["user_message"])
        if security_route is not None:
            return {"route": security_route, "active_domain": security_route.domain, "llm_calls": 0}

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
        decision = self._apply_routing_guards(
            decision, state["user_message"], date.fromisoformat(today)
        )
        if (
            state.get("active_domain") == "onboarding"
            and state.get("onboarding_context")
            and decision.domain == "general"
        ):
            decision = decision.model_copy(
                update={"domain": "onboarding", "intent": "start_onboarding", "confidence": 0.99}
            )
        elif (
            state.get("active_domain") == "parking"
            and state.get("parking_context")
            and decision.domain == "general"
        ):
            decision = decision.model_copy(
                update={"domain": "parking", "intent": "parking", "confidence": 0.99}
            )
        elif (
            state.get("active_domain") == "leave"
            and state.get("leave_context")
            and (
                decision.domain == "general"
                or self._message_is_only_leave_type(state["user_message"])
                or self._extract_leave_dates(
                    state["user_message"], date.fromisoformat(today)
                ) != (None, None)
            )
        ):
            decision = decision.model_copy(
                update={"domain": "leave", "intent": "apply_leave", "confidence": 0.99}
            )
        return {"route": decision, "active_domain": decision.domain, "llm_calls": calls}

    @staticmethod
    def _security_route(message: str) -> RouteDecision | None:
        """Route explicit policy-manipulation attacks without asking a model to obey them."""
        normalized = message.casefold()
        attempts_to_override = bool(
            re.search(r"\b(ignore|disregard|override|bypass)\b", normalized)
        )
        attempts_to_invent = bool(
            re.search(r"\b(invent|fabricate|make\s+up|change)\b", normalized)
        )
        targets_policy = "policy" in normalized and bool(
            re.search(r"\b(document|rule|policy|policies)\b", normalized)
        )
        if attempts_to_override and attempts_to_invent and targets_policy:
            return RouteDecision(
                domain="policy",
                intent="policy_question",
                confidence=1.0,
            )
        return None

    @staticmethod
    def _route_destination(state: AgentState) -> str:
        domain = state["route"].domain
        if domain in {"policy", "leave", "onboarding", "parking", "general"}:
            return domain
        return "unsupported"

    def _handle_policy(self, state: AgentState) -> AgentState:
        context = self.policies.search(state["user_message"])
        system = (
            "Answer the employee's HR policy question using only the supplied policy context. "
            "Do not add rules that are absent, and explicitly mention any conflict in the passages. "
            "Lead with the direct answer and use at most three short sentences unless the employee "
            "asks for steps or a detailed explanation. Use plain text only: no Markdown, headings, "
            "bullets, quotations, document names, page numbers, or inline citations. The interface "
            "shows source documents separately. Use natural grammar and spacing, such as '12 days'."
        )
        raw, calls = self._complete(
            state,
            "standard",
            system,
            f"Question:\n{state['user_message']}\n\nPolicy context:\n{context.text}",
        )
        return {
            "response": self._clean_policy_response(raw),
            "sources": context.sources,
            "active_domain": "policy",
            "llm_calls": calls,
        }

    @staticmethod
    def _clean_policy_response(response: str) -> str:
        """Keep policy answers readable when a model ignores presentation instructions."""
        cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", response, flags=re.DOTALL)
        cleaned = re.sub(r"__(.*?)__", r"\1", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r"\s*\[[^\]\n]*\.pdf[^\]\n]*\]", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(
            r"(?<=\d)(?=(?:days?|weeks?|months?|years?|hours?)\b)",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"[ \t]+([.,;:])", r"\1", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _handle_leave(self, state: AgentState) -> AgentState:
        route: RouteDecision = state["route"]
        if route.intent == "leave_balance":
            balances = self.leave.get_leave_balance(self.actor, route.leave_type)
            lines = [
                f"{balance.leave_type.value.title()}: {self._number(balance.available_days)} available "
                f"({self._number(balance.pending_days)} pending)"
                for balance in balances
            ]
            return {"response": "Your leave balance:\n" + "\n".join(lines), "active_domain": "leave", "leave_context": {}}
        if route.intent == "leave_requests":
            requests = self.leave.get_my_leave_requests(self.actor)
            if not requests:
                return {"response": "You do not have any leave requests.", "active_domain": "leave", "leave_context": {}}
            lines = [
                f"#{item.id}: {item.leave_type.value.title()} {item.start_date} to {item.end_date} "
                f"— {self._number(item.working_days)} day(s), {item.status.value.title()}"
                for item in requests[:10]
            ]
            return {"response": "Your recent leave requests:\n" + "\n".join(lines), "active_domain": "leave", "leave_context": {}}
        if route.intent == "manager_leave_requests":
            requests = self.leave.get_managed_leave_requests(self.actor)
            if not requests:
                return {"response": "There are no pending leave requests in your approval queue.",
                        "active_domain": "leave", "leave_context": {}}
            lines = [
                f"#{item.id}: {item.employee_name or item.employee_code or item.employee_id} — "
                f"{item.leave_type.value.title()} {item.start_date} to {item.end_date}, "
                f"{self._number(item.working_days)} day(s)"
                for item in requests[:20]
            ]
            return {"response": "Pending leave approvals:\n" + "\n".join(lines),
                    "active_domain": "leave", "leave_context": {}}
        if route.intent == "leave_request_history":
            if route.request_id is None:
                return {"response": "Please provide the leave request ID.", "active_domain": "leave", "leave_context": {}}
            events = self.leave.get_leave_request_history(self.actor, route.request_id)
            lines = [
                f"{item.to_status.value.title()} by user #{item.actor_user_id}"
                + (f" — {item.comment}" if item.comment else "")
                for item in events
            ]
            return {"response": f"History for leave request #{route.request_id}:\n" + "\n".join(lines),
                    "active_domain": "leave", "leave_context": {}}
        if route.intent in {"approve_leave_request", "reject_leave_request", "cancel_leave_request"}:
            if route.request_id is None:
                return {"response": "Please provide the leave request ID.", "active_domain": "leave", "leave_context": {}}
            if route.intent == "reject_leave_request" and not route.reason:
                return {"response": "Please provide a reason for rejecting the leave request.",
                        "active_domain": "leave", "leave_context": {}}
            if route.intent == "cancel_leave_request":
                request = self.leave.prepare_leave_cancellation(self.actor, route.request_id)
                action_type = "cancel_leave_request"
                arguments = {"request_id": route.request_id, "reason": route.reason}
                summary = f"Cancel your pending leave request #{request.id}"
            else:
                request = self.leave.prepare_leave_decision(self.actor, route.request_id)
                if route.intent == "approve_leave_request":
                    action_type = "approve_leave_request"
                    arguments = {"request_id": route.request_id, "comment": route.reason}
                    summary = f"Approve leave request #{request.id} for {request.employee_name or request.employee_code}"
                else:
                    action_type = "reject_leave_request"
                    arguments = {"request_id": route.request_id, "reason": route.reason}
                    summary = f"Reject leave request #{request.id}: {route.reason}"
            action = self.pending.propose(
                self.actor, state["session_id"], action_type, arguments, summary
            )
            return {
                "response": f"{action.summary}. Reply yes to confirm or cancel.",
                "active_domain": "leave",
                "leave_context": {},
                "pending_summary": action.summary,
            }
        if route.intent == "apply_leave":
            context = self._merge_leave_context(
                state.get("leave_context", {}), route, state["user_message"]
            )
            route = route.model_copy(
                update={
                    "leave_type": context.get("leave_type"),
                    "start_date": date.fromisoformat(context["start_date"]) if context.get("start_date") else None,
                    "end_date": date.fromisoformat(context["end_date"]) if context.get("end_date") else None,
                    "reason": context.get("reason"),
                }
            )
        else:
            context = {}
        if route.intent in {"leave_eligibility", "apply_leave", "calculate_leave_days", "holidays"}:
            missing = self._missing_leave_fields(route)
            if missing:
                return {
                    "response": f"Please provide {', '.join(missing)}.",
                    "active_domain": "leave",
                    "leave_context": context if route.intent == "apply_leave" else {},
                }
        if route.intent == "holidays":
            holidays = sorted(self.leave.get_holidays(route.start_date, route.end_date))
            message = "No configured holidays fall in that range."
            if holidays:
                message = "Configured holidays in that range: " + ", ".join(map(str, holidays)) + "."
            return {"response": message, "active_domain": "leave", "leave_context": {}}
        if route.intent == "calculate_leave_days":
            days = self.leave.calculate_leave_days(route.start_date, route.end_date)
            return {"response": f"That range contains {self._number(days)} working leave day(s).",
                    "active_domain": "leave", "leave_context": {}}
        if route.intent in {"leave_eligibility", "apply_leave"}:
            eligibility = self.leave.check_leave_eligibility(
                self.actor, route.leave_type, route.start_date, route.end_date
            )
            if not eligibility.eligible:
                return {"response": f"You are not eligible for this request: {eligibility.reason}.",
                        "active_domain": "leave", "leave_context": {}}
            if route.intent == "leave_eligibility":
                return {
                    "response": (
                        f"You are eligible. The request uses {self._number(eligibility.working_days)} "
                        f"working day(s), and you have {self._number(eligibility.available_days)} available."
                    ),
                    "active_domain": "leave",
                    "leave_context": {},
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
                "leave_context": {},
                "pending_summary": action.summary,
            }
        return {
            "response": "I can help with leave balance, eligibility, applications, holidays, and request history.",
            "active_domain": "leave",
            "leave_context": {},
        }

    def _handle_onboarding(self, state: AgentState) -> AgentState:
        route: RouteDecision = state["route"]
        if route.intent == "onboarding_approvals":
            requests = self.onboarding.get_pending_approvals(self.actor)
            if not requests:
                return {
                    "response": "There are no onboarding requests pending approval.",
                    "active_domain": "onboarding",
                }
            lines = [
                f"#{item.id}: {item.candidate.name} — {item.candidate.designation}, "
                f"joining {item.candidate.joining_date}"
                for item in requests[:20]
            ]
            return {
                "response": "Pending onboarding approvals:\n" + "\n".join(lines),
                "active_domain": "onboarding",
            }
        if route.intent in {"approve_onboarding", "reject_onboarding"}:
            if route.request_id is None:
                return {
                    "response": "Please provide the onboarding request ID.",
                    "active_domain": "onboarding",
                }
            if route.intent == "reject_onboarding" and not route.reason:
                return {
                    "response": "Please provide a reason for rejecting the onboarding request.",
                    "active_domain": "onboarding",
                }
            request = self.onboarding.prepare_review(self.actor, route.request_id)
            if route.intent == "approve_onboarding":
                action_type = "approve_onboarding"
                arguments = {"request_id": request.id, "comment": route.reason}
                summary = f"Approve onboarding request #{request.id} for {request.candidate.name}"
            else:
                action_type = "reject_onboarding"
                arguments = {"request_id": request.id, "reason": route.reason}
                summary = f"Reject onboarding request #{request.id} for {request.candidate.name}: {route.reason}"
            action = self.pending.propose(
                self.actor, state["session_id"], action_type, arguments, summary
            )
            return {
                "response": f"{action.summary}. Reply yes to confirm or cancel.",
                "active_domain": "onboarding",
                "pending_summary": action.summary,
            }
        if route.intent == "onboarding_status":
            if route.request_id is not None:
                request = self.onboarding.get_onboarding_status(self.actor, route.request_id)
            else:
                query = self._trusted_onboarding_lookup(route, state["user_message"])
                if query is None:
                    return {
                        "response": "Please provide the employee name, email, or onboarding request ID.",
                        "active_domain": "onboarding",
                    }
                request = self.onboarding.find_onboarding_status(self.actor, query)
            task_lines = [
                f"{task.title}: {task.status.value.replace('_', ' ').title()}"
                for task in request.tasks
            ]
            return {
                "response": (
                    f"{request.candidate.name} — {request.candidate.designation}\n"
                    f"Joining: {request.candidate.joining_date}\n"
                    f"Status: {request.status.value.replace('_', ' ').title()} "
                    f"({request.completed_tasks}/{request.total_tasks} completed)\n"
                    + "\n".join(task_lines)
                ),
                "active_domain": "onboarding",
            }

        require_role(self.actor, "MANAGER", "HR")

        context = self._merge_onboarding_context(
            state.get("onboarding_context", {}), route, state["user_message"]
        )
        required = (
            ("name", "employee name"),
            ("email", "email"),
            ("designation", "designation"),
            ("department", "department"),
            ("reporting_manager", "reporting manager"),
            ("joining_date", "joining date"),
            ("location", "location"),
            ("employment_type", "employment type"),
        )
        missing = [label for field, label in required if not context.get(field)]
        if missing:
            return {
                "response": "Please provide " + ", ".join(missing) + ".",
                "active_domain": "onboarding",
                "onboarding_context": context,
            }

        candidate = OnboardingCandidate(
            name=context["name"],
            email=context["email"],
            designation=context["designation"],
            department=context["department"],
            reporting_manager=context["reporting_manager"],
            joining_date=date.fromisoformat(context["joining_date"]),
            location=context["location"],
            employment_type=context["employment_type"],
        )
        plan = self.onboarding.prepare_plan(self.actor, candidate)
        arguments = {
            "name": plan.candidate.name,
            "email": plan.candidate.email,
            "designation": plan.candidate.designation,
            "department": plan.candidate.department,
            "reporting_manager": plan.candidate.reporting_manager,
            "joining_date": plan.candidate.joining_date.isoformat(),
            "location": plan.candidate.location,
            "employment_type": plan.candidate.employment_type,
        }
        summary = (
            f"Create onboarding for {plan.candidate.name} ({plan.candidate.designation}), "
            f"joining {plan.candidate.joining_date}, with 4 provisioning requests"
        )
        action = self.pending.propose(
            self.actor, state["session_id"], "create_onboarding", arguments, summary
        )
        task_lines = "\n".join(f"- {TASK_TITLES[item]}" for item in plan.task_types)
        return {
            "response": (
                "New employee onboarding\n"
                f"Name: {plan.candidate.name}\n"
                f"Email: {plan.candidate.email}\n"
                f"Role: {plan.candidate.designation}\n"
                f"Department: {plan.candidate.department}\n"
                f"Manager: {plan.candidate.reporting_manager}\n"
                f"Joining date: {plan.candidate.joining_date}\n"
                f"Location: {plan.candidate.location}\n"
                f"Employment type: {plan.candidate.employment_type}\n\n"
                f"Provisioning requests:\n{task_lines}\n\n"
                "Reply yes to confirm or cancel."
            ),
            "active_domain": "onboarding",
            "onboarding_context": context,
            "pending_summary": action.summary,
        }

    @staticmethod
    def _merge_onboarding_context(
        existing: dict[str, str], route: RouteDecision, message: str
    ) -> dict[str, str]:
        context = dict(existing)
        fields = {
            "name": route.employee_name,
            "designation": route.designation,
            "department": route.department,
            "reporting_manager": route.reporting_manager,
            "location": route.location,
            "employment_type": route.employment_type,
        }
        for field, value in fields.items():
            if value and HRAssistantOrchestrator._value_is_explicit(value, message):
                context[field] = value.strip()
        labelled_patterns = {
            "name": r"(?:employee\s+)?name\s*:\s*([^,;\n]+)",
            "designation": r"(?:designation|role)\s*:\s*([^,;\n]+)",
            "department": r"department\s*:\s*([^,;\n]+)",
            "reporting_manager": r"(?:reporting\s+manager|manager)\s*:\s*([^,;\n]+)",
            "location": r"location\s*:\s*([^,;\n]+)",
            "employment_type": r"employment\s+type\s*:\s*([^,;\n]+)",
        }
        for field, pattern in labelled_patterns.items():
            match = re.search(pattern, message, re.I)
            if match:
                context[field] = match.group(1).strip()
        email_match = re.search(
            r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", message, re.I
        )
        if email_match:
            context["email"] = email_match.group(0).casefold()
        iso_date_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", message)
        if iso_date_match:
            context["joining_date"] = iso_date_match.group(0)
        elif route.joining_date is not None and HRAssistantOrchestrator._contains_date_reference(message):
            context["joining_date"] = route.joining_date.isoformat()
        return context

    @staticmethod
    def _trusted_onboarding_lookup(route: RouteDecision, message: str) -> str | None:
        email_match = re.search(
            r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", message, re.I
        )
        if email_match:
            return email_match.group(0)
        if route.employee_name and HRAssistantOrchestrator._value_is_explicit(
            route.employee_name, message
        ):
            return route.employee_name.strip()
        return None

    @staticmethod
    def _value_is_explicit(value: str, message: str) -> bool:
        normalized_value = " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())
        normalized_message = " ".join(re.sub(r"[^\w]+", " ", message.casefold()).split())
        return bool(normalized_value) and normalized_value in normalized_message

    def _handle_parking(self, state: AgentState) -> AgentState:
        route: RouteDecision = state["route"]
        context = dict(state.get("parking_context", {}))
        requested_date = route.parking_date
        if requested_date is None and context.get("requested_date"):
            requested_date = date.fromisoformat(context["requested_date"])
        if requested_date is not None:
            context["requested_date"] = requested_date.isoformat()

        if route.intent == "parking_vehicle":
            vehicle = self.parking.get_vehicle(self.actor)
            description = f" ({vehicle.make_model})" if vehicle.make_model else ""
            return {
                "response": (
                    f"Your registered vehicle is {vehicle.registration_number}, "
                    f"{vehicle.vehicle_type.value.lower()}{description}."
                ),
                "active_domain": "parking",
                "parking_context": context,
            }

        if route.intent == "parking_admin_reservations":
            if requested_date is None:
                return {
                    "response": "Please provide the reservation date for the Parking Admin queue.",
                    "active_domain": "parking",
                    "parking_context": context,
                }
            reservations = self.parking.get_daily_reservations(self.actor, requested_date)
            if not reservations:
                return {
                    "response": f"There are no parking reservations for {requested_date}.",
                    "active_domain": "parking",
                    "parking_context": context,
                }
            lines = [
                f"#{item.id}: slot {item.slot.code} — "
                f"{item.employee_name or item.employee_code or item.employee_id}, "
                f"{item.vehicle_registration or 'vehicle unavailable'}, "
                f"{item.status.value.replace('_', ' ').title()}"
                for item in reservations
            ]
            return {
                "response": f"Parking reservations for {requested_date}:\n" + "\n".join(lines),
                "active_domain": "parking",
                "parking_context": context,
            }

        admin_intents = {
            "check_in_parking",
            "admin_cancel_parking",
            "mark_parking_no_show",
            "override_parking_no_show",
            "complete_parking",
        }
        if route.intent in admin_intents:
            if route.request_id is None:
                return {
                    "response": "Please provide the parking reservation ID.",
                    "active_domain": "parking",
                    "parking_context": context,
                }
            if route.intent in {"admin_cancel_parking", "override_parking_no_show"} and not route.reason:
                return {
                    "response": "Please provide a reason for this Parking Administrator action.",
                    "active_domain": "parking",
                    "parking_context": context,
                }
            if route.intent == "check_in_parking":
                reservation = self.parking.prepare_check_in(
                    self.actor, route.request_id, route.reason
                )
                action_type = "check_in_parking"
                summary = (
                    f"Check in parking reservation #{reservation.id} for "
                    f"{reservation.employee_name or reservation.employee_code} at slot {reservation.slot.code}"
                )
            elif route.intent == "admin_cancel_parking":
                reservation = self.parking.prepare_admin_cancellation(
                    self.actor, route.request_id, route.reason
                )
                action_type = "admin_cancel_parking"
                summary = f"Cancel parking reservation #{reservation.id}: {route.reason}"
            elif route.intent == "mark_parking_no_show":
                reservation = self.parking.prepare_no_show(self.actor, route.request_id)
                action_type = "mark_parking_no_show"
                summary = (
                    f"Mark parking reservation #{reservation.id} for "
                    f"{reservation.employee_name or reservation.employee_code} as a no-show"
                )
            elif route.intent == "override_parking_no_show":
                reservation = self.parking.prepare_no_show_override(
                    self.actor, route.request_id, route.reason
                )
                action_type = "override_parking_no_show"
                summary = f"Correct the no-show on parking reservation #{reservation.id}: {route.reason}"
            else:
                reservation = self.parking.prepare_completion(self.actor, route.request_id)
                action_type = "complete_parking"
                summary = f"Complete parking reservation #{reservation.id}"
            action = self.pending.propose(
                self.actor,
                state["session_id"],
                action_type,
                {"reservation_id": reservation.id, "reason": route.reason},
                summary,
            )
            return {
                "response": f"{action.summary}. Reply yes to confirm or cancel.",
                "active_domain": "parking",
                "parking_context": context,
                "pending_summary": action.summary,
            }

        if route.intent == "parking_reservations":
            reservations = self.parking.get_my_reservations(self.actor)
            if requested_date is not None:
                reservations = [
                    item for item in reservations if item.reservation_date == requested_date
                ]
            if not reservations:
                suffix = f" for {requested_date}" if requested_date else ""
                return {
                    "response": f"You do not have any parking reservations{suffix}.",
                    "active_domain": "parking",
                    "parking_context": context,
                }
            lines = [
                f"#{item.id}: {item.reservation_date} — slot {item.slot.code}, "
                f"{item.status.value.replace('_', ' ').title()}"
                for item in reservations[:10]
            ]
            return {
                "response": "Your parking reservations:\n" + "\n".join(lines),
                "active_domain": "parking",
                "parking_context": context,
            }

        if route.intent in {
            "parking_availability",
            "reserve_parking",
            "cancel_parking",
            "join_parking_waitlist",
            "parking",
        } and requested_date is None:
            return {
                "response": "Please provide the parking date.",
                "active_domain": "parking",
                "parking_context": context,
            }

        if route.intent == "cancel_parking":
            reservation = self.parking.prepare_cancellation(self.actor, requested_date)
            summary = (
                f"Cancel parking reservation #{reservation.id}, slot {reservation.slot.code}, "
                f"for {reservation.reservation_date}"
            )
            action = self.pending.propose(
                self.actor,
                state["session_id"],
                "cancel_parking",
                {"reservation_id": reservation.id, "reason": route.reason},
                summary,
            )
            return {
                "response": f"{action.summary}. Reply yes to confirm or cancel.",
                "active_domain": "parking",
                "parking_context": context,
                "pending_summary": action.summary,
            }

        if route.intent == "join_parking_waitlist":
            vehicle = self.parking.prepare_waitlist(self.actor, requested_date)
            summary = (
                f"Join the parking waitlist for {requested_date} using vehicle "
                f"{vehicle.registration_number}"
            )
            action = self.pending.propose(
                self.actor,
                state["session_id"],
                "join_parking_waitlist",
                {"requested_date": requested_date.isoformat()},
                summary,
            )
            return {
                "response": f"{action.summary}. Reply yes to confirm or cancel.",
                "active_domain": "parking",
                "parking_context": context,
                "pending_summary": action.summary,
            }

        existing = self.parking.get_active_reservation(self.actor, requested_date)
        if existing is not None:
            return {
                "response": (
                    f"You already have slot {existing.slot.code} reserved for {requested_date} "
                    f"under reservation #{existing.id}."
                ),
                "active_domain": "parking",
                "parking_context": context,
            }
        try:
            vehicle, slot = self.parking.prepare_reservation(self.actor, requested_date)
        except ParkingUnavailableError:
            waiting = self.parking.get_waitlist_entry(self.actor, requested_date)
            if waiting is not None:
                return {
                    "response": f"Parking is full and you are already on the waitlist for {requested_date}.",
                    "active_domain": "parking",
                    "parking_context": context,
                }
            vehicle = self.parking.prepare_waitlist(self.actor, requested_date)
            summary = (
                f"Join the parking waitlist for {requested_date} using vehicle "
                f"{vehicle.registration_number}"
            )
            action = self.pending.propose(
                self.actor,
                state["session_id"],
                "join_parking_waitlist",
                {"requested_date": requested_date.isoformat()},
                summary,
            )
            return {
                "response": (
                    f"All regular parking slots are reserved for {requested_date}. "
                    f"{action.summary}. Reply yes to confirm or cancel."
                ),
                "active_domain": "parking",
                "parking_context": context,
                "pending_summary": action.summary,
            }

        summary = (
            f"Reserve parking slot {slot.code} for {requested_date} using vehicle "
            f"{vehicle.registration_number}"
        )
        action = self.pending.propose(
            self.actor,
            state["session_id"],
            "reserve_parking",
            {"requested_date": requested_date.isoformat(), "slot_id": slot.id},
            summary,
        )
        return {
            "response": (
                f"Slot {slot.code} is available for {requested_date} for your registered vehicle "
                f"{vehicle.registration_number}. Reply yes to reserve it or cancel."
            ),
            "active_domain": "parking",
            "parking_context": context,
            "pending_summary": action.summary,
        }

    @staticmethod
    def _handle_unsupported_domain(state: AgentState) -> AgentState:
        domain = state["route"].domain
        return {
            "response": f"The {domain} workflow is planned for a later phase and is not available yet.",
            "active_domain": domain,
        }

    @staticmethod
    def _handle_general(state: AgentState) -> AgentState:
        return {
            "response": (
                "Hello! I can help with HR policy questions and leave workflows, including balances, "
                "eligibility, applications, request history, and manager approvals. Managers and HR "
                "can create and track employee onboarding, while HR administrators can approve or "
                "reject onboarding requests and activate employee accounts. I can also check, reserve, "
                "show, cancel, or waitlist your workplace parking."
            ),
            "active_domain": "general",
        }

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
        leave_context = json.dumps(state.get("leave_context", {}), sort_keys=True)
        onboarding_context = json.dumps(state.get("onboarding_context", {}), sort_keys=True)
        parking_context = json.dumps(state.get("parking_context", {}), sort_keys=True)
        return (
            f"Today's date is {today}.\n"
            f"Current leave application context: {leave_context}\n"
            f"Current onboarding context: {onboarding_context}\n"
            f"Current parking context: {parking_context}\n"
            f"{self._history_text(state)}"
        )

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
    def _apply_routing_guards(
        decision: RouteDecision, message: str, today: date | None = None
    ) -> RouteDecision:
        """Enforce high-value routing and extraction invariants after probabilistic classification."""
        normalized = message.casefold()
        patterns = {
            "CASUAL": r"\b(casual(?:\s+leave)?|casula(?:\s+leave)?|cl)\b",
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
            bool(re.search(r"\b(apply|submit|request|want|take)\b", normalized))
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
        holiday_list_signal = (
            "holiday" in normalized
            and (
                "configured" in normalized
                or bool(re.search(r"\b(list|show|which|what)\b", normalized))
                or len(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", message)) >= 2
            )
            and not bool(re.search(r"\b(count|policy|approved leave)\b", normalized))
        )
        unsupported_action_signal = any(
            phrase in normalized
            for phrase in ("expense", "payroll", "bank account", "laptop", "performance review", "insurance")
        )
        request_id_match = re.search(
            r"(?:request(?:\s+id)?\s*#?|#)\s*(\d+)", normalized
        ) or re.search(r"\b(?:approve|reject|decline|cancel|history)\D{0,12}(\d+)\b", normalized)
        request_id = int(request_id_match.group(1)) if request_id_match else None
        reason_match = re.search(r"\b(?:because|reason\s*:|due\s+to)\s+(.+)$", message, re.I)
        explicit_reason = reason_match.group(1).strip() if reason_match else None
        approval_queue_signal = any(
            phrase in normalized
            for phrase in ("pending approvals", "approval queue", "requests to approve", "team leave requests")
        )
        history_signal = bool(re.search(r"\b(history|audit(?:\s+trail)?)\b", normalized)) and "request" in normalized
        own_request_list_signal = (
            (
                personal
                or bool(re.search(r"\bleave\s+request(?:s)?\s+(?:status|list|summary)\b", normalized))
                or bool(re.search(r"\b(?:show|view|list|recent|status)\b", normalized))
            )
            and bool(re.search(r"\b(recent|list|show|view|status|my)\b", normalized))
            and bool(re.search(r"\bleave\s+requests?\b", normalized))
            and not history_signal
            and request_id is None
            and not re.search(r"\b(approve|reject|decline|cancel|withdraw)\b", normalized)
        )
        approve_signal = bool(re.search(r"\bapprove\b", normalized)) and "request" in normalized
        reject_signal = bool(re.search(r"\b(reject|decline)\b", normalized)) and "request" in normalized
        cancel_request_signal = (
            bool(re.search(r"\b(cancel|withdraw)\b", normalized))
            and "request" in normalized
            and (personal or "my" in normalized)
        )
        onboarding_status_signal = (
            "onboarding" in normalized
            and bool(re.search(r"\b(status|progress|tracking|track)\b", normalized))
        )
        onboarding_queue_signal = (
            "onboarding" in normalized
            and any(phrase in normalized for phrase in (
                "pending approvals", "approval queue", "requests to approve", "pending requests"
            ))
        )
        onboarding_context_signal = "onboarding" in normalized or "new hire" in normalized
        onboarding_approve_signal = (
            onboarding_context_signal and bool(re.search(r"\bapprove\b", normalized))
            and "request" in normalized
        )
        onboarding_reject_signal = (
            onboarding_context_signal and bool(re.search(r"\b(reject|decline)\b", normalized))
            and "request" in normalized
        )
        onboarding_start_signal = bool(
            re.search(r"\bonboard\b", normalized)
            or (
                "onboarding" in normalized
                and re.search(r"\b(start|create|begin|new employee|new hire)\b", normalized)
            )
        )
        parking_signal = bool(
            re.search(
                r"\b(parking|park|slot|reservation|booking|waitlist|waiting\s+list|registered\s+vehicle)\b",
                normalized,
            )
        ) or decision.domain == "parking"
        parking_id_match = re.search(
            r"\b(?:parking\s+)?reservation(?:\s+id)?\s*#?\s*(\d+)\b", normalized
        ) or re.search(
            r"\b(?:check\s*in|no[ -]?show|complete|late\s+cancel)\D{0,16}(\d+)\b",
            normalized,
        )
        parking_reservation_id = (
            int(parking_id_match.group(1)) if parking_id_match else decision.request_id
        )
        parking_date: date | None = None
        parking_iso_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", message)
        if parking_iso_dates:
            parking_date = date.fromisoformat(parking_iso_dates[0])
        elif re.search(r"\bday after tomorrow\b", normalized):
            parking_date = (today or date.today()) + timedelta(days=2)
        elif re.search(r"\btomorrow\b", normalized):
            parking_date = (today or date.today()) + timedelta(days=1)
        elif re.search(r"\btoday\b", normalized):
            parking_date = today or date.today()
        elif HRAssistantOrchestrator._contains_date_reference(message):
            parking_date = decision.parking_date

        parking_intent: str | None = None
        if parking_signal:
            if re.search(r"\b(correct|reverse|override)\b", normalized) and re.search(
                r"\bno[ -]?show\b", normalized
            ):
                parking_intent = "override_parking_no_show"
            elif re.search(r"\b(check\s*in|checked\s*in|arrived|arrival)\b", normalized) and not re.search(
                r"\b(queue|list|show|view)\b", normalized
            ):
                parking_intent = "check_in_parking"
            elif re.search(r"\b(mark|record)\b", normalized) and re.search(
                r"\bno[ -]?show\b", normalized
            ):
                parking_intent = "mark_parking_no_show"
            elif re.search(r"\bcomplete(?:d)?\b", normalized):
                parking_intent = "complete_parking"
            elif re.search(r"\b(admin|administrator|late)\b", normalized) and re.search(
                r"\b(cancel|cancellation)\b", normalized
            ):
                parking_intent = "admin_cancel_parking"
            elif re.search(r"\b(arrival\s+queue|parking\s+admin\s+queue|all\s+parking\s+reservations|today(?:'s)?\s+parking)\b", normalized):
                parking_intent = "parking_admin_reservations"
            elif re.search(r"\b(cancel|withdraw)\b", normalized):
                parking_intent = "cancel_parking"
            elif re.search(r"\b(waitlist|waiting\s+list|queue)\b", normalized):
                parking_intent = "join_parking_waitlist"
            elif re.search(r"\b(vehicle|registration)\b", normalized):
                parking_intent = "parking_vehicle"
            elif re.search(r"\b(book|reserve)\b", normalized):
                parking_intent = "reserve_parking"
            elif re.search(r"\b(available|availability|can\s+i|get\s+parking)\b", normalized):
                parking_intent = "parking_availability"
            elif re.search(r"\b(show|view|list|status|what(?:'s|\s+is))\b", normalized) and re.search(
                r"\b(reservation|booking)\b", normalized
            ):
                parking_intent = "parking_reservations"
            else:
                parking_intent = "parking"

        updates: dict[str, object] = {}
        if parking_intent:
            updates.update(
                domain="parking",
                intent=parking_intent,
                confidence=max(decision.confidence, 0.98),
                parking_date=parking_date,
                request_id=parking_reservation_id,
                reason=explicit_reason or decision.reason,
                leave_type=None,
                start_date=None,
                end_date=None,
            )
        elif onboarding_queue_signal:
            updates.update(
                domain="onboarding", intent="onboarding_approvals",
                confidence=max(decision.confidence, 0.98), request_id=None,
                leave_type=None, start_date=None, end_date=None,
            )
        elif onboarding_approve_signal:
            updates.update(
                domain="onboarding", intent="approve_onboarding",
                confidence=max(decision.confidence, 0.98), request_id=request_id,
                reason=explicit_reason or decision.reason,
                leave_type=None, start_date=None, end_date=None,
            )
        elif onboarding_reject_signal:
            updates.update(
                domain="onboarding", intent="reject_onboarding",
                confidence=max(decision.confidence, 0.98), request_id=request_id,
                reason=explicit_reason or decision.reason,
                leave_type=None, start_date=None, end_date=None,
            )
        elif onboarding_status_signal:
            updates.update(
                domain="onboarding", intent="onboarding_status",
                confidence=max(decision.confidence, 0.98), request_id=request_id,
                leave_type=None, start_date=None, end_date=None,
            )
        elif onboarding_start_signal:
            updates.update(
                domain="onboarding", intent="start_onboarding",
                confidence=max(decision.confidence, 0.98), request_id=None,
                leave_type=None, start_date=None, end_date=None,
            )
        elif unsupported_action_signal:
            updates.update(
                domain="general", intent="general", confidence=max(decision.confidence, 0.95),
                leave_type=None, start_date=None, end_date=None,
            )
        elif own_request_list_signal:
            updates.update(
                domain="leave", intent="leave_requests", confidence=max(decision.confidence, 0.98),
                request_id=None, leave_type=None, start_date=None, end_date=None,
            )
        elif history_signal:
            updates.update(
                domain="leave", intent="leave_request_history", confidence=max(decision.confidence, 0.98),
                request_id=request_id, leave_type=None, start_date=None, end_date=None,
            )
        elif approve_signal:
            updates.update(
                domain="leave", intent="approve_leave_request", confidence=max(decision.confidence, 0.98),
                request_id=request_id, reason=explicit_reason or decision.reason,
                leave_type=None, start_date=None, end_date=None,
            )
        elif reject_signal:
            updates.update(
                domain="leave", intent="reject_leave_request", confidence=max(decision.confidence, 0.98),
                request_id=request_id, reason=explicit_reason or decision.reason,
                leave_type=None, start_date=None, end_date=None,
            )
        elif cancel_request_signal:
            updates.update(
                domain="leave", intent="cancel_leave_request", confidence=max(decision.confidence, 0.98),
                request_id=request_id, reason=explicit_reason or decision.reason,
                leave_type=None, start_date=None, end_date=None,
            )
        elif approval_queue_signal:
            updates.update(
                domain="leave", intent="manager_leave_requests", confidence=max(decision.confidence, 0.98),
                request_id=None, leave_type=None, start_date=None, end_date=None,
            )
        elif holiday_list_signal:
            updates.update(
                domain="leave", intent="holidays", confidence=max(decision.confidence, 0.98),
                request_id=None, leave_type=None,
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
            parsed_start, parsed_end = HRAssistantOrchestrator._extract_leave_dates(message, today)
            if parsed_start and parsed_end:
                updates.update(start_date=parsed_start, end_date=parsed_end)
            elif not HRAssistantOrchestrator._contains_date_reference(message):
                updates.update(start_date=None, end_date=None)

        return decision.model_copy(update=updates) if updates else decision

    @staticmethod
    def _extract_leave_dates(message: str, today: date | None = None) -> tuple[date | None, date | None]:
        current = today or date.today()
        normalized = message.casefold()
        iso_dates = [date.fromisoformat(value) for value in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", message)]
        if len(iso_dates) == 1:
            return iso_dates[0], iso_dates[0]
        if len(iso_dates) >= 2:
            return iso_dates[0], iso_dates[1]

        anchor = HRAssistantOrchestrator._extract_single_leave_date(message, current)
        duration_match = re.search(
            r"\b(?:for\s+)?(\d{1,2})\s+(?:working\s+)?days?\s+(?:from|starting|start(?:ing)?\s+from)\b",
            normalized,
        )
        trailing_duration_match = re.search(r"\bfor\s+(\d{1,2})\s+(?:working\s+)?days?\b", normalized)
        duration = int((duration_match or trailing_duration_match).group(1)) if (duration_match or trailing_duration_match) else None
        if duration and anchor:
            return anchor, anchor + timedelta(days=duration - 1)
        if anchor:
            return anchor, anchor
        return None, None

    @staticmethod
    def _extract_single_leave_date(message: str, current: date) -> date | None:
        normalized = message.casefold()
        if re.search(r"\bday after tomorrow\b", normalized):
            return current + timedelta(days=2)
        if re.search(r"\btomorrow\b", normalized):
            return current + timedelta(days=1)
        if re.search(r"\btoday\b", normalized):
            return current

        month_values = {
            "jan": 1, "january": 1,
            "feb": 2, "february": 2,
            "mar": 3, "march": 3,
            "apr": 4, "april": 4,
            "may": 5,
            "jun": 6, "june": 6,
            "jul": 7, "july": 7,
            "aug": 8, "august": 8,
            "sep": 9, "sept": 9, "september": 9,
            "oct": 10, "october": 10,
            "nov": 11, "november": 11,
            "dec": 12, "december": 12,
        }
        month_pattern = "|".join(sorted(month_values, key=len, reverse=True))
        day_month = re.search(
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})(?:\s+(\d{{4}}))?\b",
            normalized,
        )
        month_day = re.search(
            rf"\b({month_pattern})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*|\s+)?(\d{{4}})?\b",
            normalized,
        )
        if day_month:
            day = int(day_month.group(1))
            month = month_values[day_month.group(2)]
            year = int(day_month.group(3) or current.year)
            return date(year, month, day)
        if month_day:
            month = month_values[month_day.group(1)]
            day = int(month_day.group(2))
            year = int(month_day.group(3) or current.year)
            return date(year, month, day)

        numeric = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", normalized)
        if numeric:
            day = int(numeric.group(1))
            month = int(numeric.group(2))
            year = int(numeric.group(3) or current.year)
            if year < 100:
                year += 2000
            return date(year, month, day)
        return None

    @staticmethod
    def _contains_date_reference(message: str) -> bool:
        month_or_weekday = (
            r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
            r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
            r"mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|"
            r"sat(?:urday)?|sun(?:day)?)\b"
        )
        relative_date = (
            r"\b(?:today|tomorrow|yesterday|day after tomorrow|next week|this week|"
            r"next month|this month)\b"
        )
        numeric_date = (
            r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?|"
            r"\d{1,2}(?:st|nd|rd|th))\b"
        )
        return bool(re.search(f"(?:{month_or_weekday}|{relative_date}|{numeric_date})", message, re.I))

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
    def _merge_leave_context(
        existing: dict[str, str], route: RouteDecision, message: str
    ) -> dict[str, str]:
        context = dict(existing)
        context["mode"] = "apply_leave"
        explicit_type = HRAssistantOrchestrator._explicit_leave_type(message)
        if explicit_type:
            context["leave_type"] = explicit_type
        elif route.leave_type:
            context["leave_type"] = route.leave_type

        parsed_start, parsed_end = HRAssistantOrchestrator._extract_leave_dates(message)
        start_date = parsed_start or route.start_date
        end_date = parsed_end or route.end_date
        if start_date:
            context["start_date"] = start_date.isoformat()
        if end_date:
            context["end_date"] = end_date.isoformat()

        reason_match = re.search(r"\b(?:because|reason\s*:|due\s+to)\s+(.+)$", message, re.I)
        if reason_match:
            context["reason"] = reason_match.group(1).strip()
        elif route.reason:
            context["reason"] = route.reason
        return context

    @staticmethod
    def _explicit_leave_type(message: str) -> str | None:
        patterns = {
            "CASUAL": r"\b(casual(?:\s+leave)?|casula(?:\s+leave)?|cl)\b",
            "SICK": r"\b(sick(?:\s+leave)?|sl)\b",
            "PRIVILEGE": r"\b(privilege(?:\s+leave)?|earned(?:\s+leave)?|pl|el)\b",
        }
        matches = [
            leave_type
            for leave_type, pattern in patterns.items()
            if re.search(pattern, message, re.I)
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _message_is_only_leave_type(message: str) -> bool:
        normalized = " ".join(re.sub(r"[^\w]+", " ", message.casefold()).split())
        return normalized in {
            "casual", "casual leave", "casula", "casula leave", "cl",
            "sick", "sick leave", "sl",
            "privilege", "privilege leave", "earned", "earned leave", "pl", "el",
        }

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

    @staticmethod
    def _safe_leave_context(value: object) -> dict[str, str]:
        allowed = {"mode", "leave_type", "start_date", "end_date", "reason"}
        if not isinstance(value, dict):
            return {}
        result = {
            key: item
            for key, item in value.items()
            if key in allowed and isinstance(item, str) and item.strip()
        }
        if "leave_type" in result and result["leave_type"] not in {"CASUAL", "SICK", "PRIVILEGE"}:
            result.pop("leave_type")
        if result.get("mode") != "apply_leave":
            result.pop("mode", None)
        for field in ("start_date", "end_date"):
            if field in result:
                try:
                    date.fromisoformat(result[field])
                except ValueError:
                    result.pop(field)
        return result

    @staticmethod
    def _safe_onboarding_context(value: object) -> dict[str, str]:
        allowed = {
            "name", "email", "designation", "department", "reporting_manager",
            "joining_date", "location", "employment_type",
        }
        if not isinstance(value, dict):
            return {}
        return {
            key: item
            for key, item in value.items()
            if key in allowed and isinstance(item, str) and item.strip()
        }

    @staticmethod
    def _safe_parking_context(value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        requested_date = value.get("requested_date")
        if not isinstance(requested_date, str):
            return {}
        try:
            date.fromisoformat(requested_date)
        except ValueError:
            return {}
        return {"requested_date": requested_date}
