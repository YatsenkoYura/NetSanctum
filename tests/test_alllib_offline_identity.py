import asyncio
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text

from app.core.migrations import installed_migrations, upgrade_database
from app.core.modules import ModuleRegistry
from app.modules.alllib.capabilities import resolve_package_resources
from app.modules.alllib.router import export_media, get_media_sync_manifest


class MemoryStorage:
    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})
        self.reads = 0

    def file_exists(self, path: str) -> bool:
        return path in self.files

    def save_stream(self, stream, path: str) -> str:
        self.files[path] = stream.read()
        return path

    def save_file_encrypted(self, data: bytes, path: str) -> str:
        self.files[path] = data
        return path

    def get_file_stream(self, path: str):
        self.reads += 1
        if path not in self.files:
            raise FileNotFoundError(path)
        return io.BytesIO(self.files[path])

    def get_file_stream_decrypted(self, path: str):
        return self.get_file_stream(path)

    def get_file_size(self, path: str) -> int:
        return len(self.files[path])

    def get_encrypted_plaintext_size(self, path: str) -> int:
        return len(self.files[path])


class Result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return list(self.values)


class Database:
    def __init__(self, media, chapters):
        self.media = media
        self.chapters = chapters
        self.commits = 0

    async def get(self, model, item_id):
        return self.media if item_id == self.media.id else None

    async def execute(self, statement):
        return Result(self.chapters)

    async def commit(self):
        self.commits += 1


def make_media(media_type: str = "novel"):
    return SimpleNamespace(
        id=7,
        media_type=media_type,
        title="Example",
        eng_name=None,
        rus_name=None,
        description="Description",
        slug="example",
        site_id=3 if media_type == "novel" else 1,
        cover_path=None,
        export_path=None,
        export_size=None,
        export_sha256=None,
    )


def make_chapter(chapter_id: int, content: str = ""):
    return SimpleNamespace(
        id=chapter_id,
        volume="1",
        number=str(chapter_id),
        volume_int=1,
        number_float=float(chapter_id),
        name=None,
        content_html=content,
        pages_list=None,
        video_path=None,
        video_size=None,
        video_sha256=None,
    )


def binary_resource(manifest: dict) -> dict:
    return next(resource for resource in manifest["resources"] if resource["type"] == "binary")


async def response_body(response) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


