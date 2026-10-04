"""The dispatcher has to actually call what the inline attribute used to call.

Converting `onclick="fn(3)"` into a name and a JSON argument list is only correct
if the dispatcher hands the same function the same arguments. That is easy to get
subtly wrong — a path sentinel, a missing `$event`, an argument order — and it
fails as a button that does nothing rather than as an error, which is the worst
way for it to fail.

So the dispatcher is extracted from the template and run in node against fake
elements. The extraction matters: this tests the code the page ships, not a copy
of it. The one thing it cannot prove is the DOM wiring — that a real click on a
real child reaches the right element — because there is no browser here; that is
what the manual pass in step 4 is for, and what `error` in the console would look
like if it were wrong.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

TEMPLATE = Path("app/modules/vault/templates/vault_dashboard.html")

# The block between these markers is the delegation machinery, verbatim.
START = "function vaultArg(spec, el, event) {"
END = "function vaultRunAction(name, el, event) {"
RUNNER_END = "const vaultActions = {};"


def extract_dispatcher() -> str:
    source = TEMPLATE.read_text()
    start = source.index(START)
    end = source.index(RUNNER_END)
    middle = source[start:end]
    # `vaultRunAction` reads `vaultActions`, which is declared after it.
    return f"const vaultActions = {{}};\n{middle}\n"


HARNESS = """
let currentCollectionId = 42;
const calls = [];
__DISPATCHER__

// A stand-in for the real actions: records what it was called with.
vaultActions.record = (...args) => calls.push(['record', args]);

// Fake elements: only what the argument resolver is allowed to touch.
function el(props) {
  return Object.assign({ value: '', textContent: '', style: {}, dataset: {} }, props);
}

