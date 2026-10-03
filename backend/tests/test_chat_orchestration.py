import json
from datetime import date

import httpx
import pytest
from sqlalchemy import select

from app.agent.orchestrator import HRAssistantOrchestrator
from app.api.routes.chat import get_llm_gateway, get_policy_service
from app.application.leave.service import LeaveService
from app.application.pending.handlers import (
    ApplyLeaveHandler,
    ApproveLeaveRequestHandler,
    CancelLeaveRequestHandler,
    RejectLeaveRequestHandler,
)
from app.application.pending.service import PendingActionCoordinator
from app.core.config import Settings
from app.core.exceptions import AuthorizationError, LLMServiceError
from app.core.security import AuthenticatedUser
from app.infrastructure.database.models import ConversationSession, LeaveRequest, PendingAction, User
from app.infrastructure.llm.mantle import MantleLLMGateway
from app.infrastructure.repositories.conversation import SQLAlchemyConversationRepository
from app.infrastructure.repositories.leave import SQLAlchemyLeaveRepository
from app.infrastructure.repositories.pending_action import SQLAlchemyPendingActionRepository
from app.llm.models import ModelTier, RouteDecision
from app.rag.models import PolicySearchResult
from app.rag.service import PolicyContext
from app.main import app


class FakeLLM:
    def __init__(self, responses: list[str]):
        self.responses = responses
        self.calls: list[tuple[ModelTier, bool]] = []

    def complete(self, tier, *, system, user, json_mode=False):
        self.calls.append((tier, json_mode))
        return self.responses.pop(0)


class FakePolicies:
    def search(self, question: str) -> PolicyContext:
        match = PolicySearchResult(
            text="Employees receive six casual leave days per calendar year.",
            score=0.9,
            document="Revised Leave Policy - I2I.pdf",
            page=2,
            section="4. Types of Leave and Entitlements",
            category="leave",
        )
        return PolicyContext("[Source: Revised Leave Policy - I2I.pdf, page 2]\n" + match.text,
                             [match.source], [match])


def actor(db_session, username="employee") -> AuthenticatedUser:
    user = db_session.scalar(select(User).where(User.username == username))
    return AuthenticatedUser(user.id, user.employee_id, user.role)


def route(**overrides) -> str:
    payload = {
        "domain": "leave",
        "intent": "leave_balance",
        "confidence": 0.95,
        "leave_type": None,
        "start_date": None,
        "end_date": None,
        "reason": None,
        "request_id": None,
        **overrides,
    }
    return json.dumps(payload)


def test_route_decision_normalizes_domain_from_intent():
    decision = RouteDecision.model_validate_json(
        route(domain="leave", intent="policy_question", confidence=0.98)
    )

    assert decision.domain == "policy"


def orchestrator(db_session, fake_llm, *, settings=None, current_actor=None):
    current_actor = current_actor or actor(db_session)
    leave = LeaveService(SQLAlchemyLeaveRepository(db_session))
    pending = PendingActionCoordinator(
        SQLAlchemyPendingActionRepository(db_session),
        {
            "apply_leave": ApplyLeaveHandler(leave),
            "approve_leave_request": ApproveLeaveRequestHandler(leave),
            "reject_leave_request": RejectLeaveRequestHandler(leave),
            "cancel_leave_request": CancelLeaveRequestHandler(leave),
        },
    )
    return HRAssistantOrchestrator(
        settings=settings or Settings(),
        actor=current_actor,
        conversations=SQLAlchemyConversationRepository(db_session),
        leave=leave,
        pending=pending,
        policies=FakePolicies(),
        llm=fake_llm,
    )


def test_chat_routes_balance_to_deterministic_leave_service(db_session):
    llm = FakeLLM([route(leave_type="CASUAL")])

    result = orchestrator(db_session, llm).chat("balance-session", "What is my CL balance?")

    assert result.domain == "leave"
    assert "Casual: 4 available" in result.message
    assert llm.calls == [("router", True)]


def test_leave_application_requires_confirmation_and_executes_on_yes(db_session):
    llm = FakeLLM([route(
        intent="apply_leave",
        leave_type="CASUAL",
        start_date="2026-10-12",
        end_date="2026-10-12",
        reason="Personal work",
    )])
    service = orchestrator(db_session, llm)

    proposal = service.chat("apply-session", "Apply casual leave on October 12")

    assert "Reply yes to confirm" in proposal.message
    assert db_session.scalars(select(LeaveRequest)).all() == []
    assert db_session.scalar(select(PendingAction).where(PendingAction.session_id == "apply-session"))

    confirmed = service.chat("apply-session", "yes")

    assert "submitted successfully" in confirmed.message
    assert len(db_session.scalars(select(LeaveRequest)).all()) == 1
    assert db_session.scalar(select(PendingAction).where(PendingAction.session_id == "apply-session")) is None
    assert len(llm.calls) == 1


