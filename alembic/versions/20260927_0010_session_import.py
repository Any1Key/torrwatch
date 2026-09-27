"""Add one-time browser session import pairings."""

import sqlalchemy as sa

from alembic import op

revision = "20260927_0010"
down_revision = "20260915_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_import_pairings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("token_hash", name="uq_session_import_pairings_token_hash"),
    )
    op.create_index(
        "ix_session_import_pairings_token_hash",
        "session_import_pairings",
        ["token_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_session_import_pairings_token_hash", table_name="session_import_pairings")
    op.drop_table("session_import_pairings")
