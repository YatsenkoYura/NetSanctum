"""Allow archived videos without a channel.

Revision ID: video_0002
Revises: video_0001
"""

import sqlalchemy as sa

from alembic import op

revision = "video_0002"
down_revision = "video_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("archived_videos") as batch_op:
        batch_op.alter_column("channel_id", existing_type=sa.String(), nullable=True)
        batch_op.alter_column(
            "platform",
            existing_type=sa.String(),
            existing_nullable=False,
            server_default=None,
        )
    with op.batch_alter_table("video_channels") as batch_op:
        batch_op.alter_column(
            "platform",
            existing_type=sa.String(),
            existing_nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("video_channels") as batch_op:
        batch_op.alter_column(
            "platform",
            existing_type=sa.String(),
            existing_nullable=False,
            server_default="youtube",
        )
    with op.batch_alter_table("archived_videos") as batch_op:
        batch_op.alter_column(
            "platform",
            existing_type=sa.String(),
            existing_nullable=False,
            server_default="youtube",
        )
        batch_op.alter_column("channel_id", existing_type=sa.String(), nullable=False)
