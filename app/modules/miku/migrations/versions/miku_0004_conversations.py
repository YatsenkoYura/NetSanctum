"""Conversations, their messages, and the notes an agent carries inside one.

Revision ID: miku_0004
Revises: miku_0003
"""

import sqlalchemy as sa

from alembic import op

revision = "miku_0004"
down_revision = "miku_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "miku_conversation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_miku_conversation_user_id", "miku_conversation", ["user_id"])
    op.create_index(
        "ix_miku_conversation_user_updated",
        "miku_conversation",
        ["user_id", "updated_at"],
    )
    op.create_table(
        "miku_conversation_message",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("command", sa.String(length=16), nullable=True),
        sa.Column("cascade_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["miku_conversation.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["cascade_id"], ["miku_cascade_log.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_miku_conversation_message_conversation_id",
        "miku_conversation_message",
        ["conversation_id"],
    )
    op.create_index(
        "ix_miku_conversation_message_thread",
        "miku_conversation_message",
        ["conversation_id", "id"],
    )
    op.create_table(
        "miku_conversation_note",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("note_key", sa.String(length=64), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["miku_conversation.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_miku_conversation_note_conversation_id",
        "miku_conversation_note",
        ["conversation_id"],
    )
    op.create_index(
        "ix_miku_conversation_note_conversation",
        "miku_conversation_note",
        ["conversation_id", "id"],
    )


def downgrade() -> None:
    op.drop_table("miku_conversation_note")
    op.drop_table("miku_conversation_message")
    op.drop_table("miku_conversation")
