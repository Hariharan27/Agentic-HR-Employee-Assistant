from datetime import date, timedelta

from sqlalchemy import select

from app.core.security import AuthenticatedUser
from app.infrastructure.database.models import (
    Employee,
    ParkingReservation,
    ParkingReservationEvent,
    ParkingSlot,
    User,
    Vehicle,
)


def _token(client, username: str, password: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _actor(db_session, username: str) -> AuthenticatedUser:
    user = db_session.scalar(select(User).where(User.username == username))
    return AuthenticatedUser(user.id, user.employee_id, user.role)


def _setup_parking(db_session):
    employee = db_session.scalar(select(Employee).where(Employee.employee_code == "E1"))
    manager = db_session.scalar(select(Employee).where(Employee.employee_code == "M1"))
    employee_vehicle = Vehicle(
        employee_id=employee.id,
        registration_number="TN01AA1001",
        vehicle_type="CAR",
        active=True,
    )
    manager_vehicle = Vehicle(
        employee_id=manager.id,
        registration_number="TN01MM1001",
        vehicle_type="CAR",
        active=True,
    )
    slots = [
        ParkingSlot(code=f"B-{index + 21}", location="Chennai HQ", active=True)
        for index in range(3)
    ]
    db_session.add_all([employee_vehicle, manager_vehicle, *slots])
    db_session.commit()
    return employee_vehicle, manager_vehicle, slots


def test_parking_admin_can_read_daily_queue(client, db_session):
    employee_vehicle, _, slots = _setup_parking(db_session)
    employee = _actor(db_session, "employee")
    db_session.add(
        ParkingReservation(
            employee_id=employee.employee_id,
            vehicle_id=employee_vehicle.id,
            slot_id=slots[0].id,
            reservation_date=date(2026, 10, 8),
            status="RESERVED",
        )
    )
    db_session.commit()
    parking_token = _token(client, "parkingadmin", "parkingadmin-password")

    response = client.get(
        "/api/v1/parking-admin/reservations",
        params={"reservation_date": "2026-10-08"},
        headers=_headers(parking_token),
    )

    assert response.status_code == 200
    assert response.json()[0]["employee_code"] == "E1"
    assert response.json()[0]["vehicle_registration"] == "TN01AA1001"
    assert response.json()[0]["status"] == "RESERVED"


def test_employee_cannot_read_parking_admin_queue(client, db_session):
    _setup_parking(db_session)
    employee_token = _token(client, "employee", "correct-password")

    response = client.get(
        "/api/v1/parking-admin/reservations",
        params={"reservation_date": "2026-10-08"},
        headers=_headers(employee_token),
    )

    assert response.status_code == 403


def test_employee_suspension_endpoint_counts_active_no_shows(client, db_session):
    employee_vehicle, _, slots = _setup_parking(db_session)
    employee = _actor(db_session, "employee")
    today = date.today()
    db_session.add_all(
        [
            ParkingReservation(
                employee_id=employee.employee_id,
                vehicle_id=employee_vehicle.id,
                slot_id=slots[index].id,
                reservation_date=today - timedelta(days=index),
                status="NO_SHOW",
            )
            for index in range(3)
        ]
    )
    db_session.commit()
    employee_token = _token(client, "employee", "correct-password")

    response = client.get(
        "/api/v1/parking/me/suspension", headers=_headers(employee_token)
    )

    assert response.status_code == 200
    assert response.json()["active"] is True
    assert response.json()["no_show_count"] == 3


def test_reservation_history_is_owner_or_parking_admin_only(client, db_session):
    employee_vehicle, manager_vehicle, slots = _setup_parking(db_session)
    employee = _actor(db_session, "employee")
    manager = _actor(db_session, "manager")
    reservation = ParkingReservation(
        employee_id=employee.employee_id,
        vehicle_id=employee_vehicle.id,
        slot_id=slots[0].id,
        reservation_date=date(2026, 10, 8),
        status="RESERVED",
    )
    other = ParkingReservation(
        employee_id=manager.employee_id,
        vehicle_id=manager_vehicle.id,
        slot_id=slots[1].id,
        reservation_date=date(2026, 10, 8),
        status="RESERVED",
    )
    db_session.add_all([reservation, other])
    db_session.flush()
    db_session.add(
        ParkingReservationEvent(
            reservation_id=reservation.id,
            actor_user_id=employee.user_id,
            from_status=None,
            to_status="RESERVED",
            reason="Reserved by employee",
        )
    )
    db_session.commit()
    employee_token = _token(client, "employee", "correct-password")
    manager_token = _token(client, "manager", "manager-password")
    parking_token = _token(client, "parkingadmin", "parkingadmin-password")

    owner = client.get(
        f"/api/v1/parking/reservations/{reservation.id}/history",
        headers=_headers(employee_token),
    )
    forbidden = client.get(
        f"/api/v1/parking/reservations/{reservation.id}/history",
        headers=_headers(manager_token),
    )
    admin = client.get(
        f"/api/v1/parking/reservations/{reservation.id}/history",
        headers=_headers(parking_token),
    )

    assert owner.status_code == 200
    assert forbidden.status_code == 403
    assert admin.status_code == 200
    assert owner.json()[0]["reason"] == "Reserved by employee"


def test_employee_lists_own_vehicles(client, db_session):
    token = client.post("/api/v1/auth/login", json={"username": "employee", "password": "correct-password"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/parking/me/vehicles", headers=headers).json() == []
    from app.infrastructure.database.models import User, Vehicle
    from sqlalchemy import select
    employee_id = db_session.scalar(select(User).where(User.username == "employee")).employee_id
    db_session.add(Vehicle(employee_id=employee_id, registration_number="TN01ZZ1234", vehicle_type="CAR", make_model="i20", active=True))
    db_session.commit()

    vehicles = client.get("/api/v1/parking/me/vehicles", headers=headers).json()

    assert vehicles == [{"registration_number": "TN01ZZ1234", "vehicle_type": "CAR", "make_model": "i20"}]
