"""Add torrent module tables

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-06 16:45:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "torrent_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("info_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("download_speed", sa.BigInteger(), nullable=True),
        sa.Column("upload_speed", sa.BigInteger(), nullable=True),
        sa.Column("downloaded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("uploaded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("ratio", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=True),
        sa.Column("tracker", sa.String(length=128), nullable=True),
        sa.Column("seeders", sa.Integer(), nullable=True),
        sa.Column("leechers", sa.Integer(), nullable=True),
        sa.Column("seed_duration_days", sa.Integer(), nullable=True),
        sa.Column("hit_and_run_risk", sa.Boolean(), nullable=True),
        sa.Column("min_ratio_required", sa.Float(), nullable=True),
        sa.Column("file_path", sa.String(length=1024), nullable=True),
        sa.Column("added_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_torrent_items_id"), "torrent_items", ["id"], unique=False)
    op.create_index(op.f("ix_torrent_items_info_hash"), "torrent_items", ["info_hash"], unique=True)
    op.create_index(op.f("ix_torrent_items_mode"), "torrent_items", ["mode"], unique=False)
    op.create_index(op.f("ix_torrent_items_status"), "torrent_items", ["status"], unique=False)
    op.create_index(op.f("ix_torrent_items_tracker"), "torrent_items", ["tracker"], unique=False)

    op.create_table(
        "torrent_tracker_stats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tracker_domain", sa.String(length=128), nullable=False),
        sa.Column("total_torrents", sa.Integer(), nullable=True),
        sa.Column("total_downloaded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("total_uploaded_bytes", sa.BigInteger(), nullable=True),
        sa.Column("overall_ratio", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_torrent_tracker_stats_id"), "torrent_tracker_stats", ["id"], unique=False)
    op.create_index(
        op.f("ix_torrent_tracker_stats_tracker_domain"),
        "torrent_tracker_stats",
        ["tracker_domain"],
        unique=True,
    )

    op.create_table(
        "torrent_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("auto_stop_ratio", sa.Float(), nullable=True),
        sa.Column("min_seed_days", sa.Integer(), nullable=True),
        sa.Column("emergency_rescue_mode", sa.Boolean(), nullable=True),
        sa.Column("max_bandwidth_mb", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_torrent_settings_id"), "torrent_settings", ["id"], unique=False)


def downgrade() -> None:
    op.drop_table("torrent_settings")
    op.drop_table("torrent_tracker_stats")
    op.drop_table("torrent_items")
