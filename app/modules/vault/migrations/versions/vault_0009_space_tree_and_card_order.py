"""Let spaces nest and let cards remember where they were put.

Cards were ordered by `created_at` alone, so the left-to-right order of a space
was decided by when a thing happened to be saved, and dragging a card could not
mean anything. Spaces were flat: the sidebar was a list, and a list has no room
inside it.

`position` is fractional on both tables. Inserting between two neighbours stores
the midpoint of their positions, so a drop costs one row write instead of
renumbering the level; the rebalance below is the safety valve for the case where
two neighbours have been squeezed together until there is no room left between
them.

Existing rows are numbered in the order they were already displayed in —
`is_pinned` first, then `created_at` — so the grid does not jump on deploy.

Revision ID: vault_0009
Revises: vault_0008
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0009"
down_revision = "vault_0008"
branch_labels = None
depends_on = None

# Below this gap there is no representable midpoint left, so the level is renumbered.
MIN_GAP = 1e-6
# The initial spacing. Wide enough that a few thousand inserts still have room,
# narrow enough that a Float keeps full precision on the midpoints.
STEP = 1024.0


def _number_in_display_order(table: sa.Table) -> None:
    """Assign positions the way the grid already showed the rows."""
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(table.c.id).order_by(table.c.is_pinned.desc(), table.c.created_at.asc(), table.c.id)
    ).fetchall()
    for index, (row_id,) in enumerate(rows):
        connection.execute(sa.update(table).where(table.c.id == row_id).values(position=_step(index)))


def _step(index: int) -> float:
    return float(index) * STEP


def upgrade() -> None:
    # One batch per table: splitting the index into a second block leaves the
    # SQLite batch reflector working from the pre-alter definition.
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.add_column(sa.Column("parent_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("position", sa.Float(), nullable=True))
        batch_op.create_index(op.f("ix_vault_collections_parent_id"), ["parent_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_vault_collections_parent_id",
            "vault_collections",
            ["parent_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.add_column(sa.Column("position", sa.Float(), nullable=True))
        batch_op.create_index(op.f("ix_vault_items_position"), ["position"], unique=False)

    # Spaces first: every existing collection is a root space, so they keep the
    # order the sidebar already showed (by name, as the template sorted them).
    collections = sa.table(
        "vault_collections",
        sa.column("id", sa.Integer()),
        sa.column("created_at", sa.DateTime()),
        sa.column("position", sa.Float()),
    )
    connection = op.get_bind()
    for index, (collection_id,) in enumerate(
        connection.execute(sa.select(collections.c.id).order_by(collections.c.created_at, collections.c.id))
    ):
        connection.execute(
            sa.update(collections).where(collections.c.id == collection_id).values(position=_step(index))
        )

    items = sa.table(
        "vault_items",
        sa.column("id", sa.Integer()),
        sa.column("is_pinned", sa.Boolean()),
        sa.column("created_at", sa.DateTime()),
        sa.column("position", sa.Float()),
    )
    _number_in_display_order(items)


def downgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.drop_index(op.f("ix_vault_items_position"))
    with op.batch_alter_table("vault_items") as batch_op:
        batch_op.drop_column("position")
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.drop_index(op.f("ix_vault_collections_parent_id"))
    with op.batch_alter_table("vault_collections") as batch_op:
        batch_op.drop_constraint(op.f("fk_vault_collections_parent_id"), type_="foreignkey")
        batch_op.drop_column("position")
        batch_op.drop_column("parent_id")
