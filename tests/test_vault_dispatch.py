"""The dispatcher has to actually call what the inline attribute used to call.

Converting `onclick="fn(3)"` into a name and a JSON argument list is only correct
if the dispatcher hands the same function the same arguments. That is easy to get
subtly wrong — a path sentinel, a missing `$event`, an argument order — and it
fails as a button that does nothing rather than as an error, which is the worst
way for it to fail.

So the dispatcher is extracted from the layout and run in node against fake
elements. The extraction matters: this tests the code the page ships, not a copy
of it. What it cannot prove is the DOM wiring — that a real click on a real child
reaches the right element — because there is no browser here. That is the manual
pass, and a mistake there shows up as a console error rather than a test failure.
"""

import contextlib
import io
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

# The layout, not the page: the dispatcher is infrastructure now, because the
# layout has handlers too and a policy covers a page along with everything it
# includes.
TEMPLATE = Path("app/core/templates/base.html")
PAGE = Path("app/modules/vault/templates/vault_dashboard.html")

# The machinery between these markers, verbatim.
START = "function netArg(spec, el, event) {"
RUNNER_END = "window.netSanctumActions = window.netSanctumActions || {};"
ACTION_END = "window.netSanctumActions,\n"


def extract_dispatcher() -> str:
    source = TEMPLATE.read_text()
    start = source.index(START)
    end = source.index("Object.assign(window.netSanctumActions, {")
    return f"window.netSanctumActions = window.netSanctumActions || {{}};\n{source[start:end]}\n"


HARNESS = """
// The page's own globals, so `window.x` in the extracted code resolves.
global.window = global;
// Enough of `document` for the registration lines; the listeners themselves are
// never invoked here, because that is the browser's job and the browser's absence
// is stated in this file's docstring.
global.document = { addEventListener() {}, querySelectorAll: () => [] };
// A property of the global object, which is what a `var` at the top level of
// the page's own script becomes in a browser.
global.currentCollectionId = 42;
const calls = [];
__DISPATCHER__

// A stand-in for the real actions: records what it was called with.
window.netSanctumActions.record = (...args) => calls.push(['record', args]);

// Fake elements: only what the argument resolver is allowed to touch.
function el(props) {
  return Object.assign({ value: '', textContent: '', style: {}, dataset: {} }, props);
}

function run(name, args, element, event) {
  const target = el(element || {});
  target.dataset.netAction = name;
  target.dataset.netArgs = JSON.stringify(args);
  netRunAction(name, target, event || { type: 'click', key: '', stopPropagation() { calls.push(['stopped']); } });
  return calls.at(-1);
}

const cases = {};
cases.numbers = run('record', [1, 2, 3]);
cases.strings = run('record', ['note', 'Все карточки']);
cases.no_args = run('record', []);
cases.el_sentinel = run('record', ['$el'], {});
cases.el_path = run('record', ['$el.value'], { value: 'привет' });
cases.el_deep_path = run('record', ['$el.dataset.row'], { dataset: { row: '7' } });
cases.el_missing_path = run('record', ['$el.nope.deeper'], {});
cases.event_sentinel = run('record', ['$event']);
cases.current_collection = run('record', ['$currentCollection']);
cases.mixed = run('record', [3, '$el.value', '$event', true, null, 'x'],
                  { value: 'v' }, { type: 'keydown', key: 'Enter' });

// The event of the dispatch in flight, which is how an action reaches past its
// own element without every action taking an extra argument.
window.netSanctumActions.stopLikeTheOriginal = function () {
  calls.push(['stopped', window.netSanctumEvent.key]);
};
cases.in_flight_event = run('stopLikeTheOriginal', [], {}, { type: 'keydown', key: 'Enter' });
cases.cleared_afterwards = window.netSanctumEvent === null;

// A name that is not registered, and arguments that are not JSON: both inert.
(() => {
  const before = calls.length;
  run('noSuchAction', [1]);
  cases.unknown_name = calls.length === before;
})();
(() => {
  const target = el({});
  target.dataset.netAction = 'record';
  target.dataset.netArgs = '[not json';
  try { netRunAction('record', target, null); cases.bad_json = true; }
  catch (e) { cases.bad_json = false; }
})();
// The same, for an action that throws: the page must not be left with a stale event.
(() => {
  window.netSanctumActions.boom = function () { throw new Error('boom'); };
  const target = el({});
  target.dataset.netAction = 'boom';
  target.dataset.netArgs = '[]';
  try { netRunAction('boom', target, null); } catch (e) { /* the throw is the action's */ }
  cases.event_cleared_after_throw = window.netSanctumEvent === null;
})();

console.log(JSON.stringify(cases));
"""


