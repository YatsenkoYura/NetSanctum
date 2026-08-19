import inspect
import os
import unittest
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import Mock, patch

from app.core.ytdlp_pipeline import (
    YtDlpErrorKind,
    YtDlpPipelineError,
    classify_ytdlp_error,
    extract_info,
    is_youtube_playlist_url,
    is_youtube_single_video_url,
    youtube_options,
)
from app.modules.music import tasks as music_tasks
from app.modules.video_archiver import tasks as video_tasks
from app.modules.video_archiver.providers import YouTubeProvider


class FakeYoutubeDL:
    calls: ClassVar[list[dict]] = []
    results: ClassVar[list] = []

    def __init__(self, options):
        self.options = options
        self.__class__.calls.append(options)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def extract_info(self, url, *, download):
        result = self.__class__.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class YtDlpPipelineTests(unittest.TestCase):
    def setUp(self):
        FakeYoutubeDL.calls = []
        FakeYoutubeDL.results = []
        self.redis = Mock()
        self.redis.eval.return_value = [1, 0]

    @staticmethod
    def settings(**overrides):
        values = {
            "YTDLP_CACHE_DIR": "/tmp/yt-dlp-cache",
            "YOUTUBE_POT_PROVIDER_URL": "http://youtube-pot:4416",
            "YOUTUBE_YTDLP_REQUEST_INTERVAL_SECONDS": 1.0,
            "YOUTUBE_YTDLP_PUBLIC_INTERVAL_SECONDS": 5.0,
            "YOUTUBE_YTDLP_AUTH_INTERVAL_SECONDS": 10.0,
            "YOUTUBE_YTDLP_BACKOFF_SECONDS": 3600,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def run_extract(self, results, **kwargs):
        FakeYoutubeDL.results = list(results)
        with (
            patch("app.core.ytdlp_pipeline.yt_dlp.YoutubeDL", FakeYoutubeDL),
            patch("app.core.ytdlp_pipeline.get_settings", return_value=self.settings()),
        ):
            return extract_info(self.redis, "https://www.youtube.com/watch?v=abcdefghijk", **kwargs)

    def test_defaults_use_upstream_clients_and_pot_provider(self):
        with patch("app.core.ytdlp_pipeline.get_settings", return_value=self.settings()):
            options = youtube_options({"format": "best"})

        self.assertNotIn("player_client", str(options))
        self.assertNotIn("user_agent", options)
        self.assertEqual({"deno": {}}, options["js_runtimes"])
        self.assertEqual(
            ["http://youtube-pot:4416"],
            options["extractor_args"]["youtubepot-bgutilhttp"]["base_url"],
        )

    def test_public_success_never_exposes_cookies(self):
        info = self.run_extract(
            [{"id": "abcdefghijk"}],
            cookies_text="# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret",
        )

        self.assertEqual("abcdefghijk", info["id"])
        self.assertEqual(1, len(FakeYoutubeDL.calls))
        self.assertNotIn("cookiefile", FakeYoutubeDL.calls[0])

    def test_auth_error_retries_once_with_temporary_cookie_file(self):
        cookies = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret"
        info = self.run_extract(
            [RuntimeError("Private video. Sign in if you've been granted access"), {"id": "abcdefghijk"}],
            cookies_text=cookies,
        )

        self.assertEqual("abcdefghijk", info["id"])
        self.assertEqual(2, len(FakeYoutubeDL.calls))
        self.assertNotIn("cookiefile", FakeYoutubeDL.calls[0])
        cookie_path = FakeYoutubeDL.calls[1]["cookiefile"]
        self.assertFalse(os.path.exists(cookie_path))
        intervals = [call.args[-1] for call in self.redis.eval.call_args_list]
        self.assertEqual([5000, 10000], intervals)

    def test_bot_challenge_retries_with_cookies_before_cooldown(self):
        cookies = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret"
        info = self.run_extract(
            [RuntimeError("Sign in to confirm you're not a bot"), {"id": "abcdefghijk"}],
            cookies_text=cookies,
        )

        self.assertEqual("abcdefghijk", info["id"])
        self.assertEqual(2, len(FakeYoutubeDL.calls))
        self.redis.setex.assert_not_called()

    def test_bot_challenge_without_cookies_opens_cooldown(self):
        with self.assertRaises(YtDlpPipelineError) as raised:
            self.run_extract([RuntimeError("Sign in to confirm you're not a bot")])

        self.assertEqual(YtDlpErrorKind.RATE_LIMITED, raised.exception.kind)
        self.redis.setex.assert_called_once()

    def test_rate_limit_opens_cooldown_without_auth_retry(self):
        with self.assertRaises(YtDlpPipelineError) as raised:
            self.run_extract(
                [RuntimeError("This content isn't available, try again later")],
                cookies_text="# Netscape HTTP Cookie File\n",
            )

        self.assertEqual(YtDlpErrorKind.RATE_LIMITED, raised.exception.kind)
        self.assertEqual(1, len(FakeYoutubeDL.calls))
        self.redis.setex.assert_called_once()

    def test_non_youtube_uses_provider_cookies_without_youtube_gate(self):
        FakeYoutubeDL.results = [{"id": "track"}]
        with patch("app.core.ytdlp_pipeline.yt_dlp.YoutubeDL", FakeYoutubeDL):
            info = extract_info(
                self.redis,
                "https://soundcloud.com/artist/track",
                platform="soundcloud",
                cookies_text="# Netscape HTTP Cookie File\n",
            )

        self.assertEqual("track", info["id"])
        self.redis.eval.assert_not_called()

    def test_error_classification_and_playlist_detection(self):
        self.assertEqual(YtDlpErrorKind.RATE_LIMITED, classify_ytdlp_error("HTTP Error 429"))
        self.assertEqual(YtDlpErrorKind.AUTH_REQUIRED, classify_ytdlp_error("Private video"))
        self.assertEqual(YtDlpErrorKind.UNAVAILABLE, classify_ytdlp_error("Video has been removed"))
        self.assertTrue(is_youtube_playlist_url("https://youtube.com/playlist?list=PL123"))
        self.assertFalse(is_youtube_playlist_url("https://youtube.com/watch?v=abcdefghijk"))
        self.assertTrue(is_youtube_single_video_url("https://youtube.com/watch?v=abcdefghijk"))
        self.assertTrue(is_youtube_single_video_url("https://youtu.be/abcdefghijk"))
        self.assertFalse(is_youtube_single_video_url("https://youtube.com/watch?v=abcdefghijk&list=PL123"))
        self.assertFalse(is_youtube_single_video_url("https://youtu.be/abcdefghijk?list=PL123"))
        self.assertFalse(is_youtube_single_video_url("https://youtube.com/shorts/abcdefghijk?list=PL123"))
        self.assertFalse(is_youtube_single_video_url("https://youtube.com/@channel"))


class MediaTaskPipelineTests(unittest.TestCase):
    def test_single_video_dispatch_skips_resolver_extraction(self):
        dispatched = SimpleNamespace(id="download-task")
        with (
            patch.object(
                video_tasks.PlatformRegistry, "require_supported_url", return_value=YouTubeProvider()
            ),
            patch.object(video_tasks, "dispatch_tracked_sync", return_value=dispatched) as dispatch,
            patch.object(video_tasks, "extract_info") as extract,
            patch.object(video_tasks, "redis_client"),
        ):
            result = video_tasks.process_video_url_task.run("https://www.youtube.com/watch?v=abcdefghijk")

        self.assertIn("download-task", result)
        extract.assert_not_called()
        dispatch.assert_called_once()

    def test_music_download_uses_one_extract_and_video_comments_are_separate(self):
        music_text = inspect.getsource(music_tasks.process_song_task.run)
        music_source = music_tasks.process_song_task.run.__code__
        video_source = video_tasks.download_video_task.run.__code__

        self.assertEqual(1, music_text.count("extract_info("))
        self.assertNotIn("fetch_video_comments_task", music_source.co_names)
        self.assertIn("fetch_video_comments_task", video_source.co_names)
