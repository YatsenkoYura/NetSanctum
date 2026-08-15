"""Align Vault server defaults with its application models.

Revision ID: vault_0002
Revises: vault_0001
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0002"
down_revision = "vault_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.alter_column(
            "color",
            existing_type=sa.String(),
            existing_nullable=False,
            server_default=None,
        )
    with op.batch_alter_table("vault_items") as batch_op:
        for column, column_type in (
            ("entry_type", sa.String()),
            ("progress_current", sa.Integer()),
            ("rewatch_count", sa.Integer()),
            ("is_pinned", sa.Boolean()),
            ("is_archived", sa.Boolean()),
            ("is_folder", sa.Boolean()),
            ("node_type", sa.String()),
            ("canvas_data", sa.JSON()),
        ):
            batch_op.alter_column(
                column,
                existing_type=column_type,
                existing_nullable=False,
                server_default=None,
            )


def downgrade() -> None:
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.alter_column(
            "color",
            existing_type=sa.String(),
            existing_nullable=False,
            server_default="teal",
        )
    defaults = (
        ("entry_type", sa.String(), "bookmark"),
        ("progress_current", sa.Integer(), "0"),
        ("rewatch_count", sa.Integer(), "0"),
        ("is_pinned", sa.Boolean(), "0"),
        ("is_archived", sa.Boolean(), "0"),
        ("is_folder", sa.Boolean(), "0"),
        ("node_type", sa.String(), "note"),
        ("canvas_data", sa.JSON(), "{}"),
    )
    with op.batch_alter_table("vault_items") as batch_op:
        for column, column_type, default in defaults:
            batch_op.alter_column(
                column,
                existing_type=column_type,
                existing_nullable=False,
                server_default=default,
            )
