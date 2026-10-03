"""A locked sealed item shows an alias instead of its title.

Revision ID: vault_0005
Revises: vault_0004
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0005"
down_revision = "vault_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.add_column(sa.Column("public_title", sa.String(), nullable=True))
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.add_column(sa.Column("public_name", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.drop_column("public_name")
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.drop_column("public_title")
