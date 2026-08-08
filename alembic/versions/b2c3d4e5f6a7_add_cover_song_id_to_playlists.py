"""Add cover_song_id to playlists table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-06 14:31:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("playlists", sa.Column("cover_song_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_playlists_cover_song_id", "playlists", "songs", ["cover_song_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_playlists_cover_song_id", "playlists", type_="foreignkey")
    op.drop_column("playlists", "cover_song_id")
