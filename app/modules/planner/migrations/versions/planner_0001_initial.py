"""Create the planner task and event tables.

Revision ID: planner_0001
Revises: None
"""

import sqlalchemy as sa

from alembic import op

revision = "planner_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "planner_task",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remind_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recurrence", sa.String(length=16), nullable=False),
        sa.Column("space_kind", sa.String(length=16), nullable=False),
        sa.Column("space_id", sa.Integer(), nullable=True),
        sa.Column("space_name", sa.String(length=120), nullable=True),
        sa.Column("raw_text", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_planner_task_user_id", "planner_task", ["user_id"])
    op.create_index(
        "ix_planner_task_user_status_due",
        "planner_task",
        ["user_id", "status", "due_at"],
    )
    op.create_index("ix_planner_task_sweep", "planner_task", ["status", "remind_at"])

    op.create_table(
        "planner_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("location", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remind_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recurrence", sa.String(length=16), nullable=False),
        sa.Column("space_kind", sa.String(length=16), nullable=False),
        sa.Column("space_id", sa.Integer(), nullable=True),
        sa.Column("space_name", sa.String(length=120), nullable=True),
        sa.Column("raw_text", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_planner_event_user_id", "planner_event", ["user_id"])
    op.create_index(
        "ix_planner_event_user_starts",
        "planner_event",
        ["user_id", "starts_at"],
    )
    op.create_index("ix_planner_event_sweep", "planner_event", ["status", "remind_at"])


def downgrade() -> None:
    op.drop_table("planner_event")
    op.drop_table("planner_task")
