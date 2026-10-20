"""What a capture rule read off the page, and why it is not in `node_type`.

The browser extension can be told "for these hosts, read these few fields off
the page and send them with the capture" — an illustration id, an author, the
tags something was published under. Those fields are worth having in the archive
and are far too small to justify shipping a whole page.

This file holds the guarantees around them, and one of them is a consequence of
a decision made elsewhere: the site name is **not** a card type. `node_type` has
to stay readable while the vault is locked, because the grid lays its tiles out
before anything is decrypted — so a value like `pixiv` in that column would
publish the owner's browsing to anyone holding the database, whatever the
encryption says. The site lives in a sealed field, and the card's *look* comes
from the view map instead.
"""

import unittest

from app.modules.vault.node_types import NodeType, looks_like_provenance
from app.modules.vault.schemas import (
    MAX_SITE_FIELD_CHARS,
    MAX_SITE_FIELDS,
    VaultCaptureCreate,
    VaultItemCreate,
)
from app.modules.vault.sealing import SEALED_FIELDS


class SiteFieldsStaySealedTests(unittest.TestCase):
    def test_both_are_sealed(self):
        for field in ("site", "site_meta"):
            self.assertIn(field, SEALED_FIELDS, field)

    def test_a_card_type_may_not_be_the_site(self):
        """The whole reason these are columns. A type is readable while locked,
        so a site named there would publish the browsing these fields keep
        private — and that is the property the neutral `node_type` has to hold."""
        self.assertNotIn("node_type", SEALED_FIELDS)
        self.assertTrue(looks_like_provenance("pixiv"))

    def test_a_locked_card_shows_no_metadata_rather_than_none(self):
        """The response schema promises a dict, so the locked branch settles it
        explicitly — like `canvas_data` and `tags` — instead of taking whatever
        the sealed-fields loop hands it."""
        from app.modules.vault.router import _apply_lock_state
        from tests.test_vault_locked_views import make_item

        item = make_item()
        item.site = "pixiv"
        item.site_meta = {"illust_id": "12345678"}
        item.sealed_payload = "nsp:v1:abc"

        locked = _apply_lock_state({"site": item.site, "site_meta": item.site_meta}, item, locked=True)

        self.assertEqual({}, locked["site_meta"])
        self.assertIsNone(locked["site"])

    def test_an_unlocked_card_shows_what_it_stored(self):
        from app.modules.vault.router import _apply_lock_state
        from tests.test_vault_locked_views import make_item

        item = make_item()
        item.site = "pixiv"
        item.site_meta = {"illust_id": "12345678"}
        item.sealed_payload = "nsp:v1:abc"

        open_ = _apply_lock_state({"site": item.site, "site_meta": item.site_meta}, item, locked=False)

        self.assertEqual("pixiv", open_["site"])
        self.assertEqual({"illust_id": "12345678"}, open_["site_meta"])

    def test_a_card_written_before_this_column_still_reads(self):
        """Every row that existed before the migration has NULL in this column,
        and a response schema that declares a dict turns one migration into a 500
        on every card in the vault."""
        import datetime

        from app.modules.vault.models import VaultItem
        from app.modules.vault.schemas import VaultItemResponse

        row = VaultItem(
            id=7,
            entry_type="bookmark",
            title="что-то",
            tags=[],
            collection_id=1,
            canvas_data={},
            site=None,
            site_meta=None,
            is_folder=False,
            node_type="note",
            is_pinned=False,
            is_archived=False,
            progress_current=0,
            progress_total=None,
            rewatch_count=0,
            created_at=datetime.datetime(2026, 1, 1),
            updated_at=datetime.datetime(2026, 1, 1),
        )

        self.assertEqual({}, VaultItemResponse.model_validate(row).site_meta)


