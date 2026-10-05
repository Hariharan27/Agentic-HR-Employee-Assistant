import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError as PydanticValidationError

from app.agent.leave_agent import LeaveAgent
from app.agent.onboarding_agent import OnboardingAgent
from app.agent.onboarding_tools import OnboardingToolExecutor
from app.agent.parking_agent import ParkingAgent
from app.agent.parking_tools import ParkingToolExecutor
from app.agent.leave_tools import LeaveToolExecutor
from app.agent.state import AgentState
from app.application.leave.service import LeaveService
from app.application.onboarding.service import OnboardingService
from app.application.parking.service import ParkingService
from app.application.pending.service import PendingActionCoordinator
from app.core.config import Settings
from app.core.exceptions import (
    ApplicationError,
    LLMServiceError,
    ParkingUnavailableError,
    PendingActionExpiredError,
)
from app.core.security import AuthenticatedUser, require_role
from app.domain.leave.plan import stored_plan_inputs
from app.domain.onboarding.draft import FIELDS as ONBOARDING_FIELDS, missing_fields
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
  onboarding_approvals, approve_onboarding, reject_onboarding, parking_vehicle, register_vehicle,
  remove_vehicle,
  parking_availability, reserve_parking, parking_reservations, cancel_parking,
  join_parking_waitlist, parking_admin_reservations, check_in_parking,
  admin_cancel_parking, mark_parking_no_show, override_parking_no_show,
  complete_parking, parking, or general
