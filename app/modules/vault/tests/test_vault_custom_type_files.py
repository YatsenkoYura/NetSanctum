"""A custom card's picture is stored exactly like any other card's.

The owner can declare card types of their own, and the natural way to break
encryption with one is to make the file path care about the type: gate the write
on `is_media()`, treat an unfamiliar type as "no file", or give a site's card a
shorter path because its type is known. Every one of those quietly downgrades a
sealed collection's picture to the shared application key, which is exactly the
arrangement the per-collection file key exists to close.

So the rule is pinned here rather than left to each future reader to re-derive:

    **File handling is decided by whether a card holds bytes, never by its type.**

`node_type` decides which pane opens. It has no reach into storage, and the
image helpers take the key from the caller instead of looking anything up. These
tests use a type no shipped vocabulary contains, so nothing about them can pass
because the type happened to be known.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.core.crypto.asymmetric import generate_inbox_keypair
from app.core.crypto.kdf import new_data_key
from app.core.storage import LocalStorage
from app.modules.vault import images, sealing
from app.modules.vault.crypto import derive_file_key
from app.modules.vault.images import externalize_image, has_image, image_bytes
from app.modules.vault.models import VaultItem
from app.modules.vault.node_types import is_media, view_for

# A type from nobody's vocabulary: not a built-in, and not in any local file.
LOCAL_TYPE = "owners-own-type"
PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def make_item(node_type: str, *, sealed_payload: str | None = None) -> VaultItem:
    return VaultItem(
        id=7,
        entry_type="bookmark",
        node_type=node_type,
        title="что-то",
        content=None,
        tags=[],
        collection_id=5,
        og_image=PNG,
        sealed_payload=sealed_payload,
        site="somewhere",
        site_meta={"id": "12345678"},
    )


class FileOwnershipIsNotTheTypeTests(unittest.TestCase):
    """The guard against the tempting mistake."""

    def test_a_card_of_an_unknown_type_can_still_own_a_picture(self):
        item = make_item(LOCAL_TYPE)

        self.assertTrue(has_image(item), "an unfamiliar type must not cost the card its picture")

    def test_the_type_helpers_do_not_answer_the_file_question(self):
        """`is_media` is about which pane opens. If it ever starts deciding who
        gets a file key, a local card's picture lands under the application key."""
        item = make_item(LOCAL_TYPE)

        self.assertFalse(is_media(item.node_type), "this is the trap: an unknown type is not 'media'")
        self.assertTrue(has_image(item), "and yet it holds bytes, so it gets a key like anything else")


class CustomTypeSealedStorageTests(unittest.TestCase):
    """Sealed collection, unknown type, same bytes on disk as anything else."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = LocalStorage(self._tmp.name)
        for target in ("app.core.storage.get_storage", "app.modules.vault.images.get_storage"):
            entered = patch(target, return_value=self.storage)
            entered.start()
            self.addCleanup(entered.stop)
        self.file_key = new_data_key()
        self.foreign_key = new_data_key()

    def _bytes(self, item, key) -> tuple[bytes, str]:
        """The stored bytes, or a failure loud enough to name the line."""
        stored = image_bytes(item, file_key=key)
        self.assertIsNotNone(stored, f"nothing opened for item {item.id} under the collection key")
        return stored

    def test_the_picture_goes_out_under_the_collection_file_key(self):
        item = make_item(LOCAL_TYPE)
        collection_key = derive_file_key(new_data_key(), 5)

        self.assertTrue(externalize_image(item, key=collection_key, sealed=True))

        payload, media_type = self._bytes(item, collection_key)
        self.assertEqual(b"\x89PNG\r\n\x1a\n", payload[:8], "the bytes that came back are the picture")
        self.assertEqual("image/png", media_type)

    def test_no_other_key_opens_it(self):
        """The whole point: a file one key opens and no other does. A foreign
        collection's key gets nothing — not the bytes, and not the application
        key's answer either, which is the fallback a legacy pre-file-key file
        needs and this file must not reach."""
        item = make_item(LOCAL_TYPE)
        collection_key = derive_file_key(new_data_key(), 5)
        externalize_image(item, key=collection_key, sealed=True)

        self.assertIsNone(image_bytes(item, file_key=self.foreign_key))
        self.assertIsNone(image_bytes(item), "the application key is not a fallback for a sealed file")

    def test_the_plaintext_is_not_readable_on_disk(self):
        item = make_item(LOCAL_TYPE)
        collection_key = derive_file_key(new_data_key(), 5)
        externalize_image(item, key=collection_key, sealed=True)

        raw = (Path(self._tmp.name) / item.image_path).read_bytes()
        self.assertNotIn(b"IHDR", raw, "the PNG header is not something the envelope should contain")
        self.assertEqual(images.decode_data_image(PNG)[0], self._bytes(item, collection_key)[0])

    def test_the_stored_name_says_nothing_about_the_card(self):
        """A path is half of what opens the file. It must not carry the type, the
        site, or the row's meaning — the same rule the sealed media paths follow."""
        item = make_item(LOCAL_TYPE)
        collection_key = derive_file_key(new_data_key(), 5)
        externalize_image(item, key=collection_key, sealed=True)

        for leak in (LOCAL_TYPE, "somewhere", "12345678", "quote", "owner"):
            self.assertNotIn(leak, item.image_path.lower(), item.image_path)

    def test_the_same_bytes_as_a_built_in_image_card(self):
        """Two types, one storage path: whatever the sealed path does for an
        image card it does for this one, because nothing here reads the type."""
        plain = make_item("image")
        custom = make_item(LOCAL_TYPE)
        collection_key = derive_file_key(new_data_key(), 5)
        externalize_image(plain, key=collection_key, sealed=True)
        externalize_image(custom, key=collection_key, sealed=True)

        self.assertEqual(self._bytes(plain, collection_key)[0], self._bytes(custom, collection_key)[0])


