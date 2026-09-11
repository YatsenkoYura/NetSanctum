"""Enforce scope-aware setting uniqueness.

Revision ID: settings_0002
Revises: settings_0001
"""

import sqlalchemy as sa

from alembic import op

revision = "settings_0002"
down_revision = "settings_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM settings
            WHERE id NOT IN (
                SELECT MAX(id)
                FROM settings
                GROUP BY scope, COALESCE(module_name, ''), COALESCE(user_id, -1), key
            )
            """
        )
    )
    constraints = {item["name"] for item in sa.inspect(op.get_bind()).get_unique_constraints("settings")}
    if "uq_settings_scope_module_user_key" in constraints:
        with op.batch_alter_table("settings") as batch:
            batch.drop_constraint("uq_settings_scope_module_user_key", type_="unique")
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("settings")}
    definitions = (
        ("uq_settings_global_key", ["key"], "scope = 'global' AND module_name IS NULL AND user_id IS NULL"),
        (
            "uq_settings_module_key",
            ["module_name", "key"],
            "scope = 'module' AND module_name IS NOT NULL AND user_id IS NULL",
        ),
        (
            "uq_settings_user_key",
            ["user_id", "key"],
            "scope = 'user' AND user_id IS NOT NULL AND module_name IS NULL",
        ),
    )
    for name, columns, condition in definitions:
        if name not in indexes:
            op.create_index(
                name,
                "settings",
                columns,
                unique=True,
                postgresql_where=sa.text(condition),
                sqlite_where=sa.text(condition),
            )


def downgrade() -> None:
    op.drop_index("uq_settings_user_key", table_name="settings")
    op.drop_index("uq_settings_module_key", table_name="settings")
    op.drop_index("uq_settings_global_key", table_name="settings")
    with op.batch_alter_table("settings") as batch:
        batch.create_unique_constraint(
            "uq_settings_scope_module_user_key",
            ["scope", "module_name", "user_id", "key"],
        )
