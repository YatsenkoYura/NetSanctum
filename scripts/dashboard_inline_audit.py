"""Count what still blocks a nonce on the dashboard's Content-Security-Policy.

`/vault/dashboard` is served with a real policy, but `script-src` still carries
`'unsafe-inline'` because the template has 88 inline event-handler attributes and
two inline `<script>` blocks. That combination is the whole of the remaining gap
between "we send a CSP" and "an injected `<script>` or `onerror=` cannot run".

Closing it means moving every handler to a delegated listener and putting a nonce
on the two script blocks. This script exists so that migration has a checklist
that is not a person's memory: it names every handler, groups the identical
ones, and exits non-zero when `--require-none` is given and any remain — which is
what the test suite and CI want once the work is done.

    python -m scripts.dashboard_inline_audit                 # the report
    python -m scripts.dashboard_inline_audit --require-none  # fails while any remain

The attribute values are printed verbatim. They are the call sites, not secrets,
and reading them is how you find out that nine of them are the same function.
"""

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# `on<event>="..."`, the HTML attribute form. Deliberately narrow: it matches the
# event types the dashboard actually uses and will not trip on `once=`, `only=` or
# a word that merely contains "on".
#
# The leading group is an attribute *boundary*, not a space. A template can write
# `{% if x %}oninput="fn()"` with no space before the attribute — valid, and it is
# exactly what the first version of this script walked past, which is how four
# handlers survived a migration that reported zero.
BOUNDARY = r"""(?:^|[\s"'%}])"""
HANDLER = re.compile(
    BOUNDARY + r'(on(?:click|input|change|keydown|keyup|submit|error|load))\s*=\s*(?:"([^"]*)"|\'([^\']*)\')',
    re.IGNORECASE,
)
SCRIPT_BODY = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
SCRIPT_TAG = re.compile(r"<script(?![^>]*\bsrc=)(?![^>]*\bnonce=)[^>]*>", re.IGNORECASE)
# The leading call in an inline handler: `fn(...)`, `if (...)`, `this.style=...`.
CALL = re.compile(r"^\s*([A-Za-z_$][\w$.]*)\s*\(")


@dataclass
class Handler:
    template: str
    event: str
    body: str


@dataclass
class Summary:
    """What still stands between the dashboard and a policy without 'unsafe-inline'."""

    handlers: list[Handler] = field(default_factory=list)
    by_function: Counter = field(default_factory=Counter)
    script_blocks: dict[str, int] = field(default_factory=dict)

    @property
    def handler_count(self) -> int:
        return len(self.handlers)

    @property
    def script_count(self) -> int:
        return sum(self.script_blocks.values())

    @property
    def remaining(self) -> int:
        return self.handler_count + self.script_count


def report(paths: list[Path]) -> Summary:
    """Scan a set of templates and summarize what blocks the nonce.

    Script bodies are removed before counting. A template literal like
    `onclick="${handler}"` is a place where markup is *built*, and counting it
    alongside real attributes means a generator with a loop in it reports five
    handlers for one line of source — or, read the other way, hides a real one.
    """
    summary = Summary()
    for path in paths:
        raw = path.read_text()
        # Scripts are counted on the raw source. Counting them after the strip was
        # a bug that reported zero nonced scripts on every file — a clean report
        # from a scan that read nothing, which is the one failure mode here worth
        # engineering against.
        source = SCRIPT_BODY.sub("", raw)
        # finditer, not findall: with this pattern the two disagree, and findall
        # hands back an empty string where a group did not take part. The body of a
        # single-quoted handler then reads as "" and gets counted as an unknown
        # action instead of the call it is.
        handlers = [match.groups() for match in HANDLER.finditer(source)]
        for event, double, single in handlers:
            body = (double if double is not None else single or "").strip()
            summary.handlers.append(Handler(template=str(path), event=event[2:], body=body))
            match = CALL.match(body)
            summary.by_function[match.group(1) if match else body.split("=")[0].strip()] += 1
        summary.script_blocks[str(path)] = len(SCRIPT_TAG.findall(raw))
    return summary


def format_report(paths: list[Path]) -> str:
    summary = report(paths)
    lines = [str(path) for path in paths]
    for count in summary.script_blocks.values():
        lines.append(f"  inline <script> without a nonce: {count}")
    lines.append(f"  inline handler attributes: {summary.handler_count}")
    for name, count in summary.by_function.most_common():
        lines.append(f"    {count:>3}  {name}")
    if summary.handler_count:
        lines.append("  every one of these must move to a delegated listener")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "templates",
        nargs="*",
        type=Path,
        default=[
            # The layout is in the list on purpose: a policy covers a whole page,
            # and a page includes its layout. The first version of this script named
            # only the dashboard template and reported it clean while thirteen
            # handlers rode in from `base.html`.
            Path("app/core/templates/base.html"),
            Path("app/modules/vault/templates/vault_dashboard.html"),
        ],
    )
    parser.add_argument(
        "--require-none",
        action="store_true",
        help="exit non-zero if any inline handler or un-nonced script block remains",
    )
    args = parser.parse_args(argv)

    paths = [path for path in args.templates if path.exists()]
    if not paths:
        print("no templates found", file=sys.stderr)
        return 2

    summary = report(paths)
    print(format_report(paths))
    if not args.require_none:
        return 0
    if summary.remaining:
        print(
            f"{summary.remaining} left to migrate before script-src can drop 'unsafe-inline'",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
