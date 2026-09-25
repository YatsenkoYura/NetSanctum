"""Behavioural evaluation for the agent cascade.

Scenarios are scripted against an *oracle model*: the ideal tool sequence a competent
model would choose. That isolates engine behaviour (loop, budgets, grounding,
deduplication) from model quality, so the same scenarios keep working when the local
model is replaced, and they are the acceptance gate for every change to the loop.

Scenarios marked ``supported=False`` document a capability the engine still lacks.
They are executed and reported as gaps, never silently skipped.
"""

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.agent.catalog import build_tool_catalog
from app.core.agent.engine import (
    AgentBudget,
    AgentTurnRequest,
    AgentUnavailableError,
    CascadeEngine,
)
from app.core.agent.primitives import AgentStep

SEARCH_CATALOG = [
    {
        "id": "search.global.v1",
        "contract": "search.query.v1",
        "module_id": "search",
        "description": "Search every indexed module",
        "request_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
        "effects": {"effect": "read", "external_io": False, "idempotent": True},
    }
]
TOOLS = build_tool_catalog(SEARCH_CATALOG)

SEARCH_RESULT = {
    "items": [
        {
            "source_module_id": "video_archiver",
            "source_integration_id": "video_archiver.search.documents.v1",
            "document_id": "video-1",
            "entity_type": "video",
            "title": "Zero Escape finale",
            "summary": "Final episode",
            "open_path": "/video-archiver/dashboard?miku_item=video-1",
            "playable": True,
            "readable": False,
        },
        {
            "source_module_id": "alllib",
            "source_integration_id": "alllib.search.documents.v1",
            "document_id": "chapter-3",
            "entity_type": "novel",
            "title": "Re:Zero chapter 3",
            "open_path": "/alllib/reader/9",
            "playable": False,
            "readable": True,
        },
    ],
    "warnings": [],
}

CHAPTER_TEXT = "Глава 3. Ремна встречает Экземпляр у ворот."
FETCH_PAGE = {
    "url": "https://example.org/article",
    "final_url": "https://example.org/article",
    "title": "Как работает парадокс",
    "content_type": "text/html",
    "text": "Парадокс возникает, когда решение выглядит очевидным и ошибочным одновременно.",
    "truncated": False,
    "bytes_read": 400,
}


def search(query: str, limit: int = 5) -> AgentStep:
    return AgentStep(kind="tool", tool="search_global_v1", arguments={"query": query, "limit": limit})


def read(ref: str) -> AgentStep:
    return AgentStep(kind="tool", tool="read", arguments={"ref": ref})


def act(ref: str, action: str) -> AgentStep:
    return AgentStep(kind="tool", tool="act", arguments={"ref": ref, "action": action})


def fetch(url: str) -> AgentStep:
    return AgentStep(kind="tool", tool="fetch", arguments={"url": url})


def final(answer: str, *refs: str) -> AgentStep:
    return AgentStep(kind="final", tool="final", arguments={"answer": answer, "refs": list(refs)})


def ask(question: str, *options: str) -> AgentStep:
    return AgentStep(kind="ask", tool="ask", arguments={"question": question, "options": list(options)})


class OracleModel:
    """A model made of decisions: exactly what the engine is supposed to obey."""

    def __init__(self, steps: list[AgentStep], *, fail_after: int | None = None):
        self.steps = list(steps)
        self.fail_after = fail_after
        self.calls = 0
        self.states: list[dict[str, Any]] = []

    async def next_step(self, message, tools, state):
        self.calls += 1
        self.states.append(state)
        if self.fail_after is not None and self.calls > self.fail_after:
            raise AgentUnavailableError("model offline")
        if not self.steps:
            return final("Пустой ответ.")
        return self.steps.pop(0)


class OracleBackend:
    """The application's side, canned: search results, chapter text, one page."""

    def __init__(self, *, search_result: dict | None = None, chapter: str = CHAPTER_TEXT):
        self.search_result = SEARCH_RESULT if search_result is None else search_result
        self.chapter = chapter
        self.invocations: list[tuple[str, dict]] = []
        self.reads: list[tuple[str, str]] = []

    async def catalog(self):
        return TOOLS

    async def invoke(self, integration_id, parameters):
        self.invocations.append((integration_id, parameters))
        return self.search_result

    async def read(self, module_id, item_id, max_chars):
        self.reads.append((module_id, item_id))
        return {"kind": "text", "title": f"{module_id}/{item_id}", "text": self.chapter, "truncated": False}


@dataclass
class Expect:
    answer_contains: str | None = None
    question: str | None = None
    question_options: list[str] = field(default_factory=list)
    refs: list[str] | None = None
    client_action: str | None = None
    steps: list[str] | None = None
    step_statuses: list[str] | None = None
    invocations: int | None = None
    reads: int | None = None
    exhausted: bool = False
    model_saw_content: bool = False


@dataclass
class Scenario:
    name: str
    message: str
    steps: list[AgentStep]
    expect: Expect
    budget: AgentBudget | None = None
    fail_after: int | None = None
    chapter: str = CHAPTER_TEXT
    supported: bool = True
    gap: str = ""


