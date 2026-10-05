"""Accounts activated by onboarding must change their temporary password on first login.

Revision ID: 0012_first_login_password_change
Revises: 0011_two_vehicles_per_employee
"""
from alembic import op
import sqlalchemy as sa


revision = "0012_first_login_password_change"
down_revision = "0011_two_vehicles_per_employee"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
