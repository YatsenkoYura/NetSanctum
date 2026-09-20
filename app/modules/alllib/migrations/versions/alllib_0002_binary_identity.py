"""Store AllLib video identities and export snapshots.

Revision ID: alllib_0002
Revises: alllib_0001
"""

import sqlalchemy as sa

from alembic import op

revision = "alllib_0002"
down_revision = "alllib_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    media_columns = {column["name"] for column in inspector.get_columns("lib_media")}
    chapter_columns = {column["name"] for column in inspector.get_columns("lib_chapters")}

    for column in (
        sa.Column("export_path", sa.String(length=510), nullable=True),
        sa.Column("export_size", sa.BigInteger(), nullable=True),
        sa.Column("export_sha256", sa.String(length=64), nullable=True),
    ):
        if column.name not in media_columns:
            op.add_column("lib_media", column)

    for column in (
        sa.Column("video_size", sa.BigInteger(), nullable=True),
        sa.Column("video_sha256", sa.String(length=64), nullable=True),
    ):
        if column.name not in chapter_columns:
            op.add_column("lib_chapters", column)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    media_columns = {column["name"] for column in inspector.get_columns("lib_media")}
    chapter_columns = {column["name"] for column in inspector.get_columns("lib_chapters")}

    for name in ("video_sha256", "video_size"):
        if name in chapter_columns:
            op.drop_column("lib_chapters", name)
    for name in ("export_sha256", "export_size", "export_path"):
        if name in media_columns:
            op.drop_column("lib_media", name)
