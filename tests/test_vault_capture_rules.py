"""The site rules the extension runs before it saves a capture.

A rule says "on this host, on this path, read these few fields", so an
illustration lands in the archive with its id, its author and its tags instead of
as a picture alone. The rule language is deliberately tiny — a host, a path
pattern, and CSS selectors — because the extension *executes* it: a rule that
could carry a function would be a way to run code in every browser the owner has,
on every site they visit, from a server the owner may only be renting.

So these tests hold two things: that a rule does what it says on a real page,
and that nothing a rule can say turns into something the page should not do.
"""

import unittest
from unittest.mock import patch

from app.modules.vault import capture_rules
from app.modules.vault.capture_rules import (
    ALLOWED_ATTR_NAMES,
    MAX_FIELDS_PER_RULE,
    public_shape,
    rule_for_url,
    rules,
    validate_rule,
)
from app.modules.vault.node_types import looks_like_provenance


def a_rule(**overrides) -> dict:
    base = {
        "id": "example",
        "site": "example",
        "node_type": "image",
        "match": {"host": "example.com", "path": "^/thing/[0-9]+$"},
        "fields": {"title": {"selector": "h1", "attr": "text"}},
    }
    base.update(overrides)
    return base


class ValidationTests(unittest.TestCase):
    def test_a_well_formed_rule_survives(self):
        rule = validate_rule(a_rule())

        self.assertIsNotNone(rule)
        self.assertEqual("example", rule["id"])
        self.assertEqual(
            {"selector": "h1", "attr": "text", "many": False, "max": 40}, rule["fields"]["title"]
        )

    def test_the_site_defaults_to_the_host(self):
        self.assertEqual("example.com", validate_rule(a_rule(site=None))["site"])

    def test_a_rule_that_is_not_an_object_is_refused(self):
        for junk in (None, [], "example", 7, True):
            self.assertIsNone(validate_rule(junk), repr(junk))

    def test_a_rule_without_a_match_is_refused(self):
        self.assertIsNone(validate_rule(a_rule(match=None)))
        self.assertIsNone(validate_rule(a_rule(match={})))
        self.assertIsNone(validate_rule(a_rule(match={"host": "example.com"})))

    def test_an_unparseable_path_pattern_is_refused_rather_than_raised(self):
        """A hand-written rule is exactly the thing most likely to have a typo,
        and the endpoint that serves these must never be why a capture fails."""
        with self.assertLogs("app.modules.vault.capture_rules", level="WARNING"):
            self.assertIsNone(validate_rule(a_rule(match={"host": "x.com", "path": "([unclosed"})))

    def test_a_rule_with_no_usable_field_is_refused(self):
        """Half a rule is worse than none: it would save a card that looks right
        and is missing the field it was matched for."""
        self.assertIsNone(validate_rule(a_rule(fields={})))
        self.assertIsNone(validate_rule(a_rule(fields=None)))

    def test_an_attribute_outside_the_language_is_dropped(self):
        """Reading `style` would pull an inline data: URL of any size through a
        field meant to hold an id."""
        with self.assertLogs("app.modules.vault.capture_rules", level="WARNING"):
            rule = validate_rule(
                a_rule(fields={"leak": {"selector": "div", "attr": "style"}, "ok": {"selector": "h1"}})
            )

        self.assertEqual(["ok"], list(rule["fields"]))

    def test_the_allowlist_is_an_allowlist(self):
        for attr in ("style", "innerHTML", "onclick", "data-anything", ""):
            self.assertNotIn(attr, ALLOWED_ATTR_NAMES, attr)

    def test_a_selector_is_capped(self):
        rule = validate_rule(a_rule(fields={"x": {"selector": "a" * 5000, "attr": "text"}}))

        self.assertLessEqual(len(rule["fields"]["x"]["selector"]), 500)

    def test_the_field_count_is_capped(self):
        fields = {f"f{i}": {"selector": "h1"} for i in range(MAX_FIELDS_PER_RULE + 20)}

        self.assertLessEqual(len(validate_rule(a_rule(fields=fields))["fields"]), MAX_FIELDS_PER_RULE)

    def test_a_value_cap_above_the_ceiling_is_pulled_back(self):
        rule = validate_rule(a_rule(fields={"tags": {"selector": "a", "many": True, "max": 5000}}))

        self.assertLessEqual(rule["fields"]["tags"]["max"], 40)

    def test_a_nonsense_max_is_the_default_rather_than_an_error(self):
        for junk in ("many", None, -3, {}):
            rule = validate_rule(a_rule(fields={"tags": {"selector": "a", "many": True, "max": junk}}))
            self.assertGreaterEqual(rule["fields"]["tags"]["max"], 1, repr(junk))


