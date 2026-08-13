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


if __name__ == "__main__":
    unittest.main()
