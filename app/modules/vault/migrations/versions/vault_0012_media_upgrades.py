"""Track the media envelope migration, per file.

The chunked file envelope became v2 with the plaintext length, the chunk size
and the chunk count inside every chunk's associated data. Objects written
before it keep opening — the reader speaks both — but the header of a v1 object
is a promise nobody authenticates, so rewriting them is worth doing.

Doing it in place is not an option: the envelope binds its own path, so a v2
object written over a v1 path would invalidate every chunk it had just written.
Each file therefore moves to a new name, is verified by reading it back, and
only then does the row point at it; the old file is deleted last.

This table is the ledger, not the state. Whether a file needs work is a fact
about its header, and whether the work is done is a fact about the row that
points at it — so the migration is idempotent and resumable with the table
deleted. What the table holds is what neither can: that a file was tried and
failed, and why, so a long run can be resumed with a report instead of retrying
one unreadable file on every pass.

Revision ID: vault_0012
Revises: vault_0011
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0012"
down_revision = "vault_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vault_media_upgrades",
        sa.Column("path", sa.String(), primary_key=True),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("column_name", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_vault_media_upgrades_state", "vault_media_upgrades", ["state"])


def downgrade() -> None:
    op.drop_index("ix_vault_media_upgrades_state", table_name="vault_media_upgrades")
    op.drop_table("vault_media_upgrades")
