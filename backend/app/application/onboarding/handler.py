from datetime import date
from typing import Any

from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from app.application.onboarding.service import OnboardingService
from app.core.exceptions import ValidationError
from app.core.security import AuthenticatedUser
from app.domain.onboarding.entities import OnboardingCandidate
from app.application.notifications.service import EmailService

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

    def __init__(
        self,
        onboarding_service: OnboardingService,
        email_service: EmailService,
        finance_notification_email: str,
        it_notification_email: str,
        facilities_notification_email: str,
    ):
        self.onboarding_service = onboarding_service
        self.email_service = email_service
        self.finance_notification_email = finance_notification_email
        self.it_notification_email = it_notification_email
        self.facilities_notification_email = facilities_notification_email

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return ApproveOnboardingArguments.model_validate(arguments).model_dump()
        except PydanticValidationError as exc:
            raise ValidationError("Invalid onboarding approval arguments") from exc

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = ApproveOnboardingArguments.model_validate(arguments)
        return self.onboarding_service.stage_approve_onboarding(
            actor,
            validated.request_id,
            validated.comment,
        )

    def after_commit(
        self,
        actor: AuthenticatedUser,
        arguments: dict[str, Any],
        result,
    ) -> None:
        candidate = result.request.candidate
        joining_date = candidate.joining_date.strftime("%d %B %Y")

        employee_details = (
            f"Employee Name: {candidate.name}\n"
            f"Employee Code: {result.employee_code}\n"
            f"Official Email: {candidate.email}\n"
            f"Designation: {candidate.designation}\n"
            f"Department: {candidate.department}\n"
            f"Employment Type: {candidate.employment_type}\n"
            f"Location: {candidate.location}\n"
            f"Joining Date: {joining_date}\n"
            f"Reporting Manager: {candidate.reporting_manager}"
        )

        # Finance notification
        self.email_service.send_safely(
            to=(self.finance_notification_email,),
            subject=(
                f"Payroll Setup Required - {candidate.name} "
                f"({result.employee_code}) - Joining {joining_date}"
            ),
            body=(
                "Dear Finance Team,\n\n"
                f"{candidate.name} has successfully completed the HR onboarding "
                "approval process. Please initiate the required payroll and salary "
                "account setup.\n\n"
                "Employee Details\n"
                "----------------\n"
                f"{employee_details}\n\n"
                "Requested Action\n"
                "----------------\n"
                "Please complete the payroll registration and salary account "
                "setup required for the employee's onboarding.\n\n"
                "Regards,\n"
                "PeopleDesk HR Assistant"
            ),
        )

        # IT notification
        self.email_service.send_safely(
            to=(self.it_notification_email,),
            subject=(
                f"IT Provisioning Required - {candidate.name} "
                f"({result.employee_code}) - Joining {joining_date}"
            ),
            body=(
                "Dear IT Team,\n\n"
                f"{candidate.name} has successfully completed the HR onboarding "
                "approval process. Please initiate the required IT provisioning "
                "before the employee's joining date.\n\n"
                "Employee Details\n"
                "----------------\n"
                f"{employee_details}\n\n"
                "Requested Actions\n"
                "-----------------\n"
                "- Provision the corporate email account\n"
                "- Set up necessary IT equipment and access\n"
                "- Arrange the required laptop/workstation\n\n"
                "Please ensure the required IT assets and account access are "
                "available for the employee's onboarding.\n\n"
                "Regards,\n"
                "PeopleDesk HR Assistant"
            ),
        )

        # Facilities notification
        self.email_service.send_safely(
            to=(self.facilities_notification_email,),
            subject=(
                f"Access Provisioning Required - {candidate.name} "
                f"({result.employee_code}) - Joining {joining_date}"
            ),
            body=(
                "Dear Facilities Team,\n\n"
                f"{candidate.name} has successfully completed the HR onboarding "
                "approval process. Please initiate the required workplace access "
                "provisioning.\n\n"
                "Employee Details\n"
                "----------------\n"
                f"{employee_details}\n\n"
                "Requested Actions\n"
                "-----------------\n"
                "- Prepare the permanent access card\n"
                "- Arrange temporary access until permanent access is available\n\n"
                "Please ensure the required access arrangements are available "
                "for the employee's joining date.\n\n"
                "Regards,\n"
                "PeopleDesk HR Assistant"
            ),
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
