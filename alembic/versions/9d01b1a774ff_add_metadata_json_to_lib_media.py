"""add_metadata_json_to_lib_media

Revision ID: 9d01b1a774ff
Revises: 8c67bff5bfcb
Create Date: 2026-06-16 02:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9d01b1a774ff"
down_revision: str | None = "8c67bff5bfcb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("lib_media", sa.Column("metadata_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("lib_media", "metadata_json")
