"""Blind-write inbox: a sealed collection can accept writes without its passphrase.

Revision ID: vault_0006
Revises: vault_0005
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0006"
down_revision = "vault_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.add_column(sa.Column("inbox_public_key", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("inbox_private_key", sa.Text(), nullable=True))
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.add_column(sa.Column("wrapped_key", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.drop_column("wrapped_key")
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.drop_column("inbox_private_key")
        batch_op.drop_column("inbox_public_key")
