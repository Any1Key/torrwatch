"""Allow monitors to be removed from active scheduling without losing history.

Revision ID: 20260914_0008
Revises: 20260914_0007
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260914_0008"
down_revision = "20260914_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("monitor_items", sa.Column("deleted_at", sa.DateTime()))
    op.create_index("ix_monitor_items_deleted_at", "monitor_items", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_monitor_items_deleted_at", table_name="monitor_items")
    op.drop_column("monitor_items", "deleted_at")
