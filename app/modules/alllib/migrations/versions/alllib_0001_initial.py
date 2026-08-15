"""AllLib schema baseline.

Revision ID: alllib_0001
Revises:
"""

import sqlalchemy as sa

from alembic import op

revision = "alllib_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lib_media",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("rus_name", sa.String(length=255), nullable=True),
        sa.Column("eng_name", sa.String(length=255), nullable=True),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("cover_path", sa.String(length=510), nullable=True),
        sa.Column("source_url", sa.String(length=510), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "slug", name="uq_lib_media_site_slug"),
    )
    op.create_table(
        "lib_chapters",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("media_id", sa.Integer(), nullable=False),
        sa.Column("volume", sa.String(length=50), nullable=False),
        sa.Column("number", sa.String(length=50), nullable=False),
        sa.Column("volume_int", sa.Integer(), nullable=False),
        sa.Column("number_float", sa.Float(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("content_html", sa.Text(), nullable=True),
        sa.Column("pages_list", sa.JSON(), nullable=True),
        sa.Column("video_path", sa.String(length=510), nullable=True),
        sa.ForeignKeyConstraint(["media_id"], ["lib_media.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("lib_chapters")
    op.drop_table("lib_media")
