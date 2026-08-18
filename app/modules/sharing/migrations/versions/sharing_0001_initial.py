"""Sharing schema baseline.

Revision ID: sharing_0001
Revises:
"""

import sqlalchemy as sa

from alembic import op

revision = "sharing_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "share_links",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("module_id", sa.String(length=63), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("selection_mode", sa.String(length=16), nullable=False),
        sa.Column("selector", sa.JSON(), nullable=False),
        sa.Column("is_public", sa.Boolean(), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("allow_download", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("access_count", sa.Integer(), nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_share_links_expires_at", "share_links", ["expires_at"])
    op.create_index("ix_share_links_module_id", "share_links", ["module_id"])
    op.create_index("ix_share_links_status", "share_links", ["status"])


def downgrade() -> None:
    op.drop_table("share_links")
