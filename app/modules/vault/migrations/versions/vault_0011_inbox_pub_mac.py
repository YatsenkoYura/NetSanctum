"""Authenticate the inbox public key.

The public half of the inbox keypair sits in the clear so a client without the
passphrase can seal into the vault. Nothing bound it to the passphrase, so a row
rewritten with an attacker's public key would silently divert every blind write —
captures, screenshots, extension drops — into hands that were never given the
passphrase. The owner would notice nothing: their own unlock still works, because
the wrapper was never touched.

`inbox_pub_mac` is an HMAC over the public key under a key derived from the KEK,
checked on every unlock. Legacy rows carry no MAC; the first unlock computes and
stores it, the same way it already re-wraps keys and seals leftover plaintext.

Revision ID: vault_0011
Revises: vault_0010
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0011"
down_revision = "vault_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.add_column(sa.Column("inbox_pub_mac", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.drop_column("inbox_pub_mac")