SCENARIOS: list[Scenario] = [
    Scenario(
        name="search_then_answer",
        message="найди Zero Escape",
        steps=[search("Zero Escape"), final("Нашла финал.", "result:1")],
        expect=Expect(answer_contains="Нашла финал", refs=["result:1"], invocations=1),
    ),
    Scenario(
        name="read_content_before_answering",
        message="прочитай финал Zero Escape и расскажи, что там",
        steps=[
            search("Zero Escape"),
            read("result:2"),
            final("В третьей главе Ремна встречает Экземпляр.", "result:2"),
        ],
        expect=Expect(
            answer_contains="Экземпляр",
            refs=["result:2"],
            reads=1,
            model_saw_content=True,
            steps=["search_global_v1", "read"],
        ),
    ),
    Scenario(
        name="fetch_page_before_answering",
        message="зайди на статью про парадоксы и перескажи",
        steps=[
            fetch("https://example.org/article"),
            final("Парадокс возникает, когда решение очевидно и ошибочно."),
        ],
        expect=Expect(answer_contains="Парадокс", steps=["fetch"]),
    ),
    Scenario(
        name="search_then_act_on_a_playable_result",
        message="включи финал Zero Escape",
        steps=[search("Zero Escape"), act("result:1", "play"), final("Запускаю финал.")],
        expect=Expect(client_action="play", steps=["search_global_v1", "act"]),
    ),
    Scenario(
        name="act_on_unplayable_result_is_refused_not_guessed",
        message="включи третью главу",
        steps=[search("Re:Zero"), act("result:2", "play"), final("Главу нельзя включить, могу прочитать.")],
        expect=Expect(
            answer_contains="нельзя включить",
            client_action=None,
            step_statuses=["success", "error"],
        ),
    ),
    Scenario(
        name="refine_before_answering",
        message="найди что-нибудь по zer escape",
        steps=[search("zer escape"), search("zer escape прохождение"), final("Нашла прохождение.")],
        expect=Expect(invocations=2, steps=["search_global_v1", "search_global_v1"]),
    ),
    Scenario(
        name="duplicate_call_keeps_model_answer",
        message="найди и опиши",
        steps=[search("Zero Escape"), search("Zero Escape"), final("Нашла оба варианта.")],
        expect=Expect(
            answer_contains="оба варианта",
            invocations=1,
            step_statuses=["success", "skipped"],
        ),
    ),
    Scenario(
        name="clarification_ends_the_turn",
        message="включи серию",
        steps=[ask("Какую именно серию?", "Zero Escape", "Re:Zero")],
        expect=Expect(
            question="Какую именно серию?",
            question_options=["Zero Escape", "Re:Zero"],
            invocations=0,
        ),
    ),
    Scenario(
        name="step_budget_is_enforced_and_explained",
        message="ищи всё подряд",
        steps=[search(f"запрос {index}") for index in range(6)],
        budget=AgentBudget(max_steps=3),
        expect=Expect(exhausted=True, steps=["search_global_v1", "search_global_v1", "search_global_v1"]),
    ),
    Scenario(
        name="model_outage_ends_the_turn_honestly",
        message="найди Zero Escape",
        steps=[search("Zero Escape")],
        fail_after=1,
        expect=Expect(answer_contains="недоступна", refs=[]),
    ),
    Scenario(
        name="unknown_tool_does_not_break_the_loop",
        message="сделай что-нибудь",
        steps=[
            AgentStep(kind="tool", tool="drop_everything", arguments={"confirm": True}),
            search("Zero Escape"),
            final("Нашла."),
        ],
        expect=Expect(
            answer_contains="Нашла",
            step_statuses=["error", "success"],
            invocations=1,
        ),
    ),
    Scenario(
        name="missing_required_argument_is_reported",
        message="найди что-нибудь",
        steps=[
            AgentStep(kind="tool", tool="search_global_v1", arguments={"limit": 5}),
            final("Не смогла найти."),
        ],
        expect=Expect(invocations=0, step_statuses=["error"]),
    ),
    Scenario(
        name="mutation_runs_without_asking_permission",
        message="сохрани это в Vault",
        steps=[search("Zero Escape"), final("Сохраню.")],
        expect=Expect(answer_contains="Сохраню", invocations=1, steps=["search_global_v1"]),
    ),
    Scenario(
        name="grounding_keeps_only_referenced_results",
        message="найди и опиши",
        steps=[search("Zero Escape"), final("Вот первый.", "result:1")],
        expect=Expect(refs=["result:1"]),
    ),
    Scenario(
        name="read_of_unknown_reference_is_an_error_step",
        message="прочитай непонятно что",
        steps=[read("result:9"), final("Не нашла такого.")],
        expect=Expect(step_statuses=["error"], reads=0),
    ),
    Scenario(
        name="chapter_text_is_bounded_by_the_reference",
        message="прочитай главу",
        steps=[search("Re:Zero"), read("result:2"), final("Прочитала.")],
        chapter=CHAPTER_TEXT * 4_000,
        expect=Expect(reads=1, model_saw_content=True, answer_contains="Прочитала"),
    ),
    Scenario(
        name="prose_from_the_model_is_still_grounded",
        message="найди Zero Escape",
        steps=[search("Zero Escape")],
        expect=Expect(answer_contains="Пустой ответ"),
    ),
]