- confidence: number from 0 to 1
- leave_type: CASUAL, SICK, EARNED, or null
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
- vehicle_registration: string or null
- vehicle_type: CAR, MOTORCYCLE, or null
- vehicle_make_model: string or null

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
Use register_vehicle when an employee wants to add, register, replace, or update their vehicle,
and remove_vehicle when they want to remove, delete, or deregister it.
Extract vehicle values only when explicitly supplied. Use parking_vehicle to view the employee's
registered vehicle. Use parking_availability when the user
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
    agent_activity: list[dict[str, str]] | None = None
    onboarding_draft: dict[str, str] | None = None


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
        self.leave_agent = LeaveAgent(
            settings=settings,
            llm=llm,
            tools=LeaveToolExecutor(
                actor=actor,
                leave=leave,
                pending=pending,
                policies=policies,
            ),
        )
        self.onboarding_agent = OnboardingAgent(
            settings=settings,
            llm=llm,
            tools=OnboardingToolExecutor(actor=actor, onboarding=onboarding, pending=pending),
        )
        self.parking_agent = ParkingAgent(
            settings=settings,
            llm=llm,
            tools=ParkingToolExecutor(
                actor=actor, parking=parking, pending=pending, today=lambda: parking._local_now().date()
            ),
        )
        #: Optional live-activity sink (streamed chat). Events: {"id", "label", "status", "commit"}.
        #: "commit" marks steps after every role gate, from which the HTTP stream may start.
        self.on_event: Callable[[dict[str, object]], None] | None = None
        for agent in (self.leave_agent, self.onboarding_agent, self.parking_agent):
            agent.on_event = self._agent_event
        self.graph = self._build_graph()

    def _emit(self, step_id: str, label: str, status: str, *, commit: bool = False) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event({"id": step_id, "label": label, "status": status, "commit": commit})
        except Exception:  # noqa: BLE001 - live display must never break a chat
            pass

    def _agent_event(self, event: dict[str, str]) -> None:
        # Agents only run after their domain's role gate, so their steps can start the stream.
        self._emit(event["id"], event["label"], event["status"], commit=True)

    def _route_node(self, state: AgentState) -> AgentState:
        result = self._route(state)
        route = result.get("route")
        if isinstance(route, RouteDecision):
            intent = (route.intent or route.domain).replace("_", " ")
            self._emit("route", f"Understood: {route.domain.title()} · {intent}", "success")
        return result

    def _confirmation_node(self, state: AgentState) -> AgentState:
        self._emit("route", "Checking your reply to the pending action", "success")
        return self._handle_confirmation(state)

    def chat(
        self, session_id: str, message: str, *, onboarding_form: dict[str, str] | None = None
    ) -> ChatResult:
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
            "leave_plan": self._safe_leave_plan(stored.get("leave_plan")),
            "onboarding_context": self._safe_onboarding_context(
                stored.get("onboarding_context")
            ),
            "parking_context": self._safe_parking_context(stored.get("parking_context")),
            "parking_plan": self._safe_parking_plan(stored.get("parking_plan")),
            "onboarding_form": self._safe_onboarding_form(onboarding_form),
            "sources": [],
            "agent_activity": [],
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
                "leave_plan": result.get("leave_plan"),
                "onboarding_context": result.get("onboarding_context", {}),
                "parking_context": result.get("parking_context", state.get("parking_context", {})),
                "parking_plan": result.get("parking_plan", state.get("parking_plan")),
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
            agent_activity=result.get("agent_activity", []),
            onboarding_draft={
                name: value
                for name, value in (result.get("onboarding_context") or {}).items()
                if name in ONBOARDING_FIELDS
            } or None,
        )

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("resolve_pending_action", self._resolve_pending_action)
        graph.add_node("confirmation", self._confirmation_node)
        graph.add_node("router", self._route_node)
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
                "leave_plan": None,
                "onboarding_context": {},
                "parking_context": {},
            "parking_plan": None,
                "parking_plan": None,
            }
        try:
            created = self.pending.confirm(self.actor, state["session_id"])
        except PendingActionExpiredError:
            return {
                "response": "This confirmation has expired. No changes were made; please start the action again.",
                "active_domain": self._action_domain(action.action_type),
                "pending_summary": None,
                "leave_plan": None,
                "onboarding_context": {},
                "parking_context": {},
            "parking_plan": None,
                "parking_plan": None,
            }
        except ApplicationError as exc:
            if action.action_type not in {
                "apply_leave",
                "apply_leave_plan",
                "cancel_leave_request",
                "approve_leave_request",
                "reject_leave_request",
            }:
                raise
            return {
                "response": (
                    f"The action could not be completed: {exc}. No changes were made. "
                    "The existing confirmation is still waiting; cancel it and create a new request if needed."
                ),
                "active_domain": self._action_domain(action.action_type),
                "pending_summary": action.summary,
            }
        action_type = action.action_type
        if action_type == "approve_leave_request":
            response = f"Leave request #{created.id} was approved successfully."
        elif action_type == "apply_leave_plan":
            requests = list(created)
            label = requests[0].leave_type.value.title()
            if len(requests) == 1:
                response = (
                    f"Your {label} leave request for {self._number(requests[0].working_days)} "
                    f"working day(s) was submitted successfully with request ID {requests[0].id}."
                )
            else:
                response = f"Your {label} leave requests were submitted successfully: " + ", ".join(
                    f"#{item.id} ({item.start_date} to {item.end_date}, "
                    f"{self._number(item.working_days)} day(s))"
                    for item in requests
                ) + "."
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
        elif action_type == "register_vehicle":
            model = f" ({created.make_model})" if created.make_model else ""
            response = (
                f"Vehicle {created.registration_number} was updated: {created.vehicle_type.value.lower()}{model}."
                if action.summary.startswith("Update vehicle")
                else f"Vehicle {created.registration_number} was registered successfully as a "
                f"{created.vehicle_type.value.lower()}{model}. You can now reserve parking."
            )
        elif action_type == "remove_vehicle":
            response = (
                f"Vehicle {created.registration_number} was removed from your parking profile. "
                "Register a vehicle again before booking parking."
            )
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
        elif action_type == "reserve_parking_plan":
            lines = []
            for item in created:
                if hasattr(item, "slot"):
                    lines.append(f"{item.reservation_date}: slot {item.slot.code} reserved (reservation #{item.id})")
                else:
                    lines.append(f"{item.requested_date}: added to the waitlist")
            response = "Parking booked:\n" + "\n".join(f"- {line}" for line in lines)
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
            "leave_plan": None,
            "onboarding_context": {},
            "parking_context": {},
            "parking_plan": None,
        }
        if action_type == "approve_onboarding":
            result["sensitive_response"] = True
        return result

    @staticmethod
    def _action_domain(action_type: str) -> str:
        if action_type in {
            "register_vehicle",
            "remove_vehicle",
            "reserve_parking",
            "reserve_parking_plan",
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
        if state.get("onboarding_form"):
            form_route = RouteDecision(domain="onboarding", intent="start_onboarding", confidence=1.0)
            return {"route": form_route, "active_domain": "onboarding", "llm_calls": 0}
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
            and (state.get("parking_context") or state.get("parking_plan"))
            and decision.domain == "general"
        ):
            parking_intent = (
                "register_vehicle"
                if (state.get("parking_context") or {}).get("mode") == "register_vehicle"
                else "parking"
            )
            decision = decision.model_copy(
                update={"domain": "parking", "intent": parking_intent, "confidence": 0.99}
            )
        elif state.get("active_domain") == "leave" and (
            self._message_is_only_leave_type(state["user_message"])
            or (
                decision.domain == "general"
                and (
                    state.get("leave_plan")
                    or self._contains_date_reference(state["user_message"])
                )
            )
            or (
                state.get("leave_plan")
                and bool(re.search(r"\b(policy|holiday|holidays|rule|rules)\b", state["user_message"], re.I))
                and bool(re.search(r"\b(this|that|it|these|those|leave)\b", state["user_message"], re.I))
            )
        ):
            # Keep a leave conversation in the Leave Agent; the agent, not routing, decides what
            # the follow-up means.
            decision = decision.model_copy(
                update={
                    "domain": "leave",
                    "intent": decision.intent if decision.domain == "leave" else "apply_leave",
                    "confidence": 0.99,
                }
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
        self._emit("policy", "Searching policy documents", "running", commit=True)
        context = self.policies.search(state["user_message"])
        documents = {str(item.get("document")) for item in context.sources if isinstance(item, dict)}
        self._emit(
            "policy",
            f"Found {len(context.sources)} passage(s) in {len(documents)} document(s)" if context.sources
            else "No matching policy passages",
            "success" if context.sources else "error",
            commit=True,
        )
        self._emit("answer", "Writing the answer from the policy text", "running", commit=True)
        system = (
            "Answer the employee's HR policy question using only the supplied policy context. "
            "Do not add rules that are absent, and explicitly mention any conflict in the passages. "
            "Lead with the direct answer and use at most three short sentences unless the employee "
            "asks for steps or a detailed explanation. Use plain text only: no Markdown, headings, "
            "bullets, quotations, document names, page numbers, or inline citations. The interface "
            "shows source documents separately. Use natural grammar and spacing, such as '12 days'. "
            "Write every number as digits (6, not six). "
            "Write leave types in full the first time (Earned Leave (EL), Privilege Leave (PL), "
            "Casual Leave (CL), Sick Leave (SL))."
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
        route: RouteDecision | None = state.get("route")
        intent = route.intent if route else None
        # Manager actions fail with 403 for employees before any model call.
        if intent in {"approve_leave_request", "reject_leave_request", "manager_leave_requests"}:
            require_role(self.actor, "MANAGER", "HR")
        result = self.leave_agent.invoke(
            session_id=state["session_id"],
            user_message=state["user_message"],
            conversation=state.get("messages", []),
            leave_plan=state.get("leave_plan"),
            llm_calls=state.get("llm_calls", 0),
            intent=intent,
        )
        return {
            "response": result["response"],
            "active_domain": "leave",
            "leave_plan": result.get("leave_plan"),
            "sources": result.get("sources", []),
            "pending_summary": result.get("pending_summary"),
            "agent_activity": result.get("agent_activity", []),
            "llm_calls": result.get("llm_calls", state.get("llm_calls", 0)),
        }

    def _handle_onboarding(self, state: AgentState) -> AgentState:
        route: RouteDecision = state["route"]
        # Role gates stay deterministic so unauthorized requests fail with 403 before any model call.
        if route.intent in {"onboarding_approvals", "approve_onboarding", "reject_onboarding"}:
            require_role(self.actor, "HR_ADMIN")
        elif route.intent == "onboarding_status":
            require_role(self.actor, "MANAGER", "HR", "HR_ADMIN")
        else:
            require_role(self.actor, "MANAGER", "HR")
        draft = dict(state.get("onboarding_context", {}))
        form = state.get("onboarding_form")
        if form:
            return self._handle_onboarding_form(state, draft, form)
        tools: OnboardingToolExecutor = self.onboarding_agent.tools  # type: ignore[assignment]
        tools.turn_text = state["user_message"]
        result = self.onboarding_agent.invoke(
            session_id=state["session_id"],
            user_message=state["user_message"],
            conversation=state.get("messages", []),
            active_plan=draft,
            llm_calls=state.get("llm_calls", 0),
            intent=route.intent,
        )
        return {
            "response": result["response"],
            "active_domain": "onboarding",
            "onboarding_context": result.get("active_plan") or {},
            "pending_summary": result.get("pending_summary"),
            "agent_activity": result.get("agent_activity", []),
            "llm_calls": result.get("llm_calls", state.get("llm_calls", 0)),
        }

    def _handle_onboarding_form(
        self, state: AgentState, draft: dict[str, str], form: dict[str, str]
    ) -> AgentState:
        """Structured form values go straight into the draft: no text parsing, no model call."""
        tools: OnboardingToolExecutor = self.onboarding_agent.tools  # type: ignore[assignment]
        updated, invalid, _ = tools.apply_fields(draft, form, require_stated=False)
        missing = missing_fields(updated)
        if invalid or missing:
            problems = [f"{label}: {reason}" for label, reason in invalid.items()]
            if missing:
                problems.append("missing " + ", ".join(missing))
            return {
                "response": "Please check the onboarding form: " + "; ".join(problems) + ".",
                "active_domain": "onboarding",
                "onboarding_context": updated,
            }
        summary, reply = tools.propose(updated, state["session_id"])
        return {
            "response": reply,
            "active_domain": "onboarding",
            "onboarding_context": updated,
            "pending_summary": summary,
        }

    @staticmethod
    def _safe_onboarding_form(value: object) -> dict[str, str] | None:
        if not isinstance(value, dict):
            return None
        form = {
            key: str(item).strip()
            for key, item in value.items()
            if key in ONBOARDING_FIELDS and isinstance(item, (str, int)) and str(item).strip()
        }
        return form or None

    @staticmethod
    def _value_is_explicit(value: str, message: str) -> bool:
        normalized_value = " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())
        normalized_message = " ".join(re.sub(r"[^\w]+", " ", message.casefold()).split())
        return bool(normalized_value) and normalized_value in normalized_message

    PARKING_AGENT_INTENTS = {
        "parking_availability", "reserve_parking", "cancel_parking", "join_parking_waitlist", "parking",
    }

    def _handle_parking(self, state: AgentState) -> AgentState:
        route: RouteDecision = state["route"]
        context = dict(state.get("parking_context", {}))

        if route.intent in self.PARKING_AGENT_INTENTS and context.get("mode") != "register_vehicle":
            tools: ParkingToolExecutor = self.parking_agent.tools  # type: ignore[assignment]
            tools.turn_text = state["user_message"]
            result = self.parking_agent.invoke(
                session_id=state["session_id"],
                user_message=state["user_message"],
                conversation=state.get("messages", []),
                active_plan=state.get("parking_plan"),
                llm_calls=state.get("llm_calls", 0),
                intent=route.intent,
            )
            return {
                "response": result["response"],
                "active_domain": "parking",
                "parking_context": context,
                "parking_plan": result.get("active_plan"),
                "pending_summary": result.get("pending_summary"),
                "agent_activity": result.get("agent_activity", []),
                "llm_calls": result.get("llm_calls", state.get("llm_calls", 0)),
            }

        if route.intent == "register_vehicle":
            context = self._merge_vehicle_context(
                context, route, state["user_message"]
            )
            context["mode"] = "register_vehicle"
            missing = [
                label
                for field, label in (
                    ("registration_number", "registration number"),
                    ("vehicle_type", "vehicle type"),
                )
                if not context.get(field)
            ]
            if missing:
                return {
                    "response": (
                        "Use the vehicle registration form below so I can collect "
                        f"{', '.join(missing)} and ask for confirmation before saving it. "
                        f"A new registration number adds a vehicle (up to {self.parking.MAX_VEHICLES}); "
                        "an existing one with new details updates it."
                    ),
                    "active_domain": "parking",
                    "parking_context": context,
                }
            registration, vehicle_type, make_model = (
                self.parking.prepare_vehicle_registration(
                    self.actor,
                    context["registration_number"],
                    context["vehicle_type"],
                    context.get("make_model"),
                )
            )
            arguments = {
                "registration_number": registration,
                "vehicle_type": vehicle_type.value,
                "make_model": make_model,
            }
            model = f" ({make_model})" if make_model else ""
            if self.parking.is_registered(self.actor, registration):
                summary = f"Update vehicle {registration} to a {vehicle_type.value.lower()}{model}"
            else:
                count = len(self.parking.list_vehicles(self.actor)) + 1
                summary = (
                    f"Register vehicle {registration} as a "
                    f"{vehicle_type.value.lower()}{model} (vehicle {count} of {self.parking.MAX_VEHICLES})"
                )
            action = self.pending.propose(
                self.actor,
                state["session_id"],
                "register_vehicle",
                arguments,
                summary,
            )
            return {
                "response": f"{action.summary}. Reply yes to confirm or cancel.",
                "active_domain": "parking",
                "parking_context": context,
                "pending_summary": action.summary,
            }

        requested_date = route.parking_date
        if requested_date is None and context.get("requested_date"):
            requested_date = date.fromisoformat(context["requested_date"])
        if requested_date is not None:
            context["requested_date"] = requested_date.isoformat()

        if route.intent == "remove_vehicle":
            context.pop("mode", None)
            compact = re.sub(r"[\s-]", "", state["user_message"]).upper()
            named = [
                item.registration_number
                for item in self.parking.list_vehicles(self.actor)
                if item.registration_number in compact
            ]
            vehicle = self.parking.prepare_vehicle_removal(
                self.actor, named[0] if len(named) == 1 else route.vehicle_registration
            )
            model = f" ({vehicle.make_model})" if vehicle.make_model else ""
            action = self.pending.propose(
                self.actor,
                state["session_id"],
                "remove_vehicle",
                {"registration_number": vehicle.registration_number},
                f"Remove vehicle {vehicle.registration_number}, {vehicle.vehicle_type.value.lower()}{model}, "
                "from your parking profile",
            )
            return {
                "response": f"{action.summary}. Reply yes to confirm or cancel.",
                "active_domain": "parking",
                "parking_context": context,
                "pending_summary": action.summary,
            }

        if route.intent == "parking_vehicle":
            vehicles = self.parking.list_vehicles(self.actor)
            if not vehicles:
                response = "You do not have a registered vehicle yet. Say \"Register my vehicle\" to add one (up to two)."
            else:
                lines = [
                    f"- {item.registration_number}, {item.vehicle_type.value.lower()}"
                    + (f" ({item.make_model})" if item.make_model else "")
                    for item in vehicles
                ]
                heading = "Your registered vehicle:" if len(vehicles) == 1 else "Your registered vehicles:"
                response = heading + "\n" + "\n".join(lines) + f"\n\nYou can register up to {self.parking.MAX_VEHICLES}."
            return {
                "response": response,
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
    def _merge_vehicle_context(
        existing: dict[str, str], route: RouteDecision, message: str
    ) -> dict[str, str]:
        context = dict(existing)
        route_fields = {
            "registration_number": route.vehicle_registration,
            "vehicle_type": route.vehicle_type,
            "make_model": route.vehicle_make_model,
        }
        for field, value in route_fields.items():
            if value and HRAssistantOrchestrator._value_is_explicit(value, message):
                context[field] = value.strip()
        patterns = {
            "registration_number": r"registration(?:\s+number)?\s*:\s*([^,;\n]+)",
            "vehicle_type": r"vehicle\s+type\s*:\s*([^,;\n]+)",
            "make_model": r"(?:make(?:\s+and)?\s+model|make/model)\s*:\s*([^,;\n]+)",
        }
        for field, pattern in patterns.items():
            match = re.search(pattern, message, re.I)
            if match:
                context[field] = match.group(1).strip()
        return context

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
                "show, cancel, or waitlist workplace parking, including registering your vehicle."
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
        plan = state.get("leave_plan")
        leave_context = json.dumps(
            {"active_leave_plan": plan.get("summary")} if isinstance(plan, dict) else {},
            sort_keys=True,
        )
        onboarding_context = json.dumps(state.get("onboarding_context", {}), sort_keys=True)
        parking_context = json.dumps(state.get("parking_context", {}), sort_keys=True)
        return (
            f"Today's date is {today}.\n"
            f"Current leave context: {leave_context}\n"
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
            "EARNED": r"\b(earned(?:\s+leave)?|el)\b",
        }
        explicit_types = [leave_type for leave_type, pattern in patterns.items() if re.search(pattern, message, re.I)]
        explicit_type = explicit_types[0] if len(explicit_types) == 1 else None
        # Privilege Leave (PL) is still a leave topic, but it is not Earned Leave.
        mentions_privilege = bool(re.search(r"\b(privilege(?:\s+leave)?|pl)\b", message, re.I))

        personal = bool(re.search(r"\b(i|my|me)\b", normalized))
        balance_signal = bool(
            re.search(r"\b(balance|available\s+to\s+me|do\s+i\s+have|i\s+have)\b", normalized)
        )
        apply_signal = (
            bool(re.search(r"\b(apply|submit|request|create|want|take|need|raise)\b", normalized))
            and (
                personal
                or normalized.lstrip().startswith(
                    ("apply ", "submit ", "request ", "create ", "raise ")
                )
                or "leave request" in normalized
            )
            and ("leave" in normalized or explicit_type is not None or mentions_privilege)
        )
        leave_topic_signal = "leave" in normalized or explicit_type is not None or mentions_privilege
        eligibility_signal = personal and leave_topic_signal and bool(
            re.search(r"\b(eligible|can\s+i|could\s+i|may\s+i)\b", normalized)
        ) and not bool(
            re.search(r"\b(apply|submit|file|raise)\b", normalized)
        )
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
                r"\b(parking|park|slots?|reservations?|bookings?|waitlist|waiting\s+list|vehicles?|registration|cars?|motorcycles?|bikes?)\b",
                normalized,
            )
        ) or decision.domain == "parking"
        vehicle_registration_match = re.search(
            r"registration(?:\s+number)?\s*:\s*([^,;\n]+)", message, re.I
        )
        vehicle_type_match = re.search(
            r"vehicle\s+type\s*:\s*([^,;\n]+)", message, re.I
        )
        vehicle_make_model_match = re.search(
            r"(?:make(?:\s+and)?\s+model|make/model)\s*:\s*([^,;\n]+)", message, re.I
        )
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
            if re.search(r"\b(remove|delete|deregister|unregister|de-register)\b", normalized) and re.search(
                r"\b(vehicles?|cars?|motorcycles?|bikes?)\b", normalized
            ):
                parking_intent = "remove_vehicle"
            elif re.search(r"\b(register|add|update|change|replace|edit)\b", normalized) and re.search(
                r"\b(vehicles?|registration|cars?|motorcycles?|bikes?)\b", normalized
            ):
                parking_intent = "register_vehicle"
            elif re.search(r"\b(correct|reverse|override)\b", normalized) and re.search(
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
            elif re.search(r"\b(vehicles?|registration)\b", normalized):
                parking_intent = "parking_vehicle"
            elif re.search(r"\b(book|reserve)\b", normalized):
                parking_intent = "reserve_parking"
            elif re.search(r"\b(available|availability|can\s+i|get\s+parking)\b", normalized):
                parking_intent = "parking_availability"
            elif re.search(r"\b(show|view|list|status|what(?:'s|\s+is))\b", normalized) and re.search(
                r"\b(reservations?|bookings?)\b", normalized
            ):
                parking_intent = "parking_reservations"
            else:
                parking_intent = "parking"

        updates: dict[str, object] = {}
        if (
            decision.intent == "leave_eligibility"
            and not leave_topic_signal
        ):
            updates.update(
                domain="general",
                intent="general",
                confidence=max(decision.confidence, 0.95),
                leave_type=None,
                start_date=None,
                end_date=None,
            )
        if parking_intent:
            updates.update(
                domain="parking",
                intent=parking_intent,
                confidence=max(decision.confidence, 0.98),
                parking_date=parking_date,
                request_id=parking_reservation_id,
                reason=explicit_reason or decision.reason,
                vehicle_registration=(
                    vehicle_registration_match.group(1).strip()
                    if vehicle_registration_match
                    else decision.vehicle_registration
                    if decision.vehicle_registration
                    and HRAssistantOrchestrator._value_is_explicit(
                        decision.vehicle_registration, message
                    )
                    else None
                ),
                vehicle_type=(
                    vehicle_type_match.group(1).strip()
                    if vehicle_type_match
                    else decision.vehicle_type
                    if decision.vehicle_type
                    and HRAssistantOrchestrator._value_is_explicit(
                        decision.vehicle_type, message
                    )
                    else None
                ),
                vehicle_make_model=(
                    vehicle_make_model_match.group(1).strip()
                    if vehicle_make_model_match
                    else decision.vehicle_make_model
                    if decision.vehicle_make_model
                    and HRAssistantOrchestrator._value_is_explicit(
                        decision.vehicle_make_model, message
                    )
                    else None
                ),
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
        elif apply_signal:
            updates.update(domain="leave", intent="apply_leave", confidence=max(decision.confidence, 0.95))
        elif balance_signal and personal:
            updates.update(domain="leave", intent="leave_balance", confidence=max(decision.confidence, 0.95))

        return decision.model_copy(update=updates) if updates else decision

    @staticmethod
    def _contains_date_reference(message: str) -> bool:
        month_or_weekday = (
            r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
            r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
            r"mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|"
            r"sat(?:urday)?|sun(?:day)?)\b"
        )
        relative_date = (
            r"\b(?:today|tomorrow|tomorow|tommorow|tommorrow|yesterday|day after tomorrow|"
            r"day after tomorow|day after tommorow|day after tommorrow|next week|this week|"
            r"next month|this month)\b"
        )
        numeric_date = (
            r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?|"
            r"\d{1,2}(?:st|nd|rd|th))\b"
        )
        return bool(re.search(f"(?:{month_or_weekday}|{relative_date}|{numeric_date})", message, re.I))

    @staticmethod
    def _message_is_only_leave_type(message: str) -> bool:
        normalized = " ".join(re.sub(r"[^\w]+", " ", message.casefold()).split())
        return normalized in {
            "casual", "casual leave", "casula", "casula leave", "cl",
            "sick", "sick leave", "sl",
            "earned", "earned leave", "el",
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
    def _safe_leave_plan(value: object) -> dict[str, object] | None:
        """Keep a persisted plan only while it is well-formed and unexpired."""
        inputs = stored_plan_inputs(value)
        if inputs is None:
            return None
        expires_at = inputs.expires_at
        now = datetime.now(expires_at.tzinfo) if expires_at.tzinfo else datetime.now()
        return value if expires_at > now else None  # type: ignore[return-value]

    @staticmethod
    def _safe_onboarding_context(value: object) -> dict[str, str]:
        allowed = {*ONBOARDING_FIELDS, "plan_id", "fingerprint"}
        if not isinstance(value, dict):
            return {}
        return {
            key: item
            for key, item in value.items()
            if key in allowed and isinstance(item, str) and item.strip()
        }

    @staticmethod
    def _safe_parking_plan(value: object) -> dict[str, object] | None:
        """Keep remembered parking dates and an unexpired parking plan."""
        if not isinstance(value, dict) or not isinstance(value.get("dates"), list):
            return None
        try:
            [date.fromisoformat(str(item)) for item in value["dates"]]
            if value.get("expires_at"):
                expires = datetime.fromisoformat(str(value["expires_at"]))
                now = datetime.now(expires.tzinfo) if expires.tzinfo else datetime.now()
                if expires <= now:
                    return {"dates": value["dates"]}
        except (TypeError, ValueError):
            return None
        return value

    @staticmethod
    def _safe_parking_context(value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        allowed = {
            "mode",
            "requested_date",
            "registration_number",
            "vehicle_type",
            "make_model",
        }
        result = {
            key: item.strip()
            for key, item in value.items()
            if key in allowed and isinstance(item, str) and item.strip()
        }
        if result.get("mode") not in {"register_vehicle"}:
            result.pop("mode", None)
        requested_date = result.get("requested_date")
        if requested_date:
            try:
                date.fromisoformat(requested_date)
            except ValueError:
                result.pop("requested_date", None)
        if result.get("vehicle_type") not in {"CAR", "MOTORCYCLE"}:
            result.pop("vehicle_type", None)
        return result
