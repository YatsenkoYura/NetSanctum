import unittest

from app.core.remote_fetch import host_in_allowlist
from app.modules.alllib.mangadex import MANGADEX_SITE_ID, MangaDexAPI
from app.modules.alllib.ranobehub import get_source_api
from app.modules.alllib.tasks import ALLLIB_COVER_HOSTS, get_branch_options

MANGA_ID = "a96676e5-8ae2-425e-b549-7f15dd34a6d8"
CHAPTER_ID = "11111111-2222-3333-4444-555555555555"


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self):
        self.requests = []
        self.posts = []

    def get(self, url, params=None, **kwargs):
        self.requests.append((url, params))
        if url.endswith(f"/manga/{MANGA_ID}"):
            return FakeResponse(
                {
                    "result": "ok",
                    "data": {
                        "id": MANGA_ID,
                        "attributes": {
                            "title": {"en": "Test Manga", "ja-ro": "Tesuto"},
                            "description": {"en": "A <great> manga"},
                            "originalLanguage": "ja",
                            "year": 2020,
                            "status": "ongoing",
                            "publicationDemographic": "seinen",
                            "availableTranslatedLanguages": ["en", "ru"],
                            "tags": [{"attributes": {"name": {"en": "Action"}}}],
                        },
                        "relationships": [
                            {"type": "cover_art", "attributes": {"fileName": "cover.jpg"}},
                            {"type": "author", "attributes": {"name": "Author"}},
                        ],
                    },
                }
            )
        if url.endswith(f"/manga/{MANGA_ID}/feed"):
            return FakeResponse(
                {
                    "result": "ok",
                    "total": 2,
                    "data": [
                        {
                            "id": CHAPTER_ID,
                            "attributes": {
                                "volume": "1",
                                "chapter": "1",
                                "title": "Opening",
                                "externalUrl": None,
                                "isUnavailable": False,
                            },
                            "relationships": [
                                {
                                    "id": "group-a",
                                    "type": "scanlation_group",
                                    "attributes": {"name": "Group"},
                                }
                            ],
                        },
                        {
                            "id": "duplicate-release",
                            "attributes": {
                                "volume": "1",
                                "chapter": "1",
                                "title": "Duplicate",
                                "externalUrl": None,
                                "isUnavailable": False,
                            },
                            "relationships": [
                                {
                                    "id": "group-b",
                                    "type": "scanlation_group",
                                    "attributes": {"name": "Other Group"},
                                }
                            ],
                        },
                    ],
                }
            )
        if url.endswith(f"/at-home/server/{CHAPTER_ID}"):
            return FakeResponse(
                {
                    "result": "ok",
                    "baseUrl": "https://uploads.example.net/token",
                    "chapter": {"hash": "hash", "data": ["1.jpg", "2.jpg"], "dataSaver": []},
                }
            )
        raise AssertionError(f"Unexpected request: {url} {params}")

    def post(self, url, json=None, **kwargs):
        self.posts.append((url, json))
        return FakeResponse({})


class MangaDexAPITests(unittest.TestCase):
    def setUp(self):
        self.api = MangaDexAPI()
        self.session = FakeSession()
        self.api.session = self.session  # type: ignore[assignment]

    def test_selects_source_and_extracts_title_id(self):
        url = f"https://mangadex.org/title/{MANGA_ID}/test-manga"

        self.assertEqual(MANGA_ID, self.api.extract_slug_from_url(url))
        self.assertEqual((MANGADEX_SITE_ID, "mangadex.org"), self.api.get_site_info_from_url(url))
        self.assertIsInstance(get_source_api(url), MangaDexAPI)

    def test_maps_metadata_feed_and_at_home_pages(self):
        info = self.api.get_novel_info(MANGA_ID)
        chapters = self.api.get_novel_chapters(MANGA_ID)
        content = self.api.get_chapter_content(MANGA_ID, "1", "1", "group-a")

        self.assertEqual("Test Manga", info["name"])
        self.assertEqual("A &lt;great&gt; manga", info["summary"])
        self.assertEqual(["en", "ru"], info["available_languages"])
        self.assertEqual(
            f"https://uploads.mangadex.org/covers/{MANGA_ID}/cover.jpg.512.jpg",
            info["cover"]["default"],
        )
        self.assertEqual(2, len(chapters))
        self.assertEqual("Opening [Group]", chapters[0]["name"])
        self.assertEqual("group-a", chapters[0]["branches"][0]["branch_id"])
        self.assertEqual(
            [
                "https://uploads.example.net/token/data/hash/1.jpg",
                "https://uploads.example.net/token/data/hash/2.jpg",
            ],
            [page["url"] for page in content["pages"]],
        )

        feed_request = next(params for url, params in self.session.requests if url.endswith("/feed"))
        self.assertEqual("en", feed_request["translatedLanguage[]"])

    def test_passes_selected_language_to_source(self):
        api = get_source_api(f"https://mangadex.org/title/{MANGA_ID}", language="ru")

        self.assertIsInstance(api, MangaDexAPI)
        self.assertEqual("ru", api.language)

    def test_allows_official_mangadex_at_home_hosts(self):
        self.assertTrue(host_in_allowlist("node.mangadex.network", ALLLIB_COVER_HOSTS))
        self.assertFalse(host_in_allowlist("mangadex.network.attacker.example", ALLLIB_COVER_HOSTS))

    def test_maps_lib_translation_branches_to_options(self):
        chapters = [
            {
                "branches": [
                    {"branch_id": 4667, "teams": [{"name": "Nippa"}]},
                    {"branch_id": 4666, "teams": [{"name": "Japit Comics"}]},
                ]
            },
            {"branches": [{"branch_id": 4667, "teams": [{"name": "Nippa"}]}]},
        ]

        self.assertEqual(
            [
                {"value": "4667", "label": "Nippa", "count": 2},
                {"value": "4666", "label": "Japit Comics", "count": 1},
            ],
            get_branch_options(chapters),
        )

    def test_reports_external_at_home_downloads(self):
        self.api.report_image_result("https://uploads.example.net/data/page.jpg", True, 1234, 42, True)

        self.assertEqual("https://api.mangadex.network/report", self.session.posts[0][0])
        self.assertEqual(
            {
                "url": "https://uploads.example.net/data/page.jpg",
                "success": True,
                "bytes": 1234,
                "duration": 42,
                "cached": True,
            },
            self.session.posts[0][1],
        )


if __name__ == "__main__":
    unittest.main()
