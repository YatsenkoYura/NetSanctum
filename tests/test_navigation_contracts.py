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

        self.assertIn('id="youtube-mini-player-host"', base)
        self.assertIn("document.addEventListener('click'", base)
        self.assertIn("htmx.ajax('GET', url.pathname + url.search", base)
        self.assertIn("select: '#main-content'", base)
        self.assertIn("push: url.pathname + url.search", base)
        self.assertIn("window.location.assign(request.url.href)", base)
        self.assertIn("window.netSanctumNavigate", base)
        self.assertIn("/^\\/youtube\\/watch\\/[^/]+$/", base)


if __name__ == "__main__":
    unittest.main()
