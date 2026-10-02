from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.application.leave.service import LeaveService
from app.application.pending.handlers import ApplyLeaveHandler
from app.application.pending.service import PendingActionCoordinator
from app.core.exceptions import (
    AuthorizationError, ConflictError, InsufficientLeaveError, NotFoundError,
    PendingActionExpiredError, ValidationError,
)
from app.core.security import AuthenticatedUser
from app.domain.pending.entities import ConfirmationDecision
from app.infrastructure.database.models import ConversationSession, LeaveRequest, PendingAction, User
from app.infrastructure.repositories.leave import SQLAlchemyLeaveRepository
from app.infrastructure.repositories.pending_action import SQLAlchemyPendingActionRepository


NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)


def actor(db_session, username: str) -> AuthenticatedUser:
    user = db_session.scalar(select(User).where(User.username == username))
    return AuthenticatedUser(user.id, user.employee_id, user.role)


def coordinator(db_session, *, now=NOW, ttl_minutes=15) -> PendingActionCoordinator:
    leave = LeaveService(SQLAlchemyLeaveRepository(db_session))
    return PendingActionCoordinator(
        SQLAlchemyPendingActionRepository(db_session),
        {"apply_leave": ApplyLeaveHandler(leave)},
        ttl_minutes=ttl_minutes,
        now=lambda: now,
    )


def create_session(db_session, owner: AuthenticatedUser, session_id: str = "session-employee") -> str:
    db_session.add(ConversationSession(id=session_id, user_id=owner.user_id, state_json="{}"))
    db_session.commit()
    return session_id


def leave_arguments(start="2026-10-12", end="2026-10-12"):
    return {"leave_type": "CASUAL", "start_date": start, "end_date": end, "reason": "Personal work"}


def test_proposal_does_not_execute_before_confirmation(db_session):
    employee = actor(db_session, "employee")
    session_id = create_session(db_session, employee)
    action = coordinator(db_session).propose(employee, session_id, "apply_leave", leave_arguments(),
                                             "Apply for one day of casual leave")
    assert action.id is not None
    assert action.user_id == employee.user_id
    assert db_session.scalars(select(LeaveRequest)).all() == []


def test_confirmation_executes_and_clears_action_atomically(db_session):
    employee = actor(db_session, "employee")
    session_id = create_session(db_session, employee)
    service = coordinator(db_session)
    service.propose(employee, session_id, "apply_leave", leave_arguments(), "Apply casual leave")
    created = service.confirm(employee, session_id)
    assert created.id is not None
    assert db_session.scalar(select(PendingAction).where(PendingAction.session_id == session_id)) is None
    assert len(db_session.scalars(select(LeaveRequest)).all()) == 1
    with pytest.raises(NotFoundError):
        service.confirm(employee, session_id)


def test_another_user_cannot_read_confirm_or_cancel_action(db_session):
    employee = actor(db_session, "employee")
    manager = actor(db_session, "manager")
    session_id = create_session(db_session, employee)
    service = coordinator(db_session)
    service.propose(employee, session_id, "apply_leave", leave_arguments(), "Apply casual leave")
    for operation in (service.get, service.confirm, service.cancel):
        with pytest.raises(AuthorizationError):
            operation(manager, session_id)
    assert db_session.scalar(select(PendingAction).where(PendingAction.session_id == session_id)) is not None


def test_expired_action_is_cleared_without_execution(db_session):
    employee = actor(db_session, "employee")
    session_id = create_session(db_session, employee)
    coordinator(db_session, now=NOW, ttl_minutes=1).propose(
        employee, session_id, "apply_leave", leave_arguments(), "Apply casual leave"
    )
    with pytest.raises(PendingActionExpiredError):
        coordinator(db_session, now=NOW + timedelta(minutes=2)).confirm(employee, session_id)
    assert db_session.scalar(select(PendingAction).where(PendingAction.session_id == session_id)) is None
    assert db_session.scalars(select(LeaveRequest)).all() == []


def test_cancellation_clears_action_without_execution(db_session):
    employee = actor(db_session, "employee")
    session_id = create_session(db_session, employee)
    service = coordinator(db_session)
    service.propose(employee, session_id, "apply_leave", leave_arguments(), "Apply casual leave")
    service.cancel(employee, session_id)
    assert service.get(employee, session_id) is None
    assert db_session.scalars(select(LeaveRequest)).all() == []


def test_business_rules_are_revalidated_at_confirmation(db_session):
    employee = actor(db_session, "employee")
    session_id = create_session(db_session, employee)
    service = coordinator(db_session)
    service.propose(employee, session_id, "apply_leave", leave_arguments(), "Apply casual leave")

    # A separate request consumes all four available days after the proposal was made.
    LeaveService(SQLAlchemyLeaveRepository(db_session)).apply_leave(
        employee, "CASUAL", date(2026, 10, 5), date(2026, 10, 9)
    )
    with pytest.raises(InsufficientLeaveError):
        service.confirm(employee, session_id)
    assert service.get(employee, session_id) is not None
    assert len(db_session.scalars(select(LeaveRequest)).all()) == 1


def test_only_one_pending_action_is_allowed_per_session(db_session):
    employee = actor(db_session, "employee")
    session_id = create_session(db_session, employee)
    service = coordinator(db_session)
    service.propose(employee, session_id, "apply_leave", leave_arguments(), "First action")
    with pytest.raises(ConflictError):
        service.propose(employee, session_id, "apply_leave", leave_arguments(), "Second action")


def test_invalid_arguments_are_rejected_before_storage(db_session):
    employee = actor(db_session, "employee")
    session_id = create_session(db_session, employee)
    with pytest.raises(ValidationError):
        coordinator(db_session).propose(employee, session_id, "apply_leave",
                                        {"leave_type": "CASUAL"}, "Invalid leave")
    assert db_session.scalars(select(PendingAction)).all() == []


@pytest.mark.parametrize(("message", "expected"), [
    ("Yes", ConfirmationDecision.CONFIRM),
    ("go ahead!", ConfirmationDecision.CONFIRM),
    ("cancel", ConfirmationDecision.CANCEL),
    ("I need to change the dates", ConfirmationDecision.UNKNOWN),
])
def test_confirmation_decision_is_deterministic(message, expected):
    assert ConfirmationDecision.from_message(message) is expected
