import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StaticCssContractTests(unittest.TestCase):
    def test_base_uses_precompiled_tailwind(self):
        base = (ROOT / "app/core/templates/base.html").read_text()

        self.assertIn('href="/static/tailwind.css"', base)
        self.assertNotIn("tailwind.min.js", base)
        self.assertNotIn("tailwind.config", base)

    def test_compiled_css_contains_responsive_and_webkit_rules(self):
        css = (ROOT / "static/tailwind.css").read_text()

        self.assertIn(r".md\:hidden", css)
        self.assertIn(r".md\:flex", css)
        self.assertIn(r".lg\:grid-cols-4", css)
        self.assertIn("-webkit-appearance:none", css)

    def test_runtime_tailwind_is_not_referenced_by_application(self):
        references = []
        for pattern in ("*.html", "*.py"):
            for path in (ROOT / "app").rglob(pattern):
                if "tailwind.min.js" in path.read_text():
                    references.append(str(path.relative_to(ROOT)))

        self.assertEqual([], references)


if __name__ == "__main__":
    unittest.main()
