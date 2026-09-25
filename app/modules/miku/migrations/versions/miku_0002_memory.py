"""Add durable MIKU memory tables.

Revision ID: miku_0002
Revises: miku_0001
"""

import sqlalchemy as sa

from alembic import op

revision = "miku_0002"
down_revision = "miku_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "miku_profile_memory",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("memory_key", sa.String(length=64), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_miku_profile_memory_user_id", "miku_profile_memory", ["user_id"])
    op.create_index(
        "ix_miku_profile_memory_user_key",
        "miku_profile_memory",
        ["user_id", "memory_key"],
    )
    op.create_index(
        "ix_miku_profile_memory_user_updated",
        "miku_profile_memory",
        ["user_id", "updated_at"],
    )

    op.create_table(
        "miku_episode_memory",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(length=160), nullable=True),
        sa.Column("tags_json", sa.JSON(), nullable=False),
        sa.Column("source_module_id", sa.String(length=63), nullable=True),
        sa.Column("source_item_id", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_miku_episode_memory_user_id", "miku_episode_memory", ["user_id"])
    op.create_index(
        "ix_miku_episode_memory_user_created",
        "miku_episode_memory",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_miku_episode_memory_user_occurred",
        "miku_episode_memory",
        ["user_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_table("miku_episode_memory")
    op.drop_table("miku_profile_memory")
