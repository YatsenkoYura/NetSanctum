"""Store archived video media metadata and compression state.

Revision ID: video_0005
Revises: video_0004
"""

import sqlalchemy as sa

from alembic import op

revision = "video_0005"
down_revision = "video_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("archived_videos")}
    additions = (
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("compression_status", sa.String(length=32), nullable=True),
        sa.Column("compression_profile", sa.String(length=64), nullable=True),
        sa.Column("compressed_at", sa.DateTime(), nullable=True),
        sa.Column("compression_error", sa.String(), nullable=True),
    )
    for column in additions:
        if column.name not in columns:
            op.add_column("archived_videos", column)


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("archived_videos")}
    for name in (
        "compression_error",
        "compressed_at",
        "compression_profile",
        "compression_status",
        "sha256",
        "file_size",
    ):
        if name in columns:
            op.drop_column("archived_videos", name)
