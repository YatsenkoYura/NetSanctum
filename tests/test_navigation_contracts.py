import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NavigationContractTests(unittest.TestCase):
    def test_module_switcher_uses_full_page_navigation(self):
        base = (ROOT / "app/core/templates/base.html").read_text()
        switcher_start = base.index("<!-- Desktop Module Switcher")
        switcher_end = base.index("<!-- Right Controls", switcher_start)
        switcher = base[switcher_start:switcher_end]

        self.assertNotIn("hx-boost", switcher)
        self.assertNotIn("hx-target", switcher)
        self.assertNotIn("hx-select", switcher)

    def test_whitelisted_navigation_is_delegated_without_mutating_links(self):
        base = (ROOT / "app/core/templates/base.html").read_text()

        self.assertIn('id="persistent-video-host"', base)
        self.assertIn('id="global-video-player"', base)
        self.assertIn('id="main-content" hx-history-elt', base)
        self.assertIn("document.addEventListener('click'", base)
        self.assertIn("preserveActiveVideo();", base)
        self.assertIn("htmx.ajax('GET', url.pathname + url.search", base)
        self.assertIn("select: '#main-content'", base)
        self.assertIn("window.location.assign(request.url.href)", base)
        self.assertIn("window.netSanctumNavigate", base)
        self.assert_history_moves_after_a_successful_swap(base)

    def test_the_address_bar_follows_the_swapped_page(self):
        """htmx 2's `ajax()` drops an unknown option without a word.

        The module switcher used to pass `push: url.pathname`, which looked like
        it moved history and did nothing at all: `ajax()` reads only target,
        source, event, handler, headers, values, swap and select. History moves
        on a response `HX-Push-Url` header, an `hx-push-url` attribute, or an
        `hx-boost`ed element — none of which this call has. So every internal
        navigation in the app swapped the content and left the URL behind, which
        read to the owner as a module that half-opened.

        The push is therefore done by hand, and only once the swap has succeeded:
        pushing before the swap would put a URL in the bar for a page that never
        arrived.
        """
        base = (ROOT / "app/core/templates/base.html").read_text()
        call = base[base.index("htmx.ajax('GET'") :]
        call = call[: call.index("return true;")]

        self.assertNotIn("push:", call, "htmx.ajax() ignores a `push` option")
        self.assert_history_moves_after_a_successful_swap(base)

    def test_back_button_does_not_leave_the_document_behind_the_url(self):
        """The other direction, and the half that is easy to miss.

        htmx restores a history entry it recognises: state `{htmx: true}` and a
        cached copy of the content. An entry this document was merely *loaded*
        at has neither, so htmx ignores the popstate, the address bar walks back
        to `/dashboard`, and the vault stays on screen under it. One reload is
        the honest repair — a Back button that shows the wrong page under a right
        URL is worse than a slow one.
        """
        base = (ROOT / "app/core/templates/base.html").read_text()

        self.assertIn("window.addEventListener('popstate'", base)
        self.assertIn("if (event.state && event.state.htmx) return;", base)
        self.assertIn("window.location.reload();", base)
        # And the comparison has to be against what was rendered, not against the
        # URL, which is the whole point.
        self.assertIn("let renderedPath = window.location.pathname", base)
        self.assertIn("renderedPath = target;", base)

    def assert_history_moves_after_a_successful_swap(self, base: str) -> None:
        handler = base[base.index("document.addEventListener('htmx:afterRequest'") :]
        handler = handler[: handler.index("window.addEventListener('popstate'")]

        self.assertIn("if (event.detail.failed)", handler)
        self.assertIn("history.pushState({htmx: true}, '', target)", handler)
        self.assertIn("/^\\/youtube\\/watch\\/[^/]+$/", base)
        self.assertIn("'/alllib/dashboard'", base)


if __name__ == "__main__":
    unittest.main()
