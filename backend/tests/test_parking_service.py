from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from app.application.parking.service import ParkingService
from app.core.config import Settings
from app.core.exceptions import AuthorizationError, ConflictError, ParkingUnavailableError, ValidationError
from app.core.security import AuthenticatedUser
from app.infrastructure.database.models import (
    Employee,
    ParkingReservation,
    ParkingReservationEvent,
    ParkingSlot,
    ParkingWaitlistEntry,
    User,
    Vehicle,
)
from app.infrastructure.repositories.parking import SQLAlchemyParkingRepository


NOW = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
BOOKING_DATE = date(2026, 10, 8)


def _actor(db_session, username: str = "employee") -> AuthenticatedUser:
    user = db_session.scalar(select(User).where(User.username == username))
    return AuthenticatedUser(user.id, user.employee_id, user.role)


def _setup_parking(db_session, *, slot_count: int = 2):
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
        ParkingSlot(
            code=f"B-{index + 21}",
            location="Chennai HQ",
            slot_type="REGULAR",
            active=True,
        )
        for index in range(slot_count)
    ]
    db_session.add_all([employee_vehicle, manager_vehicle, *slots])
    db_session.commit()
    return employee_vehicle, manager_vehicle, slots


def _service(db_session, *, now=NOW) -> ParkingService:
    return ParkingService(
        SQLAlchemyParkingRepository(db_session), Settings(), now=lambda: now
    )


def test_employee_can_read_vehicle_and_database_availability(db_session):
    employee_vehicle, manager_vehicle, slots = _setup_parking(db_session)
    manager = _actor(db_session, "manager")
    db_session.add(
        ParkingReservation(
            employee_id=manager.employee_id,
            vehicle_id=manager_vehicle.id,
            slot_id=slots[0].id,
            reservation_date=BOOKING_DATE,
            status="RESERVED",
        )
    )
    db_session.commit()

    availability = _service(db_session).check_availability(
        _actor(db_session), BOOKING_DATE
    )

    assert availability.vehicle.id == employee_vehicle.id
    assert [slot.code for slot in availability.available_slots] == ["B-22"]


def test_reservation_is_revalidated_and_audited(db_session):
    _, _, slots = _setup_parking(db_session, slot_count=1)
    actor = _actor(db_session)
    service = _service(db_session)
    _, offered = service.prepare_reservation(actor, BOOKING_DATE)

    created = service.reserve_parking(actor, BOOKING_DATE, offered.id)

    assert created.slot.code == "B-21"
    assert created.status.value == "RESERVED"
    assert db_session.scalar(select(func.count()).select_from(ParkingReservationEvent)) == 1
    with pytest.raises(ConflictError, match="already have"):
        service.prepare_reservation(actor, BOOKING_DATE)


def test_confirmation_fails_when_offered_slot_was_taken(db_session):
    _, manager_vehicle, slots = _setup_parking(db_session, slot_count=1)
    actor = _actor(db_session)
    manager = _actor(db_session, "manager")
    service = _service(db_session)
    _, offered = service.prepare_reservation(actor, BOOKING_DATE)
    db_session.add(
        ParkingReservation(
            employee_id=manager.employee_id,
            vehicle_id=manager_vehicle.id,
            slot_id=slots[0].id,
            reservation_date=BOOKING_DATE,
            status="RESERVED",
        )
    )
    db_session.commit()

    with pytest.raises(ParkingUnavailableError, match="no longer available"):
        service.reserve_parking(actor, BOOKING_DATE, offered.id)


def test_employee_cancellation_respects_cutoff_and_ownership(db_session):
    _, _, slots = _setup_parking(db_session, slot_count=1)
    employee = _actor(db_session)
    manager = _actor(db_session, "manager")
    service = _service(db_session)
    created = service.reserve_parking(employee, BOOKING_DATE, slots[0].id)

    with pytest.raises(AuthorizationError):
        service.stage_cancellation(manager, created.id)

    cancelled = service.cancel_parking(employee, created.id, "Plans changed")
    assert cancelled.status.value == "CANCELLED"
    events = db_session.scalars(
        select(ParkingReservationEvent).order_by(ParkingReservationEvent.id)
    ).all()
    assert [event.to_status for event in events] == ["RESERVED", "CANCELLED"]
    assert events[-1].reason == "Plans changed"

    late_service = _service(
        db_session, now=datetime(2026, 10, 7, 14, 31, tzinfo=UTC)
    )
    second = ParkingReservation(
        employee_id=employee.employee_id,
        vehicle_id=db_session.scalar(
            select(Vehicle).where(Vehicle.employee_id == employee.employee_id)
        ).id,
        slot_id=slots[0].id,
        reservation_date=BOOKING_DATE,
        status="RESERVED",
    )
    db_session.add(second)
    db_session.commit()
    with pytest.raises(ConflictError, match="cutoff has passed"):
        late_service.prepare_cancellation(employee, BOOKING_DATE)


