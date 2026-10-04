from fastapi import APIRouter

from app.api.dependencies import Database
from app.api.schemas.notifications import InboundEmailRequest
from app.application.notifications.inbound import InboundEmailProcessor
from app.application.notifications.models import InboundEmail
from app.application.onboarding.reply_interpreter import OnboardingReplyInterpreter
from app.application.onboarding.service import OnboardingService
from app.core.config import get_settings
from app.infrastructure.llm.mantle import MantleLLMGateway
from app.infrastructure.repositories.onboarding import SQLAlchemyOnboardingRepository


router = APIRouter(
    prefix="/api/v1/inbound",
    tags=["inbound integrations"],
)


@router.post("/email")
def process_inbound_email(
    body: InboundEmailRequest,
    db: Database,
) -> dict:
    settings = get_settings()

    email = InboundEmail(
        sender=body.sender,
        subject=body.subject,
        body=body.body,
    )

    processor = InboundEmailProcessor(
        finance_email=settings.finance_notification_email,
        it_email=settings.it_notification_email,
        facilities_email=settings.facilities_notification_email,
    )

    # Deterministic correlation and sender authorization.
    request_id = processor.extract_onboarding_request_id(email)
    department = processor.identify_department(email)
    allowed_tasks = processor.allowed_tasks(department)

    # AI interprets the natural-language department reply.
    interpreter = OnboardingReplyInterpreter(
        MantleLLMGateway(settings)
    )

    decision = interpreter.interpret(
        department=department,
        email_body=email.body,
        allowed_tasks=allowed_tasks,
    )

    # The interpreter has already validated the model's decisions
    # against the department task allowlist.
    updates = {
        update.task_type: update.status
        for update in decision.updates
    }

    onboarding = OnboardingService(
        SQLAlchemyOnboardingRepository(db)
    )

    result = onboarding.apply_inbound_task_updates(
        request_id=request_id,
        updates=updates,
    )

    return {
        "request_id": request_id,
        "department": department.value,
        "interpreted_updates": [
            {
                "task_type": update.task_type.value,
                "status": update.status.value,
            }
            for update in decision.updates
        ],
        "onboarding_status": result.status.value,
        "completed_tasks": result.completed_tasks,
        "total_tasks": result.total_tasks,
    }