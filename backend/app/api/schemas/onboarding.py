from datetime import date, datetime

from pydantic import BaseModel, Field

from app.domain.onboarding.entities import OnboardingCandidate


class OnboardingCandidateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    email: str = Field(min_length=3, max_length=255)
    designation: str = Field(min_length=1, max_length=120)
    department: str = Field(min_length=1, max_length=120)
    reporting_manager: str = Field(min_length=1, max_length=160)
    joining_date: date
    location: str = Field(min_length=1, max_length=120)
    employment_type: str = Field(min_length=1, max_length=40)

    def to_domain(self) -> OnboardingCandidate:
        return OnboardingCandidate(**self.model_dump())


class EmployeeExistsResponse(BaseModel):
    email: str
    exists: bool


class ReportingManagerResponse(BaseModel):
    id: int
    name: str
    employee_code: str | None
    designation: str | None
    department: str | None


class OnboardingPlanResponse(BaseModel):
    candidate: OnboardingCandidateRequest
    manager_employee_id: int
    task_types: list[str]
    task_titles: list[str]


class OnboardingTaskResponse(BaseModel):
    id: int
    task_type: str
    title: str
    status: str
    created_at: datetime | None
    updated_at: datetime | None


class OnboardingStatusResponse(BaseModel):
    id: int
    candidate: OnboardingCandidateRequest
    manager_employee_id: int
    created_by_user_id: int
    status: str
    completed_tasks: int
    total_tasks: int
    tasks: list[OnboardingTaskResponse]
    created_at: datetime | None
    updated_at: datetime | None
    reviewed_by_user_id: int | None
    review_comment: str | None
    reviewed_at: datetime | None
    activated_employee_id: int | None
    activated_user_id: int | None
