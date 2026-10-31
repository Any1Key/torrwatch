"""Add proxy assignment to tracker sessions."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261101_0013"
down_revision = "20261029_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tracker_sessions",
        sa.Column("proxy_profile_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tracker_sessions", "proxy_profile_id")
