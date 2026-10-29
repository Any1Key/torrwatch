"""Add per-channel monitor tag subscriptions.

Revision ID: 20261029_0012
Revises: 20261016_0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261029_0012"
down_revision = "20261016_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_channels",
        sa.Column("monitor_tags_json", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("notification_channels", "monitor_tags_json")
