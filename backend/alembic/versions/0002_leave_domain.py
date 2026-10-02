"""Phase 1A deterministic leave domain.

Revision ID: 0002_leave_domain
Revises: 0001_foundation
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_leave_domain"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "leave_balances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("leave_type", sa.String(32), nullable=False),
        sa.Column("total_days", sa.Numeric(6, 2), nullable=False),
        sa.Column("used_days", sa.Numeric(6, 2), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("employee_id", "leave_type", name="uq_leave_balances_employee_type"),
        sa.CheckConstraint("total_days >= 0", name="ck_leave_balances_total_nonnegative"),
        sa.CheckConstraint("used_days >= 0 AND used_days <= total_days", name="ck_leave_balances_used_valid"),
    )
    op.create_index("ix_leave_balances_employee_id", "leave_balances", ["employee_id"])
    op.create_table(
        "leave_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("leave_type", sa.String(32), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("working_days", sa.Numeric(6, 2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("end_date >= start_date", name="ck_leave_requests_date_order"),
        sa.CheckConstraint("working_days > 0", name="ck_leave_requests_positive_days"),
    )
    op.create_index("ix_leave_requests_employee_status", "leave_requests", ["employee_id", "status"])
    op.create_index("ix_leave_requests_employee_dates", "leave_requests", ["employee_id", "start_date", "end_date"])
    op.create_table(
        "holidays",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("holiday_date", sa.Date(), nullable=False, unique=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
    )
    op.create_index("ix_holidays_date", "holidays", ["holiday_date"])


def downgrade() -> None:
    op.drop_table("holidays")
    op.drop_table("leave_requests")
    op.drop_table("leave_balances")

