"""Scope tabletop rooms created through shared operator links.

Revision ID: tabletop_0002
Revises: tabletop_0001
"""

import sqlalchemy as sa

from alembic import op

revision = "tabletop_0002"
down_revision = "tabletop_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("tabletop_rooms")}
    if "operator_share_id" not in columns:
        op.add_column(
            "tabletop_rooms",
            sa.Column("operator_share_id", sa.String(length=36), nullable=True),
        )
        op.create_index(
            "ix_tabletop_rooms_operator_share_id",
            "tabletop_rooms",
            ["operator_share_id"],
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("tabletop_rooms")}
    if "operator_share_id" in columns:
        op.drop_index("ix_tabletop_rooms_operator_share_id", table_name="tabletop_rooms")
        op.drop_column("tabletop_rooms", "operator_share_id")
