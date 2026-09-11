"""Phase 8 notification channels and durable notification jobs.

Revision ID: 20260912_0006
Revises: 20260911_0005
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260912_0006"
down_revision = "20260911_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("encrypted_config", sa.Text(), nullable=False),
        sa.Column("event_types_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", name="uq_notification_channels_name"),
    )
    op.create_table(
        "notification_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "channel_id", sa.Integer(), sa.ForeignKey("notification_channels.id"), nullable=False
        ),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id")),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("active_key", sa.String(255)),
        sa.Column("worker_id", sa.String(255)),
        sa.Column("lease_expires_at", sa.DateTime()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("last_error", sa.Text()),
        sa.UniqueConstraint("active_key", name="uq_notification_jobs_active_key"),
    )
    op.create_index(
        "ix_notification_jobs_claimable", "notification_jobs", ["status", "next_attempt_at"]
    )


def downgrade() -> None:
    op.drop_table("notification_jobs")
    op.drop_table("notification_channels")
