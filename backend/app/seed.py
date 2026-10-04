import argparse
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.infrastructure.database.models import (
    ConversationSession,
    Employee,
    Holiday,
    LeaveBalance,
    LeaveRequest,
    LeaveRequestEvent,
    OnboardingRequest,
    OnboardingTask,
    ParkingReservation,
    ParkingReservationEvent,
    ParkingSlot,
    ParkingWaitlistEntry,
    PendingAction,
    User,
    Vehicle,
)
from app.infrastructure.database.session import SessionLocal


DEMO_USERS = (
    {
        "employee": {
            "employee_code": "E1001",
            "name": "Asha Rao",
            "email": "asha@example.test",
            "designation": "Software Engineer",
            "department": "Engineering",
            "manager_name": "Karthik Iyer",
            "location": "Chennai",
            "employment_type": "Permanent",
            "joining_date": date(2023, 1, 9),
        },
        "username": "employee",
        "password": "employee123",
        "role": "EMPLOYEE",
    },
    {
        "employee": {
            "employee_code": "M1001",
            "name": "Karthik Iyer",
            "email": "karthik@example.test",
            "designation": "Engineering Manager",
            "department": "Engineering",
            "manager_name": None,
            "location": "Chennai",
            "employment_type": "Permanent",
            "joining_date": date(2020, 6, 1),
        },
        "username": "manager",
        "password": "manager123",
        "role": "MANAGER",
    },
    {
        "employee": {
            "employee_code": "H1001",
            "name": "Meera Nair",
            "email": "meera@example.test",
            "designation": "HR Partner",
            "department": "People",
            "manager_name": None,
            "location": "Bengaluru",
            "employment_type": "Permanent",
            "joining_date": date(2021, 4, 12),
        },
        "username": "hr",
        "password": "hr12345",
        "role": "HR",
    },
    {
        "employee": {
            "employee_code": "H1002",
            "name": "Nandhini Kumar",
            "email": "nandhini@example.test",
            "designation": "HR Administrator",
            "department": "People",
            "manager_name": None,
            "location": "Chennai",
            "employment_type": "Permanent",
            "joining_date": date(2020, 2, 10),
        },
        "username": "hradmin",
        "password": "hradmin123",
        "role": "HR_ADMIN",
    },
    {
        "employee": {
            "employee_code": "P1001",
            "name": "Arun Prakash",
            "email": "arun.parking@example.test",
            "designation": "Workplace Operations Administrator",
            "department": "Workplace Operations",
            "manager_name": None,
            "location": "Chennai",
            "employment_type": "Permanent",
            "joining_date": date(2021, 8, 16),
        },
        "username": "parkingadmin",
        "password": "parkingadmin123",
        "role": "PARKING_ADMIN",
    },
)

DEMO_BALANCES = {
    "EMPLOYEE": (("CASUAL", 12, 8), ("PRIVILEGE", 18, 3), ("SICK", 10, 1)),
    "MANAGER": (("CASUAL", 12, 2), ("PRIVILEGE", 18, 4), ("SICK", 10, 0)),
    "HR": (("CASUAL", 12, 1), ("PRIVILEGE", 18, 2), ("SICK", 10, 0)),
    "HR_ADMIN": (("CASUAL", 12, 1), ("PRIVILEGE", 18, 2), ("SICK", 10, 0)),
    "PARKING_ADMIN": (("CASUAL", 12, 1), ("PRIVILEGE", 18, 2), ("SICK", 10, 0)),
}

DEMO_VEHICLES = {
    "EMPLOYEE": ("TN01AR1001", "CAR", "Hyundai i20"),
    "MANAGER": ("TN01KI1001", "CAR", "Honda City"),
    "HR": ("KA01MN1001", "CAR", "Tata Nexon"),
    "HR_ADMIN": ("TN01NK1001", "MOTORCYCLE", "TVS Ntorq"),
}

DEMO_PARKING_SLOTS = (
    ("B-21", "Chennai HQ - Basement B", "REGULAR"),
    ("B-22", "Chennai HQ - Basement B", "REGULAR"),
    ("B-23", "Chennai HQ - Basement B", "REGULAR"),
    ("B-24", "Chennai HQ - Basement B", "REGULAR"),
    ("B-25", "Chennai HQ - Basement B", "ACCESSIBLE"),
)


