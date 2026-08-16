import unittest

from app.modules.alllib.novelbin import NOVELBIN_SITE_ID, NovelBinAPI
from app.modules.alllib.ranobehub import get_source_api

SLUG = "test-novel"


class FakeResponse:
    def __init__(self, url, text, status_code=200):
        self.url = url
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self):
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(url)
        if url.endswith(f"/novel-bin/{SLUG}/"):
            return FakeResponse(
                url,
                """
                <meta property="og:novel:novel_name" content="Test Novel">
                <meta name="description" content="A &quot;good&quot; novel">
                <meta property="og:image" content="/files/image/test.jpg">
                <meta property="og:novel:author" content="Writer">
                <meta property="og:novel:genre" content="Fantasy">
                <meta property="og:novel:status" content="OnGoing">
                <div class="desc-text" itemprop="description"><p>A full description.</p></div>
                <a href="/novel-bin/test-novel/chapter-1" title="Chapter 1: Start">One</a>
                <a href="/novel-bin/test-novel/chapter-2" title="Chapter 2: Next">Two</a>
                """,
            )
        if url.endswith(f"/novel-bin/{SLUG}/chapter-1"):
            return FakeResponse(
                url,
                """
                <div id="chr-content" onclick="bad()">
                    <h4>Chapter 1</h4><p>Safe &amp; sound</p>
                    <script>alert(1)</script>
                    <img data-src="/files/image/illustration.jpg" onerror="bad()">
                    <img src="https://127.0.0.1/secret">
                </div>
                """,
            )
        raise AssertionError(f"Unexpected request: {url}")


class NovelBinAPITests(unittest.TestCase):
    def setUp(self):
        self.api = NovelBinAPI(f"https://novel-bin.net/novel-bin/{SLUG}/")
        self.api.session = FakeSession()  # type: ignore[assignment]

    def test_selects_source_and_extracts_slug(self):
        url = f"https://novel-bin.net/novel-bin/{SLUG}/chapter-1"

        self.assertEqual(SLUG, self.api.extract_slug_from_url(url))
        self.assertEqual((NOVELBIN_SITE_ID, "novel-bin.net"), self.api.get_site_info_from_url(url))
        self.assertIsInstance(get_source_api(url), NovelBinAPI)

    def test_maps_metadata_chapters_and_sanitized_content(self):
        info = self.api.get_novel_info(SLUG)
        chapters = self.api.get_novel_chapters(SLUG)
        content = self.api.get_chapter_content(SLUG, "0", "1", "0")["content"]

        self.assertEqual("Test Novel", info["name"])
        self.assertEqual("A full description.", info["summary"])
        self.assertEqual("https://novel-bin.net/files/image/test.jpg", info["cover"]["default"])
        self.assertEqual(["1", "2"], [chapter["number"] for chapter in chapters])
        self.assertIn("<p>Safe &amp; sound</p>", content)
        self.assertIn('src="https://novel-bin.net/files/image/illustration.jpg"', content)
        self.assertNotIn("script", content)
        self.assertNotIn("alert", content)
        self.assertNotIn("127.0.0.1", content)
        self.assertNotIn("onerror", content)


if __name__ == "__main__":
    unittest.main()
