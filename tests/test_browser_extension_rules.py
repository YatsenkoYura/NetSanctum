"""The site rules, from the server to the page and back.

The extension downloads rules and runs their selectors in the page before it
saves a capture, which makes it the one place in this feature where a bad input
becomes code execution. So the wiring is pinned here as source-level contracts —
the pattern this repository already uses for the overlay — and the extractor
itself is exercised against a real DOM in `jsdom` by the developer who changes a
selector.

What these tests hold: the rules arrive from the instance over the same
authorization as a capture, they are filtered down to shapes the extension can
act on before anything runs them, and no failure anywhere on that path can stop a
picture from being saved.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "clients" / "browser-extension"
BACKGROUND = (ROOT / "background.js").read_text(encoding="utf-8")
CONTENT = (ROOT / "content.js").read_text(encoding="utf-8")


def section(source: str, start: str, end: str) -> str:
    return source[source.index(start) : source.index(end, source.index(start))]


class RulesArriveOverTheSameAuthorizationTests(unittest.TestCase):
    def test_the_rules_are_fetched_not_hardcoded(self):
        self.assertIn("/api/vault/capture-rules", BACKGROUND)
        self.assertNotIn("pixiv", BACKGROUND.lower(), "no site belongs in the extension")

    def test_the_fetch_is_a_get_that_carries_the_token(self):
        """`apiFetch` always sends a body; a GET with one is ignored at best and
        refused by a proxy at worst."""
        getter = section(BACKGROUND, "async function apiGet(", "/* ── Site rules")
        self.assertIn('method: "GET"', getter)
        self.assertIn("Authorization", getter)
        self.assertIn("ensureSession", getter)

    def test_a_stale_token_is_retried_rather_than_giving_up_on_the_rules(self):
        getter = section(BACKGROUND, "async function apiGet(", "/* ── Site rules")
        self.assertIn("ensureSession(true)", getter)

    def test_the_server_side_endpoint_needs_the_bearer_token(self):
        """Which sites an owner has rules for is itself a description of their
        browsing — the same reason `node_type` may not name a site."""
        router = Path("app/modules/vault/router.py").read_text(encoding="utf-8")
        endpoint = section(router, '@router.get("/api/vault/capture-rules"', '@router.get("/api/vault/stats"')
        self.assertIn("get_current_bearer_user", endpoint)

    def test_the_response_carries_no_compiled_pattern(self):
        schemas = Path("app/modules/vault/schemas.py").read_text(encoding="utf-8")
        self.assertIn("class VaultCaptureRulesResponse", schemas)


class NothingExecutableArrivesTests(unittest.TestCase):
    """A rule is data. If it could carry a function, this endpoint would be a way
    to run code in every browser the owner has."""

    def test_the_extension_keeps_only_shapes_it_can_act_on(self):
        filterer = section(BACKGROUND, "function usableRules(", "async function loadCaptureRules")
        self.assertIn('typeof rule.match.host !== "string"', filterer)
        self.assertIn('typeof spec.selector !== "string"', filterer)

    def test_an_attribute_is_a_name_not_a_property_read(self):
        """`spec.attr` is passed to getAttribute, so it can only ever read a named
        attribute — but the extractor still has to say so."""
        extractor = section(CONTENT, "function extractValue(", "function extractFields")
        self.assertIn("getAttribute(attr)", extractor)
        self.assertNotIn("element[", extractor)

    def test_the_server_refuses_an_attribute_outside_the_language(self):
        rules_module = Path("app/modules/vault/capture_rules.py").read_text(encoding="utf-8")
        allowlist = section(rules_module, "ALLOWED_ATTR_NAMES = frozenset(", "def _text(")
        for attr in ("style", "innerHTML", "onclick"):
            self.assertNotIn(f'"{attr}"', allowlist, attr)


class NothingOnThatPathCanCostAPictureTests(unittest.TestCase):
    """A capture rule is an enrichment. Every failure on this path has to end in
    "the picture was saved anyway"."""

    def test_a_failed_rules_fetch_is_swallowed(self):
        loader = section(BACKGROUND, "async function loadCaptureRules(", "function ruleForUrl")
        self.assertIn("catch", loader)
        self.assertIn("captureRules = []", loader)

    def test_reading_the_fields_is_swallowed_at_both_ends(self):
        reader = section(BACKGROUND, "async function readRuleFields(", "/* ── Image helpers")
        self.assertIn("catch", reader)
        self.assertIn("return {}", reader)
        extract = section(CONTENT, "function extractFields(", "/* ── Cross-frame")
        self.assertIn("catch", extract)

    def test_the_enrichment_happens_after_the_picture_and_before_the_upload(self):
        """After, because the overlay comes off while the document is still there
        and the bytes are already in hand; before, because the upload is where it
        has to be attached."""
        flow = section(BACKGROUND, "const rule = ruleForUrl(page.url);", 'stage = "upload"')
        self.assertIn("readRuleFields", flow)
        self.assertIn("payload.site", flow)
        self.assertIn("payload.site_meta", flow)

    def test_a_page_with_no_rule_is_untouched(self):
        flow = section(BACKGROUND, "const rule = ruleForUrl(page.url);", 'stage = "upload"')
        self.assertIn("if (rule)", flow)


class TheExtractorIsBoundedTests(unittest.TestCase):
    """A `many` selector on a page with four thousand tags is the normal case, not
    the exotic one, so the count is capped per field as well as per value."""

    def test_every_value_and_count_is_capped(self):
        extractor = section(CONTENT, "function extractFields(", "/* ── Cross-frame")
        self.assertIn("MAX_EXTRACT_CHARS", extractor)
        self.assertIn("MAX_EXTRACT_VALUES", extractor)
        self.assertIn("MAX_EXTRACT_FIELDS", extractor)
        self.assertIn(".slice(0, MAX_EXTRACT_CHARS)", extractor)
        self.assertIn("if (values.length >= cap) break;", extractor)

    def test_the_rules_are_the_only_thing_the_page_is_asked(self):
        """One message, one job: the background names the fields, the page runs
        them. The page is never asked to fetch anything."""
        handler = section(CONTENT, 'message.type === "netsanctum:extract"', "if (message.type ===")
        self.assertIn("extractFields(message.fields)", handler)
        self.assertNotIn("fetch(", handler)


class MatchingIsHostThenPathTests(unittest.TestCase):
    def test_a_subdomain_matches_but_a_lookalike_does_not(self):
        matcher = section(BACKGROUND, "function ruleForUrl(", "/* What the rule read off")
        self.assertIn('host !== wanted && !host.endsWith("." + wanted)', matcher)
        self.assertIn('parsed.protocol !== "http:"', matcher)

    def test_only_the_path_is_matched(self):
        """A query string is not part of a page's shape; matching it would let one
        rule claim every page of a site."""
        matcher = section(BACKGROUND, "function ruleForUrl(", "/* What the rule read off")
        self.assertIn("parsed.pathname", matcher)
        self.assertNotIn("parsed.search", matcher)


class RulesAreReadOnceTests(unittest.TestCase):
    def test_the_server_snapshots_the_rule_set(self):
        """`rule_for_url` runs on every capture; re-validating every rule each
        time would put a filesystem walk and a burst of warnings in the middle of
        saving a picture."""
        rules_module = Path("app/modules/vault/capture_rules.py").read_text(encoding="utf-8")
        self.assertIn("_LOADED", rules_module)
        self.assertIn("def forget()", rules_module)

    def test_the_extension_reads_them_before_the_overlay_goes_up(self):
        arm = section(BACKGROUND, "async function arm(tab)", "async function captureTab")
        self.assertLess(arm.index("loadCaptureRules"), arm.index("netsanctum:arm"))

    def test_the_extension_caches_within_a_lifetime(self):
        self.assertIn("RULES_MAX_AGE_MS", BACKGROUND)


if __name__ == "__main__":
    unittest.main()
