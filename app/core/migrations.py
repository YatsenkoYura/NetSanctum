"""Framework runner for legacy adoption and independent module migrations."""

import argparse
import importlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from sqlalchemy import (
    Column,
    Connection,
    Engine,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    select,
    text,
)

from alembic import command
from app.core.config import get_settings
from app.core.database import Base
from app.core.module_types import MigrationSpec, ModuleSpec
from app.core.modules import ModuleRecord, ModuleRegistry, module_registry

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGED_ALEMBIC_ROOT = PROJECT_ROOT / "netsanctum_alembic"
LEGACY_ALEMBIC_ROOT = PACKAGED_ALEMBIC_ROOT if PACKAGED_ALEMBIC_ROOT.is_dir() else PROJECT_ROOT / "alembic"
MODULE_ENV_ROOT = LEGACY_ALEMBIC_ROOT / "module_env"
LEGACY_HEAD = "i3j4k5l6m7n8"
MIGRATION_LOCK_ID = 5640003949526500692
OWNERSHIP_TABLE = "netsanctum_migration_ownership"


@dataclass(frozen=True, slots=True)
class ModuleMigration:
    module_id: str
    package: str
    spec: ModuleSpec
    migration: MigrationSpec
    versions_path: Path

    @property
    def version_table(self) -> str:
        return f"alembic_version_{self.module_id}"


def _module_migration(record: ModuleRecord) -> ModuleMigration | None:
    spec = record.spec
    if not spec or not spec.migrations:
        return None
    package_module = importlib.import_module(record.package)
    package_file = getattr(package_module, "__file__", None)
    if not package_file:
        raise RuntimeError(f"Module {record.id!r} package has no filesystem path")
    versions_path = Path(package_file).resolve().parent / spec.migrations.path / "versions"
    if not versions_path.is_dir():
        raise RuntimeError(f"Module {record.id!r} migration path is missing: {versions_path}")
    return ModuleMigration(record.id, record.package, spec, spec.migrations, versions_path)


def installed_migrations(registry: ModuleRegistry = module_registry) -> list[ModuleMigration]:
    return [
        migration
        for record in registry.installed_records()
        if (migration := _module_migration(record)) is not None
    ]


def _legacy_config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(LEGACY_ALEMBIC_ROOT))
    config.attributes["connection"] = connection
    return config


def _module_metadata(migration: ModuleMigration):
    if migration.spec.models:
        models_module = importlib.import_module(migration.spec.models)
    else:
        raise RuntimeError(f"Module {migration.module_id!r} migrations require models")
    model_tables = {
        value.__table__.name
        for value in vars(models_module).values()
        if isinstance(value, type)
        and value.__module__ == models_module.__name__
        and isinstance(getattr(value, "__table__", None), Table)
    }
    model_tables.update(value.name for value in vars(models_module).values() if isinstance(value, Table))
    declared_tables = set(migration.migration.tables)
    if model_tables != declared_tables:
        missing = model_tables - declared_tables
        stale = declared_tables - model_tables
        details = []
        if missing:
            details.append("undeclared model tables: " + ", ".join(sorted(missing)))
        if stale:
            details.append("tables missing from models: " + ", ".join(sorted(stale)))
        raise RuntimeError(
            f"Module {migration.module_id!r} migration ownership mismatch; " + "; ".join(details)
        )
    return Base.metadata


def _module_config(connection: Connection, migration: ModuleMigration) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MODULE_ENV_ROOT))
    config.set_main_option("version_locations", str(migration.versions_path))
    config.set_main_option("version_path_separator", "os")
    config.attributes.update(
        {
            "connection": connection,
            "database_url": str(connection.engine.url),
            "target_metadata": _module_metadata(migration),
            "version_table": migration.version_table,
            "owned_tables": migration.migration.owned_tables,
        }
    )
    return config


def _register_table_ownership(connection: Connection, registry: ModuleRegistry) -> None:
    metadata = MetaData()
    ownership = Table(
        OWNERSHIP_TABLE,
        metadata,
        Column("table_name", String(63), primary_key=True),
        Column("module_id", String(63), nullable=False),
    )
    metadata.create_all(connection, tables=[ownership], checkfirst=True)
    existing = dict(connection.execute(select(ownership.c.table_name, ownership.c.module_id)).tuples().all())
    for record in registry.declared_records():
        if not record.spec or not record.spec.migrations:
            continue
        for table_name in record.spec.migrations.owned_tables:
            owner = existing.get(table_name)
            if owner and owner != record.id:
                raise RuntimeError(
                    f"Migration table {table_name!r} is permanently owned by module {owner!r}, "
                    f"not {record.id!r}"
                )
            if not owner:
                connection.execute(ownership.insert().values(table_name=table_name, module_id=record.id))
                existing[table_name] = record.id


