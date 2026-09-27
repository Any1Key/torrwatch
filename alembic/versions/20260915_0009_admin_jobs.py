"""Persist non-secret administrative operation snapshots in the existing queue."""

import sqlalchemy as sa

from alembic import op

revision = "20260915_0009"
down_revision = "20260914_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"))
    op.add_column(
        "tracker_sessions",
        sa.Column("auth_status", sa.String(32), nullable=False, server_default="UNVERIFIED"),
    )
    op.add_column("tracker_sessions", sa.Column("imported_at", sa.DateTime()))


def downgrade() -> None:
    op.drop_column("tracker_sessions", "imported_at")
    op.drop_column("tracker_sessions", "auth_status")
    op.drop_column("jobs", "payload_json")
