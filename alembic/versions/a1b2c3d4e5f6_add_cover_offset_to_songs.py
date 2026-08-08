"""add_cover_offset_to_songs

Revision ID: a1b2c3d4e5f6
Revises: 9d01b1a774ff
Create Date: 2026-08-06 13:15:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "9d01b1a774ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("songs", sa.Column("cover_offset_x", sa.Float(), nullable=False, server_default="50"))
    op.add_column("songs", sa.Column("cover_offset_y", sa.Float(), nullable=False, server_default="50"))


def downgrade() -> None:
    op.drop_column("songs", "cover_offset_y")
    op.drop_column("songs", "cover_offset_x")
