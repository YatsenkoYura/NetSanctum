"""add_workspace_fields_to_vault

Revision ID: h2i3j4k5l6m7
Revises: g1h2i3j4k5l6
Create Date: 2026-08-07 20:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

revision = "h2i3j4k5l6m7"
down_revision = "g1h2i3j4k5l6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vault_items", sa.Column("parent_id", sa.Integer(), nullable=True))
    op.add_column("vault_items", sa.Column("is_folder", sa.Boolean(), server_default="0", nullable=False))
    op.add_column("vault_items", sa.Column("node_type", sa.String(), server_default="note", nullable=False))
    op.add_column("vault_items", sa.Column("canvas_data", sa.JSON(), server_default="{}", nullable=False))

    op.create_foreign_key(
        "fk_vault_items_parent_id", "vault_items", "vault_items", ["parent_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index(op.f("ix_vault_items_parent_id"), "vault_items", ["parent_id"], unique=False)
    op.create_index(op.f("ix_vault_items_is_folder"), "vault_items", ["is_folder"], unique=False)
    op.create_index(op.f("ix_vault_items_node_type"), "vault_items", ["node_type"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_vault_items_node_type"), table_name="vault_items")
    op.drop_index(op.f("ix_vault_items_is_folder"), table_name="vault_items")
    op.drop_index(op.f("ix_vault_items_parent_id"), table_name="vault_items")
    op.drop_constraint("fk_vault_items_parent_id", "vault_items", type_="foreignkey")

    op.drop_column("vault_items", "canvas_data")
    op.drop_column("vault_items", "node_type")
    op.drop_column("vault_items", "is_folder")
    op.drop_column("vault_items", "parent_id")
