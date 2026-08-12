import asyncio
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from app.core.storage import LocalStorage
from app.modules.alllib.api import LibParser
from app.modules.alllib.router import _create_pairing_code, _is_allowed_lib_url, _verify_pairing_code
from app.modules.vault.services import _is_public_http_url


class AttributeCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.attributes: dict[str, str | None] = {}

    def handle_starttag(self, tag, attrs):
        self.attributes.update(attrs)


class LibParserSecurityTests(unittest.TestCase):
    def setUp(self):
        self.parser = LibParser()

    def test_text_and_link_attributes_are_escaped(self):
        content = [
            {
                "type": "text",
                "text": "<script>alert(1)</script>",
                "marks": [{"type": "link", "attrs": {"href": "javascript:alert(1)"}}],
            }
        ]

        rendered = self.parser.json_to_html(content, [])

        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("href='#'", rendered)

    def test_image_drops_untrusted_attributes(self):
        content = [
            {
                "type": "image",
                "attrs": {
                    "src": "https://img.cdnlibs.org/page.jpg",
                    "alt": 'cover" onerror="alert(1)',
                    "onerror": "alert(1)",
                },
            }
        ]

        rendered = self.parser.json_to_html(content, [])
        collector = AttributeCollector()
        collector.feed(rendered)

        self.assertNotIn("onerror", collector.attributes)
        self.assertIn("&quot; onerror=&quot;", rendered)


class ExternalFetchSecurityTests(unittest.TestCase):
    def test_image_proxy_only_accepts_known_https_hosts(self):
        self.assertTrue(_is_allowed_lib_url("https://img.cdnlibs.org/page.jpg"))
        self.assertTrue(_is_allowed_lib_url("https://ranobelib.me/uploads/page.jpg"))
        self.assertFalse(_is_allowed_lib_url("http://cdnlibs.org/page.jpg"))
        self.assertFalse(_is_allowed_lib_url("https://cdnlibs.org.example.com/page.jpg"))
        self.assertFalse(_is_allowed_lib_url("https://127.0.0.1/admin"))

    def test_pairing_code_requires_valid_signature(self):
        self.assertTrue(_verify_pairing_code(_create_pairing_code()))
        self.assertFalse(_verify_pairing_code("invalid"))
        self.assertFalse(_verify_pairing_code("nonce.invalid-signature"))

    def test_vault_metadata_fetch_rejects_private_addresses(self):
        self.assertFalse(asyncio.run(_is_public_http_url("http://127.0.0.1/admin")))
        self.assertFalse(asyncio.run(_is_public_http_url("http://[::1]/admin")))
        self.assertFalse(asyncio.run(_is_public_http_url("file:///etc/passwd")))


class LocalStorageSecurityTests(unittest.TestCase):
    def test_similarly_prefixed_sibling_is_not_inside_storage_root(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent) / "storage"
            storage = LocalStorage(str(root))

            with self.assertRaises(ValueError):
                storage.save_file(b"secret", "../storage-backup/escaped.txt")


if __name__ == "__main__":
    unittest.main()
