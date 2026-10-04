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
import re
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


class IncludedPartialsTests(unittest.TestCase):
    """A page is its own template plus whatever the layout pulls in.

    `media_player_script.html` is included by the layout, so it is on the
    dashboard whether or not the dashboard mentions it — and it was a `<script>`
    with no nonce, which the policy blocked, which took the media player with it.
    """

    def test_every_included_template_nonces_its_inline_scripts(self):
        layout = LAYOUT.read_text()
        included = re.findall(r'\{% include "([^"]+)"', layout)
        self.assertTrue(included, "the layout includes partials")

        for name in included:
            matches = list(Path("app").rglob(name))
            self.assertTrue(matches, f"{name} is included but not in the tree")
            for path in matches:
                for line in path.read_text().splitlines():
                    if re.search(r"<script(?![^>]*\bsrc=)[^>]*>", line):
                        self.assertIn("csp_nonce", line, f"{path} has an inline script with no nonce")


def allowlist_blocks(source: str) -> list[str]:
    """Every registry block in a template.

    All of them: a template can register from more than one script block — the
    calendar does — and a check that reads only the first one reported the page
    clean while the second block was still holding bare references.
    """
    blocks = []
    for match in re.finditer(r"Object\.assign\(window\.netSanctumActions, \{", source):
        start = match.start()
        blocks.append(source[start : source.index("\n});", start)])
    return blocks


class LazyRegistrationTests(unittest.TestCase):
    """Registration must not capture a value that does not exist yet.

    A page's script is several blocks. An earlier one that lists a function
    declared in a later one captures `undefined`, and the failure appears as a
    button that throws on click with nothing wrong at registration to explain it.
    An inline attribute never had this problem: it was resolved at click time.
    """

    def setUp(self):
        self.page = DASHBOARD.read_text()

    def _allowlist(self) -> str:
        return "\n".join(allowlist_blocks(self.page))

    def test_no_action_is_registered_by_value(self):
        for line in self._allowlist().splitlines():
            stripped = line.strip().rstrip(",")
            if not stripped or stripped.startswith("//") or any(c in stripped for c in ":=('\""):
                continue
            for name in stripped.split(","):
                self.assertFalse(
                    re.fullmatch(r"\s*[A-Za-z_$][\w$]*\s*", name),
                    f"{name.strip()} is registered by value; it must be netLazy",
                )

    def test_every_named_action_goes_through_net_lazy(self):
        block = self._allowlist()
        named = re.findall(r"([A-Za-z_$][\w$]*): netLazy\('([A-Za-z_$][\w$]*)'\)", block)

        self.assertGreater(len(named), 40)
        for key, target in named:
            self.assertEqual(key, target, "a lazy registration must keep the name it stands for")

    def test_every_lazy_target_is_a_function_declaration_in_the_page(self):
        """`window[name]` is only a function if it was declared as one."""
        for _, target in re.findall(
            r"([A-Za-z_$][\w$]*): netLazy\('([A-Za-z_$][\w$]*)'\)", self._allowlist()
        ):
            # re.M, not assertRegex: `^` has to be able to match a line start in
            # a 4000-line template, and a failed assertRegex dumps the whole file
            # into the output, which buries the one name that mattered.
            declared = re.search(rf"^(?:async )?function {re.escape(target)}\(", self.page, re.M)
            self.assertTrue(declared, f"netLazy('{target}') has no function declaration to resolve")

    def test_the_layout_resolves_lazily_too(self):
        self.assertIn("function netLazy(name)", LAYOUT.read_text())


class FontPolicyTests(unittest.TestCase):
    """The page's type comes from a host the policy has to name, or it does not
    load at all — and a page that silently loses its type is easy to miss."""

    def test_the_font_origins_are_allowed(self):
        from app.core.http_security import DASHBOARD_CONTENT_SECURITY_POLICY

        self.assertIn("https://fonts.googleapis.com", DASHBOARD_CONTENT_SECURITY_POLICY)
        self.assertIn("https://fonts.gstatic.com", DASHBOARD_CONTENT_SECURITY_POLICY)

    def test_no_directive_is_declared_twice(self):
        """A repeated directive is ignored by the browser, so the second one is
        a comment that reads as policy."""
        from app.core.http_security import DASHBOARD_CONTENT_SECURITY_POLICY

        names = [directive.split()[0] for directive in DASHBOARD_CONTENT_SECURITY_POLICY.split("; ")]

        self.assertEqual(sorted(set(names)), sorted(names), names)

    def test_the_page_actually_asks_for_those_fonts(self):
        self.assertIn("fonts.googleapis.com", LAYOUT.read_text())


