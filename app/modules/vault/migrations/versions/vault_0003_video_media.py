"""Store video files in Vault's own storage instead of pointing at another module.

Revision ID: vault_0003
Revises: vault_0002
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0003"
down_revision = "vault_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.add_column(sa.Column("media_path", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("media_mime", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("media_size", sa.Integer(), nullable=True))
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.create_index(op.f("ix_vault_items_media_path"), ["media_path"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.drop_index(op.f("ix_vault_items_media_path"))
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.drop_column("media_size")
        batch_op.drop_column("media_mime")
        batch_op.drop_column("media_path")