def test_pending_leave_can_be_cancelled_without_execution(db_session):
    llm = FakeLLM([route(
        intent="apply_leave",
        leave_type="CASUAL",
        start_date="2026-10-13",
        end_date="2026-10-13",
    )])
    service = orchestrator(db_session, llm)
    service.chat("cancel-session", "Apply casual leave on October 13")

    cancelled = service.chat("cancel-session", "cancel")

    assert "cancelled" in cancelled.message
    assert db_session.scalars(select(LeaveRequest)).all() == []
    assert db_session.scalar(select(PendingAction).where(PendingAction.session_id == "cancel-session")) is None


def test_manager_can_list_direct_report_approval_queue_in_chat(db_session):
    LeaveService(SQLAlchemyLeaveRepository(db_session)).apply_leave(
        actor(db_session), "CASUAL", date(2026, 10, 12), date(2026, 10, 12), "Appointment"
    )
    llm = FakeLLM([route(domain="general", intent="general")])

    result = orchestrator(
        db_session, llm, current_actor=actor(db_session, "manager")
    ).chat("manager-queue", "Show my pending approvals")

    assert result.intent == "manager_leave_requests"
    assert "Pending leave approvals" in result.message
    assert "Test Employee" in result.message


def test_manager_chat_approval_requires_confirmation_and_is_atomic(db_session):
    created = LeaveService(SQLAlchemyLeaveRepository(db_session)).apply_leave(
        actor(db_session), "CASUAL", date(2026, 10, 12), date(2026, 10, 12), "Appointment"
    )
    llm = FakeLLM([route(domain="general", intent="general")])
    service = orchestrator(db_session, llm, current_actor=actor(db_session, "manager"))

    proposal = service.chat("manager-approve", f"Approve leave request #{created.id}")
    assert "Reply yes to confirm" in proposal.message
    assert db_session.get(LeaveRequest, created.id).status == "PENDING"

    confirmed = service.chat("manager-approve", "yes")
    assert f"request #{created.id} was approved" in confirmed.message
    assert db_session.get(LeaveRequest, created.id).status == "APPROVED"
    assert db_session.scalar(select(PendingAction).where(
        PendingAction.session_id == "manager-approve"
    )) is None


def test_manager_chat_rejection_requires_reason_before_confirmation(db_session):
    created = LeaveService(SQLAlchemyLeaveRepository(db_session)).apply_leave(
        actor(db_session), "CASUAL", date(2026, 10, 12), date(2026, 10, 12)
    )
    llm = FakeLLM([
        route(intent="reject_leave_request", request_id=created.id),
        route(intent="reject_leave_request", request_id=created.id, reason="Release deadline"),
    ])
    service = orchestrator(db_session, llm, current_actor=actor(db_session, "manager"))

    missing = service.chat("manager-reject", f"Reject request #{created.id}")
    assert "provide a reason" in missing.message
    proposal = service.chat(
        "manager-reject", f"Reject request #{created.id} because Release deadline"
    )
    assert "Reply yes to confirm" in proposal.message

    confirmed = service.chat("manager-reject", "yes")
    assert "was rejected" in confirmed.message
    assert db_session.get(LeaveRequest, created.id).status == "REJECTED"


def test_employee_chat_cancellation_requires_confirmation(db_session):
    created = LeaveService(SQLAlchemyLeaveRepository(db_session)).apply_leave(
        actor(db_session), "CASUAL", date(2026, 10, 12), date(2026, 10, 12)
    )
    service = orchestrator(db_session, FakeLLM([route(intent="leave_requests")]))

    proposal = service.chat("employee-cancel-request", f"Cancel my leave request #{created.id}")
    assert "Reply yes to confirm" in proposal.message
    assert db_session.get(LeaveRequest, created.id).status == "PENDING"

    confirmed = service.chat("employee-cancel-request", "yes")
    assert "was cancelled" in confirmed.message
    assert db_session.get(LeaveRequest, created.id).status == "CANCELLED"


def test_employee_cannot_approve_a_leave_request_through_chat(db_session):
    created = LeaveService(SQLAlchemyLeaveRepository(db_session)).apply_leave(
        actor(db_session), "CASUAL", date(2026, 10, 12), date(2026, 10, 12)
    )

    with pytest.raises(AuthorizationError):
        orchestrator(db_session, FakeLLM([route(intent="leave_requests")])).chat(
            "employee-approve", f"Approve leave request #{created.id}"
        )


