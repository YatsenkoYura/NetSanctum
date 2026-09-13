import asyncio
import hashlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.contracts.video_archive_v1 import ArchiveVideoRequest
from app.contracts.video_source_catalog_v1 import VideoSourceRequest
from app.core.browser_runtime import BrowserRuntime
from app.core.module_types import IntegrationContext, IntegrationRejectedError, IntegrationServiceError
from app.modules.video_archiver.integrations import archive_source_video
from app.modules.video_archiver.tasks import download_video_task
from app.modules.youtube.capabilities import resolve_entity
from app.modules.youtube.integrations import video_source_catalog
from app.modules.youtube.module import MODULE as YOUTUBE_MODULE
from app.modules.youtube.router import RANGE_HEADER
from app.modules.youtube.services import (
    YouTubeAPIError,
    YouTubeClient,
    _compact_number,
    _page_offset,
    _sapisid_authorization,
    _youtube_cookie_jar,
    resolve_stream,
)


class YouTubeModuleTests(unittest.TestCase):
    def test_search_normalizes_remote_entities(self):
        result_payload = SimpleNamespace(
            title="example", items=[SimpleNamespace(entity_type="youtube_video", duration=192, view_count=42)]
        )
        with patch(
            "app.modules.youtube.services._innertube_catalog_sync",
            return_value=result_payload,
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

    def test_recommendations_use_authenticated_home_feed(self):
        with patch(
            "app.modules.youtube.services._innertube_catalog_sync",
            return_value=SimpleNamespace(title="recommended", items=[]),
        ) as extract:
            result = asyncio.run(YouTubeClient("cookies").recommendations())

        self.assertEqual("recommended", result.title)
        self.assertEqual(":ytrec", extract.call_args.args[0])

    def test_recommendations_require_account_session(self):
        with self.assertRaises(YouTubeAPIError) as raised:
            asyncio.run(YouTubeClient().recommendations())
        self.assertEqual(401, raised.exception.status_code)

    def test_shorts_use_the_browser_catalog_target(self):
        with patch.object(
            YouTubeClient,
            "_browser_catalog",
            AsyncMock(return_value=SimpleNamespace(title="Shorts", items=[])),
        ) as catalog:
            result = asyncio.run(YouTubeClient("cookies").shorts())

        self.assertEqual("Shorts", result.title)
        self.assertEqual(":ytshorts", catalog.call_args.args[0])

    def test_empty_history_is_a_valid_catalog(self):
        with patch(
            "app.modules.youtube.services._innertube_catalog_sync",
            return_value=SimpleNamespace(title="History", items=[]),
        ):
            result = asyncio.run(YouTubeClient("cookies").history())

        self.assertEqual("History", result.title)
        self.assertEqual([], result.items)

    def test_browser_catalog_extracts_structured_cards(self):
        record = SimpleNamespace(id="youtube")
        policy = YOUTUBE_MODULE.browser_policies[0]
        browser_rows = [
            {
                "title": "Example",
                "title_text": "Example",
                "href": "/watch?v=dQw4w9WgXcQ",
                "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
                "thumbnail_lazy": None,
                "channel": "Channel",
                "channel_href": "/channel/UC123456",
                "duration": "3:12",
                "views": "29,685 views",
            }
        ]
        with (
            patch(
                "app.modules.youtube.services.module_registry.browser_policy", return_value=(record, policy)
            ),
            patch(
                "app.modules.youtube.services.browser_runtime_client.start",
                AsyncMock(return_value={"session_id": "session-1"}),
            ),
            patch("app.modules.youtube.services.browser_runtime_client.navigate", AsyncMock()),
            patch(
                "app.modules.youtube.services.browser_runtime_client.query",
                AsyncMock(return_value=browser_rows),
            ),
            patch("app.modules.youtube.services.browser_runtime_client.scroll", AsyncMock()),
            patch("app.modules.youtube.services.browser_runtime_client.close", AsyncMock()),
            patch("app.modules.youtube.services.asyncio.sleep", AsyncMock()),
        ):
            result = asyncio.run(
                YouTubeClient("cookies")._browser_catalog("ytsearch24:example", "Search: example", None)
            )

        self.assertEqual("dQw4w9WgXcQ", result.items[0].entity_id)
        self.assertEqual(29685, result.items[0].view_count)
        self.assertEqual(192, result.items[0].duration)

    def test_full_view_counts_parse_without_compaction(self):
        self.assertEqual(29685, _compact_number("29,685 views"))
        self.assertEqual(1_200_000, _compact_number("1,2 млн просмотров"))

    def test_browser_login_network_is_restricted_to_google_and_youtube(self):
        policy = YOUTUBE_MODULE.browser_policies[0]
        self.assertTrue(BrowserRuntime._allowed_url(policy, "https://accounts.google.com/signin"))
        self.assertTrue(BrowserRuntime._allowed_url(policy, "https://www.recaptcha.net/recaptcha/api.js"))
        self.assertTrue(BrowserRuntime._allowed_url(policy, "https://www.youtube-nocookie.com/"))
        self.assertTrue(BrowserRuntime._allowed_url(policy, "wss://www.youtube.com/live"))
        self.assertTrue(BrowserRuntime._allowed_url(policy, "https://www.youtube.com/"))
        self.assertFalse(BrowserRuntime._allowed_url(policy, "https://youtube.com.evil.example/"))
        self.assertFalse(BrowserRuntime._allowed_url(policy, "http://accounts.google.com/signin"))

    def test_browser_policy_hosts_are_in_example_deployment_allowlist(self):
        policy = YOUTUBE_MODULE.browser_policies[0]
        line = next(
            line
            for line in Path(".env.example").read_text().splitlines()
            if line.startswith("BROWSER_EGRESS_HOSTS=")
        )
        deployment_hosts = set(line.partition("=")[2].split(","))

        self.assertLessEqual(set(policy.allowed_hosts), deployment_hosts)

    def test_catalog_request_has_a_deadline(self):
        dashboard = (Path("app/modules/youtube/templates/youtube_dashboard.html")).read_text()

        self.assertIn("timedOut = true;", dashboard)
        self.assertIn("}, 60000);", dashboard)
        self.assertIn("Catalog request timed out. Please try again.", dashboard)

    def test_catalog_dashboard_uses_dense_responsive_layout(self):
        dashboard = Path("app/modules/youtube/templates/youtube_dashboard.html").read_text()

        self.assertIn("sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-4", dashboard)
        self.assertIn('id="youtube-sort"', dashboard)
        self.assertIn("min-h-11 w-full", dashboard)

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
        self.assertEqual("Example", result["title"])
        self.assertEqual([], result["captions"])

    def test_mse_tracks_are_deduplicated_by_quality_and_audio_language(self):
        from app.modules.youtube.services import _mse_tracks

        stored, public = _mse_tracks(
            {
                "formats": [
                    {
                        "url": "https://a.googlevideo.com/v1",
                        "ext": "mp4",
                        "vcodec": "avc",
                        "acodec": "none",
                        "height": 720,
                        "tbr": 500,
                    },
                    {
                        "url": "https://a.googlevideo.com/v2",
                        "ext": "mp4",
                        "vcodec": "avc",
                        "acodec": "none",
                        "height": 720,
                        "tbr": 1000,
                    },
                    {
                        "url": "https://a.googlevideo.com/a1",
                        "ext": "m4a",
                        "vcodec": "none",
                        "acodec": "mp4a",
                        "language": "ru",
                        "abr": 64,
                    },
                    {
                        "url": "https://a.googlevideo.com/a2",
                        "ext": "m4a",
                        "vcodec": "none",
                        "acodec": "mp4a",
                        "language": "ru",
                        "abr": 128,
                    },
                ]
            }
        )

        self.assertEqual(1, len(public["video_tracks"]))
        self.assertEqual(1, len(public["audio_tracks"]))
        self.assertEqual("https://a.googlevideo.com/v2", stored["v0"]["url"])
        self.assertEqual("https://a.googlevideo.com/a2", stored["a0"]["url"])

    def test_stream_exposes_local_mse_mp4_track_metadata_only(self):
        info = {
            "url": "https://example.googlevideo.com/muxed?signature=secret",
            "formats": [
                {
                    "url": "https://example.googlevideo.com/video?signature=secret",
                    "ext": "mp4",
                    "vcodec": "avc1.640028",
                    "acodec": "none",
                    "height": 1080,
                },
                {
                    "url": "https://example.googlevideo.com/audio?signature=secret",
                    "ext": "mp4",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "language": "en",
                },
                {
                    "url": "https://example.googlevideo.com/webm?signature=secret",
                    "ext": "webm",
                    "vcodec": "vp9",
                    "acodec": "none",
                },
            ],
        }
        with patch("app.modules.youtube.services._stream_info_sync", return_value=info):
            result = asyncio.run(YouTubeClient().create_stream("dQw4w9WgXcQ"))

        self.assertEqual('video/mp4; codecs="avc1.640028"', result["mse"]["video_tracks"][0]["mime"])
        self.assertEqual('audio/mp4; codecs="mp4a.40.2"', result["mse"]["audio_tracks"][0]["mime"])
        self.assertNotIn("googlevideo", result["mse"]["video_tracks"][0]["url"])

    def test_stream_proxy_accepts_only_a_single_byte_range(self):
        self.assertIsNotNone(RANGE_HEADER.fullmatch("bytes=0-1048575"))
        self.assertIsNotNone(RANGE_HEADER.fullmatch("bytes=500-"))
        self.assertIsNotNone(RANGE_HEADER.fullmatch("bytes=-500"))
        self.assertIsNone(RANGE_HEADER.fullmatch("bytes=0-1,2-3"))
        self.assertIsNone(RANGE_HEADER.fullmatch("bytes=-"))

    def test_entity_resolver_builds_canonical_urls(self):
        video = asyncio.run(resolve_entity(None, "youtube_video", "dQw4w9WgXcQ"))
        playlist = asyncio.run(resolve_entity(None, "youtube_playlist", "PL123456"))

        assert video is not None
        assert playlist is not None
        self.assertEqual("https://www.youtube.com/watch?v=dQw4w9WgXcQ", video["source_url"])
        self.assertEqual("https://www.youtube.com/playlist?list=PL123456", playlist["source_url"])

    def test_channel_uses_server_side_channel_feed(self):
        payload = {
            "title": "Channel - Videos",
            "channel": "Channel",
            "description": "Channel description",
            "thumbnails": [
                {"id": "banner_uncropped", "url": "https://yt3.googleusercontent.com/banner"},
                {"id": "avatar_uncropped", "url": "https://yt3.googleusercontent.com/avatar"},
            ],
            "entries": [],
        }
        with patch(
            "app.modules.youtube.services._extract_cached",
            AsyncMock(return_value=payload),
        ) as extract:
            result = asyncio.run(YouTubeClient().channel_videos("UC123456"))
        self.assertEqual("Channel", result.title)
        self.assertEqual("Channel description", result.description)
        self.assertIn("banner", result.thumbnail_url or "")
        self.assertIn("avatar", result.avatar_url or "")
        self.assertEqual("https://www.youtube.com/channel/UC123456/videos", extract.call_args.args[0])

    def test_innertube_catalog_normalizes_video_renderers(self):
        payload = {
            "contents": {
                "videoRenderer": {
                    "videoId": "dQw4w9WgXcQ",
                    "title": {"simpleText": "Example"},
                    "ownerText": {"simpleText": "Channel"},
                    "viewCountText": {"simpleText": "29,685 views"},
                    "lengthText": {"simpleText": "3:12"},
                    "thumbnail": {"thumbnails": [{"url": "https://i.ytimg.com/example.jpg"}]},
                }
            }
        }
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
        with (
            patch(
                "app.modules.youtube.services._innertube_bootstrap_sync",
                return_value=("key", {"client": {"clientVersion": "1.20260101.00.00"}}),
            ),
            patch("app.modules.youtube.services.requests.post", return_value=response) as post,
        ):
            result = asyncio.run(YouTubeClient(catalog_mode="innertube").search("example"))

        self.assertEqual("dQw4w9WgXcQ", result.items[0].entity_id)
        self.assertEqual(29_685, result.items[0].view_count)
        self.assertEqual("1", post.call_args.kwargs["headers"]["X-YouTube-Client-Name"])

    def test_netscape_cookie_parser_scopes_cookies_to_youtube(self):
        jar = _youtube_cookie_jar(
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\tprimary\n"
            "example.com\tFALSE\t/\tTRUE\t2147483647\tSAPISID\tunrelated\n"
            ".youtube.com\tTRUE\tnot-a-path\tTRUE\t2147483647\tbad\tvalue\n"
        )

        self.assertEqual(["SAPISID"], [cookie.name for cookie in jar])
        self.assertEqual(".youtube.com", next(iter(jar)).domain)

    def test_sapisid_authorization_includes_secure_variants(self):
        jar = _youtube_cookie_jar(
            ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\tprimary\n"
            ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID1P\tfirst-party\n"
            ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID3P\tthird-party\n"
        )
        timestamp = 1_700_000_000
        authorization = _sapisid_authorization(jar, timestamp=timestamp)

        expected = []
        for value, header in (
            ("primary", "SAPISIDHASH"),
            ("first-party", "SAPISID1PHASH"),
            ("third-party", "SAPISID3PHASH"),
        ):
            digest = hashlib.sha1(f"{timestamp} {value} https://www.youtube.com".encode()).hexdigest()
            expected.append(f"{header} {timestamp}_{digest}")
        self.assertEqual(" ".join(expected), authorization)

    def test_innertube_authenticated_request_sends_cookie_auth_and_visitor(self):
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})
        cookies = ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSAPISID\tprimary\n"
        with (
            patch(
                "app.modules.youtube.services._innertube_bootstrap_sync",
                return_value=(
                    "key",
                    {"client": {"clientVersion": "1.20260101.00.00", "visitorData": "visitor"}},
                ),
            ),
            patch("app.modules.youtube.services.requests.post", return_value=response) as post,
        ):
            result = asyncio.run(YouTubeClient(cookies).subscriptions())

        self.assertEqual([], result.items)
        self.assertEqual("FEsubscriptions", post.call_args.kwargs["json"]["browseId"])
        headers = post.call_args.kwargs["headers"]
        self.assertEqual("https://www.youtube.com", headers["Origin"])
        self.assertEqual("https://www.youtube.com", headers["X-Origin"])
        self.assertEqual("visitor", headers["X-Goog-Visitor-Id"])
        self.assertTrue(headers["Authorization"].startswith("SAPISIDHASH "))
        self.assertEqual("primary", next(iter(post.call_args.kwargs["cookies"])).value)

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

    def test_radio_mix_entries_are_playlists_not_video_ids(self):
        payload = {
            "title": "Discover",
            "entries": [
                {"id": "RDCogKdPkYMoI", "title": "Mix - Norma Tale"},
                {"id": "dQw4w9WgXcQ", "title": "Video"},
            ],
        }
        with patch(
            "app.modules.youtube.services._extract_cached",
            AsyncMock(return_value=payload),
        ):
            result = asyncio.run(YouTubeClient()._catalog("playlist", "Discover", None))

        self.assertEqual(["youtube_playlist", "youtube_video"], [item.entity_type for item in result.items])
        self.assertEqual("https://www.youtube.com/playlist?list=RDCogKdPkYMoI", result.items[0].source_url)

    def test_radio_mix_uses_chromium_catalog(self):
        browser_result = SimpleNamespace(title="Mix", items=[])
        with patch(
            "app.modules.youtube.services.YouTubeClient._browser_catalog",
            AsyncMock(return_value=browser_result),
        ) as catalog:
            result = asyncio.run(YouTubeClient().playlist_items("RDCogKdPkYMoI"))

        self.assertEqual("Mix", result.title)
        self.assertEqual("https://www.youtube.com/watch?list=RDCogKdPkYMoI", catalog.call_args.args[0])

    def test_catalog_integration_preserves_upstream_rate_limit(self):
        context = IntegrationContext(session=None, user=None, registry=None)
        client = SimpleNamespace(
            recommendations=AsyncMock(side_effect=YouTubeAPIError("Rate limited", status_code=429))
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
        self.assertIs(download_video_task, dispatch.call_args.args[0])
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