class SiteMetaBoundsTests(unittest.TestCase):
    """A rule must not be able to cost a collection an unbounded blob."""

    def _capture(self, **extra):
        return VaultCaptureCreate(kind="media", title="что-то", image="data:image/png;base64,AAAA", **extra)

    def test_the_fields_survive_a_capture(self):
        capture = self._capture(site="pixiv", site_meta={"illust_id": "12345678", "pages": 4})

        self.assertEqual("pixiv", capture.site)
        self.assertEqual("12345678", capture.site_meta["illust_id"])
        self.assertEqual(4, capture.site_meta["pages"])

    def test_a_capture_without_a_rule_still_works(self):
        capture = self._capture()

        self.assertIsNone(capture.site)
        self.assertEqual({}, capture.site_meta)

    def test_the_field_count_is_capped(self):
        meta = {f"f{index}": "x" for index in range(MAX_SITE_FIELDS + 20)}

        self.assertLessEqual(len(self._capture(site_meta=meta).site_meta), MAX_SITE_FIELDS)

    def test_a_value_is_cut_rather_than_the_capture_refused(self):
        """A rule that matched one site too eagerly should cost that site's extra
        field, not the picture the owner was trying to save."""
        capture = self._capture(site_meta={"blurb": "y" * (MAX_SITE_FIELD_CHARS * 3)})

        self.assertEqual(MAX_SITE_FIELD_CHARS, len(capture.site_meta["blurb"]))

    def test_a_list_value_keeps_its_shape_and_its_cap(self):
        capture = self._capture(site_meta={"tags": [f"tag{index}" for index in range(MAX_SITE_FIELDS + 10)]})

        self.assertIsInstance(capture.site_meta["tags"], list)
        self.assertLessEqual(len(capture.site_meta["tags"]), MAX_SITE_FIELDS)

    def test_the_whole_blob_is_bounded_by_construction(self):
        """No separate bulk ceiling: the per-value and per-count caps are the
        bound, and a guard that could never fire would only look like one."""
        meta = {f"f{index}": "y" * (MAX_SITE_FIELD_CHARS * 4) for index in range(MAX_SITE_FIELDS * 4)}
        blob = self._capture(site_meta=meta).site_meta

        self.assertLessEqual(len(blob), MAX_SITE_FIELDS)
        self.assertLessEqual(
            sum(len(str(value)) for value in blob.values()),
            MAX_SITE_FIELDS * MAX_SITE_FIELD_CHARS,
        )

    def test_whitespace_only_values_are_dropped_rather_than_stored_blank(self):
        capture = self._capture(site="   ", site_meta={"blank": "   ", "real": " kept "})

        self.assertIsNone(capture.site)
        self.assertEqual({"real": "kept"}, capture.site_meta)

    def test_numbers_and_booleans_are_kept_as_themselves(self):
        """Coercing an id to a string is fine; coercing a count to "4" is the kind
        of thing that later reads as text and sorts as text."""
        capture = self._capture(site_meta={"pages": 4, "ratio": 1.5, "nsfw": False})

        self.assertEqual(4, capture.site_meta["pages"])
        self.assertEqual(1.5, capture.site_meta["ratio"])
        self.assertIs(False, capture.site_meta["nsfw"])

    def test_the_item_write_path_bounds_them_too(self):
        """Otherwise the ceiling is one endpoint away from being advisory."""
        item = VaultItemCreate(
            title="что-то",
            site="pixiv",
            site_meta={"tags": [f"tag{index}" for index in range(MAX_SITE_FIELDS + 10)]},
        )

        self.assertLessEqual(len(item.site_meta["tags"]), MAX_SITE_FIELDS)

    def test_an_edit_cannot_rewrite_what_the_page_said(self):
        """`site_meta` is deliberately absent from the update model: it is what
        the capture read, and a hand edit is not that."""
        from app.modules.vault.schemas import VaultItemUpdate

        self.assertNotIn("site_meta", VaultItemUpdate.model_fields)
        self.assertNotIn("site", VaultItemUpdate.model_fields)

    def test_the_card_type_a_capture_makes_is_still_neutral(self):
        """A rule may suggest a type, and the type must not be the site's name."""
        from app.modules.vault.node_types import CAPTURE_KINDS

        for name, spec in CAPTURE_KINDS.items():
            self.assertFalse(looks_like_provenance(spec.node_type), f"{name} -> {spec.node_type}")
        for member in NodeType:
            self.assertFalse(looks_like_provenance(member.value), member.value)


if __name__ == "__main__":
    unittest.main()
