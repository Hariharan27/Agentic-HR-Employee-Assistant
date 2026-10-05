"""Parking slots are for one vehicle type: cars park in car slots, motorcycles in bike slots.

Revision ID: 0013_slot_vehicle_types
Revises: 0012_first_login_password_change
"""
from alembic import op
import sqlalchemy as sa


revision = "0013_slot_vehicle_types"
down_revision = "0012_first_login_password_change"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "parking_slots",
        sa.Column("vehicle_type", sa.String(length=16), nullable=False, server_default="CAR"),
    )
    op.create_check_constraint(
        "ck_parking_slots_vehicle_type", "parking_slots", "vehicle_type IN ('CAR', 'MOTORCYCLE')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_parking_slots_vehicle_type", "parking_slots", type_="check")
    op.drop_column("parking_slots", "vehicle_type")
