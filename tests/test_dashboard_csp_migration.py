"""`/vault/dashboard` runs a policy with no `'unsafe-inline'` for scripts.

That is only true because of two things that are easy to undo by accident: every
script block carries a nonce, and every handler is delegated through an attribute
the policy cannot execute. Both are asserted here against the shipped templates.

The audit tool is the checklist, and it has to be right or it is worse than
nothing. Two of its earlier versions reported a page clean while handlers were
still there — once because it only read the page's own template and thirteen rode
in from the layout, once because an attribute written as `{% if x %}oninput="fn"`
has no space before it and the pattern required one. Both are now regression
tests, along with a third: it counted script blocks *after* stripping them, so it
reported zero non-nonced scripts on every file, which is a clean report from a
scan that read nothing.
"""

import contextlib
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.http_security import DASHBOARD_CONTENT_SECURITY_POLICY, dashboard_csp
from scripts.dashboard_inline_audit import HANDLER, SCRIPT_BODY, format_report, main, report

LAYOUT = Path("app/core/templates/base.html")
DASHBOARD = Path("app/modules/vault/templates/vault_dashboard.html")


class AuditToolTests(unittest.TestCase):
    """The tool, checked against a page written to contain every trap."""

    def setUp(self):
        self._dir = TemporaryDirectory()
        self.path = Path(self._dir.name) / "probe.html"
        self.path.write_text(
            '<button onclick="a()">x</button>\n'
            '<p>{%}oninput="b()">\n'
            "<div onchange='c()'>\n"
            '<span ONCLICK="upper()">\n'
            '<script>var x = "onclick=\\"y()\\"";</script>\n'
        )
        self.addCleanup(self._dir.cleanup)

    def summary(self):
        return report([self.path])

    def test_it_finds_an_attribute_written_without_a_space_before_it(self):
        """The one that hid four handlers through a whole migration."""
        self.assertIn("b()", [h.body for h in self.summary().handlers])

    def test_it_finds_a_single_quoted_handler(self):
        self.assertIn("c()", [h.body for h in self.summary().handlers])
        self.assertNotIn("", [h.body for h in self.summary().handlers])

    def test_it_finds_an_uppercase_attribute(self):
        """HTML attribute names are case-insensitive, so `ONCLICK=` runs."""
        self.assertIn("upper()", [h.body for h in self.summary().handlers])

    def test_it_does_not_count_a_handler_inside_a_script(self):
        """A template literal is where markup is *built*, not markup."""
        self.assertNotIn("y()", [h.body for h in self.summary().handlers])

    def test_it_counts_script_blocks_on_the_raw_source(self):
        self.assertEqual(1, self.summary().script_count)

    def test_it_reports_every_handler_exactly_once(self):
        summary = self.summary()

        self.assertEqual(4, summary.handler_count)
        self.assertEqual(summary.handler_count, sum(summary.by_function.values()))

    def test_a_single_quoted_body_is_read_not_emptied(self):
        """`findall` hands back `''` where a group did not take part, which made
        one handler report as an unknown action with no name."""
        self.assertIsNone(HANDLER.search("<div onchange='c()'>").group(2))

    def test_a_clean_file_reports_clean_and_counts_what_it_read(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "a.log"
            path.write_text("ordinary lines")
            summary = report([path])

        self.assertEqual(0, summary.handler_count)
        self.assertEqual(0, summary.script_blocks[str(path)], "a .log has no script blocks")

    def test_the_report_names_every_template_it_read(self):
        """A report that names one file of two is how thirteen handlers hid."""
        printed = format_report([LAYOUT, DASHBOARD])

        self.assertIn(str(LAYOUT), printed)
        self.assertIn(str(DASHBOARD), printed)


class DashboardIsCleanTests(unittest.TestCase):
    def test_no_inline_handler_remains_in_either_template(self):
        summary = report([LAYOUT, DASHBOARD])

        self.assertEqual(0, summary.handler_count, [h.body for h in summary.handlers])

    def test_every_script_block_carries_a_nonce(self):
        summary = report([LAYOUT, DASHBOARD])

        self.assertEqual(0, summary.script_count, summary.script_blocks)

    def test_the_tripwire_passes(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--require-none", str(LAYOUT), str(DASHBOARD)])

        self.assertEqual(0, code)

    def test_the_tripwire_still_fires_on_a_page_that_has_handlers(self):
        """A guard that cannot fail is not a guard."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "x.html"
            path.write_text('<button onclick="a()">x</button>')
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main(["--require-none", str(path)])

        self.assertEqual(1, code)


class PolicyTests(unittest.TestCase):
    def test_scripts_get_no_unsafe_inline(self):
        self.assertIn("script-src 'self'", DASHBOARD_CONTENT_SECURITY_POLICY)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", DASHBOARD_CONTENT_SECURITY_POLICY)

    def test_a_nonce_is_added_per_response(self):
        script_src = next(d for d in dashboard_csp("abc123").split("; ") if d.startswith("script-src"))

        self.assertEqual("script-src 'self' 'nonce-abc123'", script_src)

    def test_the_policy_without_a_nonce_still_blocks_inline_script(self):
        """A bug in the middleware must not turn into a permissive page."""
        script_src = next(d for d in dashboard_csp("").split("; ") if d.startswith("script-src"))

        self.assertEqual("script-src 'self'", script_src)

    def test_the_rest_of_the_policy_is_tight(self):
        for directive in (
            "default-src 'none'",
            "object-src 'none'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
            "connect-src 'self'",
            "form-action 'self'",
        ):
            self.assertIn(directive, DASHBOARD_CONTENT_SECURITY_POLICY)

    def test_inline_styles_are_still_allowed_and_that_is_known(self):
        """Styles are a separate piece of work. Asserting it here keeps the
        policy honest about what it does and does not cover."""
        self.assertIn("style-src 'self' 'unsafe-inline'", DASHBOARD_CONTENT_SECURITY_POLICY)

    def test_the_policy_only_covers_the_dashboard(self):
        from app.core.http_security import DASHBOARD_CSP_PREFIXES

        self.assertEqual(("/vault/dashboard",), DASHBOARD_CSP_PREFIXES)


class TemplateBoundaryTests(unittest.TestCase):
    """Where the migration's guarantee actually comes from."""

    def test_the_nonce_is_rendered_conditionally(self):
        """On a page with no nonce the attribute is omitted, not empty."""
        for path in (LAYOUT, DASHBOARD):
            self.assertIn("{% if csp_nonce() %}nonce=", path.read_text())

    def test_the_dispatcher_lives_in_the_layout(self):
        source = LAYOUT.read_text()

        self.assertIn("function netRunAction(", source)
        self.assertIn("data-net-action", source)
        self.assertNotIn("data-vault-on", DASHBOARD.read_text(), "one attribute name, not two")

    def test_the_page_registers_into_the_shared_registry(self):
        self.assertIn("Object.assign(window.netSanctumActions, {", DASHBOARD.read_text())

    def test_a_script_body_is_never_a_place_a_handler_is_counted(self):
        """The stripping the audit relies on: a `<script>` block's contents are
        code, and code may legitimately contain the text of an attribute."""
        source = DASHBOARD.read_text()
        stripped = SCRIPT_BODY.sub("", source)

        self.assertLess(len(stripped), len(source))
        self.assertNotIn("onclick=", stripped)


if __name__ == "__main__":
    unittest.main()
