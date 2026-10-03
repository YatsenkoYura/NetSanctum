"""Sealed Vault collections: a wrapped per-collection key and sealed item payloads.

Revision ID: vault_0004
Revises: vault_0003
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0004"
down_revision = "vault_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Added nullable first, backfilled, then tightened: a NOT NULL column cannot be
    # added to a table that already has rows. The result carries no server default,
    # matching the Python-side defaults on the model (see vault_0002).
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.add_column(sa.Column("is_encrypted", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("key_salt", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("wrapped_key", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("key_kdf", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("key_kdf_params", sa.JSON(), nullable=True))
    op.execute("UPDATE vault_collections SET is_encrypted = false WHERE is_encrypted IS NULL")
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.alter_column(
            "is_encrypted",
            existing_type=sa.Boolean(),
            nullable=False,
            existing_server_default=None,
            server_default=None,
        )
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.create_index(op.f("ix_vault_collections_is_encrypted"), ["is_encrypted"], unique=False)

    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.add_column(sa.Column("sealed_payload", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.drop_column("sealed_payload")
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.drop_index(op.f("ix_vault_collections_is_encrypted"))
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.drop_column("key_kdf_params")
        batch_op.drop_column("key_kdf")
        batch_op.drop_column("wrapped_key")
        batch_op.drop_column("key_salt")
        batch_op.drop_column("is_encrypted")
