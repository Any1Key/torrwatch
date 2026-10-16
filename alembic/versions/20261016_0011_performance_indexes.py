"""Add indexes for release history and global event feeds."""

from __future__ import annotations

from alembic import op

revision = "20261016_0011"
down_revision = "20260927_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_release_versions_monitor_detected",
        "release_versions",
        ["monitor_id", "detected_at"],
    )
    op.create_index("ix_events_created", "events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_events_created", table_name="events")
    op.drop_index("ix_release_versions_monitor_detected", table_name="release_versions")