def _next_demo_workday() -> date:
    candidate = date.today() + timedelta(days=14)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _next_parking_workday() -> date:
    candidate = date.today() + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _clear_demo_activity(db: Session, user_ids: list[int], employee_ids: list[int]) -> None:
    onboarding_rows = db.execute(
        select(
            OnboardingRequest.id,
            OnboardingRequest.activated_user_id,
            OnboardingRequest.activated_employee_id,
        ).where(OnboardingRequest.created_by_user_id.in_(user_ids))
    ).all()
    onboarding_ids = [row.id for row in onboarding_rows]
    activated_user_ids = [row.activated_user_id for row in onboarding_rows if row.activated_user_id]
    activated_employee_ids = [
        row.activated_employee_id for row in onboarding_rows if row.activated_employee_id
    ]
    parking_employee_ids = [*employee_ids, *activated_employee_ids]
    parking_reservation_ids = select(ParkingReservation.id).where(
        ParkingReservation.employee_id.in_(parking_employee_ids)
    )
    db.execute(
        delete(ParkingReservationEvent).where(
            ParkingReservationEvent.reservation_id.in_(parking_reservation_ids)
        )
    )
    db.execute(
        delete(ParkingWaitlistEntry).where(
            ParkingWaitlistEntry.employee_id.in_(parking_employee_ids)
        )
    )
    db.execute(
        delete(ParkingReservation).where(
            ParkingReservation.employee_id.in_(parking_employee_ids)
        )
    )
    if onboarding_ids:
        db.execute(
            delete(OnboardingTask).where(
                OnboardingTask.onboarding_request_id.in_(onboarding_ids)
            )
        )
        db.execute(delete(OnboardingRequest).where(OnboardingRequest.id.in_(onboarding_ids)))
    if activated_employee_ids:
        activated_leave_ids = select(LeaveRequest.id).where(
            LeaveRequest.employee_id.in_(activated_employee_ids)
        )
        db.execute(
            delete(LeaveRequestEvent).where(
                LeaveRequestEvent.leave_request_id.in_(activated_leave_ids)
            )
        )
        db.execute(
            delete(LeaveRequest).where(LeaveRequest.employee_id.in_(activated_employee_ids))
        )
        db.execute(
            delete(LeaveBalance).where(LeaveBalance.employee_id.in_(activated_employee_ids))
        )
    if activated_user_ids:
        db.execute(delete(PendingAction).where(PendingAction.user_id.in_(activated_user_ids)))
        db.execute(
            delete(ConversationSession).where(
                ConversationSession.user_id.in_(activated_user_ids)
            )
        )
        db.execute(delete(User).where(User.id.in_(activated_user_ids)))
    if activated_employee_ids:
        db.execute(delete(Vehicle).where(Vehicle.employee_id.in_(activated_employee_ids)))
        db.execute(delete(Employee).where(Employee.id.in_(activated_employee_ids)))
    request_ids = select(LeaveRequest.id).where(LeaveRequest.employee_id.in_(employee_ids))
    db.execute(delete(LeaveRequestEvent).where(LeaveRequestEvent.leave_request_id.in_(request_ids)))
    db.execute(delete(LeaveRequest).where(LeaveRequest.employee_id.in_(employee_ids)))
    db.execute(delete(PendingAction).where(PendingAction.user_id.in_(user_ids)))
    db.execute(delete(ConversationSession).where(ConversationSession.user_id.in_(user_ids)))


