"""A screenshot must not stay in the database as base64 text.

It used to: the `data:` URL from the extension or the clipboard landed verbatim in
`og_image`, a text column. That cost a third more space than the bytes needed and
left the most private thing in Vault unencrypted while the video files were
encrypted. These tests hold the file-and-fallback behaviour in place.
"""

import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.core.storage import LocalStorage
from app.modules.vault.models import VaultItem


def png_data_url(payload: bytes = b"\x89PNG\r\n\x1a\n" + b"payload" * 20) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


class ImageStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.patches = [
            patch("app.modules.vault.images.storage_root", return_value=self.root),
            patch("app.modules.vault.images.get_storage", return_value=LocalStorage(str(self.root))),
        ]
        for entered in self.patches:
            entered.start()
            self.addCleanup(entered.stop)

    def test_a_stored_image_lands_in_encrypted_storage(self):
        from app.modules.vault.images import store_image_bytes

        payload = b"\x89PNG" + b"secret pixels" * 50
        path = store_image_bytes(payload, "image/png", 42)

        self.assertEqual("vault/images/42.png.enc", path)
        stored = (self.root / path).read_bytes()
        self.assertNotIn(payload, stored)

    def test_a_stored_image_reads_back_byte_for_byte(self):
        from app.modules.vault.images import store_image_bytes

        payload = b"\x89PNG" + bytes(range(256)) * 4
        path = store_image_bytes(payload, "image/png", 7)

        storage = LocalStorage(str(self.root))
        self.assertEqual(payload, storage.get_file_decrypted(path))

    def test_an_unsupported_image_type_is_refused(self):
        from app.modules.vault.images import store_image_bytes

        with self.assertRaises(ValueError):
            store_image_bytes(b"x", "image/tiff", 1)


class ExternalizeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        patches = [
            patch("app.modules.vault.images.storage_root", return_value=self.root),
            patch("app.modules.vault.images.get_storage", return_value=LocalStorage(str(self.root))),
        ]
        for entered in patches:
            entered.start()
            self.addCleanup(entered.stop)

    def test_an_embedded_image_moves_out_of_the_column(self):
        from app.modules.vault.images import externalize_image

        item = VaultItem(id=11, title="Скриншот", og_image=png_data_url())

        self.assertTrue(externalize_image(item))
        self.assertIsNone(item.og_image)
        self.assertEqual("vault/images/11.png.enc", item.image_path)

    def test_an_external_url_is_left_alone(self):
        from app.modules.vault.images import externalize_image

        item = VaultItem(id=12, og_image="https://example.com/photo.png")

        self.assertFalse(externalize_image(item))
        self.assertEqual("https://example.com/photo.png", item.og_image)
        self.assertIsNone(item.image_path)

    def test_no_image_at_all_is_a_no_op(self):
        from app.modules.vault.images import externalize_image

        item = VaultItem(id=13, og_image=None)

        self.assertFalse(externalize_image(item))
        self.assertIsNone(item.image_path)


class ImageReadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        patches = [
            patch("app.modules.vault.images.storage_root", return_value=self.root),
            patch("app.modules.vault.images.get_storage", return_value=LocalStorage(str(self.root))),
        ]
        for entered in patches:
            entered.start()
            self.addCleanup(entered.stop)

    def test_a_file_backed_image_is_read_from_the_file(self):
        from app.modules.vault.images import image_bytes, store_image_bytes

        payload = b"\x89PNG" + b"pixels" * 100
        item = VaultItem(id=21, og_image=None, image_path=store_image_bytes(payload, "image/png", 21))

        self.assertEqual((payload, "image/png"), image_bytes(item))

    def test_a_legacy_row_still_reads_from_the_column(self):
        """Nothing may break for rows written before images moved to files."""
        from app.modules.vault.images import image_bytes

        url = png_data_url(b"\x89PNG" + b"old" * 40)
        item = VaultItem(id=22, og_image=url, image_path=None)

        decoded = image_bytes(item)

        self.assertEqual("image/png", decoded[1])
        self.assertEqual(url.split(",", 1)[1], base64.b64encode(decoded[0]).decode("ascii"))

    def test_a_missing_file_falls_back_to_the_column(self):
        from app.modules.vault.images import image_bytes

        item = VaultItem(
            id=23, image_path="vault/images/23.png.enc", og_image=png_data_url(b"\x89PNG" + b"kept")
        )

        self.assertIsNotNone(image_bytes(item))

    def test_a_missing_file_with_nothing_else_reads_as_absent(self):
        from app.modules.vault.images import image_bytes

        item = VaultItem(id=24, image_path="vault/images/24.png.enc", og_image=None)

        self.assertIsNone(image_bytes(item))

    def test_has_image_covers_both_carriers(self):
        from app.modules.vault.images import has_image

        self.assertTrue(has_image(VaultItem(id=1, og_image=png_data_url())))
        self.assertTrue(has_image(VaultItem(id=2, image_path="vault/images/2.png.enc")))
        self.assertFalse(has_image(VaultItem(id=3, og_image="https://example.com/a.png", image_path=None)))
        self.assertFalse(has_image(VaultItem(id=4, og_image=None, image_path=None)))

    def test_the_media_type_follows_the_file_suffix(self):
        from app.modules.vault.images import media_type_for

        self.assertEqual("image/png", media_type_for("vault/images/1.png.enc"))
        self.assertEqual("image/jpeg", media_type_for("vault/images/1.jpg.enc"))
        self.assertEqual("image/webp", media_type_for("vault/images/1.webp.enc"))


if __name__ == "__main__":
    unittest.main()
