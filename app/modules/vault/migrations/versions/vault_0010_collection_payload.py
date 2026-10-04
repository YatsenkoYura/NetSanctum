"""Give a sealed collection a payload of its own, and drop the unused private key column.

A sealed collection had nowhere to put its own metadata. `description` was the
hole: the sidebar replaced `name` with the alias while a collection was locked, but
the description stayed in the clear in the row, so a locked vault still handed its
own description to anybody who read the table.

`sealed_payload` gives a collection the same shape an item has had all along. The
data migration is deliberately absent: sealing the description needs the
collection's key, which only the owner's passphrase produces. `unlock_collection`
does it on the way through instead, the same way it re-wraps a key under the
current KDF — the one moment the passphrase has been proved and the key is in hand.

`inbox_private_key` is dropped. It arrived with `vault_0006` and nothing has ever
written it: the inbox private key lives wrapped in `wrapped_key`, and the plaintext
copy this column invited never had a writer. A sealed collection carrying a column
named after a private key is the kind of thing that eventually gets filled in by
someone in a hurry.

Revision ID: vault_0010
Revises: vault_0009
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0010"
down_revision = "vault_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.add_column(sa.Column("sealed_payload", sa.Text(), nullable=True))
    with op.batch_alter_table("vault_collections") as batch_op:
        # Not `wrapped_key`: on a collection that already holds the wrapped inbox
        # private key. Two wrappers, two columns.
        batch_op.add_column(sa.Column("sealed_wrapped_key", sa.Text(), nullable=True))
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.drop_column("inbox_private_key")


def downgrade() -> None:
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.add_column(sa.Column("inbox_private_key", sa.Text(), nullable=True))
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.drop_column("sealed_wrapped_key")
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.drop_column("sealed_payload")
