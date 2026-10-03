"""Give media metadata structural columns so a sealed item keeps its poster.

The download worker writes the poster path, title, duration and dimensions into
`canvas_data`, which is one of the SEALED_FIELDS. For an item in a sealed
collection that write landed in the readable JSON while `sealed_payload` kept the
old blob, so the next unlock overwrote it and the card lost its face. The same
write put `media_mime` into the clear for a collection that promises otherwise.

These columns are structural rather than author-written, so they stay in the
clear by design — exactly like `media_path`, which has always been readable. The
worker cannot re-seal an item: opening one needs the inbox private key, and that
lives in Redis under a per-tab token the worker does not have.

Revision ID: vault_0008
Revises: vault_0007
"""

import sqlalchemy as sa

from alembic import op

revision = "vault_0008"
down_revision = "vault_0007"
branch_labels = None
depends_on = None

# Keys that used to hide inside `canvas_data`. Kept as a table so the copy and
# the cleanup cannot drift apart.
MOVED_KEYS = (
    "media_status",
    "media_title",
    "media_duration",
    "media_width",
    "media_height",
    "media_thumbnail_path",
)

COLUMNS = (
    ("media_status", sa.String()),
    ("media_title", sa.String()),
    ("media_duration", sa.Float()),
    ("media_width", sa.Integer()),
    ("media_height", sa.Integer()),
    ("media_thumbnail_path", sa.String()),
)


def upgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        for name, column_type in COLUMNS:
            batch_op.add_column(sa.Column(name, column_type, nullable=True))

    # Typed Core statements rather than raw text: SQLAlchemy is then responsible
    # for turning the JSON column into a dict on the way in and back into a string
    # on the way out, which is what keeps this identical on PostgreSQL and SQLite.
    items = sa.table(
        "vault_items",
        sa.column("id", sa.Integer()),
        sa.column("canvas_data", sa.JSON()),
        sa.column("media_path", sa.String()),
        *(sa.column(name, column_type) for name, column_type in COLUMNS),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(items.c.id, items.c.canvas_data).where(items.c.media_path.isnot(None))
    ).fetchall()
    for item_id, canvas_data in rows:
        if not isinstance(canvas_data, dict):
            continue
        values = {key: canvas_data[key] for key in MOVED_KEYS if canvas_data.get(key) is not None}
        if values:
            connection.execute(sa.update(items).where(items.c.id == item_id).values(**values))
        # The columns are authoritative from here on. Leaving a second copy behind
        # would let the two disagree after an unlock restored the old blob.
        if any(key in canvas_data for key in MOVED_KEYS):
            trimmed = {key: value for key, value in canvas_data.items() if key not in MOVED_KEYS}
            connection.execute(sa.update(items).where(items.c.id == item_id).values(canvas_data=trimmed))


def downgrade() -> None:
    with op.batch_alter_table("vault_items") as batch_op:
        for name, _column_type in reversed(COLUMNS):
            batch_op.drop_column(name)
