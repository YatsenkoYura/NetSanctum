"""Vault schema baseline.

Revision ID: vault_0001
Revises:
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vault_collections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("color", sa.String(), server_default="teal", nullable=False),
        sa.Column("icon", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vault_collections_name", "vault_collections", ["name"])
    op.create_table(
        "vault_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entry_type", sa.String(), server_default="bookmark", nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("og_title", sa.String(), nullable=True),
        sa.Column("og_description", sa.Text(), nullable=True),
        sa.Column("og_image", sa.String(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("progress_current", sa.Integer(), server_default="0", nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("rewatch_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("is_pinned", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("is_archived", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("collection_id", sa.Integer(), nullable=True),
        sa.Column("related_entity_type", sa.String(), nullable=True),
        sa.Column("related_entity_id", sa.String(), nullable=True),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("is_folder", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("node_type", sa.String(), server_default="note", nullable=False),
        sa.Column("canvas_data", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["collection_id"], ["vault_collections.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["vault_items.id"],
            name="fk_vault_items_parent_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "category",
        "collection_id",
        "created_at",
        "entry_type",
        "is_archived",
        "is_folder",
        "is_pinned",
        "node_type",
        "parent_id",
        "related_entity_type",
        "status",
        "title",
    ):
        op.create_index(f"ix_vault_items_{column}", "vault_items", [column])


def downgrade() -> None:
    op.drop_table("vault_items")
    op.drop_table("vault_collections")
