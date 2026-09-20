import asyncio
import hashlib
import io
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import BigInteger, create_engine, inspect, text

from app.core.migrations import downgrade_module, upgrade_database
from app.core.modules import ModuleRegistry
from app.modules.music import capabilities, router, tasks
from app.modules.music.models import Song


def build_manifest(**kwargs):
    return {
        "package_id": kwargs["package_id"],
        "root_url": kwargs["root_url"],
        "resources": kwargs["resources"],
    }


class Result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class Database:
    def __init__(self, entity, *results):
        self.entity = entity
        self.results = list(results)
        self.statements = []
        self.commits = 0

    async def get(self, model, item_id):
        return self.entity

    async def execute(self, statement):
        self.statements.append(statement)
        return Result(self.results.pop(0))

    async def commit(self):
        self.commits += 1


class MemoryStorage:
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.opened = []
        self.deleted = []

    def file_exists(self, path):
        return path in self.files

    def get_file_stream(self, path):
        self.opened.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return io.BytesIO(self.files[path])

    def save_file_from_path(self, source_path, destination):
        self.files[destination] = Path(source_path).read_bytes()
        return destination

    def delete_file(self, path):
        self.deleted.append(path)
        return self.files.pop(path, None) is not None


def make_song(song_id, content=b"audio", *, known=True, cover_file_id=None):
    return SimpleNamespace(
        id=song_id,
        title=f"Song {song_id}",
        author="Artist",
        audio_file_id=f"music/audio/{song_id}.mp3",
        audio_file_size=len(content) if known else None,
        audio_sha256=hashlib.sha256(content).hexdigest() if known else None,
        cover_file_id=cover_file_id,
    )


