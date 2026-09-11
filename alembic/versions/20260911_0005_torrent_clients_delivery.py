"""Phase 6 torrent clients and durable delivery queue.

Revision ID: 20260911_0005
Revises: 20260910_0004
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260911_0005"
down_revision = "20260910_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "torrent_clients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("username", sa.String(255)),
        sa.Column("encrypted_password", sa.Text()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("default_save_path", sa.Text()),
        sa.Column("default_category", sa.String(255)),
        sa.Column("default_tags_json", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", name="uq_torrent_clients_name"),
    )
    op.create_table(
        "delivery_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("release_id", sa.Integer(), sa.ForeignKey("release_versions.id"), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("torrent_clients.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("active_key", sa.String(255)),
        sa.Column("worker_id", sa.String(255)),
        sa.Column("lease_expires_at", sa.DateTime()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("last_error", sa.Text()),
        sa.UniqueConstraint("active_key", name="uq_delivery_jobs_active_key"),
    )
    op.create_index("ix_delivery_jobs_claimable", "delivery_jobs", ["status", "next_attempt_at"])
    op.create_index("ix_delivery_jobs_release_id", "delivery_jobs", ["release_id"])
    op.create_index("ix_delivery_jobs_client_id", "delivery_jobs", ["client_id"])
    op.create_index("ix_delivery_jobs_lease_expires_at", "delivery_jobs", ["lease_expires_at"])
    op.create_index("ix_delivery_jobs_next_attempt_at", "delivery_jobs", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_table("delivery_jobs")
    op.drop_table("torrent_clients")
