"""Room for the sealed offline package wrapper.

A sealed package (`vault_sealed_<id>`) serves a collection's content as transfer-v1
ciphertext. The package DEK is derived, not stored — but the passphrase-wrapped
copy of it has to live somewhere the manifest can serve without the passphrase,
exactly like the vault wrapper itself. These two columns hold the salt and the
wrapped bytes; both are public by design and useless without the passphrase.

The first unlock after this migration fills them in; a collection that was never
unlocked since simply reports "unlock once first" from the sealed manifest
endpoint rather than guessing. No backfill: writing rows here needs the
passphrase, and only an unlock has it.

Revision ID: vault_0013
Revises: vault_0012
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0013"
down_revision = "vault_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.add_column(sa.Column("sealed_pkg_salt", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("sealed_pkg_wrapped", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("sealed_pkg_kdf", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.drop_column("sealed_pkg_kdf")
        batch_op.drop_column("sealed_pkg_wrapped")
        batch_op.drop_column("sealed_pkg_salt")
