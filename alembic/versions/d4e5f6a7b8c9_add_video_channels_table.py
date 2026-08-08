"""Add video_channels table and channel_id foreign key

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-07 00:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create video_channels table
    op.create_table(
        "video_channels",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("custom_url", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("avatar_path", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_video_channels_name"), "video_channels", ["name"], unique=False)

    # 2. Populate video_channels from existing archived_videos if any
    op.execute("""
        INSERT INTO video_channels (id, name, avatar_path, created_at, updated_at)
        SELECT DISTINCT
            COALESCE(channel_id, 'UC_unknown'),
            COALESCE(channel_name, 'Unknown Channel'),
            channel_avatar_url,
            NOW(),
            NOW()
        FROM archived_videos
        WHERE channel_id IS NOT NULL AND channel_id != ''
        ON CONFLICT (id) DO NOTHING;
    """)

    # 3. Add FK constraint to archived_videos
    # Check if channel_id index exists
    try:
        op.create_index(
            op.f("ix_archived_videos_channel_id"), "archived_videos", ["channel_id"], unique=False
        )
    except Exception:
        pass

    try:
        op.create_foreign_key(
            "fk_archived_videos_video_channels",
            "archived_videos",
            "video_channels",
            ["channel_id"],
            ["id"],
            ondelete="SET NULL",
        )
    except Exception:
        pass


def downgrade() -> None:
    try:
        op.drop_constraint("fk_archived_videos_video_channels", "archived_videos", type_="foreignkey")
    except Exception:
        pass
    op.drop_index(op.f("ix_video_channels_name"), table_name="video_channels")
    op.drop_table("video_channels")
