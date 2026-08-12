"""scope lib media slug uniqueness by site

Revision ID: i3j4k5l6m7n8
Revises: h2i3j4k5l6m7
Create Date: 2026-08-12 00:00:00.000000
"""

from alembic import op

revision = "i3j4k5l6m7n8"
down_revision = "h2i3j4k5l6m7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ranobe_novels_slug_key", "lib_media", type_="unique")
    op.create_unique_constraint("uq_lib_media_site_slug", "lib_media", ["site_id", "slug"])


def downgrade() -> None:
    op.drop_constraint("uq_lib_media_site_slug", "lib_media", type_="unique")
    op.create_unique_constraint("ranobe_novels_slug_key", "lib_media", ["slug"])
