import re
from enum import StrEnum

from app.application.notifications.models import InboundEmail
from app.core.exceptions import ValidationError
from app.domain.onboarding.entities import OnboardingTaskType


_ONBOARDING_REFERENCE_PATTERN = re.compile(
    r"\[Onboarding\s+#(\d+)\]",
    re.IGNORECASE,
)


class NotificationDepartment(StrEnum):
    FINANCE = "FINANCE"
    IT = "IT"
    FACILITIES = "FACILITIES"


_ALLOWED_TASKS: dict[
    NotificationDepartment,
    frozenset[OnboardingTaskType],
] = {
    NotificationDepartment.FINANCE: frozenset(
        {
            OnboardingTaskType.PAYROLL_SETUP,
        }
    ),
    NotificationDepartment.IT: frozenset(
        {
            OnboardingTaskType.CORPORATE_EMAIL,
            OnboardingTaskType.LAPTOP,
        }
    ),
    NotificationDepartment.FACILITIES: frozenset(
        {
            OnboardingTaskType.ACCESS_CARD,
            OnboardingTaskType.TEMPORARY_ACCESS_CARD,
        }
    ),
}


class InboundEmailProcessor:
    """Processes provider-independent inbound email metadata."""

    def __init__(
        self,
        *,
        finance_email: str,
        it_email: str,
        facilities_email: str,
    ) -> None:
        self._department_by_sender = {
            finance_email.strip().casefold(): NotificationDepartment.FINANCE,
            it_email.strip().casefold(): NotificationDepartment.IT,
            facilities_email.strip().casefold(): NotificationDepartment.FACILITIES,
        }

    def extract_onboarding_request_id(
        self,
        email: InboundEmail,
    ) -> int:
        match = _ONBOARDING_REFERENCE_PATTERN.search(email.subject)

        if match is None:
            raise ValidationError(
                "The email does not contain a valid onboarding reference."
            )

        return int(match.group(1))

    def identify_department(
        self,
        email: InboundEmail,
    ) -> NotificationDepartment:
        sender = email.sender.strip().casefold()

        department = self._department_by_sender.get(sender)

        if department is None:
            raise ValidationError(
                "The email sender is not authorized for onboarding updates."
            )

        return department

    def allowed_tasks(
        self,
        department: NotificationDepartment,
    ) -> frozenset[OnboardingTaskType]:
        return _ALLOWED_TASKS[department]