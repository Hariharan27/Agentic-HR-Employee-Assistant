from functools import lru_cache
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends

from app.agent.orchestrator import HRAssistantOrchestrator
from app.api.dependencies import AppSettings, CurrentUser, Database
from app.api.schemas.chat import ChatRequest, ChatResponse
from app.application.leave.service import LeaveService
from app.application.pending.handlers import ApplyLeaveHandler
from app.application.pending.service import PendingActionCoordinator
from app.core.config import get_settings
from app.infrastructure.embeddings.bge import BGEEmbeddingProvider
from app.infrastructure.llm.mantle import MantleLLMGateway
from app.infrastructure.repositories.conversation import SQLAlchemyConversationRepository
from app.infrastructure.repositories.leave import SQLAlchemyLeaveRepository
from app.infrastructure.repositories.pending_action import SQLAlchemyPendingActionRepository
from app.infrastructure.vector_store.qdrant import QdrantPolicyVectorStore
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


@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    actor: CurrentUser,
    db: Database,
    settings: AppSettings,
    llm: LLM,
    policies: Policies,
) -> ChatResponse:
    leave = LeaveService(SQLAlchemyLeaveRepository(db))
    pending = PendingActionCoordinator(
        SQLAlchemyPendingActionRepository(db),
        {"apply_leave": ApplyLeaveHandler(leave)},
    )
    service = HRAssistantOrchestrator(
        settings=settings,
        actor=actor,
        conversations=SQLAlchemyConversationRepository(db),
        leave=leave,
        pending=pending,
        policies=policies,
        llm=llm,
    )
    result = service.chat(body.session_id or str(uuid4()), body.message)
    return ChatResponse(
        session_id=result.session_id,
        message=result.message,
        domain=result.domain,
        intent=result.intent,
        sources=result.sources,
        pending_action=result.pending_action,
    )
