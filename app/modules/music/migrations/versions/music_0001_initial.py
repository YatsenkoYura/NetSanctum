"""Music schema baseline.

Revision ID: music_0001
Revises:
"""

import sqlalchemy as sa

from alembic import op

revision = "music_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "songs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("original_artist", sa.String(length=255), nullable=True),
        sa.Column("cover_file_id", sa.String(length=255), nullable=True),
        sa.Column("audio_file_id", sa.String(length=255), nullable=False),
        sa.Column("youtube_url", sa.String(length=255), nullable=True),
        sa.Column("cover_offset_x", sa.Float(), server_default="50", nullable=False),
        sa.Column("cover_offset_y", sa.Float(), server_default="50", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "playlists",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("cover_song_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["cover_song_id"],
            ["songs.id"],
            name="fk_playlists_cover_song_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "playlist_songs",
        sa.Column("playlist_id", sa.Integer(), nullable=False),
        sa.Column("song_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["playlist_id"], ["playlists.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["song_id"], ["songs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("playlist_id", "song_id"),
    )


def downgrade() -> None:
    op.drop_table("playlist_songs")
    op.drop_table("playlists")
    op.drop_table("songs")
