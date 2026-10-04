from datetime import date
from typing import Any

from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from app.application.onboarding.service import OnboardingService
from app.core.exceptions import ValidationError
from app.core.security import AuthenticatedUser
from app.domain.onboarding.entities import OnboardingCandidate


class CreateOnboardingArguments(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    email: str = Field(min_length=3, max_length=255)
    designation: str = Field(min_length=1, max_length=120)
    department: str = Field(min_length=1, max_length=120)
    reporting_manager: str = Field(min_length=1, max_length=160)
    joining_date: date
    location: str = Field(min_length=1, max_length=120)
    employment_type: str = Field(min_length=1, max_length=40)

    def to_candidate(self) -> OnboardingCandidate:
        return OnboardingCandidate(**self.model_dump())


class ApproveOnboardingArguments(BaseModel):
    request_id: int = Field(gt=0)
    comment: str | None = Field(default=None, max_length=1000)


class RejectOnboardingArguments(BaseModel):
    request_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000)


class CreateOnboardingHandler:
    """Confirmed mutation adapter; PendingActionCoordinator owns the transaction."""

    action_type = "create_onboarding"

    def __init__(self, onboarding_service: OnboardingService):
        self.onboarding_service = onboarding_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            validated = CreateOnboardingArguments.model_validate(arguments)
            return validated.model_dump(mode="json")
        except PydanticValidationError as exc:
            raise ValidationError("Invalid arguments for employee onboarding") from exc

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = CreateOnboardingArguments.model_validate(arguments)
        return self.onboarding_service.stage_create_onboarding(actor, validated.to_candidate())


class ApproveOnboardingHandler:
    action_type = "approve_onboarding"

    def __init__(self, onboarding_service: OnboardingService):
        self.onboarding_service = onboarding_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return ApproveOnboardingArguments.model_validate(arguments).model_dump()
        except PydanticValidationError as exc:
            raise ValidationError("Invalid onboarding approval arguments") from exc

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = ApproveOnboardingArguments.model_validate(arguments)
        return self.onboarding_service.stage_approve_onboarding(
            actor, validated.request_id, validated.comment
        )


class RejectOnboardingHandler:
    action_type = "reject_onboarding"

    def __init__(self, onboarding_service: OnboardingService):
        self.onboarding_service = onboarding_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return RejectOnboardingArguments.model_validate(arguments).model_dump()
        except PydanticValidationError as exc:
            raise ValidationError("Invalid onboarding rejection arguments") from exc

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = RejectOnboardingArguments.model_validate(arguments)
        return self.onboarding_service.stage_reject_onboarding(
            actor, validated.request_id, validated.reason
        )