class AllLibOfflineIdentityTests(unittest.TestCase):
    def test_offline_detail_uses_the_snapshotted_export_artifact(self):
        template = (Path(__file__).parents[1] / "app/modules/alllib/templates/alllib_detail.html").read_text()

        self.assertIn("media.export_sha256", template)
        self.assertIn("/export?artifact=", template)

    def test_alllib_binary_identity_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(f"sqlite:///{Path(directory) / 'alllib.db'}")
            self.addCleanup(engine.dispose)
            registry = ModuleRegistry.discover(installed_modules={"alllib"})

            upgrade_database(engine, registry)

            inspector = inspect(engine)
            media_columns = {column["name"] for column in inspector.get_columns("lib_media")}
            chapter_columns = {column["name"] for column in inspector.get_columns("lib_chapters")}
            migration = next(item for item in installed_migrations(registry) if item.module_id == "alllib")
            with engine.connect() as connection:
                revision = connection.execute(
                    text(f"SELECT version_num FROM {migration.version_table}")
                ).scalar_one()

        self.assertEqual("alllib_0002", revision)
        self.assertLessEqual({"export_path", "export_size", "export_sha256"}, media_columns)
        self.assertLessEqual({"video_size", "video_sha256"}, chapter_columns)

    def test_real_epub_and_cbz_snapshots_are_deterministic_and_exact(self):
        for media_type in ("novel", "manga"):
            with self.subTest(media_type=media_type):
                media = make_media(media_type)
                chapter = make_chapter(1, "<p>Chapter text</p>")
                storage = MemoryStorage()
                if media_type == "manga":
                    chapter.pages_list = ["alllib/manga/page-2.jpg", "alllib/manga/page-1.jpg"]
                    storage.files.update(
                        {
                            "alllib/manga/page-2.jpg": b"second-page",
                            "alllib/manga/page-1.jpg": b"first-page",
                        }
                    )
                db = Database(media, [chapter])

                with (
                    patch("app.modules.alllib.router.get_storage", return_value=storage),
                    patch("app.modules.alllib.epub_builder.get_storage", return_value=storage),
                ):
                    first = asyncio.run(get_media_sync_manifest(7, db=db, user=None, hybrid=False))
                    first_resource = binary_resource(first)
                    artifact = storage.files[media.export_path]
                    second = asyncio.run(get_media_sync_manifest(7, db=db, user=None, hybrid=False))

                self.assertEqual(len(artifact), first_resource["size"])
                self.assertEqual(hashlib.sha256(artifact).hexdigest(), first_resource["sha256"])
                self.assertEqual(first_resource, binary_resource(second))

    def test_anime_known_and_legacy_video_identity(self):
        known_bytes = b"known-video"
        known = make_chapter(1)
        known.video_path = "alllib/anime/known.mp4"
        known.video_size = len(known_bytes)
        known.video_sha256 = hashlib.sha256(known_bytes).hexdigest()
        media = make_media("anime")
        media.site_id = 6
        storage = MemoryStorage({known.video_path: known_bytes})
        db = Database(media, [known])

        with patch("app.modules.alllib.router.get_storage", return_value=storage):
            manifest = asyncio.run(get_media_sync_manifest(7, db=db, user=None, hybrid=False))

        resource = binary_resource(manifest)
        self.assertEqual(len(known_bytes), resource["size"])
        self.assertEqual(hashlib.sha256(known_bytes).hexdigest(), resource["sha256"])
        self.assertEqual(0, storage.reads)
        self.assertEqual(0, db.commits)

        legacy_bytes = b"legacy-video-content"
        legacy = make_chapter(2)
        legacy.video_path = "alllib/anime/legacy.mp4"
        db.chapters = [legacy]
        storage.files[legacy.video_path] = legacy_bytes

        with patch("app.modules.alllib.router.get_storage", return_value=storage):
            first = asyncio.run(get_media_sync_manifest(7, db=db, user=None, hybrid=False))
            second = asyncio.run(get_media_sync_manifest(7, db=db, user=None, hybrid=False))

        expected_hash = hashlib.sha256(legacy_bytes).hexdigest()
        self.assertEqual(len(legacy_bytes), binary_resource(first)["size"])
        self.assertEqual(expected_hash, binary_resource(first)["sha256"])
        self.assertEqual(binary_resource(first), binary_resource(second))
        self.assertEqual(1, storage.reads)
        self.assertEqual(1, db.commits)

    def test_export_snapshot_exact_bytes_updates_and_resolver_reuses_it(self):
        media = make_media()
        chapters = [make_chapter(2, "second"), make_chapter(1, "first")]
        db = Database(media, chapters)
        storage = MemoryStorage()
        build_calls = []

        def build_export(current_media, current_chapters):
            payload = "|".join(
                f"{chapter.id}:{chapter.content_html}" for chapter in current_chapters
            ).encode()
            build_calls.append(payload)
            return payload, "application/epub+zip", "Example.epub"

        with (
            patch("app.modules.alllib.router.get_storage", return_value=storage),
            patch("app.core.responses.get_storage", return_value=storage),
            patch("app.modules.alllib.router._build_media_export", side_effect=build_export),
        ):
            initial = asyncio.run(get_media_sync_manifest(7, db=db, user=None, hybrid=False))
            initial_export = binary_resource(initial)
            initial_hash = hashlib.sha256(build_calls[-1]).hexdigest()
            self.assertEqual(initial_hash, initial_export["sha256"])
            self.assertEqual(len(build_calls[-1]), initial_export["size"])

            response = asyncio.run(export_media(7, db=db, user=None, artifact=initial_hash))
            self.assertEqual(build_calls[-1], asyncio.run(response_body(response)))

            chapters.append(make_chapter(3, "third"))
            added = asyncio.run(get_media_sync_manifest(7, db=db, user=None, hybrid=False))
            self.assertNotEqual(initial_hash, binary_resource(added)["sha256"])
            self.assertIn("/alllib/ui/chapter/3?package_id=novel_7", {r["url"] for r in added["resources"]})

            chapters[1].content_html = "first changed"
            changed = asyncio.run(get_media_sync_manifest(7, db=db, user=None, hybrid=False))
            self.assertNotEqual(binary_resource(added)["sha256"], binary_resource(changed)["sha256"])

            chapters.pop(0)
            removed = asyncio.run(get_media_sync_manifest(7, db=db, user=None, hybrid=False))
            removed_urls = {resource["url"] for resource in removed["resources"]}
            self.assertNotIn("/alllib/ui/chapter/2?package_id=novel_7", removed_urls)
            self.assertNotEqual(binary_resource(changed)["sha256"], binary_resource(removed)["sha256"])

            old_response = asyncio.run(export_media(7, db=db, user=None, artifact=initial_hash))
            self.assertEqual(build_calls[0], asyncio.run(response_body(old_response)))

            calls_before_resolver = len(build_calls)
            resolved = asyncio.run(resolve_package_resources("novel_7", db))
            self.assertEqual(calls_before_resolver, len(build_calls))
            self.assertEqual(binary_resource(removed), next(r for r in resolved if r["type"] == "binary"))


if __name__ == "__main__":
    unittest.main()