def test_waitlist_requires_full_parking_and_prevents_duplicates(db_session):
    employee_vehicle, manager_vehicle, slots = _setup_parking(db_session, slot_count=1)
    employee = _actor(db_session)
    manager = _actor(db_session, "manager")
    service = _service(db_session)

    with pytest.raises(ConflictError, match="currently available"):
        service.prepare_waitlist(employee, BOOKING_DATE)

    db_session.add(
        ParkingReservation(
            employee_id=manager.employee_id,
            vehicle_id=manager_vehicle.id,
            slot_id=slots[0].id,
            reservation_date=BOOKING_DATE,
            status="RESERVED",
        )
    )
    db_session.commit()

    service.prepare_waitlist(employee, BOOKING_DATE)
    entry = service.join_waitlist(employee, BOOKING_DATE)
    assert entry.status.value == "WAITING"
    assert entry.vehicle_id == employee_vehicle.id
    assert db_session.scalar(select(func.count()).select_from(ParkingWaitlistEntry)) == 1
    with pytest.raises(ConflictError, match="already on"):
        service.prepare_waitlist(employee, BOOKING_DATE)


def test_parking_admin_can_check_in_complete_and_audit_arrival(db_session):
    _, _, slots = _setup_parking(db_session, slot_count=1)
    employee = _actor(db_session)
    parking_admin = _actor(db_session, "parkingadmin")
    reservation = _service(db_session).reserve_parking(employee, BOOKING_DATE, slots[0].id)
    service = _service(db_session, now=datetime(2026, 10, 8, 4, 0, tzinfo=UTC))

    with pytest.raises(AuthorizationError):
        service.prepare_check_in(employee, reservation.id)

    checked_in = service.stage_check_in(parking_admin, reservation.id)
    completed = service.stage_completion(parking_admin, reservation.id)

    assert checked_in.status.value == "CHECKED_IN"
    assert completed.status.value == "COMPLETED"
    events = db_session.scalars(
        select(ParkingReservationEvent).order_by(ParkingReservationEvent.id)
    ).all()
    assert [event.to_status for event in events] == ["RESERVED", "CHECKED_IN", "COMPLETED"]


def test_parking_admin_late_cancel_requires_reason(db_session):
    _, _, slots = _setup_parking(db_session, slot_count=1)
    employee = _actor(db_session)
    parking_admin = _actor(db_session, "parkingadmin")
    reservation = _service(db_session).reserve_parking(employee, BOOKING_DATE, slots[0].id)
    service = _service(db_session, now=datetime(2026, 10, 7, 15, 30, tzinfo=UTC))

    with pytest.raises(ValidationError, match="reason"):
        service.prepare_admin_cancellation(parking_admin, reservation.id, "")

    cancelled = service.stage_admin_cancellation(
        parking_admin, reservation.id, "Employee called workplace team after cutoff"
    )

    assert cancelled.status.value == "CANCELLED"
    assert db_session.scalars(
        select(ParkingReservationEvent).order_by(ParkingReservationEvent.id)
    ).all()[-1].reason == "Employee called workplace team after cutoff"


def test_no_show_cutoff_override_and_suspension(db_session):
    employee_vehicle, _, slots = _setup_parking(db_session, slot_count=3)
    employee = _actor(db_session)
    parking_admin = _actor(db_session, "parkingadmin")
    today = date(2026, 10, 8)
    db_session.add_all(
        [
            ParkingReservation(
                employee_id=employee.employee_id,
                vehicle_id=employee_vehicle.id,
                slot_id=slots[index].id,
                reservation_date=today.replace(day=6 + index),
                status="NO_SHOW",
            )
            for index in range(2)
        ]
    )
    reservation = ParkingReservation(
        employee_id=employee.employee_id,
        vehicle_id=employee_vehicle.id,
        slot_id=slots[2].id,
        reservation_date=today,
        status="RESERVED",
    )
    db_session.add(reservation)
    db_session.commit()

    early = _service(db_session, now=datetime(2026, 10, 8, 5, 40, tzinfo=UTC))
    with pytest.raises(ConflictError, match="before"):
        early.prepare_no_show(parking_admin, reservation.id)

    after_cutoff = _service(db_session, now=datetime(2026, 10, 8, 5, 46, tzinfo=UTC))
    no_show = after_cutoff.stage_no_show(parking_admin, reservation.id)
    assert no_show.status.value == "NO_SHOW"
    suspension = after_cutoff.get_suspension(employee)
    assert suspension.active is True
    assert suspension.no_show_count == 3

    with pytest.raises(ConflictError, match="suspended"):
        after_cutoff.prepare_reservation(employee, date(2026, 10, 9))

    corrected = after_cutoff.stage_no_show_override(
        parking_admin, reservation.id, "Employee was present; scan missed"
    )
    assert corrected.status.value == "CANCELLED"
    assert after_cutoff.get_suspension(employee).active is False
