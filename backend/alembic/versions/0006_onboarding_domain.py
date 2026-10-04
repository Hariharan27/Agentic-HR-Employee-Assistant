"""Add deterministic onboarding requests and provisioning tasks.

Revision ID: 0006_onboarding_domain
Revises: 0005_leave_event_backfill
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_onboarding_domain"
down_revision = "0005_leave_event_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onboarding_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("employee_name", sa.String(160), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("designation", sa.String(120), nullable=False),
        sa.Column("department", sa.String(120), nullable=False),
        sa.Column(
            "manager_employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("manager_name", sa.String(160), nullable=False),
        sa.Column("joining_date", sa.Date(), nullable=False),
        sa.Column("location", sa.String(120), nullable=False),
        sa.Column("employment_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="IN_PROGRESS"),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_onboarding_requests_email_status", "onboarding_requests", ["email", "status"]
    )
    op.create_index(
        "ix_onboarding_requests_manager_status",
        "onboarding_requests",
        ["manager_employee_id", "status"],
    )
    op.create_index(
        "ix_onboarding_requests_creator", "onboarding_requests", ["created_by_user_id"]
    )

    op.create_table(
        "onboarding_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "onboarding_request_id",
            sa.Integer(),
            sa.ForeignKey("onboarding_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_type", sa.String(48), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "onboarding_request_id", "task_type", name="uq_onboarding_tasks_request_type"
        ),
    )
    op.create_index(
        "ix_onboarding_tasks_request_status",
        "onboarding_tasks",
        ["onboarding_request_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("onboarding_tasks")
    op.drop_table("onboarding_requests")
