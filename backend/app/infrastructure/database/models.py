from datetime import date, datetime

from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    designation: Mapped[str] = mapped_column(String(120))
    department: Mapped[str] = mapped_column(String(120))
    manager_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    manager_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    location: Mapped[str] = mapped_column(String(120))
    employment_type: Mapped[str] = mapped_column(String(40))
    joining_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_employees_email", "email"),
        Index("ix_employees_manager_employee_id", "manager_employee_id"),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(24))
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="RESTRICT"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_users_username", "username"),)


class ConversationSession(Base):
    __tablename__ = "conversation_sessions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (Index("ix_conversation_sessions_user_id", "user_id"),)


class PendingAction(Base):
    """Shared confirmation record. Domain workflows start using it in Phase 1."""

    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("conversation_sessions.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    action_type: Mapped[str] = mapped_column(String(64))
    arguments_json: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(String(500))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("session_id", name="uq_pending_actions_session"),
        Index("ix_pending_actions_user_id", "user_id"),
        Index("ix_pending_actions_expires_at", "expires_at"),
    )


class LeaveBalance(Base):
    __tablename__ = "leave_balances"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    leave_type: Mapped[str] = mapped_column(String(32))
    total_days: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    used_days: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("0"))
    carry_forward_limit_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), default=Decimal("0"), server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("employee_id", "leave_type", name="uq_leave_balances_employee_type"),
        CheckConstraint("total_days >= 0", name="ck_leave_balances_total_nonnegative"),
        CheckConstraint("used_days >= 0 AND used_days <= total_days", name="ck_leave_balances_used_valid"),
        Index("ix_leave_balances_employee_id", "employee_id"),
    )


class LeaveRequest(Base):
    __tablename__ = "leave_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    manager_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    leave_type: Mapped[str] = mapped_column(String(32))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    working_days: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    decided_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_leave_requests_date_order"),
        CheckConstraint("working_days > 0", name="ck_leave_requests_positive_days"),
        Index("ix_leave_requests_employee_status", "employee_id", "status"),
        Index("ix_leave_requests_manager_status", "manager_employee_id", "status"),
        Index("ix_leave_requests_employee_dates", "employee_id", "start_date", "end_date"),
    )


class LeaveRequestEvent(Base):
    __tablename__ = "leave_request_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    leave_request_id: Mapped[int] = mapped_column(
        ForeignKey("leave_requests.id", ondelete="CASCADE")
    )
    actor_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str] = mapped_column(String(24))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_leave_request_events_request_created", "leave_request_id", "created_at"),
    )


class Holiday(Base):
    __tablename__ = "holidays"

    id: Mapped[int] = mapped_column(primary_key=True)
    holiday_date: Mapped[date] = mapped_column(Date, unique=True)
    name: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(40), default="PUBLIC")

    __table_args__ = (Index("ix_holidays_date", "holiday_date"),)


class OnboardingRequest(Base):
    __tablename__ = "onboarding_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(255))
    designation: Mapped[str] = mapped_column(String(120))
    department: Mapped[str] = mapped_column(String(120))
    manager_employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT")
    )
    manager_name: Mapped[str] = mapped_column(String(160))
    joining_date: Mapped[date] = mapped_column(Date)
    location: Mapped[str] = mapped_column(String(120))
    employment_type: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24), default="PENDING_APPROVAL")
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    activated_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_onboarding_requests_email_status", "email", "status"),
        Index("ix_onboarding_requests_manager_status", "manager_employee_id", "status"),
        Index("ix_onboarding_requests_creator", "created_by_user_id"),
        Index("ix_onboarding_requests_review_status", "reviewed_by_user_id", "status"),
    )


class OnboardingTask(Base):
    __tablename__ = "onboarding_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    onboarding_request_id: Mapped[int] = mapped_column(
        ForeignKey("onboarding_requests.id", ondelete="CASCADE")
    )
    task_type: Mapped[str] = mapped_column(String(48))
    title: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "onboarding_request_id", "task_type", name="uq_onboarding_tasks_request_type"
        ),
        Index("ix_onboarding_tasks_request_status", "onboarding_request_id", "status"),
    )


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), unique=True
    )
    registration_number: Mapped[str] = mapped_column(String(32), unique=True)
    vehicle_type: Mapped[str] = mapped_column(String(24))
    make_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("vehicle_type IN ('CAR', 'MOTORCYCLE')", name="ck_vehicles_type"),
        Index("ix_vehicles_employee_active", "employee_id", "active"),
    )


class ParkingSlot(Base):
    __tablename__ = "parking_slots"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(24), unique=True)
    location: Mapped[str] = mapped_column(String(120))
    slot_type: Mapped[str] = mapped_column(String(24), default="REGULAR")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "slot_type IN ('REGULAR', 'ACCESSIBLE')", name="ck_parking_slots_type"
        ),
        Index("ix_parking_slots_active_type", "active", "slot_type"),
    )


class ParkingReservation(Base):
    __tablename__ = "parking_reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE")
    )
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id", ondelete="RESTRICT"))
    slot_id: Mapped[int] = mapped_column(
        ForeignKey("parking_slots.id", ondelete="RESTRICT")
    )
    reservation_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), default="RESERVED")
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    checked_in_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    no_show_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('RESERVED', 'CHECKED_IN', 'COMPLETED', 'CANCELLED', 'NO_SHOW')",
            name="ck_parking_reservations_status",
        ),
        Index(
            "uq_parking_reservations_employee_date_active",
            "employee_id",
            "reservation_date",
            unique=True,
            postgresql_where=text("status IN ('RESERVED', 'CHECKED_IN')"),
            sqlite_where=text("status IN ('RESERVED', 'CHECKED_IN')"),
        ),
        Index(
            "uq_parking_reservations_slot_date_active",
            "slot_id",
            "reservation_date",
            unique=True,
            postgresql_where=text("status IN ('RESERVED', 'CHECKED_IN')"),
            sqlite_where=text("status IN ('RESERVED', 'CHECKED_IN')"),
        ),
        Index("ix_parking_reservations_employee_status", "employee_id", "status"),
        Index("ix_parking_reservations_date_status", "reservation_date", "status"),
    )


class ParkingReservationEvent(Base):
    __tablename__ = "parking_reservation_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(
        ForeignKey("parking_reservations.id", ondelete="CASCADE")
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "from_status IS NULL OR from_status IN "
            "('RESERVED', 'CHECKED_IN', 'COMPLETED', 'CANCELLED', 'NO_SHOW')",
            name="ck_parking_reservation_events_from_status",
        ),
        CheckConstraint(
            "to_status IN ('RESERVED', 'CHECKED_IN', 'COMPLETED', 'CANCELLED', 'NO_SHOW')",
            name="ck_parking_reservation_events_to_status",
        ),
        Index(
            "ix_parking_reservation_events_reservation_created",
            "reservation_id",
            "created_at",
        ),
    )


class ParkingWaitlistEntry(Base):
    __tablename__ = "parking_waitlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE")
    )
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id", ondelete="RESTRICT"))
    requested_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), default="WAITING")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('WAITING', 'ALLOCATED', 'CANCELLED')",
            name="ck_parking_waitlist_status",
        ),
        Index(
            "uq_parking_waitlist_employee_date_waiting",
            "employee_id",
            "requested_date",
            unique=True,
            postgresql_where=text("status = 'WAITING'"),
            sqlite_where=text("status = 'WAITING'"),
        ),
        Index("ix_parking_waitlist_date_status", "requested_date", "status"),
    )