class OrderingTests(unittest.TestCase):
    """The dispatcher has to be defined before anything that uses it.

    Found in a browser, twice, in two different guises: an action registered by
    value when its function was declared in a later script block, and then the
    dispatcher itself sitting after `{% block content %}` in the layout, so a page
    that registered into the registry executed before the registry existed. Both
    present as `x is not defined` at a line nobody was looking at.
    """

    def setUp(self):
        self.layout = LAYOUT.read_text()

    def _line_of(self, needle: str, *, anchored: bool = False) -> int:
        """Where something is in the layout.

        `anchored` matches the start of a line, which is what a Jinja tag needs:
        a comment that mentions `{% block content %}` in prose is not a block, and
        a test that cannot tell the difference reports nonsense.
        """
        if anchored:
            match = re.search(rf"^[ \t]*{re.escape(needle)}", self.layout, re.M)
            assert match, f"{needle} not found in the layout"
            index = match.start()
        else:
            index = self.layout.index(needle)
        return self.layout[:index].count("\n") + 1

    def test_the_dispatcher_precedes_the_block_pages_render_into(self):
        dispatcher = self._line_of("function netRunAction(")
        content = self._line_of("{% block content %}", anchored=True)

        self.assertLess(
            dispatcher,
            content,
            "a page's scripts register actions into the registry; the registry must exist first",
        )

    def test_the_dispatcher_precedes_the_trailing_script_block(self):
        self.assertLess(
            self._line_of("function netRunAction("),
            self._line_of("{% block scripts_extra %}", anchored=True),
        )

    def test_a_page_uses_the_dispatcher_only_from_inside_its_own_scripts(self):
        """Every use has to be inside a script block, or it is markup."""
        page = DASHBOARD.read_text()
        outside = re.sub(r"<script[^>]*>.*?</script>", "", page, flags=re.DOTALL)

        self.assertNotIn("netLazy(", outside)
        self.assertNotIn("netSanctumActions", outside)


class RemoteImagePolicyTests(unittest.TestCase):
    """A card's picture is often somebody else's, and `img-src` has to say so.

    `og_image` on a captured page is a remote address and `safeExternalUrl` lets
    through exactly `http:` and `https:`. The policy said neither, so the browser
    dropped every remote thumbnail in the grid and the console said only that a
    stylesheet or an image was blocked.
    """

    def test_the_image_origins_are_allowed(self):
        from app.core.http_security import DASHBOARD_CONTENT_SECURITY_POLICY

        img = next(d for d in DASHBOARD_CONTENT_SECURITY_POLICY.split("; ") if d.startswith("img-src"))

        self.assertIn("https:", img)
        self.assertIn("http:", img)

    def test_the_page_can_actually_render_a_remote_picture(self):
        """Otherwise the policy allows a thing the page never does."""
        page = DASHBOARD.read_text()

        self.assertIn("function safeExternalUrl", page)
        self.assertIn("safeImageUrl(item.og_image)", page)

    def test_scripts_and_frames_stay_closed(self):
        """Allowing remote images is a decision about images only."""
        from app.core.http_security import DASHBOARD_CONTENT_SECURITY_POLICY

        self.assertIn("script-src 'self'", DASHBOARD_CONTENT_SECURITY_POLICY)
        self.assertIn("object-src 'none'", DASHBOARD_CONTENT_SECURITY_POLICY)
        self.assertIn("default-src 'none'", DASHBOARD_CONTENT_SECURITY_POLICY)

    def test_video_stays_this_origin(self):
        """A vault's own files are served from here, so there is no reason to
        let a page pull video from anywhere."""
        from app.core.http_security import DASHBOARD_CONTENT_SECURITY_POLICY

        media = next(d for d in DASHBOARD_CONTENT_SECURITY_POLICY.split("; ") if d.startswith("media-src"))

        self.assertEqual("media-src 'self' blob:", media)


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
