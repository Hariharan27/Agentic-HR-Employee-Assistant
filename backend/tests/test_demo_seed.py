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
    LeaveRequestEvent,
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
        request = db.scalar(select(LeaveRequest).where(LeaveRequest.employee_id == employee.id))
        parking_admin = db.scalar(select(User).where(User.username == "parkingadmin"))
        vehicle = db.scalar(select(Vehicle).where(Vehicle.employee_id == employee.id))

        assert verify_password("employee123", employee_user.password_hash)
        assert casual.total_days == Decimal("12")
        assert casual.used_days == Decimal("8")
        assert request.status == "PENDING"
        assert request.manager_employee_id == employee.manager_employee_id
        assert parking_admin.role == "PARKING_ADMIN"
        assert verify_password("parkingadmin123", parking_admin.password_hash)
        assert vehicle.registration_number == "TN01AR1001"
        assert db.scalar(select(func.count()).select_from(ParkingSlot)) == 5
        assert db.scalar(select(func.count()).select_from(ParkingReservation)) == 1
        assert db.scalar(select(func.count()).select_from(ParkingReservationEvent)) == 1

        open_slot = db.scalar(select(ParkingSlot).where(ParkingSlot.code == "B-22"))
        extra_reservation = ParkingReservation(
            employee_id=employee.id,
            vehicle_id=vehicle.id,
            slot_id=open_slot.id,
            reservation_date=date.today() + timedelta(days=30),
            status="RESERVED",
        )
        db.add(extra_reservation)
        db.flush()
        db.add_all(
            [
                ParkingReservationEvent(
                    reservation_id=extra_reservation.id,
                    actor_user_id=employee_user.id,
                    from_status=None,
                    to_status="RESERVED",
                    reason="Temporary demo booking",
                ),
                ParkingWaitlistEntry(
                    employee_id=employee.id,
                    vehicle_id=vehicle.id,
                    requested_date=date.today() + timedelta(days=31),
                    status="WAITING",
                ),
            ]
        )

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
        casual.used_days = Decimal("11")
        employee_user.password_hash = "changed-for-test"
        db.commit()

        seed_database(db, reset_demo=True)
        db.commit()

        assert db.scalar(select(func.count()).select_from(ConversationSession)) == 0
        assert db.scalar(select(func.count()).select_from(PendingAction)) == 0
        assert db.scalar(
            select(func.count()).select_from(LeaveRequest).where(
                LeaveRequest.employee_id == employee.id
            )
        ) == 1
        assert db.scalar(select(func.count()).select_from(LeaveRequestEvent)) == 1
        assert db.scalar(select(func.count()).select_from(ParkingReservation)) == 1
        assert db.scalar(select(func.count()).select_from(ParkingReservationEvent)) == 1
        assert db.scalar(select(func.count()).select_from(ParkingWaitlistEntry)) == 0
        assert db.scalar(select(func.count()).select_from(ParkingSlot)) == 5
        assert casual.used_days == Decimal("8")
        assert verify_password("employee123", employee_user.password_hash)
