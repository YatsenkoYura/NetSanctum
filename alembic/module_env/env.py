"""Shared Alembic environment for independent module histories."""

from alembic import context

config = context.config
target_metadata = config.attributes.get("target_metadata")
version_table = config.attributes["version_table"]
owned_tables = frozenset(config.attributes["owned_tables"])


def include_object(obj, name, type_, reflected, compare_to):
    table = obj if type_ == "table" else getattr(obj, "table", None)
    return table is None or table.name in owned_tables


def configure(connection=None, url=None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        version_table=version_table,
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
        literal_binds=connection is None,
        dialect_opts={"paramstyle": "named"} if connection is None else None,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    configure(url=config.attributes["database_url"])
else:
    configure(connection=config.attributes["connection"])
