from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from app.application.onboarding.service import OnboardingService
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError, ValidationError
from app.core.security import AuthenticatedUser, verify_password
from app.domain.onboarding.entities import (
    OnboardingCandidate,
    OnboardingStatus,
    OnboardingTaskStatus,
    OnboardingTaskType,
)
from app.infrastructure.database.models import Employee, LeaveBalance, OnboardingRequest, User
from app.infrastructure.repositories.onboarding import SQLAlchemyOnboardingRepository


def _actor(db_session, username: str) -> AuthenticatedUser:
    user = db_session.scalar(select(User).where(User.username == username))
    return AuthenticatedUser(user.id, user.employee_id, user.role)


def _candidate(**overrides) -> OnboardingCandidate:
    values = {
        "name": "Priya Raman",
        "email": "Priya.Raman@example.com",
        "designation": "Backend Developer",
        "department": "Engineering",
        "reporting_manager": "Test Manager",
        "joining_date": date(2026, 10, 15),
        "location": "Chennai",
        "employment_type": "Permanent",
    }
    values.update(overrides)
    return OnboardingCandidate(**values)


def _service(db_session) -> OnboardingService:
    return OnboardingService(SQLAlchemyOnboardingRepository(db_session))


def test_manager_creates_onboarding_with_only_core_provisioning_tasks(db_session):
    created = _service(db_session).create_onboarding(_actor(db_session, "manager"), _candidate())

    assert created.id is not None
    assert created.candidate.email == "priya.raman@example.com"
    assert created.status is OnboardingStatus.PENDING_APPROVAL
    assert tuple(task.task_type for task in created.tasks) == (
        OnboardingTaskType.CORPORATE_EMAIL,
        OnboardingTaskType.LAPTOP,
        OnboardingTaskType.ACCESS_CARD,
        OnboardingTaskType.TEMPORARY_ACCESS_CARD,
    )
    assert created.completed_tasks == 0
    assert created.total_tasks == 4


def test_hr_can_create_onboarding(db_session):
    created = _service(db_session).create_onboarding(_actor(db_session, "hr"), _candidate())

    assert created.created_by_user_id == _actor(db_session, "hr").user_id


def test_employee_cannot_prepare_onboarding(db_session):
    with pytest.raises(AuthorizationError):
        _service(db_session).prepare_plan(_actor(db_session, "employee"), _candidate())


def test_existing_employee_email_is_rejected_case_insensitively(db_session):
    with pytest.raises(ConflictError, match="employee with this email"):
        _service(db_session).prepare_plan(
            _actor(db_session, "manager"), _candidate(email="EMPLOYEE@TEST.LOCAL")
        )


def test_duplicate_active_onboarding_is_rejected(db_session):
    service = _service(db_session)
    manager = _actor(db_session, "manager")
    service.create_onboarding(manager, _candidate())

    with pytest.raises(ConflictError, match="active onboarding request"):
        service.create_onboarding(manager, _candidate(email="PRIYA.RAMAN@EXAMPLE.COM"))


def test_missing_required_field_is_rejected(db_session):
    with pytest.raises(ValidationError, match="department"):
        _service(db_session).prepare_plan(
            _actor(db_session, "manager"), _candidate(department="  ")
        )


def test_completing_all_tasks_completes_request(db_session):
    service = _service(db_session)
    manager = _actor(db_session, "manager")
    created = service.create_onboarding(manager, _candidate())
    approved = service.approve_onboarding(_actor(db_session, "hradmin"), created.id)

    updated = approved.request
    for task in approved.request.tasks:
        updated = service.update_task_status(
            manager, created.id, task.id, OnboardingTaskStatus.COMPLETED
        )

    assert updated.completed_tasks == 4
    assert updated.status is OnboardingStatus.COMPLETED


def test_task_from_another_request_cannot_be_updated(db_session):
    service = _service(db_session)
    manager = _actor(db_session, "manager")
    first = service.create_onboarding(manager, _candidate())
    second = service.create_onboarding(
        manager,
        _candidate(name="Arun Kumar", email="arun.kumar@example.com"),
    )
    service.approve_onboarding(_actor(db_session, "hradmin"), first.id)

    with pytest.raises(NotFoundError, match="task was not found for this request"):
        service.update_task_status(
            manager, first.id, second.tasks[0].id, OnboardingTaskStatus.COMPLETED
        )


def test_hr_admin_approval_atomically_activates_employee_account(db_session):
    service = _service(db_session)
    created = service.create_onboarding(_actor(db_session, "manager"), _candidate())

    result = service.approve_onboarding(_actor(db_session, "hradmin"), created.id, "Approved")

    assert result.request.status is OnboardingStatus.ACTIVE
    assert result.employee_code == datetime.now(UTC).strftime("I%y001")
    assert result.username == result.employee_code
    user = db_session.scalar(select(User).where(User.username == result.username))
    employee = db_session.scalar(select(Employee).where(Employee.id == user.employee_id))
    balances = db_session.scalars(
        select(LeaveBalance).where(LeaveBalance.employee_id == employee.id)
    ).all()
    assert employee.email == "priya.raman@example.com"
    assert user.role == "EMPLOYEE"
    assert verify_password(result.temporary_password, user.password_hash)
    assert {item.leave_type for item in balances} == {"CASUAL", "PRIVILEGE", "SICK"}


def test_only_hr_admin_can_review_and_requester_cannot_self_approve(db_session):
    service = _service(db_session)
    created = service.create_onboarding(_actor(db_session, "manager"), _candidate())

    with pytest.raises(AuthorizationError):
        service.approve_onboarding(_actor(db_session, "hr"), created.id)

    # A defensive invariant also blocks an HR administrator from reviewing a
    # request attributed to the same user, even if data was imported externally.
    row = db_session.get(OnboardingRequest, created.id)
    row.created_by_user_id = _actor(db_session, "hradmin").user_id
    db_session.commit()
    with pytest.raises(AuthorizationError, match="own onboarding"):
        service.approve_onboarding(_actor(db_session, "hradmin"), created.id)


def test_rejected_onboarding_creates_no_employee_account(db_session):
    service = _service(db_session)
    created = service.create_onboarding(_actor(db_session, "manager"), _candidate())

    rejected = service.reject_onboarding(
        _actor(db_session, "hradmin"), created.id, "Joining date is not confirmed"
    )

    assert rejected.status is OnboardingStatus.REJECTED
    assert rejected.review_comment == "Joining date is not confirmed"
    assert not service.repository.employee_email_exists(created.candidate.email)


def test_approved_onboarding_cannot_be_approved_twice(db_session):
    service = _service(db_session)
    created = service.create_onboarding(_actor(db_session, "manager"), _candidate())
    service.approve_onboarding(_actor(db_session, "hradmin"), created.id)

    with pytest.raises(ConflictError, match="Only a pending onboarding request"):
        service.approve_onboarding(_actor(db_session, "hradmin"), created.id)

    assert db_session.scalar(
        select(func.count()).select_from(Employee).where(
            Employee.email == "priya.raman@example.com"
        )
    ) == 1