def test_authorized_request_history_is_available_in_chat(db_session):
    created = LeaveService(SQLAlchemyLeaveRepository(db_session)).apply_leave(
        actor(db_session), "CASUAL", date(2026, 10, 12), date(2026, 10, 12)
    )

    result = orchestrator(db_session, FakeLLM([route(intent="leave_requests")])).chat(
        "employee-request-history", f"Show history for request #{created.id}"
    )

    assert result.intent == "leave_request_history"
    assert "Pending by user" in result.message


def test_recent_leave_requests_is_distinct_from_single_request_audit_history(db_session):
    LeaveService(SQLAlchemyLeaveRepository(db_session)).apply_leave(
        actor(db_session), "CASUAL", date(2026, 10, 12), date(2026, 10, 12)
    )

    result = orchestrator(
        db_session, FakeLLM([route(intent="leave_request_history")])
    ).chat("employee-recent-requests", "Show my recent leave requests")

    assert result.intent == "leave_requests"
    assert "Your recent leave requests" in result.message


def test_manager_can_switch_from_approval_queue_to_policy_question(db_session):
    llm = FakeLLM([
        route(intent="general", domain="general"),
        route(intent="policy_question", domain="policy"),
        "The policy provides six casual leave days [Revised Leave Policy - I2I.pdf, page 2].",
    ])
    service = orchestrator(db_session, llm, current_actor=actor(db_session, "manager"))

    queue = service.chat("manager-domain-switch", "Show my pending approvals")
    policy = service.chat("manager-domain-switch", "What is the casual leave policy?")

    assert queue.intent == "manager_leave_requests"
    assert policy.domain == "policy"
    assert policy.intent == "policy_question"
    assert policy.sources[0]["document"] == "Revised Leave Policy - I2I.pdf"


def test_router_cannot_invent_an_unstated_leave_type(db_session):
    llm = FakeLLM([route(
        intent="apply_leave",
        leave_type="CASUAL",
        start_date="2026-10-14",
        end_date="2026-10-14",
    )])

    result = orchestrator(db_session, llm).chat("missing-type-session", "Apply leave on 2026-10-14")

    assert "leave type" in result.message
    assert db_session.scalar(
        select(PendingAction).where(PendingAction.session_id == "missing-type-session")
    ) is None


def test_personal_balance_guard_overrides_policy_misclassification(db_session):
    llm = FakeLLM([route(domain="policy", intent="policy_question", leave_type="CASUAL")])

    result = orchestrator(db_session, llm).chat("guarded-balance", "How many casual days do I have in my balance?")

    assert result.intent == "leave_balance"
    assert "Casual: 4 available" in result.message


def test_policy_guard_routes_company_requirements_to_rag(db_session):
    llm = FakeLLM([
        route(domain="general", intent="general"),
        "Use the documented company requirement [Revised Leave Policy - I2I.pdf, page 2].",
    ])

    result = orchestrator(db_session, llm).chat(
        "guarded-policy", "What company policy requirements apply to casual leave?"
    )

    assert result.intent == "policy_question"
    assert result.sources[0]["document"] == "Revised Leave Policy - I2I.pdf"


def test_policy_guard_routes_resignation_notice_question_to_rag(db_session):
    llm = FakeLLM([
        route(domain="leave", intent="leave_eligibility"),
        "Employees serving notice cannot use leave [Revised Leave Policy - I2I.pdf, page 2].",
    ])

    result = orchestrator(db_session, llm).chat(
        "guarded-resignation-notice", "Can an employee serving resignation notice use CL, SL, PL, EL or WFH?"
    )

    assert result.intent == "policy_question"
    assert result.sources[0]["document"] == "Revised Leave Policy - I2I.pdf"


def test_iso_date_guard_preserves_user_supplied_order():
    decision = RouteDecision.model_validate_json(route(
        intent="calculate_leave_days",
        start_date="2026-11-01",
        end_date="2026-11-10",
    ))

    guarded = HRAssistantOrchestrator._apply_routing_guards(
        decision, "Calculate leave days from 2026-11-10 to 2026-11-01"
    )

    assert guarded.start_date.isoformat() == "2026-11-10"
    assert guarded.end_date.isoformat() == "2026-11-01"


def test_apply_leave_guard_takes_priority_over_incidental_i_have_phrase():
    decision = RouteDecision.model_validate_json(route(intent="leave_balance", leave_type="SICK"))

    guarded = HRAssistantOrchestrator._apply_routing_guards(
        decision, "Apply sick leave on 2026-11-12 because I have a medical appointment"
    )

    assert guarded.intent == "apply_leave"
    assert guarded.leave_type == "SICK"


def test_non_leave_submission_is_not_treated_as_leave_application():
    decision = RouteDecision.model_validate_json(route(intent="apply_leave", leave_type=None))

    guarded = HRAssistantOrchestrator._apply_routing_guards(decision, "Submit my travel expense claim")

    assert guarded.intent == "general"