def _adopt_or_upgrade(
    connection: Connection,
    migration: ModuleMigration,
    *,
    legacy_database: bool,
) -> None:
    inspector = inspect(connection)
    config = _module_config(connection, migration)
    if not inspector.has_table(migration.version_table):
        existing_owned = {table for table in migration.migration.owned_tables if inspector.has_table(table)}
        legacy_tables = set(migration.migration.legacy_tables)
        existing_legacy = {table for table in legacy_tables if inspector.has_table(table)}
        if existing_owned and not legacy_database:
            raise RuntimeError(
                f"Module {migration.module_id!r} has unmanaged tables without a legacy migration marker"
            )
        if existing_legacy and existing_legacy != legacy_tables:
            missing = legacy_tables - existing_legacy
            raise RuntimeError(
                f"Cannot adopt partial schema for module {migration.module_id!r}; missing tables: "
                + ", ".join(sorted(missing))
            )
        if existing_legacy:
            command.stamp(config, migration.migration.baseline_revision)
    command.upgrade(config, "head")
    try:
        command.check(config)
    except Exception as exc:
        raise RuntimeError(
            f"Module {migration.module_id!r} schema does not match its migration head"
        ) from exc


@contextmanager
def migration_transaction(engine: Engine):
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": MIGRATION_LOCK_ID},
            )
        yield connection


def upgrade_database(
    engine: Engine,
    registry: ModuleRegistry = module_registry,
    module_id: str | None = None,
) -> None:
    migrations = installed_migrations(registry)
    if module_id:
        migrations = [migration for migration in migrations if migration.module_id == module_id]
        if not migrations:
            raise RuntimeError(f"Installed module {module_id!r} has no migrations")

    with migration_transaction(engine) as connection:
        _register_table_ownership(connection, registry)
        legacy_database = inspect(connection).has_table("alembic_version")
        needs_adoption = any(
            not inspect(connection).has_table(migration.version_table) for migration in migrations
        )
        if legacy_database and needs_adoption:
            command.upgrade(_legacy_config(connection), LEGACY_HEAD)
        for migration in migrations:
            _adopt_or_upgrade(
                connection,
                migration,
                legacy_database=legacy_database,
            )


def downgrade_module(
    engine: Engine,
    module_id: str,
    revision: str,
    registry: ModuleRegistry = module_registry,
) -> None:
    migration = next(
        (item for item in installed_migrations(registry) if item.module_id == module_id),
        None,
    )
    if not migration:
        raise RuntimeError(f"Installed module {module_id!r} has no migrations")
    with migration_transaction(engine) as connection:
        command.downgrade(_module_config(connection, migration), revision)


def check_migrations(
    engine: Engine,
    registry: ModuleRegistry = module_registry,
    module_id: str | None = None,
) -> None:
    migrations = installed_migrations(registry)
    if module_id:
        migrations = [migration for migration in migrations if migration.module_id == module_id]
        if not migrations:
            raise RuntimeError(f"Installed module {module_id!r} has no migrations")
    with migration_transaction(engine) as connection:
        for migration in migrations:
            command.check(_module_config(connection, migration))


def create_revision(
    engine: Engine,
    module_id: str,
    message: str,
    registry: ModuleRegistry = module_registry,
    version_path: Path | None = None,
) -> None:
    migration = next(
        (item for item in installed_migrations(registry) if item.module_id == module_id),
        None,
    )
    if not migration:
        raise RuntimeError(f"Installed module {module_id!r} has no migrations")
    if not migration.package.startswith("app.modules.") and version_path is None:
        raise RuntimeError("External modules must provide --version-path to their source migration directory")
    if version_path is not None:
        resolved_path = version_path.resolve()
        if not resolved_path.is_dir():
            raise RuntimeError(f"Migration version path does not exist: {resolved_path}")
        migration = ModuleMigration(
            migration.module_id,
            migration.package,
            migration.spec,
            migration.migration,
            resolved_path,
        )
    with migration_transaction(engine) as connection:
        command.revision(
            _module_config(connection, migration),
            message=message,
            autogenerate=True,
            version_path=str(migration.versions_path),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    upgrade_parser = subparsers.add_parser("upgrade")
    upgrade_parser.add_argument("module", nargs="?")

    downgrade_parser = subparsers.add_parser("downgrade")
    downgrade_parser.add_argument("module")
    downgrade_parser.add_argument("revision")

    revision_parser = subparsers.add_parser("revision")
    revision_parser.add_argument("module")
    revision_parser.add_argument("-m", "--message", required=True)
    revision_parser.add_argument("--version-path", type=Path)

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("module", nargs="?")

    args = parser.parse_args()
    engine = create_engine(get_settings().DATABASE_URL_SYNC)
    try:
        if args.command == "upgrade":
            upgrade_database(engine, module_id=args.module)
        elif args.command == "downgrade":
            downgrade_module(engine, args.module, args.revision)
        elif args.command == "revision":
            create_revision(engine, args.module, args.message, version_path=args.version_path)
        else:
            check_migrations(engine, module_id=args.module)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
