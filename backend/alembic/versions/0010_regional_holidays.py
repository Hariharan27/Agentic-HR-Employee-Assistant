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
    # 0002 created an unnamed UNIQUE(holiday_date); PostgreSQL named it holidays_holiday_date_key.
    op.drop_constraint("holidays_holiday_date_key", "holidays", type_="unique")
    op.create_unique_constraint("uq_holidays_date_region", "holidays", ["holiday_date", "region"])


def downgrade() -> None:
    op.drop_constraint("uq_holidays_date_region", "holidays", type_="unique")
    op.execute("DELETE FROM holidays WHERE region IS NOT NULL AND id NOT IN (SELECT MIN(id) FROM holidays GROUP BY holiday_date)")
    op.create_unique_constraint("holidays_holiday_date_key", "holidays", ["holiday_date"])
    op.drop_column("holidays", "region")
