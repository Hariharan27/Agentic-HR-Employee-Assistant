from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.application.leave.service import LeaveService
from app.core.exceptions import AuthorizationError, ConflictError
from app.core.security import AuthenticatedUser
from app.domain.leave.entities import LeaveStatus
from app.infrastructure.database.models import LeaveBalance, User
from app.infrastructure.repositories.leave import SQLAlchemyLeaveRepository


def actor(db_session, username: str) -> AuthenticatedUser:
    user = db_session.scalar(select(User).where(User.username == username))
    return AuthenticatedUser(user.id, user.employee_id, user.role)


def service(db_session) -> LeaveService:
    return LeaveService(SQLAlchemyLeaveRepository(db_session))


def submit_one_day(db_session):
    return service(db_session).apply_leave(
        actor(db_session, "employee"), "CASUAL",
        date(2026, 10, 12), date(2026, 10, 12), "Personal appointment",
    )


def test_manager_queue_is_scoped_to_direct_reports(db_session):
    created = submit_one_day(db_session)

    requests = service(db_session).get_managed_leave_requests(actor(db_session, "manager"))

    assert [item.id for item in requests] == [created.id]
    assert requests[0].manager_employee_id == actor(db_session, "manager").employee_id


def test_approval_consumes_balance_once_and_records_history(db_session):
    created = submit_one_day(db_session)
    manager = actor(db_session, "manager")

    approved = service(db_session).approve_leave_request(manager, created.id, "Coverage confirmed")

    assert approved.status is LeaveStatus.APPROVED
    balance = db_session.scalar(select(LeaveBalance).where(
        LeaveBalance.employee_id == created.employee_id,
        LeaveBalance.leave_type == "CASUAL",
    ))
    assert balance.used_days == Decimal("9.00")
    events = service(db_session).get_leave_request_history(manager, created.id)
    assert [event.to_status for event in events] == [LeaveStatus.PENDING, LeaveStatus.APPROVED]
    assert events[-1].comment == "Coverage confirmed"

    with pytest.raises(ConflictError, match="Only pending"):
        service(db_session).approve_leave_request(manager, created.id)
    assert balance.used_days == Decimal("9.00")


def test_rejection_releases_reserved_days_without_consuming_balance(db_session):
    created = submit_one_day(db_session)
    employee = actor(db_session, "employee")

    before = service(db_session).get_leave_balance(employee, "CASUAL")[0]
    assert before.available_days == Decimal("3.00")
    rejected = service(db_session).reject_leave_request(
        actor(db_session, "manager"), created.id, "Release deadline"
    )

    assert rejected.status is LeaveStatus.REJECTED
    after = service(db_session).get_leave_balance(employee, "CASUAL")[0]
    assert after.available_days == Decimal("4.00")
    assert after.used_days == Decimal("8.00")


def test_employee_can_cancel_only_own_pending_request(db_session):
    created = submit_one_day(db_session)
    employee = actor(db_session, "employee")

    with pytest.raises(AuthorizationError):
        service(db_session).cancel_leave_request(actor(db_session, "manager"), created.id)

    cancelled = service(db_session).cancel_leave_request(employee, created.id, "Plans changed")
    assert cancelled.status is LeaveStatus.CANCELLED
    assert service(db_session).get_leave_balance(employee, "CASUAL")[0].available_days == Decimal("4.00")


def test_employee_cannot_perform_manager_decision(db_session):
    created = submit_one_day(db_session)

    with pytest.raises(AuthorizationError):
        service(db_session).approve_leave_request(actor(db_session, "employee"), created.id)


def test_hr_can_review_requests_without_direct_report_scope(db_session):
    created = submit_one_day(db_session)
    hr = actor(db_session, "hr")

    assert [item.id for item in service(db_session).get_managed_leave_requests(hr)] == [created.id]
    approved = service(db_session).approve_leave_request(hr, created.id, "HR exception approval")
    assert approved.status is LeaveStatus.APPROVED


def _token(client, username: str, password: str) -> str:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_manager_lifecycle_endpoints_enforce_roles_and_return_audit(client, db_session):
    created = submit_one_day(db_session)
    employee_token = _token(client, "employee", "correct-password")
    manager_token = _token(client, "manager", "manager-password")

    forbidden = client.get("/api/v1/manager/leave-requests", headers=_headers(employee_token))
    assert forbidden.status_code == 403

    queue = client.get("/api/v1/manager/leave-requests", headers=_headers(manager_token))
    assert queue.status_code == 200
    assert [item["id"] for item in queue.json()] == [created.id]

    approved = client.post(
        f"/api/v1/manager/leave-requests/{created.id}/approve",
        json={"comment": "Approved via API"}, headers=_headers(manager_token),
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"

    history = client.get(
        f"/api/v1/leave/requests/{created.id}/history", headers=_headers(employee_token)
    )
    assert history.status_code == 200
    assert [item["to_status"] for item in history.json()] == ["PENDING", "APPROVED"]
