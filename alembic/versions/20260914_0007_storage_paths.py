"""Named administrator-configured storage paths.

Revision ID: 20260914_0007
Revises: 20260912_0006
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260914_0007"
down_revision = "20260912_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storage_paths",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", name="uq_storage_paths_name"),
    )
    op.create_index("ix_storage_paths_enabled", "storage_paths", ["enabled"])


def downgrade() -> None:
    op.drop_index("ix_storage_paths_enabled", table_name="storage_paths")
    op.drop_table("storage_paths")
