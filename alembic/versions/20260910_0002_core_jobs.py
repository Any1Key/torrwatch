"""Phase 1 core domain and durable jobs.

Revision ID: 20260910_0002
Revises: 20260910_0001
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260910_0002"
down_revision = "20260910_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monitor_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("external_tracker_id", sa.String(255)),
        sa.Column("plugin_id", sa.String(128)),
        sa.Column("tracker_account_id", sa.Integer()),
        sa.Column("proxy_override_id", sa.Integer()),
        sa.Column("torrent_client_id", sa.Integer()),
        sa.Column("client_save_path", sa.Text()),
        sa.Column("category", sa.String(255)),
        sa.Column("tags_json", sa.Text()),
        sa.Column("initial_sync_mode", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("check_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_check_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("next_check_at", sa.DateTime(timezone=True)),
        sa.Column("last_update_at", sa.DateTime(timezone=True)),
        sa.Column("current_release_id", sa.Integer()),
        sa.Column("current_infohash_v1", sa.String(64)),
        sa.Column("current_infohash_v2", sa.String(128)),
        sa.Column("current_status", sa.String(32), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_monitor_items_schedule", "monitor_items", ["enabled", "paused", "next_check_at"]
    )
    op.create_index("ix_monitor_items_next_check_at", "monitor_items", ["next_check_at"])
    op.create_table(
        "release_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("monitor_id", sa.Integer(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version_key", sa.String(512)),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column("infohash_v1", sa.String(64)),
        sa.Column("infohash_v2", sa.String(128)),
        sa.Column("torrent_sha256", sa.String(64)),
        sa.Column("torrent_name", sa.String(1024)),
        sa.Column("total_size", sa.Integer()),
        sa.Column("file_count", sa.Integer()),
        sa.Column("file_path", sa.Text()),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(512)),
        sa.ForeignKeyConstraint(["monitor_id"], ["monitor_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("monitor_id", "idempotency_key", name="uq_release_monitor_idempotency"),
    )
    op.create_index("ix_release_versions_monitor_id", "release_versions", ["monitor_id"])
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("monitor_id", sa.Integer()),
        sa.Column("tracker_plugin_id", sa.String(128)),
        sa.Column("level", sa.String(32), nullable=False),
        sa.Column("event_code", sa.String(128), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["monitor_id"], ["monitor_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_events_monitor_created", "events", ["monitor_id", "created_at"])
    op.create_index("ix_events_monitor_id", "events", ["monitor_id"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_type", sa.String(64), nullable=False),
        sa.Column("monitor_id", sa.Integer()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("active_key", sa.String(255)),
        sa.Column("worker_id", sa.String(255)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.ForeignKeyConstraint(["monitor_id"], ["monitor_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_key", name="uq_jobs_active_key"),
    )
    op.create_index("ix_jobs_claimable", "jobs", ["status", "next_attempt_at"])
    op.create_index("ix_jobs_monitor_id", "jobs", ["monitor_id"])
    op.create_index("ix_jobs_lease_expires_at", "jobs", ["lease_expires_at"])
    op.create_index("ix_jobs_next_attempt_at", "jobs", ["next_attempt_at"])
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    op.drop_table("jobs")
    op.drop_table("events")
    op.drop_table("release_versions")
    op.drop_table("monitor_items")
