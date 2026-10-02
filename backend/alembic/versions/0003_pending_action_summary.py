"""Complete the reusable pending-action persistence model.

Revision ID: 0003_pending_action_summary
Revises: 0002_leave_domain
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_pending_action_summary"
down_revision = "0002_leave_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pending_actions", sa.Column("summary", sa.String(500), nullable=True))
    op.execute("UPDATE pending_actions SET summary = action_type WHERE summary IS NULL")
    with op.batch_alter_table("pending_actions") as batch_op:
        batch_op.alter_column("summary", existing_type=sa.String(500), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("pending_actions") as batch_op:
        batch_op.drop_column("summary")
