import hmac
from typing import Annotated

from fastapi import APIRouter, Header

from app.api.dependencies import AppSettings, Database
from app.api.schemas.notifications import InboundEmailRequest
from app.application.notifications.inbound import InboundEmailProcessor
from app.application.notifications.models import InboundEmail
from app.application.onboarding.reply_interpreter import OnboardingReplyInterpreter
from app.application.onboarding.service import OnboardingService
from app.core.config import Settings
from app.core.exceptions import AuthenticationError, IntegrationNotConfiguredError
from app.infrastructure.llm.mantle import MantleLLMGateway
from app.infrastructure.repositories.onboarding import SQLAlchemyOnboardingRepository


router = APIRouter(
    prefix="/api/v1/inbound",
    tags=["inbound integrations"],
)


def verify_inbound_token(token: str | None, settings: Settings) -> None:
    """Only the configured email provider may post department replies."""
    expected = settings.inbound_email_token
    if not expected:
        raise IntegrationNotConfiguredError("Inbound email processing is not configured")
    if not token or not hmac.compare_digest(token.encode(), expected.encode()):
        raise AuthenticationError("Invalid inbound integration token")


@router.post("/email")
def process_inbound_email(
    body: InboundEmailRequest,
    db: Database,
    settings: AppSettings,
    x_inbound_token: Annotated[str | None, Header()] = None,
) -> dict:
    verify_inbound_token(x_inbound_token, settings)

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