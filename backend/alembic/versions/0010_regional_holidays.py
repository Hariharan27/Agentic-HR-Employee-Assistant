"""Holidays per regional calendar (Tamil Nadu, Karnataka).

Revision ID: 0010_regional_holidays
Revises: 0009_policy_aligned_leave_schema
"""
from alembic import op
import sqlalchemy as sa


revision = "0010_regional_holidays"
down_revision = "0009_policy_aligned_leave_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("holidays", sa.Column("region", sa.String(40), nullable=True))
    # 0002 created an unnamed UNIQUE(holiday_date). Look its name up instead of assuming
    # PostgreSQL's default (holidays_holiday_date_key), so startup migrations cannot fail on it.
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints("holidays"):
        if constraint.get("column_names") == ["holiday_date"] and constraint.get("name"):
            op.drop_constraint(constraint["name"], "holidays", type_="unique")
    for index in inspector.get_indexes("holidays"):
        if index.get("unique") and index.get("column_names") == ["holiday_date"] and index.get("name"):
            op.drop_index(index["name"], table_name="holidays")
    op.create_unique_constraint("uq_holidays_date_region", "holidays", ["holiday_date", "region"])


def downgrade() -> None:
    op.drop_constraint("uq_holidays_date_region", "holidays", type_="unique")
    op.execute("DELETE FROM holidays WHERE region IS NOT NULL AND id NOT IN (SELECT MIN(id) FROM holidays GROUP BY holiday_date)")
    op.create_unique_constraint("holidays_holiday_date_key", "holidays", ["holiday_date"])
    op.drop_column("holidays", "region")
