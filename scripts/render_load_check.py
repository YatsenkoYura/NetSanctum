"""Run a saved page's own scripts to the end of their top level, in node.

The browser found what no amount of reading found: a registry that captured a
function before it existed, then a registry that did not exist at all when the
page tried to use it. Both presented as `x is not defined` on a line nobody was
looking at. A `node --check` says nothing about either, because both are correct
code in the wrong order.

So: point this at a page saved from a running deployment and it will execute each
inline script in document order, in script scope, against a stub DOM small enough
to read. A `ReferenceError` here is a real one — the page really does reference
something that is not there.

    curl -s -b "access_token=$TOKEN" http://localhost:3000/vault/dashboard > page.html
    python -m scripts.render_load_check page.html

What it cannot do is tell a real error from a stub's limits. The stub answers
`getElementById` with null, so anything that runs after the page has booted and
touches the DOM will complain; those failures come *after* the line this prints
and are not findings. Only a failure inside a script's own top level is.

Exit code is 1 when a script throws at the top level, which is the case worth
stopping for.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Every page's script runs to the end of its top level against this and nothing
# more. It is deliberately thin: the point is to catch a name that is not
# defined, not to emulate a browser.
RUNNER = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const noop = () => {};
const style = new Proxy({}, { set: noop, get: () => '' });
const classList = { add: noop, remove: noop, toggle: noop, contains: () => false, replace: noop };
// Methods an element needs before a page's own top-level code can touch it. An
// explicit list rather than a pattern: the one thing this stub must not do is
// be clever enough to be wrong in a way that looks like a page bug.
const METHODS = new Set([
  'addEventListener', 'removeEventListener', 'dispatchEvent', 'attachShadow',
  'appendChild', 'insertBefore', 'removeChild', 'replaceChild', 'remove',
  'setAttribute', 'removeAttribute', 'focus', 'blur', 'click', 'play', 'pause',
  'load', 'removeAttributeNode', 'insertAdjacentHTML', 'scrollIntoView',
]);
const NULLABLE = new Set(['querySelector', 'closest', 'getRootNode']);
const element = () => new Proxy({}, {
  get: (target, key) => {
    if (key === 'style') return style;
    if (key === 'dataset') return {};
    if (key === 'classList') return classList;
    if (METHODS.has(key)) return noop;
    if (key === 'querySelectorAll' || key === 'getElementsByClassName' || key === 'getElementsByTagName') return () => [];
    if (NULLABLE.has(key)) return () => null;
    if (key === 'getBoundingClientRect') return () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 });
    if (key === 'children' || key === 'childNodes') return [];
    if (key === 'value' || key === 'textContent' || key === 'innerHTML' || key === 'checked') return '';
    return element();
  },
  set: () => true,
});
// The page reaches for these on the global object, not on an element.
global.addEventListener = noop;
global.removeEventListener = noop;
global.dispatchEvent = noop;
global.getComputedStyle = () => ({});
global.window = global;
global.document = {
  readyState: 'complete', cookie: '', title: '', head: element(), body: element(),
  documentElement: element(), createElement: () => element(), createTextNode: () => element(),
  createDocumentFragment: () => element(), createComment: () => element(),
  getElementById: () => null, querySelectorAll: () => [], querySelector: () => null,
  elementsFromPoint: () => [],
  addEventListener: noop, removeEventListener: noop, dispatchEvent: noop,
  execCommand: () => true,
};
global.location = { href: 'http://localhost/', origin: 'http://localhost', pathname: '/', search: '', reload: noop, assign: noop };
// `defineProperty`, not assignment: node ships its own `navigator` as a
// getter-only global, so `global.navigator = {...}` is accepted and silently
// discarded, and every page that reaches for `navigator.clipboard` then fails on
// a name that is really there in a browser.
Object.defineProperty(global, 'navigator', {
  configurable: true,
  value: {
    userAgent: 'node',
    language: 'ru',
    languages: ['ru'],
    clipboard: { writeText: () => Promise.resolve(), readText: () => Promise.resolve('') },
  },
});
global.localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
global.sessionStorage = global.localStorage;
global.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
global.matchMedia = () => ({ matches: false, addEventListener: noop });
global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
global.setInterval = () => 0;
global.clearInterval = noop;
global.alert = noop;
global.confirm = () => true;
global.HTMLElement = class HTMLElement {};
global.Node = class Node {};
global.customElements = { define: noop, get: () => undefined };
global.CustomEvent = class CustomEvent { constructor() {} };
global.Event = global.CustomEvent;
global.MutationObserver = class { observe() {} disconnect() {} };
global.ResizeObserver = global.MutationObserver;
global.IntersectionObserver = global.MutationObserver;
global.Audio = function Audio() { return element(); };
global.Image = function Image() { return element(); };

const failures = [];
for (const file of process.argv.slice(2)) {
  const name = file.split('/').pop();
  try {
    vm.runInThisContext(fs.readFileSync(file, 'utf8'), { filename: name });
  } catch (error) {
    failures.push({ block: name, error: `${error.constructor.name}: ${error.message}` });
  }
}
console.log(JSON.stringify({ failures, globals: Object.keys(global).length }));
process.exit(0);
"""

SCRIPT_TAG = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL)


def inline_scripts(html: str) -> list[str]:
    return SCRIPT_TAG.findall(html)


def run(html: str) -> dict:
    """Execute each inline script in order. Returns what failed at the top level."""
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node is not installed")
    scripts = inline_scripts(html)
    if not scripts:
        return {"failures": [], "blocks": 0}
    with tempfile.TemporaryDirectory() as directory:
        paths = []
        for index, source in enumerate(scripts):
            path = Path(directory) / f"block{index}.js"
            path.write_text(source)
            paths.append(path)
        runner = Path(directory) / "runner.js"
        runner.write_text(RUNNER)
        result = subprocess.run(
            [node, str(runner), *[str(p) for p in paths]], capture_output=True, text=True, timeout=120
        )
    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "{}"
    try:
        outcome = json.loads(line)
    except json.JSONDecodeError:
        return {"failures": [{"block": "runner", "error": result.stderr[-400:]}], "blocks": len(scripts)}
    outcome["blocks"] = len(scripts)
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("page", type=Path, help="an HTML file saved from the running deployment")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if not args.page.is_file():
        print(f"no such file: {args.page}", file=sys.stderr)
        return 2
    outcome = run(args.page.read_text())

    if args.json:
        print(json.dumps(outcome, indent=2))
    elif outcome["failures"]:
        print(f"{len(outcome['failures'])} of {outcome['blocks']} inline scripts threw at the top level:")
        for failure in outcome["failures"]:
            print(f"  {failure['block']}: {failure['error']}")
        print("\nA failure after the page boots is the stub's limits, not the page's.")
    else:
        print(f"all {outcome['blocks']} inline scripts ran to the end of their top level")

    return 1 if outcome["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
