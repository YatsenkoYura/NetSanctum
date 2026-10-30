"""Wrapped key for images written while a sealed Vault is locked.

Revision ID: vault_0016
Revises: vault_0015
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0016"
down_revision = "vault_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.add_column(sa.Column("image_key_wrap", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.drop_column("image_key_wrap")
