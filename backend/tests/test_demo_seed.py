from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.security import verify_password
from app.infrastructure.database.base import Base
from app.infrastructure.database.models import (
    ConversationSession,
    Employee,
    LeaveBalance,
    LeaveRequest,
    ParkingReservation,
    ParkingReservationEvent,
    ParkingSlot,
    ParkingWaitlistEntry,
    PendingAction,
    User,
    Vehicle,
)
from app.seed import seed_database


def test_demo_reset_is_repeatable_and_restores_baseline():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)

    with TestingSession() as db:
        seed_database(db, reset_demo=True)
        db.commit()

        employee = db.scalar(select(Employee).where(Employee.employee_code == "I26004"))
        employee_user = db.scalar(select(User).where(User.username == "employee"))
        casual = db.scalar(
            select(LeaveBalance).where(
                LeaveBalance.employee_id == employee.id,
                LeaveBalance.leave_type == "CASUAL",
            )
        )
        parking_admin = db.scalar(select(User).where(User.username == "parkingadmin"))

        assert verify_password("Advik!Desk-2026", employee_user.password_hash)
        assert casual.total_days == Decimal("6")
        assert casual.used_days == Decimal("2")
        assert db.scalar(select(func.count()).select_from(LeaveRequest)) == 0
        assert parking_admin.role == "PARKING_ADMIN"
        assert verify_password("Dhaswanth!Desk-2026", parking_admin.password_hash)
        assert db.scalar(select(Vehicle.registration_number).where(Vehicle.employee_id == employee_user.employee_id)) == "TN01AR1001"
        assert db.scalar(select(func.count()).select_from(Vehicle)) == 1
        assert db.scalar(select(func.count()).select_from(ParkingSlot)) == 5
        assert db.scalar(select(func.count()).select_from(ParkingReservation)) == 0
        assert db.scalar(select(func.count()).select_from(ParkingReservationEvent)) == 0

        conversation = ConversationSession(
            id="demo-reset-test",
            user_id=employee_user.id,
            state_json='{"messages": []}',
        )
        db.add(conversation)
        db.flush()
        db.add(
            PendingAction(
                session_id=conversation.id,
                user_id=employee_user.id,
                action_type="APPLY_LEAVE",
                arguments_json="{}",
                summary="Temporary demo action",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        casual.used_days = Decimal("5")
        employee_user.password_hash = "changed-for-test"
        legacy_employee = Employee(
            employee_code="E1001",
            name="Asha Rao",
            email="asha@example.test",
            designation="Software Engineer",
            department="Engineering",
            manager_name="Karthik Iyer",
            location="Chennai",
            employment_type="Permanent",
            joining_date=date(2022, 5, 2),
        )
        db.add(legacy_employee)
        db.flush()
        db.add(
            LeaveRequest(
                employee_id=legacy_employee.id,
                manager_employee_id=employee.manager_employee_id,
                leave_type="CASUAL",
                start_date=date.today() + timedelta(days=10),
                end_date=date.today() + timedelta(days=10),
                working_days=Decimal("1"),
                status="PENDING",
            )
        )
        db.commit()

        seed_database(db, reset_demo=True)
        db.commit()

        assert db.scalar(select(func.count()).select_from(ConversationSession)) == 0
        assert db.scalar(select(func.count()).select_from(PendingAction)) == 0
        assert db.scalar(
            select(func.count()).select_from(LeaveRequest).where(
                LeaveRequest.employee_id == employee.id
            )
        ) == 0
        assert db.scalar(select(Employee).where(Employee.employee_code == "E1001")) is None
        assert db.scalar(select(func.count()).select_from(ParkingReservation)) == 0
        assert db.scalar(select(func.count()).select_from(ParkingReservationEvent)) == 0
        assert db.scalar(select(func.count()).select_from(ParkingWaitlistEntry)) == 0
        assert db.scalar(select(func.count()).select_from(ParkingSlot)) == 5
        assert db.scalar(select(Vehicle.registration_number).where(Vehicle.employee_id == employee_user.employee_id)) == "TN01AR1001"
        assert db.scalar(select(func.count()).select_from(Vehicle)) == 1
        assert casual.used_days == Decimal("2")
        assert verify_password("Advik!Desk-2026", employee_user.password_hash)


def test_seed_loads_both_regional_2026_holiday_calendars_idempotently():
    from app.infrastructure.database.models import Holiday

    engine = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    with TestingSession() as db:
        db.add(Holiday(holiday_date=date(2026, 10, 2), name="Gandhi Jayanti", category="PUBLIC"))  # legacy, no region
        db.commit()
        seed_database(db, reset_demo=True)
        seed_database(db, reset_demo=True)
        db.commit()

        rows = db.scalars(select(Holiday)).all()
        by_region = {}
        for row in rows:
            by_region.setdefault(row.region, {})[row.holiday_date] = row.name

    assert None not in by_region
    assert len(by_region["TAMIL_NADU"]) == 12 and len(by_region["KARNATAKA"]) == 12
    assert by_region["TAMIL_NADU"][date(2026, 10, 19)] == "Ayudha Poojai"
    assert by_region["TAMIL_NADU"][date(2026, 11, 8)] == "Diwali"
    assert by_region["KARNATAKA"][date(2026, 11, 10)] == "Diwali"
