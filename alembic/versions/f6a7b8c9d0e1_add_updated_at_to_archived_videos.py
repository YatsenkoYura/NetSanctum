"""add_updated_at_to_archived_videos

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-07 07:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("archived_videos", sa.Column("updated_at", sa.DateTime(), nullable=True))
    # Fill existing rows
    op.execute("UPDATE archived_videos SET updated_at = archived_at WHERE updated_at IS NULL")


def downgrade() -> None:
    op.drop_column("archived_videos", "updated_at")
