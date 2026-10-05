from datetime import date

import pytest
from sqlalchemy import func, select

from app.application.onboarding.handler import CreateOnboardingHandler
from app.application.onboarding.service import OnboardingService
from app.core.exceptions import ValidationError
from app.core.security import AuthenticatedUser
from app.domain.onboarding.entities import OnboardingCandidate
from app.infrastructure.database.models import OnboardingRequest, User
from app.infrastructure.repositories.onboarding import SQLAlchemyOnboardingRepository


def _token(client, username: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _candidate_payload(**overrides) -> dict:
    values = {
        "name": "Priya Raman",
        "email": "priya.raman@example.com",
        "designation": "Backend Developer",
        "department": "Engineering",
        "reporting_manager": "Test Manager",
        "joining_date": "2026-10-15",
        "location": "Chennai",
        "employment_type": "Permanent",
    }
    values.update(overrides)
    return values


def _manager_actor(db_session) -> AuthenticatedUser:
    user = db_session.scalar(select(User).where(User.username == "manager"))
    return AuthenticatedUser(user.id, user.employee_id, user.role)


def _create_onboarding(db_session):
    candidate = OnboardingCandidate(
        name="Priya Raman",
        email="priya.raman@example.com",
        designation="Backend Developer",
        department="Engineering",
        reporting_manager="Test Manager",
        joining_date=date(2026, 10, 15),
        location="Chennai",
        employment_type="Permanent",
    )
    return OnboardingService(SQLAlchemyOnboardingRepository(db_session)).create_onboarding(
        _manager_actor(db_session), candidate
    )


def test_manager_can_preview_plan_without_creating_data(client, db_session):
    manager_token = _token(client, "manager", "manager-password")

    response = client.post(
        "/api/v1/manager/onboarding/plan",
        json=_candidate_payload(),
        headers=_headers(manager_token),
    )

    assert response.status_code == 200
    assert response.json()["task_types"] == [
        "CORPORATE_EMAIL",
        "LAPTOP",
        "ACCESS_CARD",
        "TEMPORARY_ACCESS_CARD",
        "PAYROLL_SETUP",
    ]
    assert db_session.scalar(select(func.count()).select_from(OnboardingRequest)) == 0


def test_manager_can_list_reporting_managers_for_structured_form(client):
    manager_token = _token(client, "manager", "manager-password")

    response = client.get(
        "/api/v1/manager/onboarding/reporting-managers",
        headers=_headers(manager_token),
    )

    assert response.status_code == 200
    payload = response.json()
    assert any(item["name"] == "Test Manager" for item in payload)
    assert all({"id", "name", "employee_code", "designation", "department"} <= set(item) for item in payload)


def test_employee_cannot_use_onboarding_tools(client):
    employee_token = _token(client, "employee", "correct-password")

    response = client.post(
        "/api/v1/manager/onboarding/plan",
        json=_candidate_payload(),
        headers=_headers(employee_token),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_duplicate_check_is_authenticated_and_role_protected(client):
    manager_token = _token(client, "manager", "manager-password")
    employee_token = _token(client, "employee", "correct-password")

    existing = client.get(
        "/api/v1/manager/onboarding/employee-exists",
        params={"email": "EMPLOYEE@TEST.LOCAL"},
        headers=_headers(manager_token),
    )
    forbidden = client.get(
        "/api/v1/manager/onboarding/employee-exists",
        params={"email": "employee@test.local"},
        headers=_headers(employee_token),
    )

    assert existing.status_code == 200
    assert existing.json() == {"email": "employee@test.local", "exists": True}
    assert forbidden.status_code == 403


def test_manager_can_read_status_by_id_or_employee_name(client, db_session):
    created = _create_onboarding(db_session)
    manager_token = _token(client, "manager", "manager-password")

    by_id = client.get(
        f"/api/v1/manager/onboarding/{created.id}", headers=_headers(manager_token)
    )
    by_name = client.get(
        "/api/v1/manager/onboarding/status/by-employee",
        params={"employee": "Priya"},
        headers=_headers(manager_token),
    )

    assert by_id.status_code == 200
    assert by_name.status_code == 200
    assert by_id.json()["id"] == by_name.json()["id"] == created.id
    assert by_name.json()["total_tasks"] == 5


def test_create_handler_produces_json_safe_arguments_and_rejects_missing_fields(db_session):
    handler = CreateOnboardingHandler(
        OnboardingService(SQLAlchemyOnboardingRepository(db_session))
    )

    validated = handler.validate_arguments(_candidate_payload())

    assert validated["joining_date"] == "2026-10-15"
    with pytest.raises(ValidationError, match="Invalid arguments"):
        handler.validate_arguments({"name": "Priya"})


def test_hr_admin_queue_and_activated_employee_can_log_in(client, db_session):
    created = _create_onboarding(db_session)
    admin_token = _token(client, "hradmin", "hradmin-password")

    queue = client.get(
        "/api/v1/hr-admin/onboarding/pending", headers=_headers(admin_token)
    )
    assert queue.status_code == 200
    assert [item["id"] for item in queue.json()] == [created.id]

    activation = OnboardingService(
        SQLAlchemyOnboardingRepository(db_session)
    ).approve_onboarding(
        AuthenticatedUser(
            db_session.scalar(select(User).where(User.username == "hradmin")).id,
            db_session.scalar(select(User).where(User.username == "hradmin")).employee_id,
            "HR_ADMIN",
        ),
        created.id,
    )
    login_response = client.post(
        "/api/v1/auth/login",
        json={"username": activation.username, "password": activation.temporary_password},
    )

    assert login_response.status_code == 200
    assert login_response.json()["role"] == "EMPLOYEE"
    assert login_response.json()["must_change_password"] is True
    temporary = activation.temporary_password
    headers = _headers(login_response.json()["access_token"])

    # Until the temporary password is changed, only the profile and password change work.
    blocked = client.get("/api/v1/leave/requests", headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "password_change_required"
    assert client.get("/api/v1/auth/me", headers=headers).json()["must_change_password"] is True

    weak = client.post("/api/v1/auth/change-password", headers=headers,
                       json={"current_password": temporary, "new_password": "short1"})
    assert weak.status_code == 400
    assert "at least 10 characters" in weak.json()["error"]["message"]
    wrong = client.post("/api/v1/auth/change-password", headers=headers,
                        json={"current_password": "not-it", "new_password": "Nila!Desk-2026"})
    assert wrong.status_code == 401

    changed = client.post("/api/v1/auth/change-password", headers=headers,
                          json={"current_password": temporary, "new_password": "Nila!Desk-2026"})
    assert changed.status_code == 200
    assert changed.json()["must_change_password"] is False
    assert client.get("/api/v1/leave/requests", headers=headers).status_code == 200
    old = client.post("/api/v1/auth/login", json={"username": activation.username, "password": temporary})
    assert old.status_code == 401
    relogin = client.post("/api/v1/auth/login", json={"username": activation.username, "password": "Nila!Desk-2026"})
    assert relogin.json()["must_change_password"] is False


def test_non_admin_cannot_read_onboarding_approval_queue(client):
    manager_token = _token(client, "manager", "manager-password")

    response = client.get(
        "/api/v1/hr-admin/onboarding/pending", headers=_headers(manager_token)
    )

    assert response.status_code == 403
