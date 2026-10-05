"""Allow up to two registered vehicles per employee.

The one-vehicle rule was a unique constraint on vehicles.employee_id; the two-vehicle limit is
enforced by ParkingService. Registration numbers stay unique across all employees.

Revision ID: 0011_two_vehicles_per_employee
Revises: 0010_regional_holidays
"""
from alembic import op
import sqlalchemy as sa


revision = "0011_two_vehicles_per_employee"
down_revision = "0010_regional_holidays"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints("vehicles"):
        if constraint.get("column_names") == ["employee_id"] and constraint.get("name"):
            op.drop_constraint(constraint["name"], "vehicles", type_="unique")
    for index in inspector.get_indexes("vehicles"):
        if index.get("unique") and index.get("column_names") == ["employee_id"] and index.get("name"):
            op.drop_index(index["name"], table_name="vehicles")


def downgrade() -> None:
    # Keep only each employee's oldest vehicle before restoring the one-vehicle rule.
    op.execute(
        "DELETE FROM vehicles v USING vehicles o "
        "WHERE v.employee_id = o.employee_id AND v.id > o.id "
        "AND NOT EXISTS (SELECT 1 FROM parking_reservations r WHERE r.vehicle_id = v.id)"
    )
    op.create_unique_constraint("uq_vehicles_employee_id", "vehicles", ["employee_id"])