def test_policy_answer_returns_grounding_sources(db_session):
    llm = FakeLLM([
        route(domain="policy", intent="policy_question"),
        "Employees receive **6days** of casual leave [Revised Leave Policy - I2I.pdf, page 2].",
    ])

    result = orchestrator(db_session, llm).chat("policy-session", "What is the casual leave policy?")

    assert result.domain == "policy"
    assert result.message == "Employees receive 6 days of casual leave."
    assert result.sources[0]["page"] == 2
    assert llm.calls == [("router", True), ("standard", False)]


def test_low_confidence_route_escalates_to_complex_model(db_session):
    llm = FakeLLM([
        route(confidence=0.4),
        route(confidence=0.98, leave_type="CASUAL"),
    ])

    result = orchestrator(db_session, llm).chat("escalation-session", "How much casual leave is left?")

    assert "Casual: 4 available" in result.message
    assert llm.calls == [("router", True), ("complex", False)]


def test_invalid_router_output_escalates_to_complex_model(db_session):
    llm = FakeLLM(["not valid json", route(confidence=0.98, leave_type="casual leave")])

    result = orchestrator(db_session, llm).chat("invalid-route-session", "How much CL is left?")

    assert "Casual: 4 available" in result.message
    assert llm.calls == [("router", True), ("complex", False)]


def test_model_call_budget_is_enforced(db_session):
    llm = FakeLLM([route(domain="policy", intent="policy_question")])
    settings = Settings(llm_max_calls_per_request=1)

    with pytest.raises(LLMServiceError, match="budget"):
        orchestrator(db_session, llm, settings=settings).chat("budget-session", "Explain leave policy")


def test_conversation_cannot_be_reused_by_another_user(db_session):
    owner = actor(db_session, "employee")
    db_session.add(ConversationSession(id="private-session", user_id=owner.user_id, state_json="{}"))
    db_session.commit()

    with pytest.raises(AuthorizationError):
        orchestrator(db_session, FakeLLM([]), current_actor=actor(db_session, "manager")).chat(
            "private-session", "hello"
        )


def test_mantle_gateway_uses_openai_chat_completions_contract():
    captured = {}

    def handler(request: httpx.Request):
        captured["request"] = request
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{\"ok\":true}"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = MantleLLMGateway(Settings(bedrock_api_key="test-key", llm_max_retries=0), client=client)

    output = gateway.complete("router", system="route", user="hello", json_mode=True)

    assert output == '{"ok":true}'
    assert captured["request"].url.path == "/v1/chat/completions"
    assert captured["request"].headers["authorization"] == "Bearer test-key"
    assert captured["body"]["model"] == "openai.gpt-oss-20b"
    assert captured["body"]["reasoning_effort"] == "low"
    assert "response_format" not in captured["body"]


def test_mantle_gateway_uses_high_reasoning_gpt_oss_for_complex_tier():
    captured = {}

    def handler(request: httpx.Request):
        captured["request"] = request
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "complex route"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = MantleLLMGateway(Settings(bedrock_api_key="test-key", llm_max_retries=0), client=client)

    output = gateway.complete("complex", system="route", user="hello")

    assert output == "complex route"
    assert captured["request"].url.path == "/v1/chat/completions"
    assert captured["request"].headers["authorization"] == "Bearer test-key"
    assert captured["body"]["model"] == "openai.gpt-oss-120b"
    assert captured["body"]["reasoning_effort"] == "medium"
    assert captured["body"]["max_tokens"] == 1200
    assert "max_completion_tokens" not in captured["body"]


def test_authenticated_chat_endpoint_uses_orchestrator(client):
    fake_llm = FakeLLM([route(leave_type="CASUAL")])
    app.dependency_overrides[get_llm_gateway] = lambda: fake_llm
    app.dependency_overrides[get_policy_service] = lambda: FakePolicies()
    try:
        token = client.post(
            "/api/v1/auth/login",
            json={"username": "employee", "password": "correct-password"},
        ).json()["access_token"]
        response = client.post(
            "/api/v1/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "What is my casual leave balance?"},
        )
    finally:
        app.dependency_overrides.pop(get_llm_gateway, None)
        app.dependency_overrides.pop(get_policy_service, None)

    assert response.status_code == 200
    assert response.json()["domain"] == "leave"
    assert response.json()["intent"] == "leave_balance"
    assert "Casual: 4 available" in response.json()["message"]
    assert response.json()["session_id"]


def test_chat_endpoint_requires_authentication(client):
    response = client.post("/api/v1/chat", json={"message": "What is my leave balance?"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"
