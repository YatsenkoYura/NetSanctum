"""What a capture rule read off the page it matched.

A capture that matched a site rule sends back the few fields worth keeping —
an illustration id, an author, the tags something was published under — and they
need somewhere to land. `site` names the rule that matched and `site_meta` holds
what it read.

Both are sealed rather than structural, which is the whole point of putting them
in columns instead of in `node_type`. A card type has to stay readable while the
vault is locked, because the grid lays its tiles out from it before anything is
decrypted; a value like `pixiv` there would tell anyone holding the database what
the owner browses, sealed vault or not. This pair is blanked by `seal_item` and
restored by `open_item` like any other owner-written field.

Revision ID: vault_0015
Revises: vault_0014
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0015"
down_revision = "vault_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.add_column(sa.Column("site", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("site_meta", sa.JSON(), nullable=True))
    # Backfilled rather than left NULL: every row that exists has NULL in this
    # column now, and a card that never matched a rule should read as "no rule
    # matched", not as a missing value the response schema has to apologise for.
    # The read path also tolerates NULL — this is what keeps the two honest.
    op.execute("UPDATE vault_items SET site_meta = '{}' WHERE site_meta IS NULL")


def downgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.drop_column("site_meta")
        batch_op.drop_column("site")