class ATypeMustNotNameItsSiteTests(unittest.TestCase):
    """The rule the rest of the module rests on.

    `node_type` is structural: the grid reads it while the vault is locked, so a
    value like `pixiv` in that column publishes the owner's browsing to anyone
    holding the database — sealed vault or not. A rule is therefore not allowed
    to ask for one, and the site goes into the sealed `site` field instead.
    """

    def test_a_rule_naming_its_source_is_refused_not_merely_trimmed(self):
        with self.assertLogs("app.modules.vault.capture_rules", level="WARNING"):
            rule = validate_rule(a_rule(node_type="pixiv"))

        self.assertIsNone(rule)

    def test_no_shipped_rule_names_its_source_in_its_type(self):
        """The site name itself is fine — it goes into a sealed field, and a card
        that says nothing about where it came from is not much of an archive
        entry. The card *type* is the one that must stay neutral."""
        for rule in rules():
            self.assertFalse(looks_like_provenance(rule["node_type"]), rule["id"])
            self.assertTrue(rule["site"], rule["id"])

    def test_a_neutral_type_is_allowed(self):
        for neutral in ("image", "bookmark", "note", "my-own-type"):
            self.assertIsNotNone(validate_rule(a_rule(node_type=neutral)), neutral)

    def test_the_shipped_pixiv_rule_asks_for_a_neutral_type(self):
        rule = validate_rule(
            {
                "id": "p",
                "site": "pixiv",
                "match": {"host": "pixiv.net", "path": "^/a$"},
                "fields": {"t": {"selector": "h1"}},
            }
        )

        self.assertEqual("image", rule["node_type"])
        self.assertEqual("pixiv", rule["site"])


class MatchingTests(unittest.TestCase):
    def setUp(self):
        # The fixture is the whole rule set, so a test is about the matching and
        # not about whatever sites happen to ship.
        capture_rules.forget()
        self.addCleanup(capture_rules.forget)
        self.cleanups = []
        entered = patch.object(capture_rules, "_shipped", return_value=[a_rule()])
        entered.start()
        self.addCleanup(entered.stop)

    def test_the_page_shape_is_matched(self):
        rule = rule_for_url("https://example.com/thing/42")

        self.assertIsNotNone(rule)
        self.assertEqual("example", rule["id"])

    def test_a_subdomain_is_the_same_site(self):
        self.assertIsNotNone(rule_for_url("https://www.example.com/thing/42"))

    def test_a_lookalike_host_is_not_the_same_site(self):
        for url in ("https://notexample.com/thing/42", "https://example.com.evil.test/thing/42"):
            self.assertIsNone(rule_for_url(url), url)

    def test_a_different_path_is_not_claimed(self):
        self.assertIsNone(rule_for_url("https://example.com/other/42"))
        self.assertIsNone(rule_for_url("https://example.com/thing/42/extra"))

    def test_a_query_string_is_not_part_of_the_page_shape(self):
        """Otherwise one rule would claim every page of a site."""
        self.assertIsNotNone(rule_for_url("https://example.com/thing/42?ref=timeline"))

    def test_a_non_page_url_is_never_claimed(self):
        for url in (None, "", "about:blank", "file:///tmp/x.html", "javascript:void(0)"):
            self.assertIsNone(rule_for_url(url), repr(url))


class ShippedRuleFileTests(unittest.TestCase):
    """Unpatched: the file that actually ships has to parse and match."""

    def setUp(self):
        capture_rules.forget()
        self.addCleanup(capture_rules.forget)

    def test_the_shipped_file_loads_and_claims_the_page_it_was_written_for(self):
        self.assertTrue(rules(), "the shipped rule file should not be empty")
        self.assertEqual("pixiv-artwork", rule_for_url("https://www.pixiv.net/artworks/98765432")["id"])
        self.assertEqual("booru-post", rule_for_url("https://danbooru.donmai.us/posts/1")["id"])

    def test_the_shipped_file_claims_nothing_else(self):
        for url in ("https://example.org/", "https://pixiv.net/users/1", "https://www.pixiv.net/"):
            self.assertIsNone(rule_for_url(url), url)


