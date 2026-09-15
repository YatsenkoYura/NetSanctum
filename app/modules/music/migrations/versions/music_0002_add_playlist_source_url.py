"""Store the source URL for synced music playlists.

Revision ID: music_0002
Revises: music_0001
"""

import sqlalchemy as sa

from alembic import op

revision = "music_0002"
down_revision = "music_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("playlists")}
    if "source_url" not in columns:
        op.add_column("playlists", sa.Column("source_url", sa.String(length=2048), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("playlists")}
    if "source_url" in columns:
        op.drop_column("playlists", "source_url")