class MusicOfflineContractTests(unittest.TestCase):
    def test_music_0004_upgrades_and_downgrades_defensively(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_engine(f"sqlite:///{Path(temp_dir) / 'music.db'}")
            self.addCleanup(engine.dispose)
            registry = ModuleRegistry.discover(installed_modules={"music"})

            upgrade_database(engine, registry)
            upgrade_database(engine, registry)
            columns = {column["name"] for column in inspect(engine).get_columns("songs")}
            self.assertLessEqual({"audio_file_size", "audio_sha256"}, columns)
            with engine.connect() as connection:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version_music")
                ).scalar_one()
            self.assertEqual("music_0004", revision)

            downgrade_module(engine, "music", "music_0003", registry)
            columns = {column["name"] for column in inspect(engine).get_columns("songs")}
            self.assertNotIn("audio_file_size", columns)
            self.assertNotIn("audio_sha256", columns)

    def test_song_model_uses_nullable_bigint_and_sha256_columns(self):
        size_column = Song.__table__.c.audio_file_size
        hash_column = Song.__table__.c.audio_sha256

        self.assertIsInstance(size_column.type, BigInteger)
        self.assertTrue(size_column.nullable)
        self.assertEqual(64, hash_column.type.length)
        self.assertTrue(hash_column.nullable)

        migration = import_module("app.modules.music.migrations.versions.music_0004_add_audio_metadata")
        self.assertEqual("music_0004", migration.revision)
        self.assertEqual("music_0003", migration.down_revision)

    def test_audio_ingestion_streams_from_path_and_returns_exact_identity(self):
        content = b"streamed audio content"
        storage = MemoryStorage()
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "audio.mp3"
            source.write_bytes(content)

            file_id, size, sha256 = tasks._save_audio_file(storage, source, "music/audio/new.mp3")

        self.assertEqual("music/audio/new.mp3", file_id)
        self.assertEqual(len(content), size)
        self.assertEqual(hashlib.sha256(content).hexdigest(), sha256)
        self.assertEqual(content, storage.files[file_id])

    def test_known_audio_identity_is_emitted_without_storage_read(self):
        song = make_song(3, b"known")

        resource = router._song_audio_resource(song)

        self.assertEqual(len(b"known"), resource["size"])
        self.assertEqual(hashlib.sha256(b"known").hexdigest(), resource["sha256"])

    def test_legacy_audio_is_streamed_once_and_persisted_during_manifest_generation(self):
        content = b"legacy audio"
        song = make_song(4, content, known=False)
        db = Database(song)
        storage = MemoryStorage({song.audio_file_id: content})

        with (
            patch.object(router, "get_storage", return_value=storage),
            patch("app.core.packages_router.make_package_manifest", side_effect=build_manifest),
        ):
            manifest = asyncio.run(router.get_song_sync_manifest(4, db=db, user=None, hybrid=False))

        audio = next(resource for resource in manifest["resources"] if resource["type"] == "binary")
        self.assertEqual(len(content), audio["size"])
        self.assertEqual(hashlib.sha256(content).hexdigest(), audio["sha256"])
        self.assertEqual([song.audio_file_id], storage.opened)
        self.assertEqual(1, db.commits)
        self.assertEqual(audio["sha256"], song.audio_sha256)

    def test_missing_legacy_audio_keeps_resource_without_identity(self):
        song = make_song(5, known=False)
        db = Database(song)

        with (
            patch.object(router, "get_storage", return_value=MemoryStorage()),
            patch("app.core.packages_router.make_package_manifest", side_effect=build_manifest),
        ):
            manifest = asyncio.run(router.get_song_sync_manifest(5, db=db, user=None, hybrid=False))

        audio = next(resource for resource in manifest["resources"] if resource["type"] == "binary")
        self.assertNotIn("size", audio)
        self.assertNotIn("sha256", audio)
        self.assertEqual(0, db.commits)

    def test_playlist_manifest_is_a_stable_full_snapshot_for_add_change_and_remove(self):
        playlist = SimpleNamespace(id=9, name="Snapshot", cover_path="music/playlist-covers/9.webp")
        storage = MemoryStorage({playlist.cover_path: b"cover"})

        async def manifest_for(songs):
            db = Database(playlist, songs)
            with (
                patch.object(router, "get_storage", return_value=storage),
                patch("app.core.packages_router.make_package_manifest", side_effect=build_manifest),
            ):
                return await router.get_playlist_sync_manifest(9, db=db, user=None, hybrid=False)

        song_1 = make_song(1, b"one")
        song_2 = make_song(2, b"two")
        initial = asyncio.run(manifest_for([song_1, song_2]))
        added = asyncio.run(manifest_for([song_1, song_2, make_song(3, b"three")]))
        changed = asyncio.run(manifest_for([song_1, make_song(2, b"two changed"), make_song(3, b"three")]))
        removed = asyncio.run(manifest_for([make_song(2, b"two changed"), make_song(3, b"three")]))

        def audio_map(manifest):
            return {
                resource["url"]: resource["sha256"]
                for resource in manifest["resources"]
                if resource["type"] == "binary"
            }

        self.assertEqual(["/music/audio/1", "/music/audio/2"], list(audio_map(initial)))
        self.assertIn("/music/audio/3", audio_map(added))
        self.assertNotEqual(audio_map(added)["/music/audio/2"], audio_map(changed)["/music/audio/2"])
        self.assertNotIn("/music/audio/1", audio_map(removed))

    def test_playlist_backfill_is_sequential_and_order_has_song_id_tie_breaker(self):
        songs = [make_song(2, b"two", known=False), make_song(7, b"seven", known=False)]
        playlist = SimpleNamespace(id=6, name="Ordered", cover_path="cover.webp")
        storage = MemoryStorage(
            {
                "cover.webp": b"cover",
                songs[0].audio_file_id: b"two",
                songs[1].audio_file_id: b"seven",
            }
        )
        db = Database(playlist, songs)

        with (
            patch.object(router, "get_storage", return_value=storage),
            patch("app.core.packages_router.make_package_manifest", side_effect=build_manifest),
        ):
            asyncio.run(router.get_playlist_sync_manifest(6, db=db, user=None, hybrid=False))

        self.assertEqual([songs[0].audio_file_id, songs[1].audio_file_id], storage.opened)
        statement = str(db.statements[0])
        self.assertIn("playlist_songs.position ASC", statement)
        self.assertIn("songs.id ASC", statement)
        self.assertEqual(1, db.commits)

    def test_playlist_cover_is_generated_before_manifest(self):
        playlist = SimpleNamespace(id=8, name="Cover", cover_path=None)
        song = make_song(1)
        db = Database(playlist, [song])

        async def generate_cover(database, target):
            target.cover_path = "music/playlist-covers/8.webp"

        with (
            patch.object(router, "_regenerate_playlist_cover", side_effect=generate_cover),
            patch("app.core.packages_router.make_package_manifest", side_effect=build_manifest),
        ):
            manifest = asyncio.run(router.get_playlist_sync_manifest(8, db=db, user=None, hybrid=False))

        urls = [resource["url"] for resource in manifest["resources"]]
        self.assertIn("/music/playlists/8/cover", urls)
        self.assertEqual(1, db.commits)

    def test_resolver_accepts_only_canonical_ids_and_verifies_returned_package_id(self):
        song_manifest = {"package_id": "song_7", "resources": [{"url": "/music/audio/7"}]}
        with patch.object(router, "get_song_sync_manifest", AsyncMock(return_value=song_manifest)):
            resources = asyncio.run(capabilities.resolve_package_resources("song_7", object()))
        self.assertEqual(song_manifest["resources"], resources)

        for invalid in ("song_0", "song_01", "song_-1", "song_7_extra", "playlist_", "video_7"):
            with self.subTest(package_id=invalid), self.assertRaises(HTTPException):
                asyncio.run(capabilities.resolve_package_resources(invalid, object()))

        with (
            patch.object(
                router,
                "get_song_sync_manifest",
                AsyncMock(return_value={"package_id": "song_8", "resources": []}),
            ),
            self.assertRaises(HTTPException),
        ):
            asyncio.run(capabilities.resolve_package_resources("song_7", object()))

    def test_playlist_manifest_and_templates_keep_package_scoped_detail_urls(self):
        playlist = SimpleNamespace(id=12, name="URLs", cover_path="cover.webp")
        db = Database(playlist, [])
        with (
            patch.object(router, "get_storage", return_value=MemoryStorage({"cover.webp": b"cover"})),
            patch("app.core.packages_router.make_package_manifest", side_effect=build_manifest),
        ):
            manifest = asyncio.run(router.get_playlist_sync_manifest(12, db=db, user=None, hybrid=False))

        urls = {resource["url"] for resource in manifest["resources"]}
        detail_url = "/music/ui/playlists/12?package_id=playlist_12"
        self.assertIn(detail_url, urls)
        self.assertEqual("/music/dashboard?package_id=playlist_12", manifest["root_url"])

        templates = Path(__file__).parents[1] / "app/modules/music/templates"
        self.assertIn("?package_id={{ package_id }}", (templates / "playlists.html").read_text())
        self.assertIn("?package_id={{ package_id }}", (templates / "playlist_detail.html").read_text())

    def test_delete_song_removes_unreferenced_audio_and_cover(self):
        song = make_song(15, cover_file_id="music/covers/15.jpg")
        storage = MemoryStorage({song.audio_file_id: b"audio", song.cover_file_id: b"cover"})

        class DeleteDatabase(Database):
            async def delete(self, entity):
                self.deleted = entity

            async def flush(self):
                pass

            async def scalar(self, statement):
                return None

        db = DeleteDatabase(song, [])
        with patch.object(router, "get_storage", return_value=storage):
            asyncio.run(router.delete_song_ui(15, db=db, user=None))

        self.assertEqual([song.audio_file_id, song.cover_file_id], storage.deleted)
        self.assertEqual(1, db.commits)


if __name__ == "__main__":
    unittest.main()