class ShadowingTests(unittest.TestCase):
    """A local rule with the same id replaces the shipped one.

    The only kind of override an owner needs is "the site changed its markup, use
    my selectors", and it has to unseat the original rather than queue behind it —
    the extension matches the first rule that claims a page.
    """

    def setUp(self):
        capture_rules.forget()
        self.addCleanup(capture_rules.forget)

    def test_a_local_rule_replaces_a_shipped_one_by_id(self):
        replacement = a_rule(
            id="pixiv-artwork", site="pixiv", match={"host": "pixiv.net", "path": "^/artworks/"}
        )
        with patch.object(capture_rules, "local", return_value=[replacement]):
            found = [r for r in rules() if r["id"] == "pixiv-artwork"]

        self.assertEqual(1, len(found), "a shadow must replace, not duplicate")
        self.assertEqual(
            {"selector": "h1", "attr": "text", "many": False, "max": 40}, found[0]["fields"]["title"]
        )


class APatternIsTheShapeOfAValueTests(unittest.TestCase):
    """An id on the web is usually *inside* a URL, not an attribute of its own.

    `og:image` on an illustration page ends in `.../98765432_p0_master1200.jpg`
    and the id is the part worth keeping. A rule that can only read whole
    attributes would store that URL under the name `illust_id` — which is the
    half-a-rule failure this module refuses everywhere else, and an invisible one,
    because the field is present and looks right.
    """

    def test_a_pattern_survives_onto_the_field(self):
        rule = validate_rule(
            a_rule(fields={"illust_id": {"selector": "meta", "attr": "content", "regex": "/([0-9]+)_p"}})
        )

        self.assertEqual("/([0-9]+)_p", rule["fields"]["illust_id"]["regex"])
        self.assertEqual(1, rule["fields"]["illust_id"]["group"])

    def test_the_group_is_taken_when_named(self):
        rule = validate_rule(a_rule(fields={"x": {"selector": "meta", "regex": "a(b)(c)", "group": 2}}))

        self.assertEqual(2, rule["fields"]["x"]["group"])

    def test_an_unparseable_pattern_drops_the_field_rather_than_being_ignored(self):
        """Silently dropping the *pattern* would leave the field reading the whole
        attribute — a URL stored under `illust_id`, visible to nobody as a
        mistake."""
        with self.assertLogs("app.modules.vault.capture_rules", level="WARNING"):
            rule = validate_rule(a_rule(fields={"broken": {"selector": "meta", "regex": "([unclosed"}}))

        self.assertIsNone(rule, "a rule whose only field asked for an impossible pattern has nothing to read")

    def test_an_oversized_pattern_drops_the_field(self):
        with self.assertLogs("app.modules.vault.capture_rules", level="WARNING"):
            rule = validate_rule(a_rule(fields={"big": {"selector": "meta", "regex": "a" * 5000}}))

        self.assertIsNone(rule)

    def test_an_unparseable_pattern_alongside_a_good_field_keeps_the_good_one(self):
        with self.assertLogs("app.modules.vault.capture_rules", level="WARNING"):
            rule = validate_rule(
                a_rule(
                    fields={
                        "broken": {"selector": "meta", "regex": "([unclosed"},
                        "title": {"selector": "h1"},
                    }
                )
            )

        self.assertEqual(["title"], list(rule["fields"]))

    def test_the_shipped_rules_all_compile(self):
        for rule in rules():
            for name, spec in rule["fields"].items():
                if "regex" in spec:
                    self.assertIsInstance(spec["regex"], str, f"{rule['id']}.{name}")
                    self.assertLessEqual(len(spec["regex"]), 200, f"{rule['id']}.{name}")


class WhatTheExtensionReceivesTests(unittest.TestCase):
    def test_no_compiled_pattern_is_serialised(self):
        """A `re.Pattern` is not JSON, and the extension matches the URL with the
        source pattern anyway."""
        import json

        shaped = public_shape(validate_rule(a_rule()))

        self.assertNotIn("_path_re", shaped)
        self.assertTrue(json.dumps(shaped))

    def test_the_shape_carries_everything_the_extension_needs(self):
        shaped = public_shape(validate_rule(a_rule()))

        self.assertEqual({"host", "path"}, set(shaped["match"]))
        self.assertIn("selector", shaped["fields"]["title"])
        self.assertIn("attr", shaped["fields"]["title"])
        self.assertEqual("example", shaped["id"])


if __name__ == "__main__":
    unittest.main()
