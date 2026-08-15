"""Settings schema baseline.

Revision ID: settings_0001
Revises:
"""

import sqlalchemy as sa

from alembic import op

revision = "settings_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("module_name", sa.String(length=128), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("value_type", sa.String(length=16), nullable=False),
        sa.Column("is_secret", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope",
            "module_name",
            "user_id",
            "key",
            name="uq_settings_scope_module_user_key",
        ),
    )
    op.create_index("ix_settings_key", "settings", ["key"])
    op.create_index("ix_settings_module_name", "settings", ["module_name"])
    op.create_index("ix_settings_scope", "settings", ["scope"])
    op.create_index("ix_settings_user_id", "settings", ["user_id"])


def downgrade() -> None:
    op.drop_table("settings")
