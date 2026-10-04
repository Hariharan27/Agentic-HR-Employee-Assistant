"""Add workplace parking persistence foundation.

Revision ID: 0008_parking_domain
Revises: 0007_onboarding_approval
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_parking_domain"
down_revision = "0007_onboarding_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vehicles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("registration_number", sa.String(32), nullable=False),
        sa.Column("vehicle_type", sa.String(24), nullable=False),
        sa.Column("make_model", sa.String(120), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("vehicle_type IN ('CAR', 'MOTORCYCLE')", name="ck_vehicles_type"),
        sa.UniqueConstraint("employee_id", name="uq_vehicles_employee_id"),
        sa.UniqueConstraint("registration_number", name="uq_vehicles_registration_number"),
    )
    op.create_index("ix_vehicles_employee_active", "vehicles", ["employee_id", "active"])

    op.create_table(
        "parking_slots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(24), nullable=False),
        sa.Column("location", sa.String(120), nullable=False),
        sa.Column("slot_type", sa.String(24), nullable=False, server_default="REGULAR"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "slot_type IN ('REGULAR', 'ACCESSIBLE')", name="ck_parking_slots_type"
        ),
        sa.UniqueConstraint("code", name="uq_parking_slots_code"),
    )
    op.create_index(
        "ix_parking_slots_active_type", "parking_slots", ["active", "slot_type"]
    )

    op.create_table(
        "parking_reservations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "vehicle_id",
            sa.Integer(),
            sa.ForeignKey("vehicles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "slot_id",
            sa.Integer(),
            sa.ForeignKey("parking_slots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reservation_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="RESERVED"),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("no_show_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('RESERVED', 'CHECKED_IN', 'COMPLETED', 'CANCELLED', 'NO_SHOW')",
            name="ck_parking_reservations_status",
        ),
    )
    op.create_index(
        "uq_parking_reservations_employee_date_active",
        "parking_reservations",
        ["employee_id", "reservation_date"],
        unique=True,
        postgresql_where=sa.text("status IN ('RESERVED', 'CHECKED_IN')"),
    )
    op.create_index(
        "uq_parking_reservations_slot_date_active",
        "parking_reservations",
        ["slot_id", "reservation_date"],
        unique=True,
        postgresql_where=sa.text("status IN ('RESERVED', 'CHECKED_IN')"),
    )
    op.create_index(
        "ix_parking_reservations_employee_status",
        "parking_reservations",
        ["employee_id", "status"],
    )
    op.create_index(
        "ix_parking_reservations_date_status",
        "parking_reservations",
        ["reservation_date", "status"],
    )

    op.create_table(
        "parking_reservation_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reservation_id",
            sa.Integer(),
            sa.ForeignKey("parking_reservations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("from_status", sa.String(24), nullable=True),
        sa.Column("to_status", sa.String(24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status IN "
            "('RESERVED', 'CHECKED_IN', 'COMPLETED', 'CANCELLED', 'NO_SHOW')",
            name="ck_parking_reservation_events_from_status",
        ),
        sa.CheckConstraint(
            "to_status IN ('RESERVED', 'CHECKED_IN', 'COMPLETED', 'CANCELLED', 'NO_SHOW')",
            name="ck_parking_reservation_events_to_status",
        ),
    )
    op.create_index(
        "ix_parking_reservation_events_reservation_created",
        "parking_reservation_events",
        ["reservation_id", "created_at"],
    )

    op.create_table(
        "parking_waitlist",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "vehicle_id",
            sa.Integer(),
            sa.ForeignKey("vehicles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("requested_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="WAITING"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('WAITING', 'ALLOCATED', 'CANCELLED')",
            name="ck_parking_waitlist_status",
        ),
    )
    op.create_index(
        "uq_parking_waitlist_employee_date_waiting",
        "parking_waitlist",
        ["employee_id", "requested_date"],
        unique=True,
        postgresql_where=sa.text("status = 'WAITING'"),
    )
    op.create_index(
        "ix_parking_waitlist_date_status",
        "parking_waitlist",
        ["requested_date", "status"],
    )


def downgrade() -> None:
    op.drop_table("parking_waitlist")
    op.drop_table("parking_reservation_events")
    op.drop_table("parking_reservations")
    op.drop_table("parking_slots")
    op.drop_table("vehicles")
