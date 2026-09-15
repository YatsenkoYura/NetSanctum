"""Store generated playlist cover paths.

Revision ID: music_0003
Revises: music_0002
"""

import sqlalchemy as sa

from alembic import op

revision = "music_0003"
down_revision = "music_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("playlists")}
    if "cover_path" not in columns:
        op.add_column("playlists", sa.Column("cover_path", sa.String(length=255), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("playlists")}
    if "cover_path" in columns:
        op.drop_column("playlists", "cover_path")
