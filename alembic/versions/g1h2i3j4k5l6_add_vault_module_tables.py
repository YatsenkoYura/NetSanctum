"""add_vault_module_tables

Revision ID: g1h2i3j4k5l6
Revises: f6a7b8c9d0e1
Create Date: 2026-08-07 14:00:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "g1h2i3j4k5l6"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create vault_collections table
    op.create_table(
        "vault_collections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("color", sa.String(), nullable=False, server_default="teal"),
        sa.Column("icon", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_vault_collections_name"), "vault_collections", ["name"], unique=False)

    # 2. Create vault_items table
    op.create_table(
        "vault_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entry_type", sa.String(), nullable=False, server_default="bookmark"),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("og_title", sa.String(), nullable=True),
        sa.Column("og_description", sa.Text(), nullable=True),
        sa.Column("og_image", sa.String(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("progress_current", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("rewatch_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("collection_id", sa.Integer(), nullable=True),
        sa.Column("related_entity_type", sa.String(), nullable=True),
        sa.Column("related_entity_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["collection_id"], ["vault_collections.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_vault_items_category"), "vault_items", ["category"], unique=False)
    op.create_index(op.f("ix_vault_items_collection_id"), "vault_items", ["collection_id"], unique=False)
    op.create_index(op.f("ix_vault_items_created_at"), "vault_items", ["created_at"], unique=False)
    op.create_index(op.f("ix_vault_items_entry_type"), "vault_items", ["entry_type"], unique=False)
    op.create_index(op.f("ix_vault_items_is_archived"), "vault_items", ["is_archived"], unique=False)
    op.create_index(op.f("ix_vault_items_is_pinned"), "vault_items", ["is_pinned"], unique=False)
    op.create_index(
        op.f("ix_vault_items_related_entity_type"), "vault_items", ["related_entity_type"], unique=False
    )
    op.create_index(op.f("ix_vault_items_status"), "vault_items", ["status"], unique=False)
    op.create_index(op.f("ix_vault_items_title"), "vault_items", ["title"], unique=False)


def downgrade() -> None:
    op.drop_table("vault_items")
    op.drop_table("vault_collections")
