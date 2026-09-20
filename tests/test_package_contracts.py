import asyncio
import json
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app.core.packages_router import (
    PackageResourceError,
    download_package_nsp,
    generate_nsp,
    make_hybrid_manifest,
    make_package_manifest,
)
from app.modules.alllib.capabilities import resolve_package_resources
from app.modules.alllib.router import _package_media_id, get_media_sync_manifest
from app.modules.video_archiver.module import MODULE as VIDEO_MODULE
from app.modules.video_archiver.router import _video_resources

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
        self.assertIn("manifest_url: manifestUrl", base)
        self.assertIn("sessionStorage.removeItem('active_package_id')", base)
        self.assertIn("input instanceof Request", base)
        self.assertIn("Offline packages are read-only.", base)
        self.assertIn("this.form.requestSubmit()", base)

    def test_manifest_rejects_external_resources_and_deduplicates_urls(self):
        manifest = make_package_manifest(
            module_id="music",
            package_id="song_1",
            package_title="Song: Example",
            root_url="/music/dashboard?package_id=song_1",
            resources=[
                {"url": "/static/tailwind.css", "type": "css"},
                {"url": "/static/tailwind.css", "type": "css"},
            ],
        )
        self.assertEqual(1, len(manifest["resources"]))

        with self.assertRaises(ValueError):
            make_package_manifest(
                module_id="music",
                package_id="song_1",
                package_title="Song: Example",
                root_url="/music/dashboard?package_id=song_1",
                resources=[{"url": "https://example.com/cover.jpg", "type": "image"}],
            )

    def test_manifest_rejects_invalid_resource_identity(self):
        base = {
            "module_id": "music",
            "package_id": "song_1",
            "package_title": "Song: Example",
            "root_url": "/music/dashboard?package_id=song_1",
        }
        invalid_resources = (
            {"url": "/music/audio/1", "type": "binary", "size": -1, "sha256": "a" * 64},
            {"url": "/music/audio/1", "type": "binary", "size": 10, "sha256": "A" * 64},
        )
        for resource in invalid_resources:
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                make_package_manifest(**base, resources=[resource])

        for legacy_resource in (
            {"url": "/music/audio/1", "type": "binary", "size": 10},
            {"url": "/music/audio/1", "type": "binary", "sha256": "a" * 64},
        ):
            with self.subTest(resource=legacy_resource):
                manifest = make_package_manifest(**base, resources=[legacy_resource])
                self.assertEqual([legacy_resource], manifest["resources"])

    def test_manifest_rejects_conflicting_duplicate_resource_definitions(self):
        with self.assertRaises(ValueError):
            make_package_manifest(
                module_id="music",
                package_id="song_1",
                package_title="Song: Example",
                root_url="/music/dashboard?package_id=song_1",
                resources=[
                    {"url": "/music/audio/1", "type": "binary"},
                    {"url": "/music/audio/1", "type": "image"},
                ],
            )

    def test_video_module_declares_package_provider(self):
        self.assertEqual(("video_playlist_", "video_"), VIDEO_MODULE.package_prefixes)
        self.assertEqual(
            "app.modules.video_archiver.capabilities:resolve_package_resources",
            VIDEO_MODULE.package_resolver,
        )

    def test_video_manifest_resource_includes_known_media_identity(self):
        video = SimpleNamespace(
            id="video-id",
            file_path="video.mp4",
            file_size=123456,
            sha256="a" * 64,
            thumbnail_path=None,
            channel_avatar_url=None,
            subtitles=None,
        )

        resources = _video_resources(video, "video_video-id")
        media = next(resource for resource in resources if resource["type"] == "binary")

        self.assertEqual(123456, media["size"])
        self.assertEqual("a" * 64, media["sha256"])

    def test_legacy_video_manifest_resource_allows_missing_media_identity(self):
        video = SimpleNamespace(
            id="legacy",
            file_path="legacy.mp4",
            file_size=None,
            sha256=None,
            thumbnail_path=None,
            channel_avatar_url=None,
            subtitles=None,
        )

        media = next(
            resource for resource in _video_resources(video, "video_legacy") if resource["type"] == "binary"
        )

        self.assertNotIn("size", media)
        self.assertNotIn("sha256", media)

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
        self.assertTrue(payload.startswith(b"/static/tailwind.css/alllib/ui/chapter/11?package_id=novel_7"))
        self.assertIn("/alllib/ui/chapter/11?package_id=novel_7", index)
        self.assertNotIn("/alllib/api/novel/7/export", index)
        self.assertEqual(64, len(index["/static/tailwind.css"]["sha256"]))

    def test_nsp_compiler_rejects_partial_packages(self):
        class Response:
            status_code = 404
            content = b""

            def __init__(self):
                self.headers = {}

        class Client:
            async def get(self, url, headers, cookies):
                return Response()

        async def compile_package():
            return b"".join(
                [
                    chunk
                    async for chunk in generate_nsp(
                        [{"url": "/missing", "type": "json"}],
                        Client(),
                        {},
                        {},
                    )
                ]
            )

        with self.assertRaises(PackageResourceError):
            asyncio.run(compile_package())

    def test_nsp_endpoint_reuses_the_trusted_request_host(self):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "path": "/api/packages/song_1/nsp",
                "query_string": b"",
                "headers": [(b"host", b"localhost")],
                "server": ("localhost", 80),
                "client": ("127.0.0.1", 1234),
            }
        )

        async def download():
            with patch(
                "app.core.packages_router.get_resources_for_package",
                AsyncMock(return_value=[{"url": "/static/placeholder.svg", "type": "image"}]),
            ):
                response = await download_package_nsp("song_1", request, user=object())
                return b"".join([chunk async for chunk in response.body_iterator])

        payload = asyncio.run(download())
        self.assertEqual(b"NSPK", payload[-4:])

    def test_offline_templates_only_request_packaged_vault_and_video_urls(self):
        vault = (ROOT / "app/modules/vault/templates/vault_dashboard.html").read_text()
        video = (ROOT / "app/modules/video_archiver/templates/video_dashboard.html").read_text()

        self.assertIn("for (let offset = 0; ; offset += 500)", vault)
        self.assertIn("{% if not package_mode %}loadManagement();{% endif %}", video)
        self.assertIn("{% if package_scope != 'video' %}", video)
        self.assertIn("video.dataset.mediaId", video)

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
