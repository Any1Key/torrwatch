"""Phase 3 namespaced plugin state."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260910_0004"
down_revision = "20260910_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plugin_id", sa.String(128), nullable=False),
        sa.Column("scope", sa.String(255), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plugin_id", "scope", "key", name="uq_plugin_states_namespace_key"),
    )


def downgrade() -> None:
    op.drop_table("plugin_states")
