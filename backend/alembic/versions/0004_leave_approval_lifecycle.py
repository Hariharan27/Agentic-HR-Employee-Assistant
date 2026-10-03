"""Phase 1E leave approval and lifecycle audit.

Revision ID: 0004_leave_approval_lifecycle
Revises: 0003_pending_action_summary
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_leave_approval_lifecycle"
down_revision = "0003_pending_action_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column(
            "manager_employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_employees_manager_employee_id", "employees", ["manager_employee_id"])
    op.execute(
        """
        UPDATE employees AS employee
        SET manager_employee_id = manager.id
        FROM employees AS manager
        WHERE employee.manager_name = manager.name
          AND employee.manager_employee_id IS NULL
        """
    )

    op.add_column(
        "leave_requests",
        sa.Column(
            "manager_employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "leave_requests",
        sa.Column(
            "decided_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("leave_requests", sa.Column("decision_comment", sa.Text(), nullable=True))
    op.add_column("leave_requests", sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        """
        UPDATE leave_requests AS request
        SET manager_employee_id = employee.manager_employee_id
        FROM employees AS employee
        WHERE request.employee_id = employee.id
          AND request.manager_employee_id IS NULL
        """
    )
    op.create_index(
        "ix_leave_requests_manager_status", "leave_requests", ["manager_employee_id", "status"]
    )

    op.create_table(
        "leave_request_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "leave_request_id",
            sa.Integer(),
            sa.ForeignKey("leave_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("from_status", sa.String(24), nullable=True),
        sa.Column("to_status", sa.String(24), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_leave_request_events_request_created",
        "leave_request_events",
        ["leave_request_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("leave_request_events")
    op.drop_index("ix_leave_requests_manager_status", table_name="leave_requests")
    op.drop_column("leave_requests", "decided_at")
    op.drop_column("leave_requests", "decision_comment")
    op.drop_column("leave_requests", "decided_by_user_id")
    op.drop_column("leave_requests", "manager_employee_id")
    op.drop_index("ix_employees_manager_employee_id", table_name="employees")
    op.drop_column("employees", "manager_employee_id")
