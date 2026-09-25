"""Cascade log and open tasks.

Revision ID: miku_0003
Revises: miku_0002
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "miku_0003"
down_revision = "miku_0002"
branch_labels = None
depends_on = None


def _json_type() -> sa.types.TypeEngine:
    """Postgres keeps the whole cascade in JSONB; other backends fall back to JSON."""
    return JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "miku_cascade_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("steps_json", _json_type(), nullable=False),
        sa.Column("skeleton_json", _json_type(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("undo_json", _json_type(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_miku_cascade_log_user_id", "miku_cascade_log", ["user_id"])
    op.create_index("ix_miku_cascade_log_request_id", "miku_cascade_log", ["request_id"])
    op.create_index(
        "ix_miku_cascade_log_user_created",
        "miku_cascade_log",
        ["user_id", "created_at"],
    )
    op.create_table(
        "miku_task",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("cursor_json", _json_type(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_miku_task_user_id", "miku_task", ["user_id"])
    op.create_index(
        "ix_miku_task_state_due",
        "miku_task",
        ["state", "due_at"],
    )


def downgrade() -> None:
    op.drop_table("miku_task")
    op.drop_table("miku_cascade_log")
