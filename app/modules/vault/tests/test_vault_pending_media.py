"""A sealed collection's video used to wait for somebody who holds its key.

The browser extension has no passphrase, so it creates the card with the
address and the title already inside the sealed payload and stops. That wait
is `pending_unlock`: the card is complete, it just has no video yet, and an
unlocked tab starts the download the worker then stores under the vault's key.

New captures no longer wait at all — the worker stores them at once as blind
writes (see `tests/test_vault_blind_media.py`) — but rows parked before that
change, and collections whose inbox key is unusable, still travel this path.
These tests pin the status and, just as importantly, the refusal in the middle
of it: a sealed video with no key has nowhere to go, and parking it under the
shared application key — which is what happened before — is the whole reason
the file key exists.
"""

import unittest

from app.modules.vault.tasks import (
    SEALED_MEDIA_PREFIX,
    MediaError,
    attach_downloaded_media,
    sealed_media_owner,
    sealed_media_path,
    video_storage_name,
)


class SealedMediaPathTests(unittest.TestCase):
    def test_a_sealed_path_is_random_and_names_its_row(self):
        first = sealed_media_path(5, 91)
        second = sealed_media_path(5, 91)

        self.assertTrue(first.startswith("vault/5/91/"))
        self.assertNotEqual(first, second, "the name must not repeat")
        self.assertTrue(first.endswith(".mp4.enc"))
        self.assertEqual(SEALED_MEDIA_PREFIX.format(collection_id=5, item_id=91), "vault/5/91")

    def test_the_owner_is_readable_back_from_the_path(self):
        self.assertEqual((5, 91), sealed_media_owner(sealed_media_path(5, 91)))

    def test_a_legacy_path_claims_nothing(self):
        """The old layout, and anything a plain collection writes.

        None is not an error: it is how the reader tells "this object is under
        the application key, serve it as before" from "this one belongs to a row".
        Those older videos are a documented residual risk, not a crash.
        """
        for path in (
            "vault/videos/91-abcdef.mp4.enc",
            "vault/images/12-0011223344556677.png.enc",
            "vault/videos/clip.mp4",
            "",
        ):
            self.assertIsNone(sealed_media_owner(path), path)

    def test_a_plain_card_keeps_its_readable_name(self):
        stem, ext = video_storage_name(
            sealed=False, info={"id": "abc123"}, url="https://example.com/x", ext=".mp4"
        )

        self.assertEqual("abc123", stem)
        self.assertEqual(".mp4", ext)


class SealedAttachmentTests(unittest.TestCase):
    def make_item(self, **overrides):
        from app.modules.vault.models import VaultItem

        values = {
            "id": 91,
            "entry_type": "bookmark",
            "title": "",
            "content": None,
            "url": None,
            "og_title": None,
            "og_description": None,
            "og_image": None,
            "tags": [],
            "canvas_data": {},
            "category": None,
            "score": None,
            "status": None,
            "media_mime": None,
            "public_title": None,
        }
        values.update(overrides)
        item = VaultItem()
        for field, value in values.items():
            setattr(item, field, value)
        return item

    def test_a_sealed_card_gets_no_readable_metadata_from_the_worker(self):
        """Duration and dimensions are sealed fields now, and this has no key."""
        # Sealing blanks these, so they arrive empty: what matters is that a
        # download does not put them back.
        item = self.make_item()

        attach_downloaded_media(
            item,
            sealed=True,
            media_path=sealed_media_path(5, 91),
            thumbnail_path=sealed_media_path(5, 91, "jpg.enc"),
            size=4096,
            mime="video/mp4",
            title="Секретный выпуск",
            info={"duration": 1800.0, "width": 1920, "height": 1080},
        )

        self.assertTrue(item.media_path.startswith("vault/5/91/"))
        self.assertIsNone(item.media_duration, "duration is sealed; the worker has no key")
        self.assertIsNone(item.media_width)
        self.assertIsNone(item.media_height)
        self.assertIsNone(item.media_title)
        self.assertIsNone(item.media_mime)

    def test_the_structural_columns_are_still_written(self):
        """What the card needs to show its state, and what says nothing."""
        item = self.make_item()

        attach_downloaded_media(
            item,
            sealed=True,
            media_path="vault/5/91/abc.enc",
            thumbnail_path="vault/5/91/def.enc",
            size=4096,
            mime="video/mp4",
            title="Секретный выпуск",
            info={"duration": 1800.0},
        )

        self.assertEqual("vault/5/91/abc.enc", item.media_path)
        self.assertEqual(4096, item.media_size)
        self.assertEqual("completed", item.media_status)

    def test_a_plain_card_still_gets_everything(self):
        item = self.make_item()

        attach_downloaded_media(
            item,
            sealed=False,
            media_path="vault/videos/91-abc.mp4",
            thumbnail_path="vault/thumbnails/91.jpg.enc",
            size=4096,
            mime="video/mp4",
            title="Обычное название",
            info={"duration": 42.0, "width": 640, "height": 480},
        )

        self.assertEqual(42.0, item.media_duration)
        self.assertEqual(640, item.media_width)
        self.assertEqual("video/mp4", item.media_mime)
        self.assertEqual("Обычное название", item.title)


class PendingUnlockStatusTests(unittest.TestCase):
    def test_pending_is_a_status_and_not_an_error(self):
        """The card is complete; it has no video yet and says so."""
        from app.modules.vault.tasks import MEDIA_PENDING_STATUS

        self.assertEqual("pending_unlock", MEDIA_PENDING_STATUS)
        self.assertNotIn("error", MEDIA_PENDING_STATUS)

    def test_the_error_codes_do_not_include_the_old_text(self):
        """The column carries a code from a closed vocabulary and nothing else."""
        codes = {member.value for member in MediaError}

        self.assertNotIn("pending_unlock", codes)
        for code in codes:
            # Words, not prose: nothing here may carry an address, a message or a
            # sentence, because this column is readable while the vault is sealed.
            self.assertNotIn(":", code, code)
            self.assertNotIn("/", code, code)
            self.assertNotIn(".", code, code)
            self.assertNotIn("http", code, code)
            self.assertEqual(code.lower(), code, code)
        self.assertIn("network", codes)
        self.assertIn("geo blocked", codes)


if __name__ == "__main__":
    unittest.main()
