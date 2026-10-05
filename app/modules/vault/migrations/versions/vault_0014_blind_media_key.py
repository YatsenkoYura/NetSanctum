"""A blind media write's key, wrapped under the collection's inbox public key.

A sealed collection's video used to wait as `pending_unlock` until an unlocked
tab lent the worker its file key — the worker had nowhere to put the bytes that
was not the weaker application key. Now it stores the file immediately, under a
random item key minted for that one download, and the item key is what this
column holds: sealed under the inbox public key, exactly like a blind card
write, bound to this row (`kind="media"`, the item id) by the v2 envelope.

Public by design and useless without the inbox private key: the next unlock
opens it and re-seals the file under the collection's file key, clearing this
column. A row that still has it is a file nobody can play yet — the endpoints
refuse it like a locked one — not a file stored weakly.

Revision ID: vault_0014
Revises: vault_0013
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0014"
down_revision = "vault_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.add_column(sa.Column("media_key_wrap", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.drop_column("media_key_wrap")
