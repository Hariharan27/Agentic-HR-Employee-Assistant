import json
import logging
import queue
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agent.orchestrator import HRAssistantOrchestrator
from app.api.dependencies import AppSettings, CurrentUser, Database
from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.core.security import AuthenticatedUser
from app.infrastructure.database.session import SessionLocal
from app.api.schemas.chat import ChatRequest, ChatResponse
from app.application.leave.service import LeaveService
from app.application.notifications.service import EmailService
from app.application.onboarding.handler import (
    ApproveOnboardingHandler,
    CreateOnboardingHandler,
    RejectOnboardingHandler,
)
from app.application.onboarding.service import OnboardingService
from app.application.parking.handlers import (
    AdminCancelParkingHandler,
    CancelParkingHandler,
    CheckInParkingHandler,
    CompleteParkingHandler,
    JoinParkingWaitlistHandler,
    MarkParkingNoShowHandler,
    OverrideParkingNoShowHandler,
    RegisterVehicleHandler,
    RemoveVehicleHandler,
    ReserveParkingHandler,
    ReserveParkingPlanHandler,
)
from app.application.parking.service import ParkingService
from app.application.pending.handlers import (
    ApplyLeaveHandler,
    ApplyLeavePlanHandler,
    ApproveLeaveRequestHandler,
    CancelLeaveRequestHandler,
    RejectLeaveRequestHandler,
)
from app.application.pending.service import PendingActionCoordinator
from app.core.config import get_settings
from app.infrastructure.embeddings.bge import BGEEmbeddingProvider
from app.infrastructure.llm.mantle import MantleLLMGateway
from app.infrastructure.repositories.conversation import SQLAlchemyConversationRepository
from app.infrastructure.repositories.leave import SQLAlchemyLeaveRepository
from app.infrastructure.repositories.onboarding import SQLAlchemyOnboardingRepository
from app.infrastructure.repositories.parking import SQLAlchemyParkingRepository
from app.infrastructure.repositories.pending_action import SQLAlchemyPendingActionRepository
from app.infrastructure.vector_store.qdrant import QdrantPolicyVectorStore
from app.infrastructure.notifications.email import ConsoleEmailGateway
from app.llm.ports import LLMGateway
from app.rag.service import PolicyKnowledgeService

router = APIRouter(prefix="/api/v1", tags=["chat"])


@lru_cache
def get_llm_gateway() -> LLMGateway:
    return MantleLLMGateway(get_settings())


@lru_cache
def get_policy_service() -> PolicyKnowledgeService:
    settings = get_settings()
    return PolicyKnowledgeService(
        BGEEmbeddingProvider(settings.embedding_model),
        QdrantPolicyVectorStore(
            collection=settings.qdrant_collection,
            vector_size=settings.rag_collection_vector_size,
            url=settings.qdrant_url,
        ),
        top_k=settings.rag_top_k,
        score_threshold=settings.rag_score_threshold,
    )


LLM = Annotated[LLMGateway, Depends(get_llm_gateway)]
Policies = Annotated[PolicyKnowledgeService, Depends(get_policy_service)]


def build_orchestrator(
    db: Session,
    actor: AuthenticatedUser,
    settings: Settings,
    llm: LLMGateway,
    policies: PolicyKnowledgeService,
) -> HRAssistantOrchestrator:
    timezone = ZoneInfo(settings.app_timezone)
    leave = LeaveService(
        SQLAlchemyLeaveRepository(db), today=lambda: datetime.now(timezone).date()
    )
    onboarding = OnboardingService(
        SQLAlchemyOnboardingRepository(db), today=lambda: datetime.now(timezone).date()
    )
    parking = ParkingService(SQLAlchemyParkingRepository(db), settings)
    email_service = EmailService(ConsoleEmailGateway())
    pending = PendingActionCoordinator(
        SQLAlchemyPendingActionRepository(db),
        {
            "apply_leave": ApplyLeaveHandler(leave),
            "apply_leave_plan": ApplyLeavePlanHandler(leave),
            "approve_leave_request": ApproveLeaveRequestHandler(leave),
            "reject_leave_request": RejectLeaveRequestHandler(leave),
            "cancel_leave_request": CancelLeaveRequestHandler(leave),
            "create_onboarding": CreateOnboardingHandler(onboarding),
            "approve_onboarding": ApproveOnboardingHandler(
                onboarding,
                email_service,
                settings.finance_notification_email,
                settings.it_notification_email,
                settings.facilities_notification_email,
            ),
            "reject_onboarding": RejectOnboardingHandler(onboarding),
            "register_vehicle": RegisterVehicleHandler(parking),
            "remove_vehicle": RemoveVehicleHandler(parking),
            "reserve_parking": ReserveParkingHandler(parking),
            "reserve_parking_plan": ReserveParkingPlanHandler(parking),
            "cancel_parking": CancelParkingHandler(parking),
            "join_parking_waitlist": JoinParkingWaitlistHandler(parking),
            "check_in_parking": CheckInParkingHandler(parking),
            "admin_cancel_parking": AdminCancelParkingHandler(parking),
            "mark_parking_no_show": MarkParkingNoShowHandler(parking),
            "override_parking_no_show": OverrideParkingNoShowHandler(parking),
            "complete_parking": CompleteParkingHandler(parking),
        },
    )
    return HRAssistantOrchestrator(
        settings=settings,
        actor=actor,
        conversations=SQLAlchemyConversationRepository(db),
        leave=leave,
        onboarding=onboarding,
        parking=parking,
        pending=pending,
        policies=policies,
        llm=llm,
    )


