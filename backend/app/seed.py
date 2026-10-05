import argparse
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.domain.leave.holidays import HOLIDAY_CALENDARS
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
            "employee_code": "I26004",
            "name": "Advik",
            "email": "advik@example.test",
            "designation": "Software Engineer",
            "department": "Engineering",
            "manager_name": "Saanvika Sree",
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
            "employee_code": "I26003",
            "name": "Saanvika Sree",
            "email": "saanvika@example.test",
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
            "employee_code": "I26002",
            "name": "Hariharan",
            "email": "hariharan@example.test",
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
            "employee_code": "I26001",
            "name": "Alaguselvi",
            "email": "alaguselvi@example.test",
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
            "employee_code": "I26005",
            "name": "Dhaswanth",
            "email": "dhaswanth@example.test",
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
    "EMPLOYEE": (("CASUAL", 6, 2, 0), ("SICK", 6, 1, 0), ("EARNED", 12, 3, 8)),
    "MANAGER": (("CASUAL", 6, 1, 0), ("SICK", 6, 0, 0), ("EARNED", 12, 2, 8)),
    "HR": (("CASUAL", 6, 1, 0), ("SICK", 6, 0, 0), ("EARNED", 12, 2, 8)),
    "HR_ADMIN": (("CASUAL", 6, 1, 0), ("SICK", 6, 0, 0), ("EARNED", 12, 2, 8)),
    "PARKING_ADMIN": (("CASUAL", 6, 1, 0), ("SICK", 6, 0, 0), ("EARNED", 12, 2, 8)),
}

DEMO_PARKING_SLOTS = (
    ("B-21", "Chennai HQ - Basement B", "REGULAR"),
    ("B-22", "Chennai HQ - Basement B", "REGULAR"),
    ("B-23", "Chennai HQ - Basement B", "REGULAR"),
    ("B-24", "Chennai HQ - Basement B", "REGULAR"),
    ("B-25", "Chennai HQ - Basement B", "ACCESSIBLE"),
)

LEGACY_DEMO_EMPLOYEE_CODES = ("E1001", "M1001", "H1001", "H1002")


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
    # Vehicles are created through the parking workflow. Remove only demo-owned
    # vehicles when resetting the demo; parking slots are master data and stay.
    db.execute(delete(Vehicle).where(Vehicle.employee_id.in_(parking_employee_ids)))
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
    _clear_legacy_demo_identities(db)


