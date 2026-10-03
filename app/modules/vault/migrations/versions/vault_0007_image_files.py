"""Move Vault images out of a base64 column and into encrypted storage.

Revision ID: vault_0007
Revises: vault_0006
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0007"
down_revision = "vault_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.add_column(sa.Column("image_path", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.drop_column("image_path")
