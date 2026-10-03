"""Backfill audit snapshots for leave requests created before Phase 1E.

Revision ID: 0005_leave_event_backfill
Revises: 0004_leave_approval_lifecycle
"""
from alembic import op

revision = "0005_leave_event_backfill"
down_revision = "0004_leave_approval_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO leave_request_events (
            leave_request_id, actor_user_id, from_status, to_status, comment, created_at
        )
        SELECT request.id, actor.id, NULL, request.status,
               'Existing request imported into lifecycle audit', request.created_at
        FROM leave_requests AS request
        JOIN users AS actor ON actor.employee_id = request.employee_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM leave_request_events AS event
            WHERE event.leave_request_id = request.id
        )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM leave_request_events
        WHERE comment = 'Existing request imported into lifecycle audit'
          AND from_status IS NULL
        """
    )
