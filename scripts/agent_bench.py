#!/usr/bin/env python3
"""Measure a local model on the real cascade: does it plan, read, act and stop?

This is the model-side counterpart of tests/eval_agent.py. That harness grades the
engine with a scripted oracle; this one grades the model with a live llama.cpp, so
choosing between candidates is a measurement instead of an opinion.

Usage: python scripts/agent_bench.py [--base http://agent-runtime:8780] [--only id]
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

DEFAULT_BASE = os.environ.get("AGENT_RUNTIME_URL", "http://127.0.0.1:8780")


@dataclass
class Check:
    """One thing the turn must show for the scenario to count as passed."""

    name: str
    test: Callable[[dict], bool]


@dataclass
class Bench:
    id: str
    message: str
    checks: list[Check] = field(default_factory=list)
    expect_question: bool = False
    expect_action: str | None = None


def _tools(result: dict) -> list[str]:
    return [step["tool"] for step in result.get("steps", [])]


def _status(result: dict, tool: str) -> str | None:
    for step in result.get("steps", []):
        if step["tool"] == tool:
            return step["status"]
    return None


def scenarios() -> list[Bench]:
    return [
        Bench(
            id="search_and_answer",
            message="найди Re:Zero в библиотеке и назови, что там есть",
            checks=[
                Check("used a read tool", lambda r: any("search" in t or "library" in t for t in _tools(r))),
                Check("tool succeeded", lambda r: any(s["status"] == "success" for s in r["steps"])),
                Check("answered in text", lambda r: len(r["answer"]) > 20),
                Check("finished on its own", lambda r: not r["exhausted"]),
            ],
        ),
        Bench(
            id="read_before_describing",
            message="найди любую ранобэ и прочитай первую главу, потом расскажи о ней",
            checks=[
                Check("read the content", lambda r: "read" in _tools(r)),
                Check("answer is not empty", lambda r: len(r["answer"]) > 20),
                Check("finished on its own", lambda r: not r["exhausted"]),
            ],
        ),
        Bench(
            id="act_when_asked",
            message="включи финальную часть прохождения Zero Escape",
            checks=[
                Check("did something", lambda r: bool(r["client_action"] or _tools(r))),
                Check("finished on its own", lambda r: not r["exhausted"]),
            ],
            expect_action="play",
        ),
        Bench(
            id="does_not_invent_references",
            message="что у меня есть в заметках про квадроквантовую физику?",
            checks=[
                Check(
                    "references are real",
                    lambda r: all(reference["item_id"] for reference in r.get("references", [])),
                ),
                Check("finished on its own", lambda r: not r["exhausted"]),
            ],
        ),
        Bench(
            id="external_page",
            message="открой https://example.com и скажи, что там написано",
            checks=[
                Check(
                    "fetched the page", lambda r: "fetch" in _tools(r) and _status(r, "fetch") == "success"
                ),
                Check("finished on its own", lambda r: not r["exhausted"]),
            ],
        ),
    ]


def run_turn(base: str, token: str, message: str, timeout: float) -> dict:
    request = urllib.request.Request(
        f"{base}/v1/turn",
        data=json.dumps({"message": message}).encode(),
        headers={"X-Agent-Runtime-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    result: dict = {}
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            frame = json.loads(line)
            if frame.get("type") == "done":
                result = frame.get("result") or {}
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--timeout", type=float, default=400)
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    token = os.environ.get("AGENT_RUNTIME_TOKEN", "")
    if not token:
        print("AGENT_RUNTIME_TOKEN is required")
        return 2

    selected = [item for item in scenarios() if not args.only or item.id == args.only]
    total = 0
    passed = 0
    durations: list[float] = []
    print(f"{'scenario':<30} {'result':<8} {'steps':<6} {'time':<7} detail")
    for bench in selected:
        started = time.monotonic()
        try:
            result = run_turn(args.base, token, bench.message, args.timeout)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            print(f"{bench.id:<30} {'ERROR':<8} {'-':<6} {'-':<7} {type(exc).__name__}")
            continue
        elapsed = time.monotonic() - started
        durations.append(elapsed)
        failures = [check.name for check in bench.checks if not check.test(result)]
        if bench.expect_action and result.get("client_action") != bench.expect_action:
            failures.append(f"client_action={result.get('client_action')!r}")
        total += 1
        if failures:
            print(
                f"{bench.id:<30} {'fail':<8} {len(result.get('steps', [])):<6} {elapsed:5.0f}s  "
                + ", ".join(failures)
            )
        else:
            passed += 1
            print(f"{bench.id:<30} {'ok':<8} {len(result.get('steps', [])):<6} {elapsed:5.0f}s  -")
    average = sum(durations) / len(durations) if durations else 0
    print(f"\npassed {passed}/{total} | avg turn {average:.0f}s")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