def run_node() -> dict:
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    script = HARNESS.replace("__DISPATCHER__", extract_dispatcher())
    path = Path("/tmp/opencode/dispatcher_harness.js")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script)
    result = subprocess.run([node, str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(f"node failed:\n{result.stderr[-2000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


class LazyResolutionTests(unittest.TestCase):
    """A page's script is several blocks, and registration order is not run order.

    This is the bug the browser found: `closeVcalModal` and `submitVcalModal` are
    declared in the calendar block, which executes *after* the block that lists
    them in the registry. Registering the value captured `undefined`, so those two
    buttons failed on click with nothing wrong at registration to explain it.
    """

    # Block one: the layout's dispatcher and the page's registry, exactly as they
    # are on the page. `closeVcalModal` is listed but not yet declared.
    EARLY_BLOCK = """
function netLazy(name) { return function () { return window[name].apply(this, arguments); }; }
function netRunAction(name, el, event) {
  const action = window.netSanctumActions[name];
  if (!action) return;
  let args;
  try { args = JSON.parse(el.dataset.netArgs || '[]'); } catch (e) { return; }
  action.apply(el, args);
}
window.netSanctumActions = {};
var outcome = { resolved: 'unset', eagerRegistration: 'not attempted' };
// The eager form, which is what the allowlist used to do. An object literal that
// names an identifier nothing has declared does not produce `undefined` — it
// throws, and it throws inside the `Object.assign`, so *nothing* gets registered.
var eager = {};
try { eager = { closeVcalModal: closeVcalModal }; }
catch (e) { outcome.eagerRegistration = e.constructor.name; }
// The lazy form it uses now: a name, resolved when the action runs.
Object.assign(window.netSanctumActions, { closeVcalModal: netLazy('closeVcalModal') });
outcome.lazyRegistration = 'ok';
"""

    # Block two: the calendar block, which runs later and declares the function.
    # Two script blocks and not one, because a single block would hoist the
    # declaration above the registration and the bug would not reproduce — which
    # is the whole reason the failure survived every test that ran the code in one
    # piece.
    LATE_BLOCK = """
function closeVcalModal() { return 'closed'; }
outcome.resolved = typeof window.closeVcalModal;
"""

    # Block three: the click.
    CLICK_BLOCK = """
const el = { dataset: { netArgs: '[]' } };
if (outcome.eagerRegistration === 'not attempted') {
  try { eager.closeVcalModal(); outcome.eagerResult = 'ran'; }
  catch (e) { outcome.eagerResult = e.constructor.name; }
} else {
  outcome.eagerResult = 'never registered';
}
try { netRunAction('closeVcalModal', el, null); outcome.lazyResult = 'ran'; }
catch (e) { outcome.lazyResult = e.constructor.name; }
"""

    RUNNER = """
const fs = require('node:fs');
const vm = require('node:vm');
global.window = global;
global.document = { addEventListener() {}, querySelectorAll: () => [] };
// Script scope, not module scope: a CommonJS module would not turn a top-level
// `function` into a global, and the test would be measuring node instead of the
// browser semantics the template depends on. Separate runs, because separate
// script tags are what makes a declaration hoisting-free.
for (const file of process.argv.slice(2)) {
  vm.runInThisContext(fs.readFileSync(file, 'utf8'));
}
console.log(JSON.stringify(outcome));
"""

    def test_a_late_definition_is_still_reachable(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        directory = Path("/tmp/opencode")
        directory.mkdir(parents=True, exist_ok=True)
        blocks = []
        for name, source in (
            ("lazy_early.js", self.EARLY_BLOCK),
            ("lazy_late.js", self.LATE_BLOCK),
            ("lazy_click.js", self.CLICK_BLOCK),
        ):
            path = directory / name
            path.write_text(source)
            blocks.append(str(path))
        runner = directory / "lazy_runner.js"
        runner.write_text(self.RUNNER)

        # The runner goes first: it is the entry point that sets up the globals
        # the other three are evaluated against.
        result = subprocess.run([node, str(runner), *blocks], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr[-800:])
        outcome = json.loads(result.stdout.strip().splitlines()[-1])

        self.assertEqual("function", outcome["resolved"], "a script-scope function is a global")
        self.assertEqual("ok", outcome["lazyRegistration"], "a lazy registration cannot fail at load")
        self.assertEqual("ran", outcome["lazyResult"], "a lazy action must survive a late definition")
        self.assertEqual(
            "ReferenceError",
            outcome["eagerRegistration"],
            "the eager form must fail here or this test is not reproducing the bug",
        )
        self.assertEqual("never registered", outcome["eagerResult"])

    def test_the_layout_defines_the_helper_the_templates_use(self):
        self.assertIn("function netLazy(name)", TEMPLATE.read_text())
        self.assertIn(
            "return function () { return window[name].apply(this, arguments); };", TEMPLATE.read_text()
        )

    def test_the_page_resolves_everything_lazily(self):
        """A single eager entry left in a registry block is a broken button.

        Every block, not the first: the calendar block is a second one, and it is
        where the remaining bare references were hiding.
        """
        blocks = allowlist_blocks(PAGE.read_text())
        self.assertGreaterEqual(len(blocks), 2, "the page registers from more than one script block")

        for index, block in enumerate(blocks):
            code = re.sub(r"//[^\n]*", "", block)
            bare = re.findall(r"(?:^|[{,])\s*([A-Za-z_$][\w$]*)\s*(?=[,}\n])", code, re.M)
            self.assertEqual([], bare, f"registry block {index} has a bare reference: {bare}")


class DispatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = run_node()

    def test_the_dispatcher_comes_from_the_shipped_layout(self):
        source = TEMPLATE.read_text()
        self.assertIn(START, source)
        self.assertIn("function netRunAction(", source)

    def test_plain_arguments_arrive_unchanged(self):
        self.assertEqual(["record", [1, 2, 3]], self.cases["numbers"])
        self.assertEqual(["record", ["note", "Все карточки"]], self.cases["strings"])
        self.assertEqual(["record", []], self.cases["no_args"])

    def test_this_becomes_the_element(self):
        # The harness's output crosses JSON on the way back, so a fake element
        # arrives as an object with the fields the resolver touched.
        recorded = self.cases["el_sentinel"][1][0]
        self.assertIsInstance(recorded, dict)
        self.assertIn("value", recorded)

    def test_a_property_path_resolves(self):
        self.assertEqual("привет", self.cases["el_path"][1][0])
        self.assertEqual("7", self.cases["el_deep_path"][1][0])

    def test_a_missing_path_is_none_and_not_a_throw(self):
        self.assertIsNone(self.cases["el_missing_path"][1][0])

    def test_event_is_available_where_the_attribute_used_it(self):
        self.assertEqual("click", self.cases["event_sentinel"][1][0]["type"])

    def test_the_current_collection_is_read_at_dispatch_time(self):
        self.assertEqual(42, self.cases["current_collection"][1][0])

    def test_order_and_types_survive_a_mixed_call(self):
        args = self.cases["mixed"][1]
        self.assertEqual([3, "v"], args[:2])
        self.assertEqual("keydown", args[2]["type"])
        self.assertEqual([True, None, "x"], args[3:])

    def test_an_action_can_reach_the_event_in_flight(self):
        """How `event.stopPropagation(); fn()` survives the migration."""
        self.assertEqual(["stopped", "Enter"], self.cases["in_flight_event"])

    def test_the_in_flight_event_is_cleared_afterwards(self):
        self.assertTrue(self.cases["cleared_afterwards"])

    def test_a_throwing_action_does_not_leave_a_stale_event(self):
        self.assertTrue(self.cases["event_cleared_after_throw"])

    def test_an_unregistered_name_does_nothing(self):
        self.assertTrue(self.cases["unknown_name"])

    def test_malformed_arguments_do_nothing_rather_than_throw(self):
        self.assertTrue(self.cases["bad_json"])


def allowlist_blocks(source: str) -> list[str]:
    """Every `Object.assign(window.netSanctumActions, {...})` block in a template."""
    blocks = []
    for match in re.finditer(r"Object\.assign\(window\.netSanctumActions, \{", source):
        start = match.start()
        blocks.append(source[start : source.index("\n});", start)])
    return blocks


def registered_actions(source: str) -> set[str]:
    """Names an `Object.assign(window.netSanctumActions, {...})` block defines."""
    block = re.search(r"Object\.assign\(window\.netSanctumActions, \{(.*?)\n\s*\}\);", source, re.DOTALL)
    if not block:
        return set()
    code = re.sub(r"//[^\n]*", "", block.group(1))
    # Several entries share a line once the file is formatted, so a per-line
    # pattern sees only the first of them.
    keys = set(re.findall(r"([A-Za-z_$][\w$]*)\s*:", code))
    bare: set[str] = set()
    for line in code.splitlines():
        stripped = line.strip().rstrip(",")
        if not stripped or any(token in stripped for token in (":", "(", "=")):
            continue
        bare |= {
            name.strip() for name in stripped.split(",") if re.fullmatch(r"[A-Za-z_$][\w$]*", name.strip())
        }
    return keys | bare


def delegated_names(source: str) -> set[str]:
    return set(re.findall(r'data-net-action="([\w$]+)"', source))


class LayoutWiringTests(unittest.TestCase):
    """What the dispatcher needs, in the layout it ships in."""

    def setUp(self):
        self.source = TEMPLATE.read_text()
        self.page = PAGE.read_text()

    def test_the_layout_registers_only_what_it_uses(self):
        missing = delegated_names(self.source) - registered_actions(self.source)
        self.assertEqual(set(), missing, "an unregistered action does nothing at all")

    def test_the_page_registers_everything_it_delegates(self):
        """The page and the layout share one registry, so both must hold up."""
        missing = delegated_names(self.page) - (
            registered_actions(self.page) | registered_actions(self.source)
        )
        self.assertEqual(set(), missing)

    def test_the_listeners_are_capture_phase(self):
        """Otherwise a delegated `stopPropagation` cannot stop anything."""
        listeners = re.findall(
            r"document\.addEventListener\(type,.*?\n\s*\}, (true|false)\);", self.source, re.S
        )
        self.assertTrue(listeners, "the dispatcher must register its listeners")
        for phase in listeners:
            self.assertEqual("true", phase)

    def test_error_is_handled_in_capture_phase_too(self):
        self.assertRegex(self.source, r"(?s)addEventListener\('error'.*?\}, true\);")

    def test_no_handler_needs_this_anymore(self):
        for raw in re.findall(r"data-net-args='(\[[^']*\])'", self.source + self.page):
            self.assertNotIn("this.", raw, "`this` is the receiver, not an argument")


class AttributeWiringTests(unittest.TestCase):
    """The attributes, which are where a migration actually goes wrong."""

    def setUp(self):
        self.sources = {"base.html": TEMPLATE.read_text(), PAGE.name: PAGE.read_text()}

    def test_no_inline_handler_survives_in_either_file(self):
        from scripts.dashboard_inline_audit import report

        summary = report([TEMPLATE, PAGE])

        self.assertEqual(0, summary.handler_count, [h.body for h in summary.handlers])

    def test_every_argument_list_is_valid_json_once_rendered(self):
        """A single quote inside `${...}` ends the attribute early."""
        for name, source in self.sources.items():
            for raw in re.findall(r"data-net-args='(\[[^']*\])'", source):
                probe = re.sub(r"\$\{\{.*?\}\}", "1", raw, flags=re.DOTALL)
                probe = re.sub(r"\$\{[^{}]*\}", "1", probe)
                probe = re.sub(r"\{\{.*?\}\}", "1", probe, flags=re.DOTALL)
                try:
                    json.loads(probe)
                except Exception as error:
                    self.fail(f"{name}: {raw} is not valid JSON: {error}")

    def test_every_script_block_carries_a_nonce(self):
        from scripts.dashboard_inline_audit import report

        summary = report([TEMPLATE, PAGE])

        self.assertEqual(0, summary.script_count)

    def test_the_sentinels_in_use_are_ones_the_resolver_knows(self):
        resolver = self.sources["base.html"]
        resolver = resolver[resolver.index(START) : resolver.index("function netRunAction(")]
        sentinels: set[str] = set()
        for raw in re.findall(r"data-net-args='(\[[^']*\])'", "".join(self.sources.values())):
            sentinels.update(re.findall(r'"\$([\w.]+)"', raw))
        self.assertTrue(sentinels, "the templates are expected to use sentinels")
        for sentinel in sentinels:
            self.assertIn(f"${sentinel.split('.')[0]}", resolver, f"${sentinel} is not a sentinel")

    def test_an_action_reaching_into_the_dom_uses_the_receiver(self):
        """`this` is the element: the dispatcher applies the action to it."""
        self.assertIn("this.closest(", self.sources[PAGE.name])
        self.assertIn("function (id)", self.sources[PAGE.name])


class PolicyTests(unittest.TestCase):
    def test_the_dashboard_policy_has_no_unsafe_inline_for_scripts(self):
        from app.core.http_security import DASHBOARD_CONTENT_SECURITY_POLICY

        self.assertIn("script-src 'self'", DASHBOARD_CONTENT_SECURITY_POLICY)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", DASHBOARD_CONTENT_SECURITY_POLICY)

    def test_the_nonce_is_added_per_response(self):
        from app.core.http_security import dashboard_csp

        script_src = next(d for d in dashboard_csp("abc123").split("; ") if d.startswith("script-src"))

        self.assertEqual("script-src 'self' 'nonce-abc123'", script_src)

    def test_the_policy_is_scoped_to_the_dashboard(self):
        from app.core.http_security import DASHBOARD_CSP_PREFIXES

        self.assertEqual(("/vault/dashboard",), DASHBOARD_CSP_PREFIXES)

    def test_a_response_without_a_nonce_still_blocks_inline_script(self):
        """The policy is safe on its own: no nonce means no inline script runs,
        which is the failure mode a bug in the middleware would produce."""
        from app.core.http_security import dashboard_csp

        for policy in (dashboard_csp(""), dashboard_csp("abc")):
            script_src = next(d for d in policy.split("; ") if d.startswith("script-src"))
            self.assertNotIn("'unsafe-inline'", script_src, script_src)

    def test_inline_styles_are_still_allowed(self):
        """Only scripts were migrated. Styles are a separate piece of work and
        pretending otherwise here would overstate what this policy does."""
        from app.core.http_security import dashboard_csp

        self.assertIn("style-src 'self' 'unsafe-inline'", dashboard_csp("abc"))


class ScriptSmokeTests(unittest.TestCase):
    def test_the_inline_scripts_parse(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        for path in (TEMPLATE, PAGE):
            blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", path.read_text(), re.DOTALL)
            self.assertTrue(blocks, f"{path} has no inline script")
            for index, block in enumerate(blocks):
                js = re.sub(
                    r"\{%.*?%\}", "", re.sub(r"\{\{.*?\}\}", "null", block, flags=re.DOTALL), flags=re.DOTALL
                )
                script = Path(f"/tmp/opencode/parse_{path.stem}_{index}.js")
                script.parent.mkdir(parents=True, exist_ok=True)
                script.write_text(js)
                with contextlib.redirect_stderr(io.StringIO()):
                    result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
                self.assertEqual(0, result.returncode, f"{path} block {index}: {result.stderr[-400:]}")


if __name__ == "__main__":
    unittest.main()
