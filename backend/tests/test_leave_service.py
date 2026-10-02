from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.application.leave.service import LeaveService
from app.core.exceptions import ConflictError, InsufficientLeaveError, ValidationError
from app.core.security import AuthenticatedUser
from app.infrastructure.database.models import LeaveRequest, User
from app.infrastructure.repositories.leave import SQLAlchemyLeaveRepository


def context(db_session, username: str) -> AuthenticatedUser:
    user = db_session.scalar(select(User).where(User.username == username))
    return AuthenticatedUser(user.id, user.employee_id, user.role)


def service(db_session) -> LeaveService:
    return LeaveService(SQLAlchemyLeaveRepository(db_session))


def test_balance_is_scoped_to_authenticated_employee(db_session):
    employee_balance = service(db_session).get_leave_balance(context(db_session, "employee"), "CASUAL")[0]
    manager_balance = service(db_session).get_leave_balance(context(db_session, "manager"), "CASUAL")[0]
    assert employee_balance.available_days == Decimal("4.00")
    assert manager_balance.available_days == Decimal("10.00")


def test_eligibility_excludes_seeded_holiday(db_session):
    result = service(db_session).check_leave_eligibility(
        context(db_session, "employee"), "CASUAL", date(2026, 10, 5), date(2026, 10, 9)
    )
    assert result.eligible is True
    assert result.working_days == Decimal("4")
    assert result.available_days == Decimal("4.00")


def test_application_creates_pending_request_and_reserves_available_balance(db_session):
    actor = context(db_session, "employee")
    created = service(db_session).apply_leave(actor, "CASUAL", date(2026, 10, 5), date(2026, 10, 9), "Family event")
    assert created.id is not None
    assert created.working_days == Decimal("4")
    assert created.status.value == "PENDING"
    assert service(db_session).get_leave_balance(actor, "CASUAL")[0].available_days == Decimal("0")


def test_pending_requests_prevent_overbooking(db_session):
    actor = context(db_session, "employee")
    service(db_session).apply_leave(actor, "CASUAL", date(2026, 10, 5), date(2026, 10, 9))
    with pytest.raises(InsufficientLeaveError):
        service(db_session).apply_leave(actor, "CASUAL", date(2026, 10, 12), date(2026, 10, 12))


def test_overlapping_active_request_is_rejected(db_session):
    actor = context(db_session, "manager")
    service(db_session).apply_leave(actor, "CASUAL", date(2026, 10, 5), date(2026, 10, 6))
    with pytest.raises(ConflictError, match="overlaps"):
        service(db_session).apply_leave(actor, "CASUAL", date(2026, 10, 6), date(2026, 10, 8))


def test_request_history_never_exposes_another_employee(db_session):
    employee = context(db_session, "employee")
    manager = context(db_session, "manager")
    service(db_session).apply_leave(employee, "PRIVILEGE", date(2026, 10, 5), date(2026, 10, 5))
    assert len(service(db_session).get_my_leave_requests(employee)) == 1
    assert service(db_session).get_my_leave_requests(manager) == []


def test_failed_application_rolls_back_without_partial_request(db_session):
    actor = context(db_session, "employee")
    with pytest.raises(InsufficientLeaveError):
        service(db_session).apply_leave(actor, "CASUAL", date(2026, 10, 5), date(2026, 10, 30))
    assert db_session.scalars(select(LeaveRequest)).all() == []


def test_unsupported_leave_type_returns_meaningful_validation_error(db_session):
    with pytest.raises(ValidationError, match="Supported values"):
        service(db_session).check_leave_eligibility(
            context(db_session, "employee"), "made-up-leave", date(2026, 10, 5), date(2026, 10, 6)
        )
