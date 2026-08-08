"""Add platform column to video_channels and archived_videos

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-07 00:35:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    try:
        op.add_column(
            "video_channels", sa.Column("platform", sa.String(), server_default="youtube", nullable=False)
        )
        op.create_index(op.f("ix_video_channels_platform"), "video_channels", ["platform"], unique=False)
    except Exception:
        pass

    try:
        op.add_column(
            "archived_videos", sa.Column("platform", sa.String(), server_default="youtube", nullable=False)
        )
        op.create_index(op.f("ix_archived_videos_platform"), "archived_videos", ["platform"], unique=False)
    except Exception:
        pass


def downgrade() -> None:
    try:
        op.drop_index(op.f("ix_archived_videos_platform"), table_name="archived_videos")
        op.drop_column("archived_videos", "platform")
    except Exception:
        pass

    try:
        op.drop_index(op.f("ix_video_channels_platform"), table_name="video_channels")
        op.drop_column("video_channels", "platform")
    except Exception:
        pass
