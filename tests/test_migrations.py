import hashlib
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.core.database import Base
from app.core.migrations import (
    LEGACY_HEAD,
    PROJECT_ROOT,
    _legacy_config,
    check_migrations,
    downgrade_module,
    installed_migrations,
    upgrade_database,
)
from app.core.module_types import MigrationSpec, ModuleSpec
from app.core.modules import ModuleRecord, ModuleRegistry, ModuleStatus


class ModuleMigrationTests(unittest.TestCase):
    def make_engine(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        database_path = Path(temporary_directory.name) / "test.db"
        engine = create_engine(f"sqlite:///{database_path}")
        self.addCleanup(engine.dispose)
        return engine

    def test_fresh_install_creates_only_installed_module_schemas(self):
        engine = self.make_engine()
        registry = ModuleRegistry.discover(installed_modules={"music"})

        upgrade_database(engine, registry)

        tables = set(inspect(engine).get_table_names())
        self.assertLessEqual(
            {
                "settings",
                "songs",
                "playlists",
                "playlist_songs",
                "alembic_version_settings",
                "alembic_version_music",
            },
            tables,
        )
        self.assertNotIn("vault_items", tables)
        self.assertNotIn("archived_videos", tables)

    def test_full_fresh_install_reaches_every_module_head(self):
        engine = self.make_engine()
        registry = ModuleRegistry.discover()

        upgrade_database(engine, registry)

        with engine.connect() as connection:
            revisions = {
                migration.module_id: connection.execute(
                    text(f"SELECT version_num FROM {migration.version_table}")
                ).scalar_one()
                for migration in installed_migrations(registry)
            }
        self.assertEqual("video_0002", revisions["video_archiver"])
        self.assertEqual("music_0001", revisions["music"])

    def test_disabled_installed_module_is_still_migrated(self):
        engine = self.make_engine()
        registry = ModuleRegistry.discover(
            enabled_modules=set(),
            installed_modules={"music"},
        )

        upgrade_database(engine, registry)

        self.assertIn("songs", inspect(engine).get_table_names())

    def test_upgrade_is_idempotent_and_preserves_data(self):
        engine = self.make_engine()
        registry = ModuleRegistry.discover(installed_modules={"music"})
        upgrade_database(engine, registry)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO settings "
                    "(scope, key, value, value_type, is_secret, created_at, updated_at) "
                    "VALUES ('global', 'sentinel', 'kept', 'string', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )

        upgrade_database(engine, registry)
        check_migrations(engine, registry)

        with engine.connect() as connection:
            value = connection.execute(text("SELECT value FROM settings WHERE key = 'sentinel'")).scalar_one()
        self.assertEqual("kept", value)

    def test_module_can_be_downgraded_and_reinstalled_independently(self):
        engine = self.make_engine()
        registry = ModuleRegistry.discover(installed_modules={"music"})
        upgrade_database(engine, registry)

        downgrade_module(engine, "music", "base", registry)

        tables = set(inspect(engine).get_table_names())
        self.assertNotIn("songs", tables)
        self.assertIn("settings", tables)

        upgrade_database(engine, registry)
        self.assertIn("songs", inspect(engine).get_table_names())

    def test_legacy_head_is_adopted_without_recreating_tables(self):
        engine = self.make_engine()
        all_modules = ModuleRegistry.discover()
        all_modules.import_models(include_disabled=True, strict=True)
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
            )
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": LEGACY_HEAD},
            )
            connection.execute(
                text(
                    "INSERT INTO settings "
                    "(scope, key, value, value_type, is_secret, created_at, updated_at) "
                    "VALUES ('global', 'sentinel', 'kept', 'string', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
        registry = ModuleRegistry.discover(installed_modules={"music"})

        upgrade_database(engine, registry)

        tables = set(inspect(engine).get_table_names())
        self.assertIn("alembic_version", tables)
        self.assertIn("alembic_version_settings", tables)
        self.assertIn("alembic_version_music", tables)
        with engine.connect() as connection:
            self.assertEqual(
                "kept",
                connection.execute(text("SELECT value FROM settings WHERE key = 'sentinel'")).scalar_one(),
            )
            self.assertEqual(
                "music_0001",
                connection.execute(text("SELECT version_num FROM alembic_version_music")).scalar_one(),
            )

    def test_unmanaged_module_schema_fails_closed(self):
        engine = self.make_engine()
        registry = ModuleRegistry.discover(installed_modules={"music"})
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE songs (id INTEGER PRIMARY KEY)"))

        with self.assertRaisesRegex(RuntimeError, "unmanaged tables"):
            upgrade_database(engine, registry)

    def test_partial_legacy_module_schema_fails_closed(self):
        engine = self.make_engine()
        registry = ModuleRegistry.discover(installed_modules={"music"})
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE songs (id INTEGER PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": LEGACY_HEAD},
            )

        with self.assertRaisesRegex(RuntimeError, "partial schema"):
            upgrade_database(engine, registry)

    def test_incompatible_legacy_schema_is_not_stamped(self):
        engine = self.make_engine()
        registry = ModuleRegistry.discover(installed_modules={"music"})
        with engine.begin() as connection:
            for table in ("songs", "playlists", "playlist_songs"):
                connection.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": LEGACY_HEAD},
            )

        with self.assertRaisesRegex(RuntimeError, "schema does not match"):
            upgrade_database(engine, registry)

    def test_all_migration_manifests_resolve_to_version_directories(self):
        registry = ModuleRegistry.discover()

        migrations = installed_migrations(registry)

        self.assertEqual(
            {"alllib", "music", "settings", "vault", "video_archiver"},
            {migration.module_id for migration in migrations},
        )
        for migration in migrations:
            self.assertTrue(migration.versions_path.is_dir())
            self.assertLessEqual(len(migration.version_table), 63)

    def test_legacy_compatibility_head_is_immutable(self):
        config = Config(str(PROJECT_ROOT / "alembic.ini"))

        self.assertEqual(LEGACY_HEAD, ScriptDirectory.from_config(config).get_current_head())

    def test_legacy_compatibility_files_are_immutable(self):
        digest = hashlib.sha256()
        for path in sorted((PROJECT_ROOT / "alembic" / "versions").glob("*.py")):
            digest.update(path.name.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")

        self.assertEqual(
            "37052d5bd0b3ea6011b239faf64fa4b820110c3764edf700499844af0eb9c46a",
            digest.hexdigest(),
        )

    def test_legacy_config_is_independent_from_working_directory(self):
        engine = self.make_engine()
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        original_directory = Path.cwd()
        self.addCleanup(os.chdir, original_directory)
        os.chdir(temporary_directory.name)

        with engine.connect() as connection:
            config = _legacy_config(connection)

        self.assertEqual(LEGACY_HEAD, ScriptDirectory.from_config(config).get_current_head())

    def test_migration_contract_rejects_framework_table_names(self):
        with self.assertRaisesRegex(ValueError, "reserved"):
            MigrationSpec(
                path="migrations",
                baseline_revision="example_0001",
                tables=("alembic_version_example",),
            )

    def test_migration_contract_rejects_unsafe_paths(self):
        with self.assertRaisesRegex(ValueError, "package-relative"):
            MigrationSpec(
                path="../migrations",
                baseline_revision="example_0001",
                tables=("example_items",),
            )

    def test_table_ownership_survives_module_removal(self):
        engine = self.make_engine()
        music_registry = ModuleRegistry.discover(installed_modules={"music"})
        upgrade_database(engine, music_registry)

        replacement_registry = ModuleRegistry()
        replacement_spec = ModuleSpec(
            id="replacement",
            version="1.0.0",
            title_en="Replacement",
            title_ru="Replacement",
            models="app.modules.vault.models",
            migrations=MigrationSpec(
                path="migrations",
                baseline_revision="vault_0001",
                tables=("songs",),
            ),
        )
        replacement_registry._records[replacement_spec.id] = ModuleRecord(
            package="app.modules.vault",
            spec=replacement_spec,
            status=ModuleStatus.ACTIVE,
        )

        with self.assertRaisesRegex(RuntimeError, "permanently owned"):
            upgrade_database(engine, replacement_registry)


@unittest.skipUnless(
    os.getenv("MIGRATION_TEST_DATABASE_URL"),
    "MIGRATION_TEST_DATABASE_URL is not configured",
)
class PostgresMigrationSmokeTests(unittest.TestCase):
    def test_fresh_install_and_complete_legacy_adoption(self):
        database_url = os.environ["MIGRATION_TEST_DATABASE_URL"]
        self.assertTrue(database_url.rsplit("/", 1)[-1].endswith("_test"))
        engine = create_engine(database_url)
        self.addCleanup(engine.dispose)
        registry = ModuleRegistry.discover()

        self.reset_schema(engine)
        upgrade_database(engine, registry)
        check_migrations(engine, registry)
        self.assertIn("alembic_version_music", inspect(engine).get_table_names())

        self.reset_schema(engine)
        legacy_config = Config(str(PROJECT_ROOT / "alembic.ini"))
        legacy_config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(legacy_config, LEGACY_HEAD)
        with engine.begin() as connection:
            statements = (
                "INSERT INTO settings "
                "(scope, key, value, value_type, is_secret, created_at, updated_at) "
                "VALUES ('global', 'sentinel', 'kept', 'string', false, NOW(), NOW())",
                "INSERT INTO songs (title, audio_file_id, created_at) "
                "VALUES ('Legacy song', 'music/audio/legacy.mp3', NOW())",
                "INSERT INTO lib_media (site_id, media_type, title, slug, created_at) "
                "VALUES (3, 'novel', 'Legacy novel', 'legacy-novel', NOW())",
                "INSERT INTO video_channels (id, name, platform) "
                "VALUES ('legacy-channel', 'Legacy channel', 'youtube')",
                "INSERT INTO archived_videos "
                "(id, title, platform, channel_id, channel_name, duration, resolution) "
                "VALUES ('legacy-video', 'Legacy video', 'youtube', 'legacy-channel', "
                "'Legacy channel', 60, '720p')",
                "INSERT INTO vault_collections (name) VALUES ('Legacy collection')",
                "INSERT INTO vault_items (title, tags) VALUES ('Legacy note', '[]')",
            )
            for statement in statements:
                connection.execute(text(statement))

        upgrade_database(engine, registry)
        check_migrations(engine, registry)

        with engine.connect() as connection:
            self.assertEqual(
                "kept",
                connection.execute(text("SELECT value FROM settings WHERE key = 'sentinel'")).scalar_one(),
            )
            self.assertEqual(
                "music_0001",
                connection.execute(text("SELECT version_num FROM alembic_version_music")).scalar_one(),
            )
            self.assertEqual(
                "video_0002",
                connection.execute(
                    text("SELECT version_num FROM alembic_version_video_archiver")
                ).scalar_one(),
            )
            for table in ("songs", "lib_media", "archived_videos", "vault_items"):
                self.assertEqual(
                    1,
                    connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one(),
                )

    def test_concurrent_startup_is_serialized(self):
        engine = self.make_engine()
        registry = ModuleRegistry.discover()
        self.reset_schema(engine)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(upgrade_database, engine, registry) for _ in range(2)]
            for future in futures:
                future.result()

        self.assertIn("alembic_version_music", inspect(engine).get_table_names())

    def test_failed_adoption_rolls_back_framework_state(self):
        engine = self.make_engine()
        registry = ModuleRegistry.discover(installed_modules={"music"})
        self.reset_schema(engine)
        with engine.begin() as connection:
            for table in ("songs", "playlists", "playlist_songs"):
                connection.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": LEGACY_HEAD},
            )

        with self.assertRaisesRegex(RuntimeError, "schema does not match"):
            upgrade_database(engine, registry)

        tables = set(inspect(engine).get_table_names())
        self.assertNotIn("alembic_version_music", tables)
        self.assertNotIn("netsanctum_migration_ownership", tables)

    def make_engine(self):
        database_url = os.environ["MIGRATION_TEST_DATABASE_URL"]
        self.assertTrue(database_url.rsplit("/", 1)[-1].endswith("_test"))
        engine = create_engine(database_url)
        self.addCleanup(engine.dispose)
        return engine

    @staticmethod
    def reset_schema(engine) -> None:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))


if __name__ == "__main__":
    unittest.main()
