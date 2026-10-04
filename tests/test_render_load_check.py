"""The load check has to be right, or it is a linter with a browser costume.

`scripts/render_load_check.py` exists because three bugs in a row got past every
other instrument: an action registered before its function existed, a registry
defined after the page that uses it, and a second registry block that a
first-block-only check never read. All three presented as `x is not defined` on a
line nobody was looking at, and `node --check` is silent about every one of them
because the code is correct and the order is wrong.

These tests pin the two ways that can go wrong: it must actually catch a name
that is not defined, and it must not report the stub's own limits as findings.
"""

import shutil
import unittest
from pathlib import Path

from scripts.render_load_check import inline_scripts, run


def setUpModule() -> None:
    """Skip the whole module where there is no node.

    The runtime image has no node, and a test that needs one is not a failure
    there — it is a test that cannot run. `run()` raises rather than pretending,
    because a checker that quietly found nothing is worse than one that says so.
    """
    if not shutil.which("node"):
        raise unittest.SkipTest("node is not installed")


class ExtractionTests(unittest.TestCase):
    def test_it_takes_only_inline_scripts(self):
        html = (
            '<script src="/static/a.js"></script>'
            "<script>var inline = 1;</script>"
            '<script nonce="abc">var also = 2;</script>'
        )

        self.assertEqual(["var inline = 1;", "var also = 2;"], inline_scripts(html))


class DetectionTests(unittest.TestCase):
    """A checker that cannot fail is not a checker."""

    def test_a_name_that_does_not_exist_is_caught(self):
        """The bug this whole tool exists for."""
        # At the top level: a name inside a callback that never fires is not a
        # load error, and a checker that reported it would be reporting noise.
        outcome = run("<script>missingThing();</script>")

        self.assertEqual(1, len(outcome["failures"]))
        self.assertIn("missingThing", outcome["failures"][0]["error"])

    def test_a_late_definition_is_caught_when_it_is_really_late(self):
        """Two script blocks: the second uses a name the first never declared."""
        outcome = run("<script>window.netX = {};</script><script>notDeclaredAnywhere();</script>")

        self.assertTrue(outcome["failures"])
        self.assertIn("notDeclaredAnywhere", outcome["failures"][0]["error"])

    def test_a_declaration_in_the_same_block_is_not_a_failure(self):
        """Otherwise every ordinary function declaration would be reported."""
        outcome = run("<script>function fine() { return 1; } window.fine = fine;</script>")

        self.assertEqual([], outcome["failures"])

    def test_the_scripts_run_in_document_order(self):
        """Order is the whole point: two blocks share one global scope, and the
        second seeing what the first defined is not a bug."""
        outcome = run(
            "<script>var shared = 1;</script>"
            "<script>if (shared !== 1) { throw new Error('wrong order'); }</script>"
        )

        self.assertEqual([], outcome["failures"])


class StubLimitTests(unittest.TestCase):
    """What the stub cannot do is not a finding."""

    def test_the_browser_apis_a_page_reaches_for_are_present(self):
        """Each of these was a false positive before it was a line in the stub."""
        api = (
            "window.addEventListener('x', () => {});"
            "document.head.appendChild(document.createElement('script'));"
            "document.querySelectorAll('div').forEach(() => {});"
            "customElements.define('x-y', class extends HTMLElement {});"
            "new MutationObserver(() => {}).observe(document.body, {});"
            "new Audio(); new Image(); document.title.length;"
            "navigator.clipboard.writeText('x'); matchMedia('(min-width: 1px)').matches;"
            "localStorage.getItem('k'); fetch('/x').then(() => {});"
        )

        self.assertEqual([], run(f"<script>{api}</script>")["failures"])

    def test_a_missing_file_is_reported_rather_than_guessed_at(self):
        from scripts.render_load_check import main

        self.assertEqual(2, main(["/nonexistent/page.html"]))


class RealPageTests(unittest.TestCase):
    """If a page has been saved for inspection, hold it to the same standard."""

    PAGE = Path("/tmp/opencode/vault-dashboard.html")

    def test_the_saved_dashboard_has_no_load_time_reference_errors(self):
        if not self.PAGE.is_file():
            self.skipTest(f"no saved page at {self.PAGE}")
        outcome = run(self.PAGE.read_text())

        self.assertEqual([], outcome["failures"], outcome["failures"])
        self.assertGreaterEqual(outcome["blocks"], 4)


if __name__ == "__main__":
    unittest.main()