def to_response(result: Any) -> ChatResponse:
    return ChatResponse(
        session_id=result.session_id,
        message=result.message,
        domain=result.domain,
        intent=result.intent,
        sources=result.sources,
        pending_action=result.pending_action,
        agent_activity=result.agent_activity or [],
        onboarding_draft=result.onboarding_draft,
    )


@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    actor: CurrentUser,
    db: Database,
    settings: AppSettings,
    llm: LLM,
    policies: Policies,
) -> ChatResponse:
    service = build_orchestrator(db, actor, settings, llm, policies)
    result = service.chat(body.session_id or str(uuid4()), body.message, onboarding_form=body.onboarding_form)
    return to_response(result)


logger = logging.getLogger("app.chat_stream")

SessionScope = Callable[[], AbstractContextManager[Session]]


@contextmanager
def _new_session() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_session_scope() -> SessionScope:
    """A fresh DB session for work that outlives the request (tests override this)."""
    return _new_session

_DONE = object()


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/chat/stream")
def chat_stream(
    body: ChatRequest,
    actor: CurrentUser,
    settings: AppSettings,
    llm: LLM,
    policies: Policies,
    session_scope: Annotated[SessionScope, Depends(get_session_scope)],
) -> StreamingResponse:
    """Same chat as POST /chat, streamed as Server-Sent Events.

    Events: ``step`` ({id, label, status}) while the agent works, then ``final`` (the ChatResponse)
    or ``error``. Steps before any role gate are held back, so an unauthorized request still fails
    with its normal HTTP status before the stream starts.
    """
    events: queue.Queue[Any] = queue.Queue()
    session_id = body.session_id or str(uuid4())

    def work() -> None:
        # The request's own DB session closes before a streamed body is sent, so use a new one.
        try:
            with session_scope() as db:
                service = build_orchestrator(db, actor, settings, llm, policies)
                service.on_event = lambda event: events.put(("step", event))
                result = service.chat(session_id, body.message, onboarding_form=body.onboarding_form)
                events.put(("final", to_response(result).model_dump(mode="json")))
        except BaseException as exc:  # noqa: BLE001 - every failure must reach the client
            events.put(("exception", exc))
        finally:
            events.put(_DONE)

    threading.Thread(target=work, name=f"chat-stream-{session_id[:8]}", daemon=True).start()

    held: list[dict[str, Any]] = []
    first: Any = None
    while True:
        item = events.get()
        if item is _DONE:
            break
        kind, payload = item
        if kind == "step" and not payload.get("commit"):
            held.append(payload)
            continue
        first = item
        break
    if first is None or first[0] == "exception":
        exc = first[1] if first else RuntimeError("The chat ended without a reply")
        raise exc  # the normal exception handlers answer with the right status (403, 422, ...)

    def stream() -> Iterator[str]:
        for step in held:
            yield _sse("step", {key: step[key] for key in ("id", "label", "status")})
        pending: list[Any] = [first]
        while True:
            item = pending.pop() if pending else events.get()
            if item is _DONE:
                return
            kind, payload = item
            if kind == "step":
                yield _sse("step", {key: payload[key] for key in ("id", "label", "status")})
            elif kind == "final":
                yield _sse("final", payload)
            else:
                logger.warning("chat_stream_failed", exc_info=payload)
                status = payload.status_code if isinstance(payload, ApplicationError) else 500
                message = str(payload) if isinstance(payload, ApplicationError) else "The assistant could not complete that request."
                yield _sse("error", {"status": status, "message": message})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
