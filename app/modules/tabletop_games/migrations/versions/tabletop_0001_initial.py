"""Tabletop games schema baseline.

Revision ID: tabletop_0001
Revises:
"""

import sqlalchemy as sa

from alembic import op

revision = "tabletop_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tabletop_rooms",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=10), nullable=False),
        sa.Column("game_id", sa.String(length=63), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("game_state", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tabletop_rooms_code", "tabletop_rooms", ["code"], unique=True)
    op.create_index("ix_tabletop_rooms_game_id", "tabletop_rooms", ["game_id"])
    op.create_index("ix_tabletop_rooms_status", "tabletop_rooms", ["status"])
    op.create_table(
        "tabletop_participants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("nickname", sa.String(length=40), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("seat", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.String(length=63), nullable=True),
        sa.Column("role_data", sa.JSON(), nullable=False),
        sa.Column("reminders", sa.JSON(), nullable=False),
        sa.Column("gm_notes", sa.Text(), nullable=False),
        sa.Column("is_alive", sa.Boolean(), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["tabletop_rooms.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_id", "nickname", name="uq_tabletop_room_nickname"),
    )
    op.create_index("ix_tabletop_participants_room_id", "tabletop_participants", ["room_id"])
    op.create_index(
        "ix_tabletop_participants_token_hash", "tabletop_participants", ["token_hash"], unique=True
    )
    op.create_table(
        "tabletop_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("sender_participant_id", sa.String(length=36), nullable=True),
        sa.Column("recipient_participant_id", sa.String(length=36), nullable=True),
        sa.Column("audience", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["tabletop_rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_participant_id"], ["tabletop_participants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["recipient_participant_id"], ["tabletop_participants.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tabletop_messages_room_id", "tabletop_messages", ["room_id"])
    op.create_index(
        "ix_tabletop_messages_sender_participant_id",
        "tabletop_messages",
        ["sender_participant_id"],
    )
    op.create_index(
        "ix_tabletop_messages_recipient_participant_id",
        "tabletop_messages",
        ["recipient_participant_id"],
    )


def downgrade() -> None:
    op.drop_table("tabletop_messages")
    op.drop_table("tabletop_participants")
    op.drop_table("tabletop_rooms")
