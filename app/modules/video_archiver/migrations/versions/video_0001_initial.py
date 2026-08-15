"""Video Archiver schema baseline.

Revision ID: video_0001
Revises:
"""

import sqlalchemy as sa

from alembic import op

revision = "video_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "video_channels",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("platform", sa.String(), server_default="youtube", nullable=False),
        sa.Column("custom_url", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("avatar_path", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_video_channels_name", "video_channels", ["name"])
    op.create_index("ix_video_channels_platform", "video_channels", ["platform"])
    op.create_table(
        "video_playlists",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "archived_videos",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("platform", sa.String(), server_default="youtube", nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("channel_name", sa.String(), nullable=False),
        sa.Column("channel_avatar_url", sa.String(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=False),
        sa.Column("resolution", sa.String(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=True),
        sa.Column("thumbnail_path", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("comments", sa.JSON(), nullable=True),
        sa.Column("subtitles", sa.JSON(), nullable=True),
        sa.Column("like_count", sa.Integer(), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("original_publish_date", sa.DateTime(), nullable=True),
        sa.Column("auto_update", sa.Boolean(), nullable=True),
        sa.Column("is_deleted_on_youtube", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["video_channels.id"],
            name="fk_archived_videos_video_channels",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_archived_videos_channel_id", "archived_videos", ["channel_id"])
    op.create_index("ix_archived_videos_platform", "archived_videos", ["platform"])
    op.create_table(
        "video_playlist_association",
        sa.Column("video_id", sa.String(), nullable=False),
        sa.Column("playlist_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["playlist_id"], ["video_playlists.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_id"], ["archived_videos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("video_id", "playlist_id"),
    )


def downgrade() -> None:
    op.drop_table("video_playlist_association")
    op.drop_table("archived_videos")
    op.drop_table("video_playlists")
    op.drop_table("video_channels")