function run(name, args, element, event) {
  const target = el(element || {});
  target.dataset.vaultOn = name;
  target.dataset.vaultArgs = JSON.stringify(args);
  vaultRunAction(name, target, event || { type: 'click', stopPropagation() { calls.push(['stopped']); } });
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

// The one call site that reaches into the DOM itself.
vaultActions.selectWorkspaceRow = function (id) {
  calls.push(['selectWorkspaceRow', id, this.querySelector('[data-workspace-name]').textContent]);
};
(() => {
  const element = el({ querySelector: () => el({ textContent: 'Моя папка' }) });
  element.dataset.vaultOn = 'selectWorkspaceRow';
  element.dataset.vaultArgs = JSON.stringify([5]);
  vaultRunAction('selectWorkspaceRow', element, null);
  cases.select_row = calls.at(-1);
})();

// A name that is not registered, and arguments that are not JSON: both must be
// inert. A dashboard whose every button throws on click is a broken dashboard.
(() => {
  const before = calls.length;
  run('noSuchAction', [1]);
  cases.unknown_name = calls.length === before;
})();
(() => {
  const target = el({});
  target.dataset.vaultOn = 'record';
  target.dataset.vaultArgs = '[not json';
  try { vaultRunAction('record', target, null); cases.bad_json = true; }
  catch (e) { cases.bad_json = false; }
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


class DispatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = run_node()

    def test_the_dispatcher_is_extracted_from_the_shipped_template(self):
        """A copy of the code would pass while the page stayed broken."""
        source = TEMPLATE.read_text()
        self.assertIn(START, source)
        self.assertIn(RUNNER_END, source)

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
        self.assertIn("dataset", recorded)

    def test_a_property_path_resolves(self):
        self.assertEqual("привет", self.cases["el_path"][1][0])
        self.assertEqual("7", self.cases["el_deep_path"][1][0])

    def test_a_missing_path_is_none_and_not_a_throw(self):
        self.assertIsNone(self.cases["el_missing_path"][1][0])

    def test_event_is_available_where_the_attribute_used_it(self):
        event = self.cases["event_sentinel"][1][0]
        # A method is not JSON, so only its data survives the trip back.
        self.assertEqual("click", event["type"])

    def test_the_current_collection_is_read_at_dispatch_time(self):
        self.assertEqual(42, self.cases["current_collection"][1][0])

    def test_order_and_types_survive_a_mixed_call(self):
        args = self.cases["mixed"][1]
        self.assertEqual([3, "v"], args[:2])
        self.assertEqual("keydown", args[2]["type"])
        self.assertEqual([True, None, "x"], args[3:])

    def test_the_dom_reading_wrapper_works(self):
        self.assertEqual(["selectWorkspaceRow", 5, "Моя папка"], self.cases["select_row"])

    def test_an_unregistered_name_does_nothing(self):
        self.assertTrue(self.cases["unknown_name"])

    def test_malformed_arguments_do_nothing_rather_than_throw(self):
        self.assertTrue(self.cases["bad_json"])


class TemplateWiringTests(unittest.TestCase):
    """What the dispatcher needs, in the template it ships in."""

    def setUp(self):
        self.source = TEMPLATE.read_text()
        self.block = re.search(r"Object\.assign\(vaultActions, \{(.*?)\n\}\);", self.source, re.DOTALL).group(
            1
        )
        # The block is a comma-separated list of bare names with a few entries
        # spelled `name: value`. Both forms are action names; everything else in
        # the block — comments, bodies, prose — is not.
        code = re.sub(r"//[^\n]*", "", self.block)
        keys = set(re.findall(r"^\s*([A-Za-z_$][\w$]*)\s*:", code, re.M))
        bare: set[str] = set()
        for line in code.splitlines():
            stripped = line.strip().rstrip(",")
            if not stripped or ":" in stripped or "(" in stripped or "=" in stripped:
                continue
            for name in stripped.split(","):
                name = name.strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", name):
                    bare.add(name)
        self.registered = keys | bare
        self.used = set(re.findall(r'data-vault-on="([\w$]+)"', self.source))

    def test_every_delegated_name_is_registered(self):
        self.assertEqual(set(), self.used - self.registered, "an unregistered action does nothing at all")

    def test_every_registered_action_exists_in_the_page(self):
        defined = set(re.findall(r"^(?:async )?function (\w+)\(", self.source, re.M))
        code = re.sub(r"//[^\n]*", "", self.block)
        arrow = set(re.findall(r"^\s+(\w+):\s*(?:\(|function|event|window)", code, re.M))
        from_window = {"sendToOutpost"}
        self.assertEqual(set(), self.registered - defined - arrow - from_window)

    def test_the_sentinels_in_use_are_ones_the_resolver_knows(self):
        resolver = self.source[self.source.index(START) : self.source.index(END)]
        args = re.findall(r"data-vault-args='\[(.*?)\]'", self.source)
        sentinels = set()
        for raw in args:
            sentinels.update(re.findall(r'"\$([\w.]+)"', raw))
        for sentinel in sentinels:
            path = sentinel.split(".")[0]
            self.assertIn(f"${path}", resolver, f"${path} is not a sentinel the resolver knows")

    def test_arguments_are_valid_json_once_rendered(self):
        """A single quote inside `${...}` would end the attribute early."""
        for raw in re.findall(r"data-vault-args='(\[[^']*\])'", self.source):
            probe = re.sub(r"\$\{\{.*?\}\}", "1", raw, flags=re.DOTALL)
            probe = re.sub(r"\$\{[^{}]*\}", "1", probe)
            probe = re.sub(r"\{\{.*?\}\}", "1", probe, flags=re.DOTALL)
            try:
                json.loads(probe)
            except Exception as error:
                self.fail(f"{raw} is not valid JSON: {error}")

    def test_the_listener_runs_in_capture_phase(self):
        """Otherwise a delegated `stopPropagation` cannot stop anything."""
        for match in re.finditer(
            r"document\.addEventListener\(type,.*?\n\s*\}, (true|false)\);", self.source, re.S
        ):
            self.assertEqual("true", match.group(1), "a delegated listener must be capture-phase")

    def test_an_action_that_reaches_into_the_dom_uses_the_receiver(self):
        """`this` is the element: the dispatcher applies the action to it."""
        self.assertIn("selectWorkspaceRow: function", self.block)
        self.assertIn("this.querySelector", self.block)

    def test_no_handler_needs_this_anymore(self):
        for raw in re.findall(r"data-vault-args='(\[[^']*\])'", self.source):
            self.assertNotIn("this.", raw, "`this` is the attribute, not the element")


if __name__ == "__main__":
    unittest.main()
