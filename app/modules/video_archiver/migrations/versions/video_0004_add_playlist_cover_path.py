"""Store generated playlist cover paths.

Revision ID: video_0004
Revises: video_0003
"""

import sqlalchemy as sa

from alembic import op

revision = "video_0004"
down_revision = "video_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("video_playlists")}
    if "cover_path" not in columns:
        op.add_column("video_playlists", sa.Column("cover_path", sa.String(length=255), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("video_playlists")}
    if "cover_path" in columns:
        op.drop_column("video_playlists", "cover_path")
