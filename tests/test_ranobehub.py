import io
import unittest
import xml.etree.ElementTree as ET
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from app.modules.alllib.epub_builder import EPUBBuilder
from app.modules.alllib.ranobehub import (
    RANOBEHUB_SITE_ID,
    RanobeHubAPI,
    get_source_api,
    sanitize_chapter_html,
)


class FakeResponse:
    def __init__(self, url, *, data=None, text="", status_code=200):
        self.url = url
        self._data = data
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": "application/json" if data is not None else "text/html"}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self):
        self.requests = []

    def get(self, url, params=None, **kwargs):
        self.requests.append((url, params))
        if url.endswith("/ranobe/696-daoist-gu-1"):
            return FakeResponse(
                url,
                text=(
                    '<meta property="og:title" content="Преподобный Гу">'
                    '<meta name="description" content="Описание">'
                ),
            )
        if url.endswith("/api/search"):
            return FakeResponse(
                url,
                data={
                    "books": [
                        {
                            "id": 696,
                            "slug": "daoist-gu-1",
                            "title": "Преподобный Гу",
                            "originalTitle": "Gu Zhen Ren",
                            "description": "Описание API",
                            "status": "В процессе",
                            "rating": 8.68,
                            "posterUrl": "/api/media/20280?v=5&size=medium",
                        }
                    ]
                },
            )
        if url.endswith("/api/books/696/chapters") and params["offset"] == 0:
            return FakeResponse(
                url,
                data={
                    "items": [
                        {
                            "id": 227846,
                            "volume": 1,
                            "number": 1,
                            "title": "Глава 1",
                            "translationOptions": [
                                {
                                    "slug": "main",
                                    "name": "Основной перевод",
                                    "isDefault": True,
                                    "isPreferred": True,
                                }
                            ],
                        }
                    ],
                    "nextOffset": 1,
                },
            )
        if url.endswith("/api/books/696/chapters") and params["offset"] == 1:
            return FakeResponse(
                url,
                data={
                    "items": [
                        {
                            "id": 227847,
                            "volume": 1,
                            "number": 1.5,
                            "title": "Глава 1.5",
                            "translationOptions": [],
                        }
                    ],
                    "nextOffset": None,
                },
            )
        if url.endswith("/api/chapters/227846"):
            return FakeResponse(
                url,
                data={
                    "chapter": {
                        "id": 227846,
                        "volume": 1,
                        "number": 1,
                        "title": "Глава 1",
                        "html": '<p>Текст</p><img src="/api/media/42?v=1&size=big">',
                        "translationBranch": {"slug": "main"},
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {url} {params}")


class RanobeHubAPITests(unittest.TestCase):
    def setUp(self):
        self.api = RanobeHubAPI()
        self.api.session = FakeSession()

    def test_extracts_book_reference_and_selects_source(self):
        url = "https://ranobehub.org/ranobe/696-daoist-gu-1/chapter/227846"

        self.assertEqual("696-daoist-gu-1", self.api.extract_slug_from_url(url))
        self.assertEqual((RANOBEHUB_SITE_ID, "ranobe.space"), self.api.get_site_info_from_url(url))
        self.assertIsInstance(get_source_api(url), RanobeHubAPI)

    def test_maps_metadata_chapters_and_content(self):
        info = self.api.get_novel_info("696-daoist-gu-1")
        chapters = self.api.get_novel_chapters("696-daoist-gu-1")
        content = self.api.get_chapter_content("696-daoist-gu-1", "1", "1", "main")

        self.assertEqual("Преподобный Гу", info["rus_name"])
        self.assertEqual("Gu Zhen Ren", info["eng_name"])
        self.assertEqual("https://ranobe.space/api/media/20280?v=5&size=medium", info["cover"]["default"])
        self.assertEqual(["1", "1.5"], [chapter["number"] for chapter in chapters])
        self.assertEqual("main", chapters[0]["branches"][0]["branch_id"])
        self.assertIn('src="https://ranobe.space/api/media/42?v=1&amp;size=big"', content["content"])

    def test_sanitizes_remote_chapter_html(self):
        content = sanitize_chapter_html(
            '<script>alert(1)</script><p onclick="alert(2)">Text</p>'
            '<img src="https://127.0.0.1/secret"><img src="/api/media/42" onerror="alert(3)">'
        )

        self.assertNotIn("script", content)
        self.assertNotIn("onclick", content)
        self.assertNotIn("127.0.0.1", content)
        self.assertNotIn("onerror", content)
        self.assertEqual('<p>Text</p><img src="https://ranobe.space/api/media/42" />', content)
        ET.fromstring(f"<div>{content}</div>")

    def test_epub_preserves_webp_cover_type(self):
        storage = SimpleNamespace(
            file_exists=lambda path: True,
            get_file_stream=lambda path: io.BytesIO(b"webp-cover"),
        )
        novel = SimpleNamespace(
            title="Title",
            eng_name=None,
            rus_name=None,
            description="Description",
            cover_path="alllib/covers/book.webp",
        )

        with patch("app.modules.alllib.epub_builder.get_storage", return_value=storage):
            epub_bytes = EPUBBuilder.build_epub(novel, [])

        with zipfile.ZipFile(io.BytesIO(epub_bytes)) as epub:
            self.assertIn("OEBPS/cover.webp", epub.namelist())
            manifest = epub.read("OEBPS/content.opf").decode()
            self.assertIn('href="cover.webp" media-type="image/webp"', manifest)


if __name__ == "__main__":
    unittest.main()
