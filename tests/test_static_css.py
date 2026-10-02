import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


class StaticCssContractTests(unittest.TestCase):
    def test_base_uses_precompiled_tailwind(self):
        base = (ROOT / "app/core/templates/base.html").read_text()

        self.assertIn('href="/static/tailwind.css?v=', base)
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

    def test_video_dashboard_avoids_missing_avatar_requests_and_redeclared_globals(self):
        dashboard = (ROOT / "app/modules/video_archiver/templates/video_dashboard.html").read_text()

        self.assertIn("if (video.channel_avatar_url)", dashboard)
        self.assertIn("v.channel_avatar_url ? `<img", dashboard)
        self.assertIn("window._controlsTimers ||= {};", dashboard)
        self.assertNotIn("const _controlsTimers", dashboard)


class SharedCalendarCoreTests(unittest.TestCase):
    """Run the shared calendar core under Node and assert its date math.

    The core is plain browser JS with no build step, so it is loaded into a
    fresh `window` and probed directly instead of being duplicated in Python.
    """

    CORE = ROOT / "static/netsanctum-calendar.js"

    @classmethod
    def setUpClass(cls):
        if shutil.which("node") is None:
            raise unittest.SkipTest("node is not installed")
        cls._dir = TemporaryDirectory()
        loader = Path(cls._dir.name) / "loader.mjs"
        loader.write_text(
            "import { readFileSync } from 'fs';\n"
            f"const src = readFileSync({str(cls.CORE)!r}, 'utf8');\n"
            "const window = {};\n"
            "new Function('window', 'document', src)(window, undefined);\n"
            "export default window.NetSanctumCalendar;\n"
        )
        cls._loader_path = str(loader)

    @classmethod
    def tearDownClass(cls):
        temp = getattr(cls, "_dir", None)
        if temp is not None:
            temp.cleanup()

    def _run(self, body: str):
        """Execute `body` against a fresh calendar core; mismatches fail the test."""
        script = Path(self._dir.name) / "probe.mjs"
        script.write_text(
            f"import NSC from {self._loader_path!r};\n"
            "const fail = [];\n"
            "const eq = (l, g, w) => { if (JSON.stringify(g) !== JSON.stringify(w)) fail.push([l, g, w]); };\n"
            f"{body}\n"
            "for (const [l, g, w] of fail) console.log(`FAIL ${l}: ${JSON.stringify(g)} != ${JSON.stringify(w)}`);\n"
            "process.exit(fail.length ? 1 : 0);\n"
        )
        proc = subprocess.run(
            ["node", str(script)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual("", proc.stdout.strip(), proc.stdout)

    def test_month_grid_is_monday_first_42_days(self):
        self._run(
            "const w = NSC.monthWeeks(new Date(2026, 9, 1));\n"
            "eq('42 cells', w.length, 42);\n"
            "eq('starts monday', (w[0].getDay() + 6) % 7, 0);\n"
            "eq('consecutive', w.every((d, i) => i === 0 || NSC.dayKey(NSC.addDays(w[i - 1], 1)) === NSC.dayKey(d)), true);\n"
            "eq('covers month', w.some(d => d.getMonth() === 9 && d.getFullYear() === 2026), true);\n"
        )

    def test_week_is_monday_to_sunday(self):
        self._run(
            "const d = NSC.weekDays(new Date(2026, 9, 2));\n"
            "eq('starts', NSC.dayKey(d[0]), '2026-09-28');\n"
            "eq('ends', NSC.dayKey(d[6]), '2026-10-04');\n"
        )

    def test_multiday_events_paint_every_covered_day(self):
        self._run(
            "eq('span', NSC.eventDayKeys({starts_at:'2026-10-05T09:00:00+03:00', ends_at:'2026-10-07T10:00:00+03:00'}), ['2026-10-05','2026-10-06','2026-10-07']);\n"
            "eq('midnight end', NSC.eventDayKeys({starts_at:'2026-10-05T22:00:00+03:00', ends_at:'2026-10-06T00:00:00+03:00'}), ['2026-10-05']);\n"
        )

    def test_all_day_detection(self):
        self._run(
            "eq('no end', NSC.isAllDayEvent({starts_at:'2026-10-05T00:00:00+03:00'}), true);\n"
            "eq('full day', NSC.isAllDayEvent({starts_at:'2026-10-05T00:00:00+03:00', ends_at:'2026-10-06T00:00:00+03:00'}), true);\n"
            "eq('timed', NSC.isAllDayEvent({starts_at:'2026-10-05T10:00:00+03:00', ends_at:'2026-10-05T11:00:00+03:00'}), false);\n"
        )

    def test_drag_preserves_wall_clock_time(self):
        self._run(
            "const iso = NSC.shiftedToDay('2026-10-05T15:00:00+03:00', '2026-10-12');\n"
            "eq('keeps time', iso.startsWith('2026-10-12T15:00'), true);\n"
            "eq('keeps offset', iso.endsWith('+03:00'), true);\n"
        )

    def test_space_colors_resolve_from_vault_collections(self):
        self._run(
            "const map = {7: {id: 7, name: 'Work', color: 'violet'}};\n"
            "eq('collection', NSC.spaceColor('collection', 7, 'Work', map), '#a78bfa');\n"
            "eq('inbox', NSC.spaceColor('none', null, null, map), '#71717a');\n"
            "eq('folder stable', NSC.spaceColor('folder', 3, 'F', map) === NSC.spaceColor('folder', 3, 'F', map), true);\n"
        )

    def test_day_keys_survive_local_timezone(self):
        self._run(
            "eq('roundtrip', NSC.fromKey(NSC.dayKey(new Date(2026, 0, 5))).getMonth(), 0);\n"
            "eq('range end', NSC.rangeOfWeeks(NSC.monthWeeks(new Date(2026, 9, 1))).to.startsWith('2026-11-08'), true);\n"
        )


if __name__ == "__main__":
    unittest.main()
