"""A media card's poster has to survive an unlock, and it has to be encrypted.

Two separate faults, both found the hard way:

- The download worker wrote the poster path, title and dimensions into
  `canvas_data`, which is a SEALED_FIELD. For an item in a sealed collection that
  write landed in the readable JSON, the next unlock restored the older blob over
  it, and the card lost its face. `media_mime` leaked the same way.
- The poster itself was written with `save_file`, so a sealed collection held an
  encrypted video next to a readable JPEG.

The poster is also served with the wrong media type for anything but a JPEG, and
the endpoint read its path out of the field that had just lost it.
"""

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from app.core.storage import LocalStorage
from app.modules.vault import images, tasks
from app.modules.vault.images import media_type_for
from app.modules.vault.router import _apply_media_state


def _dict_string_keys(node):
    """Literal string keys of a dict expression, including `**spread` merges."""
    import ast

    if not isinstance(node, ast.Dict):
        return []
    keys = []
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.append(key.value)
        elif isinstance(key, ast.Dict):
            keys.extend(_dict_string_keys(key))
    return keys


class PosterIsEncryptedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.storage = LocalStorage(str(self.root))
        self.patch = unittest.mock.patch.object(tasks, "_storage_root", return_value=self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.patch_storage = unittest.mock.patch.object(tasks, "get_storage", return_value=self.storage)
        self.patch_storage.start()
        self.addCleanup(self.patch_storage.stop)

    def _store(self, content_type="image/jpeg") -> str:
        fetched = (b"\xff\xd8\xff\xe0fake-jpeg", content_type, "image/jpeg")
        with unittest.mock.patch("app.core.remote_fetch.fetch_bytes_checked", return_value=fetched):
            path = tasks._store_thumbnail(self.storage, {"thumbnail": "https://example.test/p.jpg"}, 7)
        self.assertIsNotNone(path)
        return str(path)

    def test_the_stored_poster_is_not_readable_on_disk(self):
        path = self._store()

        raw = (self.root / path).read_bytes()
        self.assertNotIn(b"fake-jpeg", raw)
        self.assertTrue(self.storage.looks_encrypted(path))

    def test_the_poster_reads_back_to_the_original_bytes(self):
        path = self._store()

        self.assertEqual(b"\xff\xd8\xff\xe0fake-jpeg", self.storage.read_maybe_encrypted(path))

    def test_the_name_records_the_media_type_so_the_endpoint_can_answer_with_it(self):
        self.assertEqual("image/png", media_type_for(self._store("image/png")))

    def test_a_poster_without_a_url_is_skipped_rather_than_failing(self):
        self.assertIsNone(tasks._store_thumbnail(self.storage, {}, 7))


class PosterSurvivesTheSealTests(unittest.TestCase):
    """The worker cannot re-seal an item, so it must not write into the seal."""

    def test_no_writer_puts_media_metadata_into_canvas_data(self):
        """A writer that rebuilds `canvas_data` with a media key is the original bug.

        Checked by parsing rather than grepping: the fault is a dict *value* with a
        `media_*` key, which no substring test can see reliably.
        """
        import ast

        offenders = []
        for path in Path("app").rglob("*.py"):
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                if not any(
                    isinstance(target, ast.Attribute) and target.attr == "canvas_data"
                    for target in node.targets
                ):
                    continue
                for key in _dict_string_keys(node.value):
                    if key.startswith("media_"):
                        offenders.append(f"{path}:{node.lineno} -> canvas_data[{key!r}]")
        self.assertEqual([], offenders)

    def test_the_card_takes_its_face_from_the_structural_column(self):
        item = unittest.mock.Mock(
            media_path="vault/videos/7-clip.mp4.enc",
            media_status="completed",
            media_duration=321.5,
            media_thumbnail_path="vault/thumbnails/7.jpg.enc",
            id=7,
        )
        serialized = {"has_image": False, "og_image": None}

        result = _apply_media_state(serialized, item)

        self.assertEqual("/api/vault/items/7/thumbnail", result["og_image"])
        self.assertEqual("completed", result["media_status"])
        self.assertEqual(321, result["media_duration"])

    def test_a_pasted_screenshot_still_wins_over_the_poster(self):
        item = unittest.mock.Mock(
            media_path="vault/videos/7-clip.mp4.enc",
            media_status="completed",
            media_duration=None,
            media_thumbnail_path="vault/thumbnails/7.jpg.enc",
            id=7,
        )
        serialized = {"has_image": True, "og_image": "/api/vault/items/7/image"}

        self.assertEqual("/api/vault/items/7/image", _apply_media_state(serialized, item)["og_image"])

    def test_a_video_without_a_poster_leaves_the_card_alone(self):
        item = unittest.mock.Mock(
            media_path="vault/videos/7-clip.mp4.enc",
            media_status="completed",
            media_duration=None,
            media_thumbnail_path=None,
            id=7,
        )

        self.assertIsNone(_apply_media_state({"has_image": False, "og_image": None}, item)["og_image"])


class LegacyPosterTests(unittest.TestCase):
    def test_a_poster_written_before_the_change_is_still_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = LocalStorage(str(root))
            storage.save_file(b"plain-old-jpeg", "vault/thumbnails/3.jpg")

            # The bytes on disk are readable, which is exactly why the reader has
            # to ask instead of assuming an envelope.
            self.assertFalse(storage.looks_encrypted("vault/thumbnails/3.jpg"))
            self.assertEqual(b"plain-old-jpeg", storage.read_maybe_encrypted("vault/thumbnails/3.jpg"))

    def test_the_pasted_image_helper_keeps_its_own_media_type_rules(self):
        self.assertEqual("image/png", images.media_type_for("vault/images/5.png.enc"))
        self.assertEqual("image/png", images.media_type_for("vault/images/5.unknown.enc"))


if __name__ == "__main__":
    unittest.main()
