"""The ceilings on card fields must not fire on ordinary input.

Every bound added here is a weight limit, not a safety one, and a weight limit
that trips on a real page is worse than no limit at all: it looks like a working
save and loses the capture. So these tests assert the opposite of what a bound
test usually asserts — that a 10 MiB inline picture, a 1500-character title, a
7000-character URL, 150 tags and a 4 MiB whiteboard all go in, and only that
absurd input does not.
"""

import base64
import unittest

from app.modules.vault.schemas import (
    MAX_CANVAS_BYTES,
    MAX_CONTENT_CHARS,
    MAX_OG_IMAGE_CHARS,
    MAX_TAGS,
    MAX_URL_CHARS,
    VaultCaptureCreate,
    VaultItemCreate,
    VaultItemUpdate,
)


def png_data_url(size: int) -> str:
    return "data:image/png;base64," + base64.b64encode(b"x" * size).decode()


class OrdinaryInputTests(unittest.TestCase):
    """Shapes the dashboard and the extension actually produce."""

    def test_an_inline_picture_survives_the_write(self):
        """The dashboard puts a data URL straight into `og_image`.

        This is the one that would have broken every card saved with a picture:
        an image is not a URL and a URL-sized ceiling cuts it in half.
        """
        card = VaultItemCreate(title="t", og_image=png_data_url(10 * 1024 * 1024))

        self.assertLessEqual(len(card.og_image or ""), MAX_OG_IMAGE_CHARS)

    def test_a_capture_with_a_long_title_and_many_tags(self):
        capture = VaultCaptureCreate(
            kind="media",
            title="Заголовок" * 200,
            image=png_data_url(512),
            tags=["  из   пробелов  ", "я" * 400, "", "обычный"] * 60,
        )

        self.assertLessEqual(len(capture.tags), MAX_TAGS)
        self.assertEqual("из пробелов", capture.tags[0])

    def test_a_url_that_stuffs_its_query(self):
        prefix = "https://example.com/?"
        card = VaultItemCreate(title="t", url=prefix + "a" * 7000)

        self.assertEqual(len(prefix) + 7000, len(card.url or ""))
        self.assertLess(len(card.url or ""), MAX_URL_CHARS)

    def test_a_long_note(self):
        card = VaultItemCreate(title="t", content="c" * 900_000)

        self.assertLessEqual(len(card.content or ""), MAX_CONTENT_CHARS)

    def test_a_big_whiteboard(self):
        card = VaultItemCreate(title="t", canvas_data={"drawing": "x" * (4 * 1024 * 1024)})

        self.assertIn("drawing", card.canvas_data)

    def test_tags_are_trimmed_rather_than_refused(self):
        """A paste of many tags is normal; failing the whole write is not."""
        card = VaultItemCreate(title="t", tags=["a"] * 500)

        self.assertEqual(MAX_TAGS, len(card.tags))


class AbsurdInputTests(unittest.TestCase):
    """The ceiling that is left is a weight ceiling."""

    def test_a_canvas_past_the_ceiling_is_refused(self):
        with self.assertRaises(ValueError):
            VaultItemCreate(title="t", canvas_data={"drawing": "x" * (MAX_CANVAS_BYTES + 1)})

    def test_a_url_past_the_ceiling_is_refused(self):
        with self.assertRaises(ValueError):
            VaultItemCreate(title="t", url="u" * (MAX_URL_CHARS + 1))

    def test_the_update_path_is_bounded_too(self):
        """A bound that only exists on create is a bound with a hole in it."""
        with self.assertRaises(ValueError):
            VaultItemUpdate(url="u" * (MAX_URL_CHARS + 1))
        with self.assertRaises(ValueError):
            VaultItemUpdate(canvas_data={"drawing": "x" * (MAX_CANVAS_BYTES + 1)})

    def test_an_update_without_those_fields_is_untouched(self):
        update = VaultItemUpdate(title="t")

        self.assertIsNone(update.tags)
        self.assertIsNone(update.canvas_data)
        self.assertIsNone(update.url)


class CaptureUrlTests(unittest.TestCase):
    def test_a_long_page_url_is_accepted(self):
        capture = VaultCaptureCreate(
            kind="media",
            title="t",
            image=png_data_url(64),
            page_url="https://example.com/?" + "a" * 7000,
        )

        self.assertTrue(capture.page_url.startswith("https://"))

    def test_a_capture_url_must_still_be_a_real_address(self):
        """The length bound is not a licence to accept anything."""
        with self.assertRaises(ValueError):
            VaultCaptureCreate(kind="media", title="t", image=png_data_url(64), page_url="file:///etc/passwd")

        with self.assertRaises(ValueError):
            VaultCaptureCreate(
                kind="media", title="t", image=png_data_url(64), page_url="https://user:pass@example.com/"
            )


if __name__ == "__main__":
    unittest.main()
