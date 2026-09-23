"""MIKU metadata-only audit baseline.

Revision ID: miku_0001
Revises:
"""

import sqlalchemy as sa

from alembic import op

revision = "miku_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "miku_turn_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("transport", sa.String(length=16), nullable=False),
        sa.Column("command", sa.String(length=16), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("warning_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_miku_turn_audit_request_id", "miku_turn_audit", ["request_id"])
    op.create_index("ix_miku_turn_audit_user_id", "miku_turn_audit", ["user_id"])
    op.create_index("ix_miku_turn_audit_user_created", "miku_turn_audit", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("miku_turn_audit")
