import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.contracts.video_archive_v1 import ArchiveVideoRequest
from app.contracts.video_source_catalog_v1 import VideoSourceRequest
from app.core.browser_runtime import BrowserRuntime
from app.core.module_types import IntegrationContext, IntegrationRejectedError, IntegrationServiceError
from app.modules.video_archiver.integrations import archive_source_video
from app.modules.youtube.capabilities import resolve_entity
from app.modules.youtube.integrations import video_source_catalog
from app.modules.youtube.module import MODULE as YOUTUBE_MODULE
from app.modules.youtube.services import YouTubeAPIError, YouTubeClient, _page_offset, resolve_stream


class YouTubeModuleTests(unittest.TestCase):
    def test_search_normalizes_remote_entities(self):
        payload = {
            "title": "example",
            "entries": [
                {
                    "id": "dQw4w9WgXcQ",
                    "title": "Example",
                    "channel": "Channel",
                    "channel_id": "UC123456",
                    "duration": 192,
                    "view_count": 42,
                }
            ],
        }
        with patch(
            "app.modules.youtube.services._extract_cached",
            AsyncMock(return_value=payload),
        ) as extract:
            result = asyncio.run(YouTubeClient().search("example"))

        self.assertEqual("youtube_video", result.items[0].entity_type)
        self.assertEqual(192, result.items[0].duration)
        self.assertEqual(42, result.items[0].view_count)
        self.assertEqual("ytsearch24:example", extract.call_args.args[0])

    def test_search_rejects_excessive_page_offsets(self):
        with self.assertRaises(YouTubeAPIError):
            _page_offset("120", 96)

    def test_subscriptions_require_account_session(self):
        with self.assertRaises(YouTubeAPIError) as raised:
            asyncio.run(YouTubeClient().subscriptions())
        self.assertEqual(401, raised.exception.status_code)

    def test_browser_login_network_is_restricted_to_google_and_youtube(self):
        policy = YOUTUBE_MODULE.browser_policies[0]
        self.assertTrue(BrowserRuntime._allowed_url(policy, "https://accounts.google.com/signin"))
        self.assertTrue(BrowserRuntime._allowed_url(policy, "wss://www.youtube.com/live"))
        self.assertTrue(BrowserRuntime._allowed_url(policy, "https://www.youtube.com/"))
        self.assertFalse(BrowserRuntime._allowed_url(policy, "https://youtube.com.evil.example/"))
        self.assertFalse(BrowserRuntime._allowed_url(policy, "http://accounts.google.com/signin"))

    def test_stream_tokens_do_not_expose_signed_remote_url(self):
        info = {
            "url": "https://example.googlevideo.com/videoplayback?signature=secret",
            "title": "Example",
            "duration": 10,
            "http_headers": {"User-Agent": "test"},
        }
        with patch("app.modules.youtube.services._stream_info_sync", return_value=info):
            result = asyncio.run(YouTubeClient().create_stream("dQw4w9WgXcQ"))

        self.assertTrue(result["stream_url"].startswith("/api/youtube/streams/"))
        self.assertNotIn("googlevideo", result["stream_url"])
        token = result["stream_url"].rsplit("/", 1)[1]
        stored = asyncio.run(resolve_stream(token))
        assert stored is not None
        self.assertEqual(info["url"], stored["url"])

    def test_entity_resolver_builds_canonical_urls(self):
        video = asyncio.run(resolve_entity(None, "youtube_video", "dQw4w9WgXcQ"))
        playlist = asyncio.run(resolve_entity(None, "youtube_playlist", "PL123456"))

        assert video is not None
        assert playlist is not None
        self.assertEqual("https://www.youtube.com/watch?v=dQw4w9WgXcQ", video["source_url"])
        self.assertEqual("https://www.youtube.com/playlist?list=PL123456", playlist["source_url"])

    def test_channel_uses_server_side_channel_feed(self):
        with patch(
            "app.modules.youtube.services._extract_cached",
            AsyncMock(return_value={"title": "Channel", "entries": []}),
        ) as extract:
            result = asyncio.run(YouTubeClient().channel_videos("UC123456"))
        self.assertEqual("Channel", result.title)
        self.assertEqual("https://www.youtube.com/channel/UC123456/videos", extract.call_args.args[0])

    def test_playlist_url_search_exposes_playlist_archive_action_entity(self):
        payload = {
            "id": "PL123456",
            "title": "Playlist",
            "entries": [{"id": "dQw4w9WgXcQ", "title": "Example"}],
        }
        with patch(
            "app.modules.youtube.services._extract_cached",
            AsyncMock(return_value=payload),
        ):
            result = asyncio.run(YouTubeClient().search("https://www.youtube.com/playlist?list=PL123456"))
        self.assertEqual(["youtube_playlist", "youtube_video"], [item.entity_type for item in result.items])

    def test_catalog_integration_preserves_upstream_rate_limit(self):
        context = IntegrationContext(session=None, user=None, registry=None)
        client = SimpleNamespace(
            popular=AsyncMock(side_effect=YouTubeAPIError("Rate limited", status_code=429))
        )
        with (
            patch(
                "app.modules.youtube.integrations.load_cookies",
                AsyncMock(return_value=None),
            ),
            patch("app.modules.youtube.integrations.YouTubeClient", return_value=client),
            self.assertRaises(IntegrationServiceError) as raised,
        ):
            asyncio.run(video_source_catalog(VideoSourceRequest(), context))
        self.assertEqual(429, raised.exception.status_code)

    def test_archive_integration_dispatches_video_archiver_task(self):
        registry = SimpleNamespace(
            resolve_entity=AsyncMock(
                return_value={
                    "title": "Example",
                    "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                }
            )
        )
        context = IntegrationContext(session=None, user=None, registry=registry)
        dispatched = SimpleNamespace(id="task-1")

        with patch(
            "app.modules.video_archiver.integrations.dispatch_tracked_async",
            AsyncMock(return_value=dispatched),
        ) as dispatch:
            result = asyncio.run(
                archive_source_video(
                    ArchiveVideoRequest(entity_type="youtube_video", entity_id="dQw4w9WgXcQ"),
                    context,
                )
            )

        self.assertEqual("task-1", result.task_id)
        self.assertEqual("youtube", result.platform)
        self.assertEqual("video_dl", dispatch.call_args.args[2])

    def test_archive_integration_rejects_playlist_fanout(self):
        registry = SimpleNamespace(
            resolve_entity=AsyncMock(
                return_value={
                    "title": "Playlist",
                    "source_url": "https://www.youtube.com/playlist?list=PL123456",
                }
            )
        )
        context = IntegrationContext(session=None, user=None, registry=registry)
        with self.assertRaises(IntegrationRejectedError):
            asyncio.run(
                archive_source_video(
                    ArchiveVideoRequest(entity_type="youtube_playlist", entity_id="PL123456"),
                    context,
                )
            )


if __name__ == "__main__":
    unittest.main()
