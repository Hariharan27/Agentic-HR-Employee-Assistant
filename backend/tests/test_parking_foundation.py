from datetime import date, datetime, UTC

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.infrastructure.database.models import (
    Employee,
    ParkingReservation,
    ParkingSlot,
    ParkingWaitlistEntry,
    Vehicle,
)
from app.infrastructure.repositories.parking import SQLAlchemyParkingRepository


BOOKING_DATE = date(2026, 10, 12)


def _employees(db_session):
    employee = db_session.scalar(select(Employee).where(Employee.employee_code == "E1"))
    manager = db_session.scalar(select(Employee).where(Employee.employee_code == "M1"))
    return employee, manager


def _parking_records(db_session):
    employee, manager = _employees(db_session)
    employee_vehicle = Vehicle(
        employee_id=employee.id,
        registration_number="TN01AA1001",
        vehicle_type="CAR",
        make_model="Hyundai i20",
        active=True,
    )
    manager_vehicle = Vehicle(
        employee_id=manager.id,
        registration_number="TN01MM1001",
        vehicle_type="CAR",
        make_model="Honda City",
        active=True,
    )
    slots = [
        ParkingSlot(code="B-21", location="Chennai HQ", slot_type="REGULAR", active=True),
        ParkingSlot(code="B-22", location="Chennai HQ", slot_type="REGULAR", active=True),
        ParkingSlot(code="B-23", location="Chennai HQ", slot_type="REGULAR", active=False),
    ]
    db_session.add_all([employee_vehicle, manager_vehicle, *slots])
    db_session.flush()
    return employee, manager, employee_vehicle, manager_vehicle, slots


def test_repository_returns_registered_vehicle_and_only_available_active_slots(db_session):
    employee, manager, employee_vehicle, manager_vehicle, slots = _parking_records(db_session)
    db_session.add(
        ParkingReservation(
            employee_id=manager.id,
            vehicle_id=manager_vehicle.id,
            slot_id=slots[0].id,
            reservation_date=BOOKING_DATE,
            status="CHECKED_IN",
            checked_in_at=datetime(2026, 10, 12, 3, 30, tzinfo=UTC),
        )
    )
    db_session.commit()

    repository = SQLAlchemyParkingRepository(db_session)

    vehicle = repository.get_active_vehicle(employee.id)
    assert vehicle is not None
    assert vehicle.registration_number == "TN01AA1001"
    assert [slot.code for slot in repository.list_active_slots()] == ["B-21", "B-22"]
    assert [slot.code for slot in repository.list_available_slots(BOOKING_DATE)] == ["B-22"]

    reservation = repository.get_active_reservation(manager.id, BOOKING_DATE)
    assert reservation is not None
    assert reservation.status.value == "CHECKED_IN"
    assert reservation.slot.code == "B-21"
    assert reservation.checked_in_at is not None


def test_cancelled_and_completed_reservations_do_not_occupy_a_slot(db_session):
    employee, manager, employee_vehicle, manager_vehicle, slots = _parking_records(db_session)
    db_session.add_all(
        [
            ParkingReservation(
                employee_id=employee.id,
                vehicle_id=employee_vehicle.id,
                slot_id=slots[0].id,
                reservation_date=BOOKING_DATE,
                status="CANCELLED",
            ),
            ParkingReservation(
                employee_id=manager.id,
                vehicle_id=manager_vehicle.id,
                slot_id=slots[1].id,
                reservation_date=BOOKING_DATE,
                status="COMPLETED",
            ),
        ]
    )
    db_session.commit()

    available = SQLAlchemyParkingRepository(db_session).list_available_slots(BOOKING_DATE)
    assert [slot.code for slot in available] == ["B-21", "B-22"]


def test_database_prevents_two_active_reservations_for_one_employee(db_session):
    employee, _, employee_vehicle, _, slots = _parking_records(db_session)
    db_session.add(
        ParkingReservation(
            employee_id=employee.id,
            vehicle_id=employee_vehicle.id,
            slot_id=slots[0].id,
            reservation_date=BOOKING_DATE,
            status="RESERVED",
        )
    )
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.add(
            ParkingReservation(
                employee_id=employee.id,
                vehicle_id=employee_vehicle.id,
                slot_id=slots[1].id,
                reservation_date=BOOKING_DATE,
                status="CHECKED_IN",
            )
        )
        db_session.commit()
    db_session.rollback()


def test_database_prevents_two_employees_from_booking_one_slot(db_session):
    employee, manager, employee_vehicle, manager_vehicle, slots = _parking_records(db_session)
    db_session.add(
        ParkingReservation(
            employee_id=employee.id,
            vehicle_id=employee_vehicle.id,
            slot_id=slots[0].id,
            reservation_date=BOOKING_DATE,
            status="RESERVED",
        )
    )
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.add(
            ParkingReservation(
                employee_id=manager.id,
                vehicle_id=manager_vehicle.id,
                slot_id=slots[0].id,
                reservation_date=BOOKING_DATE,
                status="RESERVED",
            )
        )
        db_session.commit()
    db_session.rollback()


def test_waitlist_prevents_duplicate_active_entry_but_allows_rejoining_after_cancel(db_session):
    employee, _, employee_vehicle, _, _ = _parking_records(db_session)
    first = ParkingWaitlistEntry(
        employee_id=employee.id,
        vehicle_id=employee_vehicle.id,
        requested_date=BOOKING_DATE,
        status="WAITING",
    )
    db_session.add(first)
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.add(
            ParkingWaitlistEntry(
                employee_id=employee.id,
                vehicle_id=employee_vehicle.id,
                requested_date=BOOKING_DATE,
                status="WAITING",
            )
        )
        db_session.commit()
    db_session.rollback()

    first.status = "CANCELLED"
    db_session.commit()
    db_session.add(
        ParkingWaitlistEntry(
            employee_id=employee.id,
            vehicle_id=employee_vehicle.id,
            requested_date=BOOKING_DATE,
            status="WAITING",
        )
    )
    db_session.commit()

    entry = SQLAlchemyParkingRepository(db_session).get_waitlist_entry(
        employee.id, BOOKING_DATE
    )
    assert entry is not None
    assert entry.status.value == "WAITING"
