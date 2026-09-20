"""Store song audio identity metadata.

Revision ID: music_0004
Revises: music_0003
"""

import sqlalchemy as sa

from alembic import op

revision = "music_0004"
down_revision = "music_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("songs")}
    additions = (
        sa.Column("audio_file_size", sa.BigInteger(), nullable=True),
        sa.Column("audio_sha256", sa.String(length=64), nullable=True),
    )
    for column in additions:
        if column.name not in columns:
            op.add_column("songs", column)


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("songs")}
    for name in ("audio_sha256", "audio_file_size"):
        if name in columns:
            op.drop_column("songs", name)
