"""The format selector built for a download must always be valid yt-dlp syntax."""

import unittest

from app.modules.video_archiver.tasks import _quality_format


class QualityFormatTests(unittest.TestCase):
    def test_a_numeric_quality_caps_the_height(self):
        selector = _quality_format("720")
        self.assertIn("[height<=720]", selector)
        self.assertEqual(3, selector.count("[height<=720]"))

    def test_best_drops_the_cap_instead_of_interpolating_it(self):
        # `height<=best` is rejected by yt-dlp as an invalid filter specification,
        # which failed the whole download rather than degrading.
        selector = _quality_format("best")
        self.assertNotIn("height", selector)
        self.assertIn("bestvideo[ext=mp4]+bestaudio[ext=m4a]", selector)
        self.assertTrue(selector.endswith("/best"))

    def test_nonsense_falls_back_to_no_cap(self):
        # DownloadRequest.quality is a free-form string, so a typo must not
        # become a broken selector.
        for value in ("", None, "  ", "1080p", "BEST", "garbage", "0", "-1", "720p"):
            with self.subTest(value=value):
                selector = _quality_format(value)
                self.assertNotIn("height", selector)

    def test_surrounding_whitespace_and_case_are_tolerated(self):
        self.assertEqual(_quality_format("best"), _quality_format(" BEST "))
        self.assertIn("[height<=1080]", _quality_format(" 1080 "))

    def test_every_selector_keeps_the_audio_fallback(self):
        for value in ("best", "720", "1080"):
            with self.subTest(value=value):
                selector = _quality_format(value)
                self.assertIn("bestaudio", selector)
                self.assertIn("[ext=mp4]", selector)
