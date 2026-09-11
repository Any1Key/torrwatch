"""Phase 2 transport security persistence."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260910_0003"
down_revision = "20260910_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "proxy_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("host", sa.String(255)),
        sa.Column("port", sa.Integer()),
        sa.Column("username", sa.String(255)),
        sa.Column("encrypted_password", sa.Text()),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("fallback_mode", sa.String(16), nullable=False),
        sa.Column("fallback_proxy_id", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["fallback_proxy_id"], ["proxy_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_proxy_profiles_name"),
    )
    op.create_table(
        "tracker_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("namespace", sa.String(255), nullable=False),
        sa.Column("encrypted_cookies", sa.Text()),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("last_successful_auth_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("namespace", name="uq_tracker_sessions_namespace"),
    )


def downgrade() -> None:
    op.drop_table("tracker_sessions")
    op.drop_table("proxy_profiles")
