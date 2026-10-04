import re
import secrets

from app.application.onboarding.ports import OnboardingRepository
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError, ValidationError
from app.core.security import AuthenticatedUser, hash_password, require_role
from app.domain.onboarding.entities import (
    OnboardingActivationResult,
    OnboardingCandidate,
    OnboardingPlan,
    OnboardingRequestData,
    OnboardingStatus,
    OnboardingTaskData,
    OnboardingTaskStatus,
    OnboardingTaskType,
)


_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_ONBOARDING_TASKS = (
    OnboardingTaskType.CORPORATE_EMAIL,
    OnboardingTaskType.LAPTOP,
    OnboardingTaskType.ACCESS_CARD,
    OnboardingTaskType.TEMPORARY_ACCESS_CARD,
)


class OnboardingService:
    """Deterministic onboarding planning and lifecycle foundation."""

    def __init__(self, repository: OnboardingRepository):
        self.repository = repository

    def prepare_plan(
        self, actor: AuthenticatedUser, candidate: OnboardingCandidate
    ) -> OnboardingPlan:
        require_role(actor, "MANAGER", "HR")
        normalized = self._normalize_candidate(candidate)
        if self.repository.employee_email_exists(normalized.email):
            raise ConflictError("An employee with this email already exists")
        if self.repository.active_onboarding_email_exists(normalized.email):
            raise ConflictError("An active onboarding request already exists for this email")
        manager = self.repository.find_employee_by_name(normalized.reporting_manager)
        if manager is None:
            raise NotFoundError("The reporting manager was not found")
        return OnboardingPlan(normalized, manager, _ONBOARDING_TASKS)

    def check_employee_exists(self, actor: AuthenticatedUser, email: str) -> bool:
        require_role(actor, "MANAGER", "HR", "HR_ADMIN")
        normalized = email.strip().casefold()
        if not _EMAIL_PATTERN.fullmatch(normalized):
            raise ValidationError("A valid employee email is required")
        return self.repository.employee_email_exists(
            normalized
        ) or self.repository.active_onboarding_email_exists(normalized)

    def create_onboarding(
        self, actor: AuthenticatedUser, candidate: OnboardingCandidate
    ) -> OnboardingRequestData:
        try:
            created = self.stage_create_onboarding(actor, candidate)
            self.repository.commit()
            return created
        except Exception:
            self.repository.rollback()
            raise

    def stage_create_onboarding(
        self, actor: AuthenticatedUser, candidate: OnboardingCandidate
    ) -> OnboardingRequestData:
        """Revalidate and stage creation for the later confirmation transaction."""
        plan = self.prepare_plan(actor, candidate)
        created = self.repository.add_request(
            plan.candidate, plan.reporting_manager.id, actor.user_id
        )
        tasks = self.repository.add_tasks(created.id, plan.task_types)
        return OnboardingRequestData(
            created.id,
            created.candidate,
            created.manager_employee_id,
            created.created_by_user_id,
            created.status,
            tasks,
            created.created_at,
            created.updated_at,
        )

    def get_onboarding_status(
        self, actor: AuthenticatedUser, request_id: int
    ) -> OnboardingRequestData:
        require_role(actor, "MANAGER", "HR", "HR_ADMIN")
        request = self.repository.get_request(request_id)
        if request is None:
            raise NotFoundError("Onboarding request was not found")
        self._authorize_request(actor, request)
        return request

    def find_onboarding_status(
        self, actor: AuthenticatedUser, employee: str
    ) -> OnboardingRequestData:
        require_role(actor, "MANAGER", "HR", "HR_ADMIN")
        query = employee.strip()
        if not query:
            raise ValidationError("Employee name or email is required")
        matches = [
            request
            for request in self.repository.find_requests_by_employee(query)
            if self._can_manage_request(actor, request)
        ]
        if not matches:
            raise NotFoundError("Onboarding request was not found")
        if len(matches) > 1:
            raise ConflictError("Multiple onboarding requests matched; use the employee email")
        return matches[0]

    def get_pending_approvals(
        self, actor: AuthenticatedUser
    ) -> list[OnboardingRequestData]:
        require_role(actor, "HR_ADMIN")
        return self.repository.list_pending_approvals()

    def prepare_review(
        self, actor: AuthenticatedUser, request_id: int
    ) -> OnboardingRequestData:
        require_role(actor, "HR_ADMIN")
        request = self.repository.get_request(request_id)
        if request is None:
            raise NotFoundError("Onboarding request was not found")
        if request.created_by_user_id == actor.user_id:
            raise AuthorizationError("You cannot approve your own onboarding request")
        if request.status is not OnboardingStatus.PENDING_APPROVAL:
            raise ConflictError("Only a pending onboarding request can be reviewed")
        return request

    def approve_onboarding(
        self, actor: AuthenticatedUser, request_id: int, comment: str | None = None
    ) -> OnboardingActivationResult:
        try:
            result = self.stage_approve_onboarding(actor, request_id, comment)
            self.repository.commit()
            return result
        except Exception:
            self.repository.rollback()
            raise

    def stage_approve_onboarding(
        self, actor: AuthenticatedUser, request_id: int, comment: str | None = None
    ) -> OnboardingActivationResult:
        request = self._get_pending_review(actor, request_id)
        if self.repository.employee_email_exists(request.candidate.email):
            raise ConflictError("An employee with this email already exists")
        normalized_comment = comment.strip() if comment and comment.strip() else None
        if normalized_comment and len(normalized_comment) > 1000:
            raise ValidationError("Review comment must be 1000 characters or fewer")
        employee_code = f"EON{request.id:05d}"
        username = employee_code.casefold()
        temporary_password = secrets.token_urlsafe(12)
        activated = self.repository.activate_request(
            request_id,
            actor.user_id,
            normalized_comment,
            employee_code,
            username,
            hash_password(temporary_password),
        )
        return OnboardingActivationResult(
            activated.request,
            activated.employee_code,
            activated.username,
            temporary_password,
        )

    def reject_onboarding(
        self, actor: AuthenticatedUser, request_id: int, reason: str
    ) -> OnboardingRequestData:
        try:
            result = self.stage_reject_onboarding(actor, request_id, reason)
            self.repository.commit()
            return result
        except Exception:
            self.repository.rollback()
            raise

    def stage_reject_onboarding(
        self, actor: AuthenticatedUser, request_id: int, reason: str
    ) -> OnboardingRequestData:
        self._get_pending_review(actor, request_id)
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValidationError("A rejection reason is required")
        if len(normalized_reason) > 1000:
            raise ValidationError("Rejection reason must be 1000 characters or fewer")
        return self.repository.reject_request(request_id, actor.user_id, normalized_reason)

    def update_task_status(
        self,
        actor: AuthenticatedUser,
        request_id: int,
        task_id: int,
        status: OnboardingTaskStatus,
    ) -> OnboardingRequestData:
        """Support simulated provisioning while keeping aggregate status deterministic."""
        require_role(actor, "MANAGER", "HR")
        try:
            request = self.repository.get_request(request_id, for_update=True)
            if request is None:
                raise NotFoundError("Onboarding request was not found")
            self._authorize_request(actor, request)
            if request.status is not OnboardingStatus.ACTIVE:
                raise ConflictError("Tasks can be updated only after onboarding is approved")
            matching_task = next((task for task in request.tasks if task.id == task_id), None)
            if matching_task is None:
                raise NotFoundError("Onboarding task was not found for this request")
            self.repository.update_task_status(task_id, status)
            refreshed = self.repository.get_request(request_id, for_update=True)
            if refreshed is None:
                raise NotFoundError("Onboarding request was not found")
            aggregate = (
                OnboardingStatus.COMPLETED
                if refreshed.tasks and all(
                    task.status is OnboardingTaskStatus.COMPLETED for task in refreshed.tasks
                )
                else OnboardingStatus.ACTIVE
            )
            if refreshed.status is not aggregate:
                refreshed = self.repository.update_request_status(request_id, aggregate)
            self.repository.commit()
            return refreshed
        except Exception:
            self.repository.rollback()
            raise

    @staticmethod
    def _authorize_request(actor: AuthenticatedUser, request: OnboardingRequestData) -> None:
        if not OnboardingService._can_manage_request(actor, request):
            raise AuthorizationError("You are not authorized to manage this onboarding request")

    @staticmethod
    def _can_manage_request(actor: AuthenticatedUser, request: OnboardingRequestData) -> bool:
        return actor.role.upper() in {"HR", "HR_ADMIN"} or (
            actor.role.upper() == "MANAGER"
            and (
                actor.user_id == request.created_by_user_id
                or actor.employee_id == request.manager_employee_id
            )
        )

    def _get_pending_review(
        self, actor: AuthenticatedUser, request_id: int
    ) -> OnboardingRequestData:
        require_role(actor, "HR_ADMIN")
        request = self.repository.get_request(request_id, for_update=True)
        if request is None:
            raise NotFoundError("Onboarding request was not found")
        if request.created_by_user_id == actor.user_id:
            raise AuthorizationError("You cannot approve your own onboarding request")
        if request.status is not OnboardingStatus.PENDING_APPROVAL:
            raise ConflictError("Only a pending onboarding request can be reviewed")
        return request

    @staticmethod
    def _normalize_candidate(candidate: OnboardingCandidate) -> OnboardingCandidate:
        values = {
            "name": candidate.name.strip(),
            "email": candidate.email.strip().casefold(),
            "designation": candidate.designation.strip(),
            "department": candidate.department.strip(),
            "reporting_manager": candidate.reporting_manager.strip(),
            "location": candidate.location.strip(),
            "employment_type": candidate.employment_type.strip(),
        }
        missing = [label.replace("_", " ") for label, value in values.items() if not value]
        if missing:
            raise ValidationError(f"Missing required onboarding fields: {', '.join(missing)}")
        if not _EMAIL_PATTERN.fullmatch(values["email"]):
            raise ValidationError("A valid employee email is required")
        if candidate.joining_date is None:
            raise ValidationError("Missing required onboarding fields: joining date")
        return OnboardingCandidate(joining_date=candidate.joining_date, **values)
