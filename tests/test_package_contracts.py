import asyncio
import json
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from app.core.packages_router import generate_nsp, make_hybrid_manifest, make_package_manifest
from app.modules.alllib.capabilities import resolve_package_resources
from app.modules.alllib.router import _package_media_id, get_media_sync_manifest
from app.modules.video_archiver.module import MODULE as VIDEO_MODULE

ROOT = Path(__file__).resolve().parents[1]


class PackageContractTests(unittest.TestCase):
    def test_manifest_contains_explicit_module_metadata(self):
        manifest = make_package_manifest(
            module_id="music",
            package_id="song_1",
            package_title="Song: Example",
            root_url="/music/dashboard?package_id=song_1",
            resources=[{"url": "/music/audio/1", "type": "binary"}],
        )

        self.assertEqual(1, manifest["schema_version"])
        self.assertEqual("music", manifest["module"]["id"])
        self.assertEqual("/music/dashboard", manifest["module"]["root_url"])

    def test_hybrid_manifest_keeps_binary_and_adds_container(self):
        manifest = make_package_manifest(
            module_id="music",
            package_id="song_1",
            package_title="Song: Example",
            root_url="/music/dashboard?package_id=song_1",
            resources=[
                {"url": "/music/dashboard", "type": "html"},
                {"url": "/music/audio/1", "type": "binary"},
            ],
        )

        hybrid = make_hybrid_manifest("song_1", manifest)

        self.assertEqual(
            [
                {"url": "/music/audio/1", "type": "binary"},
                {"url": "/api/packages/song_1/nsp", "type": "container"},
            ],
            hybrid["resources"],
        )

    def test_desktop_bridge_uses_manifest_url_not_manifest_payload(self):
        base = (ROOT / "app/core/templates/base.html").read_text()

        self.assertIn("__NETSANCTUM_DESKTOP__?.requestDownload", base)
        self.assertIn("requestDownload(manifestUrl)", base)
        self.assertIn("pathHasPackage", base)

    def test_video_module_declares_package_provider(self):
        self.assertEqual(("video_playlist_", "video_"), VIDEO_MODULE.package_prefixes)
        self.assertEqual(
            "app.modules.video_archiver.capabilities:resolve_package_resources",
            VIDEO_MODULE.package_resolver,
        )

    def test_alllib_manifest_covers_package_runtime_urls(self):
        media = SimpleNamespace(
            id=7,
            media_type="novel",
            title="Example",
            cover_path=None,
        )
        chapter = SimpleNamespace(
            id=11,
            content_html="",
            pages_list=None,
            video_path=None,
        )

        class Result:
            def scalars(self):
                return self

            def all(self):
                return [chapter]

        class Database:
            async def get(self, model, item_id):
                return media

            async def execute(self, statement):
                return Result()

        manifest = asyncio.run(get_media_sync_manifest(7, db=Database(), user=None, hybrid=False))
        urls = {resource["url"] for resource in manifest["resources"]}

        self.assertIn("/alllib/ui/chapter/11?package_id=novel_7", urls)
        self.assertNotIn("/alllib/ui/active_downloads?package_id=novel_7", urls)
        self.assertNotIn("/alllib/ui/settings?package_id=novel_7", urls)
        self.assertNotIn("/alllib/dashboard", urls)
        self.assertNotIn("/alllib/ui/chapter/11", urls)
        self.assertIn("/static/placeholder.svg", urls)
        self.assertEqual("/alllib/reader/7?package_id=novel_7", manifest["root_url"])

    def test_alllib_package_ids_are_strict(self):
        self.assertEqual(7, _package_media_id("novel_7"))
        self.assertIsNone(_package_media_id(None))
        with self.assertRaises(HTTPException):
            _package_media_id("video_7")
        with self.assertRaises(HTTPException):
            _package_media_id("novel_0")

    def test_nsp_compiler_preserves_query_bearing_resource_keys(self):
        resources = [
            {"url": "/static/tailwind.css", "type": "css"},
            {"url": "/alllib/ui/chapter/11?package_id=novel_7", "type": "html"},
            {"url": "/alllib/api/novel/7/export", "type": "binary"},
        ]

        class Response:
            status_code = 200

            def __init__(self, url):
                self.headers = {"content-type": "text/plain"}
                self.content = url.encode()

        class Client:
            async def get(self, url, headers, cookies):
                return Response(url)

        async def compile_package():
            return b"".join(
                [
                    chunk
                    async for chunk in generate_nsp(
                        resources,
                        Client(),
                        {},
                        {},
                    )
                ]
            )

        payload = asyncio.run(compile_package())
        index_offset, magic = struct.unpack(">Q4s", payload[-12:])
        index = json.loads(payload[index_offset:-12])

        self.assertEqual(b"NSPK", magic)
        self.assertIn("/alllib/ui/chapter/11?package_id=novel_7", index)
        self.assertNotIn("/alllib/api/novel/7/export", index)

    def test_alllib_package_resolver_rejects_type_aliases(self):
        media = SimpleNamespace(id=7, media_type="novel", title="Example", cover_path=None)

        class Result:
            def scalars(self):
                return self

            def all(self):
                return []

        class Database:
            async def get(self, model, item_id):
                return media

            async def execute(self, statement):
                return Result()

        with self.assertRaises(ValueError):
            asyncio.run(resolve_package_resources("anime_7", Database()))


if __name__ == "__main__":
    unittest.main()
