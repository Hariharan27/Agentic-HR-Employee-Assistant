"""Align leave balances with policy entitlement and carry forward rules.

Revision ID: 0009_policy_aligned_leave_schema
Revises: 0008_parking_domain
"""
from alembic import op
import sqlalchemy as sa


revision = "0009_policy_aligned_leave_schema"
down_revision = "0008_parking_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "leave_balances",
        sa.Column(
            "carry_forward_limit_days",
            sa.Numeric(6, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute("UPDATE leave_balances SET leave_type = 'EARNED' WHERE leave_type = 'PRIVILEGE'")
    op.execute("UPDATE leave_requests SET leave_type = 'EARNED' WHERE leave_type = 'PRIVILEGE'")
    op.execute("UPDATE leave_balances SET carry_forward_limit_days = 8 WHERE leave_type = 'EARNED'")


def downgrade() -> None:
    op.execute("UPDATE leave_balances SET leave_type = 'PRIVILEGE' WHERE leave_type = 'EARNED'")
    op.execute("UPDATE leave_requests SET leave_type = 'PRIVILEGE' WHERE leave_type = 'EARNED'")
    op.drop_column("leave_balances", "carry_forward_limit_days")