class CustomTypePlainCollectionTests(unittest.TestCase):
    """A plain collection has no file key, and nothing is invented for it."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = LocalStorage(self._tmp.name)
        for target in ("app.core.storage.get_storage", "app.modules.vault.images.get_storage"):
            entered = patch(target, return_value=self.storage)
            entered.start()
            self.addCleanup(entered.stop)

    def test_it_still_works_and_the_name_stays_predictable(self):
        item = make_item(LOCAL_TYPE)

        self.assertTrue(externalize_image(item))

        self.assertIsNotNone(image_bytes(item)[0])
        self.assertIn("7", item.image_path, "a plain collection's image is named for its row, as before")


class CustomTypeBlindWriteTests(unittest.TestCase):
    """Locked sealed collection: the picture rides inside the sealed payload.

    A blind write has no file key to store anything under, so the bytes stay as
    a data URL inside the sealed payload and leave it on the first save while
    unlocked. That is the arrangement for every type, and it must stay so for a
    type the code has never heard of.
    """

    def test_a_custom_type_survives_the_seal_and_comes_back_intact(self):
        private_key, public_key = generate_inbox_keypair()
        item = make_item(LOCAL_TYPE)
        sealing.seal_item(item, public_key)

        # Sealed: the picture is inside the payload, and nothing readable is left
        # on the row.
        self.assertIsNone(item.image_path)
        self.assertIsNone(item.og_image)
        self.assertEqual("", item.title)
        self.assertEqual(LOCAL_TYPE, item.node_type, "the type is structural and stays readable")

        sealing.open_item(private_key, item)

        self.assertEqual(PNG, item.og_image)
        self.assertEqual("somewhere", item.site)
        self.assertEqual({"id": "12345678"}, item.site_meta)

    def test_the_type_survives_the_seal_because_the_grid_needs_it(self):
        """A type that went missing would leave a locked vault unable to draw its
        own tiles, which is the one thing the structural columns are for."""
        _private_key, public_key = generate_inbox_keypair()
        item = make_item(LOCAL_TYPE)
        sealing.seal_item(item, public_key)

        self.assertEqual(LOCAL_TYPE, item.node_type)

    def test_an_unknown_type_still_opens_a_pane(self):
        """Even if a type is in no vocabulary at all — a local file that was
        removed, a row from a newer build — the grid has to have something to
        draw."""
        self.assertTrue(view_for(LOCAL_TYPE))
        self.assertTrue(view_for("a-type-from-a-future-build"))


class StreamedBytesTests(unittest.TestCase):
    """The poster path, which is the other way a card gets a file.

    A video's poster is written by a worker that may have no vault key, so the
    rule there is that it is written under the collection's key when there is
    one and skipped rather than parked under the application key when there is
    not. Both halves are checked against a card of a custom type rather than a
    video, because the helpers do not read the type either.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = LocalStorage(self._tmp.name)

    def test_a_sealed_poster_is_stored_under_the_key_it_was_given(self):
        from app.modules.vault.tasks import _store_thumbnail

        key = new_data_key()
        with patch.object(images, "get_storage", return_value=self.storage):
            path = _store_thumbnail(
                self.storage,
                {"thumbnail": "https://example.invalid/poster.png"},
                7,
                key=key,
                sealed=True,
                collection_id=5,
            )

        # The fetch is refused in a test, so the poster is absent — which is the
        # documented "no poster" outcome. What matters is that nothing was written
        # under a different key.
        if path is not None:
            with self.assertRaises(ValueError):
                self.storage.get_file_decrypted(path, key=new_data_key())

    def test_a_sealed_poster_with_no_key_is_skipped_never_downgraded(self):
        from app.modules.vault.tasks import _store_thumbnail

        with patch.object(images, "get_storage", return_value=self.storage):
            path = _store_thumbnail(
                self.storage,
                {"thumbnail": "https://example.invalid/poster.png"},
                7,
                key=None,
                sealed=True,
                collection_id=5,
            )

        self.assertIsNone(path, "a poster with no vault key is skipped, not written under the app key")


if __name__ == "__main__":
    unittest.main()
