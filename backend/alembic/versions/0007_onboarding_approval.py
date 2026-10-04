"""Add onboarding approval and account activation lifecycle.

Revision ID: 0007_onboarding_approval
Revises: 0006_onboarding_domain
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_onboarding_approval"
down_revision = "0006_onboarding_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "onboarding_requests",
        sa.Column(
            "reviewed_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("onboarding_requests", sa.Column("review_comment", sa.Text(), nullable=True))
    op.add_column(
        "onboarding_requests", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "onboarding_requests",
        sa.Column(
            "activated_employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "onboarding_requests",
        sa.Column(
            "activated_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_unique_constraint(
        "uq_onboarding_requests_activated_employee", "onboarding_requests", ["activated_employee_id"]
    )
    op.create_unique_constraint(
        "uq_onboarding_requests_activated_user", "onboarding_requests", ["activated_user_id"]
    )
    op.create_index(
        "ix_onboarding_requests_review_status",
        "onboarding_requests",
        ["reviewed_by_user_id", "status"],
    )
    op.execute(
        "UPDATE onboarding_requests SET status = 'PENDING_APPROVAL' WHERE status = 'IN_PROGRESS'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE onboarding_requests SET status = 'IN_PROGRESS' WHERE status IN ('PENDING_APPROVAL', 'ACTIVE', 'REJECTED')"
    )
    op.drop_index("ix_onboarding_requests_review_status", table_name="onboarding_requests")
    op.drop_constraint(
        "uq_onboarding_requests_activated_user", "onboarding_requests", type_="unique"
    )
    op.drop_constraint(
        "uq_onboarding_requests_activated_employee", "onboarding_requests", type_="unique"
    )
    op.drop_column("onboarding_requests", "activated_user_id")
    op.drop_column("onboarding_requests", "activated_employee_id")
    op.drop_column("onboarding_requests", "reviewed_at")
    op.drop_column("onboarding_requests", "review_comment")
    op.drop_column("onboarding_requests", "reviewed_by_user_id")
