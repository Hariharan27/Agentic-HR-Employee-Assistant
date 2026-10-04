from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class OnboardingStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class OnboardingTaskStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class OnboardingTaskType(StrEnum):
    CORPORATE_EMAIL = "CORPORATE_EMAIL"
    LAPTOP = "LAPTOP"
    ACCESS_CARD = "ACCESS_CARD"
    TEMPORARY_ACCESS_CARD = "TEMPORARY_ACCESS_CARD"
    PAYROLL_SETUP = "PAYROLL_SETUP"


TASK_TITLES: dict[OnboardingTaskType, str] = {
    OnboardingTaskType.CORPORATE_EMAIL: "Request corporate email",
    OnboardingTaskType.LAPTOP: "Request laptop",
    OnboardingTaskType.ACCESS_CARD: "Request permanent access card",
    OnboardingTaskType.TEMPORARY_ACCESS_CARD: "Request temporary access card",
    OnboardingTaskType.PAYROLL_SETUP: "Set up payroll and salary account",
}


@dataclass(frozen=True, slots=True)
class EmployeeReference:
    id: int
    name: str
    employee_code: str | None = None
    designation: str | None = None
    department: str | None = None


@dataclass(frozen=True, slots=True)
class OnboardingCandidate:
    name: str
    email: str
    designation: str
    department: str
    reporting_manager: str
    joining_date: date
    location: str
    employment_type: str


@dataclass(frozen=True, slots=True)
class OnboardingPlan:
    candidate: OnboardingCandidate
    reporting_manager: EmployeeReference
    task_types: tuple[OnboardingTaskType, ...]


@dataclass(frozen=True, slots=True)
class OnboardingTaskData:
    id: int | None
    onboarding_request_id: int
    task_type: OnboardingTaskType
    title: str
    status: OnboardingTaskStatus = OnboardingTaskStatus.PENDING
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OnboardingRequestData:
    id: int | None
    candidate: OnboardingCandidate
    manager_employee_id: int
    created_by_user_id: int
    status: OnboardingStatus = OnboardingStatus.PENDING_APPROVAL
    tasks: tuple[OnboardingTaskData, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None
    reviewed_by_user_id: int | None = None
    review_comment: str | None = None
    reviewed_at: datetime | None = None
    activated_employee_id: int | None = None
    activated_user_id: int | None = None

    @property
    def completed_tasks(self) -> int:
        return sum(task.status is OnboardingTaskStatus.COMPLETED for task in self.tasks)

    @property
    def total_tasks(self) -> int:
        return len(self.tasks)


@dataclass(frozen=True, slots=True)
class ActivatedAccountData:
    request: OnboardingRequestData
    employee_code: str
    username: str
    user_id: int


@dataclass(frozen=True, slots=True)
class OnboardingActivationResult:
    request: OnboardingRequestData
    employee_code: str
    username: str
    temporary_password: str
