"""Store the source URL for synced video playlists.

Revision ID: video_0003
Revises: video_0002
"""

import sqlalchemy as sa

from alembic import op

revision = "video_0003"
down_revision = "video_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("video_playlists")}
    if "source_url" not in columns:
        op.add_column("video_playlists", sa.Column("source_url", sa.String(length=2048), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("video_playlists")}
    if "source_url" in columns:
        op.drop_column("video_playlists", "source_url")