def run_scenario(scenario: Scenario) -> dict[str, Any]:
    """Execute one scenario against the real cascade engine and grade the outcome."""
    model = OracleModel(scenario.steps, fail_after=scenario.fail_after)
    backend = OracleBackend(chapter=scenario.chapter)
    request = AgentTurnRequest(
        message=scenario.message,
        budget=scenario.budget or AgentBudget(),
    )
    try:
        result = asyncio.run(CascadeEngine(model, backend).run(request))
    except Exception as exc:
        return {
            "name": scenario.name,
            "error": f"{type(exc).__name__}: {exc}",
            "steps": 0,
            "ok": False,
            "failures": [f"engine raised {type(exc).__name__}"],
        }
    observed: dict[str, Any] = {
        "name": scenario.name,
        "answer": result.answer,
        "question": result.question,
        "refs": result.refs,
        "client_action": result.client_action,
        "steps": len(result.steps),
        "tool_names": [record.tool for record in result.steps],
        "statuses": [record.status for record in result.steps],
        "invocations": len(backend.invocations),
        "reads": len(backend.reads),
        "exhausted": result.exhausted,
        "model_saw_content": any(
            bool(item.get("text")) for state in model.states for item in state.get("executed", [])
        ),
    }
    expect = scenario.expect
    failures: list[str] = []
    if expect.answer_contains and expect.answer_contains not in result.answer:
        failures.append(f"answer {result.answer!r} misses {expect.answer_contains!r}")
    if expect.question is not None and result.question != expect.question:
        failures.append(f"question={result.question!r} != {expect.question!r}")
    if expect.question_options and list(result.question_options) != expect.question_options:
        failures.append(f"options={result.question_options} != {expect.question_options}")
    if expect.refs is not None and list(result.refs) != expect.refs:
        failures.append(f"refs={result.refs} != {expect.refs}")
    if expect.client_action is not None and result.client_action != expect.client_action:
        failures.append(f"client_action={result.client_action!r} != {expect.client_action!r}")
    if expect.steps is not None and observed["tool_names"] != expect.steps:
        failures.append(f"steps={observed['tool_names']} != {expect.steps}")
    if expect.step_statuses is not None and observed["statuses"] != expect.step_statuses:
        failures.append(f"statuses={observed['statuses']} != {expect.step_statuses}")
    if expect.invocations is not None and observed["invocations"] != expect.invocations:
        failures.append(f"invocations={observed['invocations']} != {expect.invocations}")
    if expect.reads is not None and observed["reads"] != expect.reads:
        failures.append(f"reads={observed['reads']} != {expect.reads}")
    if observed["exhausted"] != expect.exhausted:
        failures.append(f"exhausted={observed['exhausted']} != {expect.exhausted}")
    if expect.model_saw_content and not observed["model_saw_content"]:
        failures.append("the model never saw any content")
    observed["failures"] = failures
    observed["ok"] = not failures
    return observed


def report() -> dict[str, Any]:
    """Run every scenario and summarise the engine's behavioural baseline."""
    results = [run_scenario(scenario) for scenario in SCENARIOS]
    supported = [
        (scenario, result) for scenario, result in zip(SCENARIOS, results, strict=True) if scenario.supported
    ]
    gaps = [
        (scenario, result)
        for scenario, result in zip(SCENARIOS, results, strict=True)
        if not scenario.supported
    ]
    return {
        "results": results,
        "scenarios": SCENARIOS,
        "supported_total": len(supported),
        "supported_passed": sum(1 for _, result in supported if result["ok"]),
        "gaps": gaps,
        "avg_steps": round(sum(result["steps"] for result in results) / len(results), 2) if results else 0.0,
        "regressions": [scenario.name for scenario, result in supported if not result["ok"]],
    }


def render(summary: dict[str, Any]) -> str:
    lines = ["scenario                                    state  result"]
    for scenario, result in zip(summary["scenarios"], summary["results"], strict=True):
        state = "ok   " if scenario.supported and result["ok"] else "FAIL " if scenario.supported else "gap  "
        detail = "; ".join(result.get("failures", []) or [result.get("error", "")])
        lines.append(f"{scenario.name:<42}{state} {detail or '-'}")
    lines.append("")
    lines.append(
        f"supported: {summary['supported_passed']}/{summary['supported_total']} "
        f"| gaps: {len(summary['gaps'])} | avg steps: {summary['avg_steps']}"
    )
    for scenario, _ in summary["gaps"]:
        lines.append(f"  gap: {scenario.name} - {scenario.gap}")
    return "\n".join(lines)


def main() -> int:
    summary = report()
    print(render(summary))
    return 1 if summary["regressions"] else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SCENARIOS", "main", "render", "report", "run_scenario"]