def _clear_legacy_demo_identities(db: Session) -> None:
    legacy_employee_ids = list(
        db.scalars(
            select(Employee.id).where(Employee.employee_code.in_(LEGACY_DEMO_EMPLOYEE_CODES))
        )
    )
    if not legacy_employee_ids:
        return
    legacy_user_ids = list(
        db.scalars(select(User.id).where(User.employee_id.in_(legacy_employee_ids)))
    )

    legacy_request_ids = select(LeaveRequest.id).where(
        LeaveRequest.employee_id.in_(legacy_employee_ids)
    )
    db.execute(
        delete(LeaveRequestEvent).where(
            LeaveRequestEvent.leave_request_id.in_(legacy_request_ids)
        )
    )
    db.execute(delete(LeaveRequest).where(LeaveRequest.employee_id.in_(legacy_employee_ids)))

    parking_reservation_ids = select(ParkingReservation.id).where(
        ParkingReservation.employee_id.in_(legacy_employee_ids)
    )
    db.execute(
        delete(ParkingReservationEvent).where(
            ParkingReservationEvent.reservation_id.in_(parking_reservation_ids)
        )
    )
    db.execute(
        delete(ParkingWaitlistEntry).where(
            ParkingWaitlistEntry.employee_id.in_(legacy_employee_ids)
        )
    )
    db.execute(
        delete(ParkingReservation).where(
            ParkingReservation.employee_id.in_(legacy_employee_ids)
        )
    )

    onboarding_ids = select(OnboardingRequest.id).where(
        OnboardingRequest.manager_employee_id.in_(legacy_employee_ids)
        | OnboardingRequest.activated_employee_id.in_(legacy_employee_ids)
    )
    db.execute(
        delete(OnboardingTask).where(OnboardingTask.onboarding_request_id.in_(onboarding_ids))
    )
    db.execute(
        delete(OnboardingRequest).where(
            OnboardingRequest.manager_employee_id.in_(legacy_employee_ids)
            | OnboardingRequest.activated_employee_id.in_(legacy_employee_ids)
        )
    )

    db.execute(delete(LeaveBalance).where(LeaveBalance.employee_id.in_(legacy_employee_ids)))
    db.execute(delete(Vehicle).where(Vehicle.employee_id.in_(legacy_employee_ids)))
    if legacy_user_ids:
        db.execute(delete(PendingAction).where(PendingAction.user_id.in_(legacy_user_ids)))
        db.execute(
            delete(ConversationSession).where(ConversationSession.user_id.in_(legacy_user_ids))
        )
        db.execute(delete(User).where(User.id.in_(legacy_user_ids)))
    for employee in db.scalars(
        select(Employee).where(Employee.manager_employee_id.in_(legacy_employee_ids))
    ):
        employee.manager_employee_id = None
    db.flush()
    db.execute(delete(Employee).where(Employee.id.in_(legacy_employee_ids)))


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
            user = db.scalar(select(User).where(User.employee_id == employee.id))
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
            user.username = item["username"]
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
        for employee in employees.values():
            earned = db.scalar(
                select(LeaveBalance).where(
                    LeaveBalance.employee_id == employee.id,
                    LeaveBalance.leave_type == "EARNED",
                )
            )
            legacy_privilege = db.scalar(
                select(LeaveBalance).where(
                    LeaveBalance.employee_id == employee.id,
                    LeaveBalance.leave_type == "PRIVILEGE",
                )
            )
            if legacy_privilege is not None and earned is None:
                legacy_privilege.leave_type = "EARNED"
            elif legacy_privilege is not None:
                db.delete(legacy_privilege)
        db.flush()
        db.execute(
            delete(LeaveBalance).where(
                LeaveBalance.employee_id.in_([employee.id for employee in employees.values()]),
                LeaveBalance.leave_type.not_in(("CASUAL", "SICK", "EARNED")),
            )
        )

    for role, employee in employees.items():
        for leave_type, total, used, carry_forward_limit in DEMO_BALANCES[role]:
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
                        carry_forward_limit_days=carry_forward_limit,
                    )
                )
            elif reset_demo:
                balance.total_days = Decimal(total)
                balance.used_days = Decimal(used)
                balance.carry_forward_limit_days = Decimal(carry_forward_limit)

    for code, location, slot_type in DEMO_PARKING_SLOTS:
        slot = db.scalar(select(ParkingSlot).where(ParkingSlot.code == code))
        if slot is None:
            db.add(ParkingSlot(code=code, location=location, slot_type=slot_type, active=True))
        elif reset_demo:
            slot.location = location
            slot.slot_type = slot_type
            slot.active = True

    regional_dates = {
        holiday_date for calendar in HOLIDAY_CALENDARS.values() for holiday_date, _ in calendar
    }
    # Earlier seeds stored a few company-wide holidays without a region; the regional calendars
    # from the 2026 holiday list PDFs replace them.
    for legacy in db.scalars(
        select(Holiday).where(Holiday.region.is_(None), Holiday.holiday_date.in_(regional_dates))
    ).all():
        db.delete(legacy)
    db.flush()
    for region, calendar in HOLIDAY_CALENDARS.items():
        for holiday_date, name in calendar:
            holiday = db.scalar(
                select(Holiday).where(Holiday.holiday_date == holiday_date, Holiday.region == region)
            )
            if holiday is None:
                db.add(Holiday(holiday_date=holiday_date, name=name, category="PUBLIC", region=region))
            else:
                holiday.name = name
                holiday.category = "PUBLIC"



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
