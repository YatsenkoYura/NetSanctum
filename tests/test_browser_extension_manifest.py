"""The extension has to reach a player that lives in a blob: iframe.

A JavaScript video player is an iframe whose document is created with
`URL.createObjectURL`, so its URL is `blob:https://site/uuid`. That URL matches
none of the patterns a content script can be given, so the script is never
injected there and the overlay simply never appears on such players. The flag
that fixes this is easy to drop by accident, which is what this test is for.
"""

import json
import unittest
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "clients" / "browser-extension" / "manifest.json"


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def content_script() -> dict:
    scripts = load_manifest()["content_scripts"]
    assert len(scripts) == 1, "the extension declares exactly one content script"
    return scripts[0]


class ContentScriptReachesBlobFramesTests(unittest.TestCase):
    def test_blob_frames_are_matched_by_the_initiator_origin(self):
        # Without this, `<all_urls>` and friends never match a blob: document and
        # the script is not injected into the player at all.
        self.assertTrue(content_script()["match_origin_as_fallback"])

    def test_about_blank_frames_are_matched(self):
        self.assertTrue(content_script()["match_about_blank"])

    def test_every_frame_is_a_target(self):
        # The player is a child frame; injecting only in the top document would
        # leave the video unreachable.
        self.assertTrue(content_script()["all_frames"])

    def test_the_match_patterns_carry_an_explicit_path(self):
        # Chrome refuses to honour match_origin_as_fallback unless the patterns
        # end in a path of `*`, so `<all_urls>` is not safe here.
        for pattern in content_script()["matches"]:
            self.assertNotEqual("<all_urls>", pattern)
            self.assertTrue(pattern.endswith("/*"), pattern)

    def test_the_scripts_run_in_the_isolated_world(self):
        # Reading a blob: URL and the page's own video element needs the page's
        # DOM, which only the isolated world shares.
        self.assertNotEqual("MAIN", content_script().get("world", "ISOLATED"))

    def test_the_permission_still_covers_every_scheme(self):
        # The content-script patterns changed; the permission must stay broad or
        # the extension cannot read pages at all.
        self.assertIn("<all_urls>", load_manifest()["permissions"])

    def test_the_manifest_is_valid_json_and_declares_mv2(self):
        manifest = load_manifest()
        self.assertEqual(2, manifest["manifest_version"])
        self.assertIn("background", manifest)


if __name__ == "__main__":
    unittest.main()
