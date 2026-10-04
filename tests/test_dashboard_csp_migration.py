"""The dashboard's CSP may only claim what the template can support.

`/vault/dashboard` gets a real Content-Security-Policy today, with
`'unsafe-inline'` still permitted for scripts because the template carries
inline handlers and two inline script blocks. Shipping the policy without
`'unsafe-inline'` first would break the dashboard in a way that looks like a bug
in the seal: buttons stop working and nothing says why.

These tests pin the two ends of that migration. The audit script is the
checklist, and the policy test asserts the gap is still declared rather than
quietly closed over a template that cannot run without it.
"""

import contextlib
import io
import unittest
from pathlib import Path

from scripts.dashboard_inline_audit import format_report, main, report

DASHBOARD = Path("app/modules/vault/templates/vault_dashboard.html")


class AuditToolTests(unittest.TestCase):
    """The tool has to be right, or the migration is done against a bad list."""

    def setUp(self):
        self.source = DASHBOARD.read_text()

    def test_it_finds_every_handler_kind_the_template_uses(self):
        summary = report([DASHBOARD])

        events = {handler.event for handler in summary.handlers}
        self.assertEqual({"click", "input", "change", "keydown", "error"}, events)
        self.assertGreater(summary.handler_count, 0)

    def test_it_groups_identical_call_sites(self):
        """Nine calls to one function are one migration, not nine."""
        summary = report([DASHBOARD])

        self.assertGreaterEqual(summary.by_function["closeUnlockModal"], 1)
        self.assertEqual(
            summary.handler_count,
            sum(summary.by_function.values()),
            "every handler must be counted exactly once",
        )

    def test_it_does_not_trip_on_words_that_contain_on(self):
        """`once`, `only` and `button` are not event handlers."""
        summary = report([DASHBOARD])

        self.assertNotIn("button", summary.by_function)
        self.assertNotIn("only", summary.by_function)

    def test_a_script_with_a_src_is_not_an_inline_script(self):
        summary = report([DASHBOARD])

        self.assertEqual(
            2,
            summary.script_blocks[str(DASHBOARD)],
            "two inline blocks; the third script tag loads netsanctum-calendar.js from /static",
        )

    def test_a_nonced_script_is_not_counted(self):
        path = Path("/tmp/opencode/nonced.html")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('<script nonce="abc">var a = 1;</script><button onclick="f()">x</button>')

        summary = report([path])

        self.assertEqual(0, summary.script_count)
        self.assertEqual(1, summary.handler_count)

    def test_require_none_fails_while_the_migration_is_unfinished(self):
        """The tripwire, checked while it is still armed."""
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--require-none", str(DASHBOARD)])

        self.assertEqual(1, code)

    def test_require_none_passes_on_a_migrated_template(self):
        path = Path("/tmp/opencode/migrated.html")
        path.write_text('<script nonce="abc">var a = 1;</script><button data-on="f">x</button>')

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--require-none", str(path)])

        self.assertEqual(0, code)

    def test_the_report_names_the_templates_it_read(self):
        printed = format_report([DASHBOARD])

        self.assertIn(str(DASHBOARD), printed)
        self.assertIn("delegated listener", printed)


class PolicyHonestyTests(unittest.TestCase):
    """A policy must not be stricter than the page can survive."""

    def test_the_script_gap_is_still_declared_in_the_policy(self):
        from app.core.http_security import DASHBOARD_CONTENT_SECURITY_POLICY

        self.assertIn("script-src 'self' 'unsafe-inline'", DASHBOARD_CONTENT_SECURITY_POLICY)

    def test_the_rest_of_the_policy_is_already_tight(self):
        from app.core.http_security import DASHBOARD_CONTENT_SECURITY_POLICY

        for directive in (
            "default-src 'none'",
            "object-src 'none'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
            "connect-src 'self'",
            "form-action 'self'",
        ):
            self.assertIn(directive, DASHBOARD_CONTENT_SECURITY_POLICY)

    def test_the_policy_only_covers_the_dashboard(self):
        """Not a site-wide header by accident."""
        from app.core.http_security import DASHBOARD_CSP_PREFIXES

        self.assertEqual(("/vault/dashboard",), DASHBOARD_CSP_PREFIXES)

    def test_the_two_are_consistent_today(self):
        """If the template is migrated, this test is the thing that fails first.

        It is the intended failure: it says the policy is now stronger than the
        template needs, which is the last step of the migration and not a
        regression.
        """
        summary = report([DASHBOARD])
        from app.core.http_security import DASHBOARD_CONTENT_SECURITY_POLICY

        has_gap = summary.remaining > 0
        permits_inline = "script-src 'self' 'unsafe-inline'" in DASHBOARD_CONTENT_SECURITY_POLICY

        self.assertEqual(
            has_gap,
            permits_inline,
            "the policy and the template disagree about 'unsafe-inline'",
        )


if __name__ == "__main__":
    unittest.main()
