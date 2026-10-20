"""A space that is gone must cost the owner a setting, not a capture.

The extension remembers which space to capture into and keeps that id in its own
storage, so it outlives the space: delete the space, or restore from a backup
that did not have it, and every capture afterwards names an id that no longer
exists. That used to be a foreign-key violation — a 500, on every capture, with
nothing in it about what was wrong.

The server now refuses such a capture by name, and the extension corrects the
setting and retries into no space at all. The picture still lands.

What is *not* done is the general case: any other 422 is a request the client got
wrong and is not the extension's to fix, and a 500 is the server's problem. Both
must reach the owner unchanged, because silently moving their cards somewhere
they did not ask for is worse than a failed save.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "clients" / "browser-extension"
BACKGROUND = (ROOT / "background.js").read_text(encoding="utf-8")


def section(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end, source.index(start))]


class TheServerRefusesByNameTests(unittest.TestCase):
    def test_a_gone_space_is_refused_before_anything_is_written(self):
        services = Path("app/modules/vault/services.py").read_text(encoding="utf-8")
        guard = section(services, "async def _assert_capture_space_exists", "async def create_captured_item")
        self.assertIn("VaultCollection.id == capture.collection_id", guard)
        self.assertIn("no longer exists", guard)
        # A `ValueError`, so the endpoint's existing handler answers 422.
        self.assertIn("raise ValueError(", guard)

    def test_the_check_runs_before_the_insert(self):
        services = Path("app/modules/vault/services.py").read_text(encoding="utf-8")
        capture = section(services, "async def create_captured_item(", "def _is_acceptable_still")
        self.assertLess(
            capture.index("_assert_capture_space_exists"),
            capture.index("create_vault_item"),
            "the space is checked before the row is written, not after",
        )

    def test_a_capture_with_no_space_is_not_checked(self):
        services = Path("app/modules/vault/services.py").read_text(encoding="utf-8")
        guard = section(services, "async def _assert_capture_space_exists", "async def create_captured_item")
        self.assertIn("if capture.collection_id is None:", guard)


class TheExtensionRepairsTheSettingTests(unittest.TestCase):
    def test_the_failure_travels_with_a_status_the_extension_can_read(self):
        """A 422 is a request the client got wrong and may fix itself; a 500 is
        the server's problem. Only the number tells them apart, and the error
        text alone cannot."""
        fetcher = section(BACKGROUND, "async function apiFetch(", "/* A read.")
        self.assertIn("failure.status = response.status", fetcher)

    def test_the_retry_goes_in_with_no_space_at_all(self):
        """Not into a default space: the owner chose one, it is gone, and a
        different space is a guess. Unfiled is the honest place for it."""
        store = section(BACKGROUND, "async function store(", "/* ── Capture flow")
        self.assertIn("collection_id: null", store)
        self.assertIn("netsanctumStorageSet({ collectionId: null })", store)

    def test_it_retries_once_and_only_for_that_one_refusal(self):
        store = section(BACKGROUND, "async function store(", "/* ── Capture flow")
        self.assertIn("if (!isGoneSpace(error) || payload.collection_id === null) throw error;", store)
        # The first attempt and one retry. A loop would re-repair a setting that is
        # already null and turn one lost space into a request storm.
        self.assertEqual(2, store.count("apiFetch(CAPTURE_PATH"))

    def test_the_recognition_is_on_the_message_not_on_the_bare_status(self):
        """Every 422 is not this. Another request's bad input must not move this
        owner's cards into a space they did not choose."""
        self.assertIn("error.status !== 422", BACKGROUND)
        self.assertIn("no longer exists", BACKGROUND)

    def test_the_owner_is_told_rather_than_left_to_find_out(self):
        store = section(BACKGROUND, "async function store(", "/* ── Capture flow")
        self.assertIn("notify(", store)
        self.assertIn("unfiled", store)

    def test_the_setting_correction_survives_a_restart(self):
        """`chrome.storage.local` is where the stale id lives in the first place —
        fixing only the in-memory copy would break again on the next capture."""
        store = section(BACKGROUND, "async function store(", "/* ── Capture flow")
        self.assertIn("settings.collectionId = null", store)


if __name__ == "__main__":
    unittest.main()
