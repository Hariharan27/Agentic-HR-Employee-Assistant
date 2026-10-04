from fastapi import APIRouter, Query

from app.api.dependencies import CurrentUser, Database
from app.api.schemas.onboarding import (
    EmployeeExistsResponse,
    OnboardingCandidateRequest,
    OnboardingPlanResponse,
    OnboardingStatusResponse,
    OnboardingTaskResponse,
    ReportingManagerResponse,
)
from app.application.onboarding.service import OnboardingService
from app.domain.onboarding.entities import TASK_TITLES, OnboardingRequestData
from app.infrastructure.repositories.onboarding import SQLAlchemyOnboardingRepository

router = APIRouter(prefix="/api/v1/manager/onboarding", tags=["employee onboarding"])
admin_router = APIRouter(prefix="/api/v1/hr-admin/onboarding", tags=["onboarding approvals"])


def _service(db: Database) -> OnboardingService:
    return OnboardingService(SQLAlchemyOnboardingRepository(db))


def _candidate_response(candidate) -> OnboardingCandidateRequest:
    return OnboardingCandidateRequest(
        name=candidate.name,
        email=candidate.email,
        designation=candidate.designation,
        department=candidate.department,
        reporting_manager=candidate.reporting_manager,
        joining_date=candidate.joining_date,
        location=candidate.location,
        employment_type=candidate.employment_type,
    )


def _status_response(item: OnboardingRequestData) -> OnboardingStatusResponse:
    return OnboardingStatusResponse(
        id=item.id,
        candidate=_candidate_response(item.candidate),
        manager_employee_id=item.manager_employee_id,
        created_by_user_id=item.created_by_user_id,
        status=item.status.value,
        completed_tasks=item.completed_tasks,
        total_tasks=item.total_tasks,
        tasks=[
            OnboardingTaskResponse(
                id=task.id,
                task_type=task.task_type.value,
                title=task.title,
                status=task.status.value,
                created_at=task.created_at,
                updated_at=task.updated_at,
            )
            for task in item.tasks
        ],
        created_at=item.created_at,
        updated_at=item.updated_at,
        reviewed_by_user_id=item.reviewed_by_user_id,
        review_comment=item.review_comment,
        reviewed_at=item.reviewed_at,
        activated_employee_id=item.activated_employee_id,
        activated_user_id=item.activated_user_id,
    )


@admin_router.get("/pending", response_model=list[OnboardingStatusResponse])
def pending_onboarding_approvals(
    actor: CurrentUser, db: Database
) -> list[OnboardingStatusResponse]:
    return [_status_response(item) for item in _service(db).get_pending_approvals(actor)]


@router.get("/employee-exists", response_model=EmployeeExistsResponse)
def check_employee_exists(
    actor: CurrentUser,
    db: Database,
    email: str = Query(min_length=3, max_length=255),
) -> EmployeeExistsResponse:
    normalized = email.strip().casefold()
    return EmployeeExistsResponse(
        email=normalized,
        exists=_service(db).check_employee_exists(actor, normalized),
    )


@router.get("/reporting-managers", response_model=list[ReportingManagerResponse])
def reporting_managers(actor: CurrentUser, db: Database) -> list[ReportingManagerResponse]:
    return [
        ReportingManagerResponse(
            id=item.id,
            name=item.name,
            employee_code=item.employee_code,
            designation=item.designation,
            department=item.department,
        )
        for item in _service(db).list_reporting_managers(actor)
    ]


@router.post("/plan", response_model=OnboardingPlanResponse)
def prepare_onboarding_plan(
    body: OnboardingCandidateRequest, actor: CurrentUser, db: Database
) -> OnboardingPlanResponse:
    plan = _service(db).prepare_plan(actor, body.to_domain())
    return OnboardingPlanResponse(
        candidate=_candidate_response(plan.candidate),
        manager_employee_id=plan.reporting_manager.id,
        task_types=[task_type.value for task_type in plan.task_types],
        task_titles=[TASK_TITLES[task_type] for task_type in plan.task_types],
    )


@router.get("/status/by-employee", response_model=OnboardingStatusResponse)
def onboarding_status_by_employee(
    actor: CurrentUser,
    db: Database,
    employee: str = Query(min_length=1, max_length=255),
) -> OnboardingStatusResponse:
    return _status_response(_service(db).find_onboarding_status(actor, employee))


@router.get("/{request_id}", response_model=OnboardingStatusResponse)
def onboarding_status(
    request_id: int, actor: CurrentUser, db: Database
) -> OnboardingStatusResponse:
    return _status_response(_service(db).get_onboarding_status(actor, request_id))
