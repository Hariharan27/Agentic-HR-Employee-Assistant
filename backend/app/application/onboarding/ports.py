from typing import Protocol

from app.domain.onboarding.entities import (
    ActivatedAccountData,
    EmployeeReference,
    OnboardingCandidate,
    OnboardingRequestData,
    OnboardingStatus,
    OnboardingTaskData,
    OnboardingTaskStatus,
    OnboardingTaskType,
)


class OnboardingRepository(Protocol):
    def employee_email_exists(self, email: str) -> bool: ...
    def next_employee_code(self) -> str: ...
    def active_onboarding_email_exists(self, email: str) -> bool: ...
    def find_employee_by_name(self, name: str) -> EmployeeReference | None: ...
    def add_request(
        self,
        candidate: OnboardingCandidate,
        manager_employee_id: int,
        created_by_user_id: int,
    ) -> OnboardingRequestData: ...
    def add_tasks(
        self, request_id: int, task_types: tuple[OnboardingTaskType, ...]
    ) -> tuple[OnboardingTaskData, ...]: ...
    def get_request(
        self, request_id: int, *, for_update: bool = False
    ) -> OnboardingRequestData | None: ...
    def find_requests_by_employee(self, query: str) -> list[OnboardingRequestData]: ...
    def list_pending_approvals(self) -> list[OnboardingRequestData]: ...
    def activate_request(
        self,
        request_id: int,
        reviewer_user_id: int,
        comment: str | None,
        employee_code: str,
        username: str,
        password_hash: str,
    ) -> ActivatedAccountData: ...
    def reject_request(
        self, request_id: int, reviewer_user_id: int, reason: str
    ) -> OnboardingRequestData: ...
    def update_task_status(
        self, task_id: int, status: OnboardingTaskStatus
    ) -> OnboardingTaskData: ...
    def update_request_status(
        self, request_id: int, status: OnboardingStatus
    ) -> OnboardingRequestData: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