def seed_database(db: Session, *, reset_demo: bool = False) -> None:
    employees: dict[str, Employee] = {}
    users: dict[str, User] = {}

    for item in DEMO_USERS:
        employee_values = item["employee"]
        employee = db.scalar(
            select(Employee).where(Employee.employee_code == employee_values["employee_code"])
        )
        if employee is None:
            employee = Employee(**employee_values)
            db.add(employee)
            db.flush()
        elif reset_demo:
            for field, value in employee_values.items():
                setattr(employee, field, value)
        employees[item["role"]] = employee

        user = db.scalar(select(User).where(User.username == item["username"]))
        if user is None:
            user = User(
                username=item["username"],
                password_hash=hash_password(item["password"]),
                role=item["role"],
                employee_id=employee.id,
            )
            db.add(user)
            db.flush()
        elif reset_demo:
            user.password_hash = hash_password(item["password"])
            user.role = item["role"]
            user.employee_id = employee.id
        users[item["role"]] = user

    employees["EMPLOYEE"].manager_employee_id = employees["MANAGER"].id

    if reset_demo:
        _clear_demo_activity(
            db,
            [user.id for user in users.values()],
            [employee.id for employee in employees.values()],
        )

    for role, employee in employees.items():
        for leave_type, total, used in DEMO_BALANCES[role]:
            balance = db.scalar(
                select(LeaveBalance).where(
                    LeaveBalance.employee_id == employee.id,
                    LeaveBalance.leave_type == leave_type,
                )
            )
            if balance is None:
                db.add(
                    LeaveBalance(
                        employee_id=employee.id,
                        leave_type=leave_type,
                        total_days=total,
                        used_days=used,
                    )
                )
            elif reset_demo:
                balance.total_days = Decimal(total)
                balance.used_days = Decimal(used)

    vehicles: dict[str, Vehicle] = {}
    for role, (registration_number, vehicle_type, make_model) in DEMO_VEHICLES.items():
        employee = employees[role]
        vehicle = db.scalar(select(Vehicle).where(Vehicle.employee_id == employee.id))
        if vehicle is None:
            vehicle = Vehicle(
                employee_id=employee.id,
                registration_number=registration_number,
                vehicle_type=vehicle_type,
                make_model=make_model,
                active=True,
            )
            db.add(vehicle)
            db.flush()
        elif reset_demo:
            vehicle.registration_number = registration_number
            vehicle.vehicle_type = vehicle_type
            vehicle.make_model = make_model
            vehicle.active = True
        vehicles[role] = vehicle

    parking_slots: dict[str, ParkingSlot] = {}
    for code, location, slot_type in DEMO_PARKING_SLOTS:
        slot = db.scalar(select(ParkingSlot).where(ParkingSlot.code == code))
        if slot is None:
            slot = ParkingSlot(
                code=code,
                location=location,
                slot_type=slot_type,
                active=True,
            )
            db.add(slot)
            db.flush()
        elif reset_demo:
            slot.location = location
            slot.slot_type = slot_type
            slot.active = True
        parking_slots[code] = slot

    year = date.today().year
    for holiday_date, name in (
        (date(year, 1, 26), "Republic Day"),
        (date(year, 8, 15), "Independence Day"),
        (date(year, 10, 2), "Gandhi Jayanti"),
        (date(year, 12, 25), "Christmas"),
    ):
        holiday = db.scalar(select(Holiday).where(Holiday.holiday_date == holiday_date))
        if holiday is None:
            db.add(Holiday(holiday_date=holiday_date, name=name, category="PUBLIC"))
        elif reset_demo:
            holiday.name = name
            holiday.category = "PUBLIC"

    if reset_demo:
        request_date = _next_demo_workday()
        request = LeaveRequest(
            employee_id=employees["EMPLOYEE"].id,
            manager_employee_id=employees["MANAGER"].id,
            leave_type="CASUAL",
            start_date=request_date,
            end_date=request_date,
            working_days=Decimal("1"),
            reason="Family appointment",
            status="PENDING",
        )
        db.add(request)
        db.flush()
        db.add(
            LeaveRequestEvent(
                leave_request_id=request.id,
                actor_user_id=users["EMPLOYEE"].id,
                from_status=None,
                to_status="PENDING",
                comment="Demo request submitted by employee",
            )
        )

    parking_date = _next_parking_workday()
    occupied = db.scalar(
        select(ParkingReservation).where(
            ParkingReservation.employee_id == employees["MANAGER"].id,
            ParkingReservation.reservation_date == parking_date,
            ParkingReservation.status.in_(("RESERVED", "CHECKED_IN")),
        )
    )
    if occupied is None:
        occupied = ParkingReservation(
            employee_id=employees["MANAGER"].id,
            vehicle_id=vehicles["MANAGER"].id,
            slot_id=parking_slots["B-21"].id,
            reservation_date=parking_date,
            status="RESERVED",
        )
        db.add(occupied)
        db.flush()
        db.add(
            ParkingReservationEvent(
                reservation_id=occupied.id,
                actor_user_id=users["MANAGER"].id,
                from_status=None,
                to_status="RESERVED",
                reason="Seeded occupied slot for the parking demo",
            )
        )


def seed(*, reset_demo: bool = False) -> None:
    with SessionLocal.begin() as db:
        seed_database(db, reset_demo=reset_demo)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed local demonstration identities and HR data")
    parser.add_argument(
        "--reset-demo",
        action="store_true",
        help="Clear demo activity and restore the leave, onboarding, and parking baseline",
    )
    args = parser.parse_args()
    seed(reset_demo=args.reset_demo)
    print("Demo data reset complete." if args.reset_demo else "Demo data seed complete.")
