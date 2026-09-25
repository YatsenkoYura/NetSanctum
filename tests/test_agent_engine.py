import asyncio
import json
import unittest

import httpx
from pydantic import ValidationError

from app.core.agent.backend import AgentBackendUnavailableError
from app.core.agent.catalog import AgentTool, build_tool_catalog
from app.core.agent.engine import (
    AgentBudget,
    AgentHistoryTurn,
    AgentTurnRequest,
    AgentUnavailableError,
    CascadeEngine,
)
from app.core.agent.model import TRUNCATION_NUDGE, OpenAICompatibleModel, step_from_completion
from app.core.agent.primitives import AgentStep
from app.core.agent.references import AgentReference, project_references

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
            "score": 0.98,
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
            "score": 0.81,
        },
    ],
    "warnings": [],
}

TOOLS = build_tool_catalog(
    [
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
)


class ScriptedModel:
    """A model made of decisions: exactly what the engine is supposed to obey."""

    def __init__(self, steps: list[AgentStep], *, fail_after: int | None = None):
        self.steps = list(steps)
        self.fail_after = fail_after
        self.calls = 0
        self.seen_states: list[dict] = []
        self.seen_tools: list[list[str]] = []

    async def next_step(self, message, tools, state):
        self.calls += 1
        self.seen_states.append(state)
        self.seen_tools.append([tool.name for tool in tools])
        if self.fail_after is not None and self.calls > self.fail_after:
            raise AgentUnavailableError("model offline")
        if not self.steps:
            return AgentStep(kind="final", tool="final", arguments={"answer": "Пустой ответ."})
        return self.steps.pop(0)


class StubBackend:
    def __init__(self, *, search: dict | None = None, text: str = "Первый абзац главы."):
        self.search = SEARCH_RESULT if search is None else search
        self.text = text
        self.invocations: list[tuple[str, dict]] = []
        self.reads: list[tuple[str, str]] = []

    async def catalog(self):
        return TOOLS

    async def invoke(self, integration_id, parameters):
        self.invocations.append((integration_id, parameters))
        if integration_id != "search.global.v1":
            raise LookupError("unknown integration")
        return self.search

    async def read(self, module_id, item_id, max_chars):
        self.reads.append((module_id, item_id))
        return {"kind": "text", "title": f"{module_id}/{item_id}", "text": self.text, "truncated": False}


def final(answer: str, *refs: str) -> AgentStep:
    return AgentStep(kind="final", tool="final", arguments={"answer": answer, "refs": list(refs)})


def search(query: str) -> AgentStep:
    return AgentStep(
        kind="tool",
        tool="search_global_v1",
        arguments={"query": query, "limit": 5},
    )


def run(engine: CascadeEngine, request: AgentTurnRequest | None = None, **kwargs):
    return asyncio.run(engine.run(request or AgentTurnRequest(message="найди Zero Escape"), **kwargs))


class CascadeEngineTests(unittest.TestCase):
    def test_search_then_read_gives_the_model_actual_content(self):
        model = ScriptedModel(
            [
                search("Zero Escape"),
                AgentStep(kind="tool", tool="read", arguments={"ref": "result:2"}),
                final("В третьей главе Ремна встречает Экземпляр.", "result:2"),
            ]
        )
        backend = StubBackend()
        events: list[tuple[str, dict]] = []

        async def progress(phase, data):
            events.append((phase, data))

        result = run(CascadeEngine(model, backend), on_progress=progress)

        self.assertEqual("В третьей главе Ремна встречает Экземпляр.", result.answer)
        self.assertEqual(["alllib", "chapter-3"], list(backend.reads[0]))
        self.assertEqual("search", backend.invocations[0][0].split(".")[0])
        # the terminal final is not a step: the loop stops when the model answers
        self.assertEqual(["success", "success"], [record.status for record in result.steps])
        self.assertEqual(2, len([phase for phase, _ in events if phase == "step"]))
        # The content must be visible to the model, not only to the human.
        read_state = model.seen_states[2]
        executed_texts = [item.get("text") for item in read_state["executed"] if item.get("tool") == "read"]
        self.assertEqual(["Первый абзац главы."], executed_texts)

    def test_act_requires_a_playable_target(self):
        model = ScriptedModel(
            [
                search("Zero Escape"),
                AgentStep(kind="tool", tool="act", arguments={"ref": "result:1", "action": "play"}),
                final("Запускаю."),
            ]
        )
        result = run(CascadeEngine(model, StubBackend()))
        self.assertEqual("play", result.client_action)
        self.assertEqual("success", result.steps[1].status)

    def test_act_on_unplayable_result_is_reported_not_guessed(self):
        model = ScriptedModel(
            [
                search("Zero Escape"),
                AgentStep(kind="tool", tool="act", arguments={"ref": "result:2", "action": "play"}),
                final("Это нельзя включить."),
            ]
        )
        result = run(CascadeEngine(model, StubBackend()))
        self.assertIsNone(result.client_action)
        self.assertEqual("error", result.steps[1].status)
        self.assertIn("cannot be played", result.steps[1].summary)

    def test_ask_ends_the_turn_with_a_question(self):
        model = ScriptedModel(
            [
                AgentStep(
                    kind="ask",
                    tool="ask",
                    arguments={"question": "Какую главу?", "options": ["Третью", "Пятую"]},
                )
            ]
        )
        result = run(CascadeEngine(model, StubBackend()))
        self.assertEqual("Какую главу?", result.question)
        self.assertEqual(["Третью", "Пятую"], result.question_options)
        self.assertEqual("", result.answer)

    def test_references_keep_numbering_across_steps(self):
        model = ScriptedModel(
            [
                search("Zero Escape"),
                search("Re:Zero"),
                final("Нашла оба."),
            ]
        )
        result = run(CascadeEngine(model, StubBackend()))
        refs = [reference.ref for reference in result.references]
        self.assertEqual(["result:1", "result:2"], refs)
        # the second search found the same documents, so nothing was renumbered
        self.assertEqual("alllib", result.references[1].module_id)

    def test_identical_step_is_not_executed_twice(self):
        model = ScriptedModel(
            [
                search("Zero Escape"),
                search("Zero Escape"),
                final("Готово."),
            ]
        )
        backend = StubBackend()
        result = run(CascadeEngine(model, backend))
        self.assertEqual(1, len(backend.invocations))
        self.assertEqual("skipped", result.steps[1].status)
        self.assertEqual("Готово.", result.answer)

    def test_step_budget_forces_an_explained_answer(self):
        model = ScriptedModel(
            [
                AgentStep(kind="tool", tool="search_global_v1", arguments={"query": f"q{index}"})
                for index in range(6)
            ]
        )
        result = run(
            CascadeEngine(model, StubBackend()),
            AgentTurnRequest(message="ищи", budget=AgentBudget(max_steps=3)),
        )
        self.assertTrue(result.exhausted)
        self.assertEqual(3, len(result.steps))
        self.assertIn("шаги", result.answer)

    def test_model_outage_ends_the_turn_honestly(self):
        model = ScriptedModel([search("Zero Escape")], fail_after=1)
        result = run(CascadeEngine(model, StubBackend()))
        self.assertIn("недоступна", result.answer)
        self.assertEqual([], result.refs)
        self.assertEqual("success", result.steps[0].status)

    def test_unknown_tool_does_not_break_the_loop(self):
        model = ScriptedModel(
            [
                AgentStep(kind="tool", tool="drop_database", arguments={"table": "all"}),
                search("Zero Escape"),
                final("Нашла."),
            ]
        )
        backend = StubBackend()
        result = run(CascadeEngine(model, backend))
        self.assertEqual("error", result.steps[0].status)
        self.assertEqual(1, len(backend.invocations))
        self.assertEqual("Нашла.", result.answer)

    def test_missing_required_argument_is_an_error_step(self):
        model = ScriptedModel(
            [
                AgentStep(kind="tool", tool="search_global_v1", arguments={"limit": 5}),
                final("Не смогла найти."),
            ]
        )
        backend = StubBackend()
        result = run(CascadeEngine(model, backend))
        self.assertEqual([], backend.invocations)
        self.assertIn("missing arguments", result.steps[0].summary)

    def test_final_without_refs_gets_context_refs(self):
        model = ScriptedModel([search("Zero Escape"), final("Нашла финал.")])
        result = run(CascadeEngine(model, StubBackend()))
        self.assertEqual(["result:1", "result:2"], result.refs)

    def test_history_and_references_reach_the_model(self):
        model = ScriptedModel([final("Ок.")])
        request = AgentTurnRequest(
            message="что дальше",
            history=[AgentHistoryTurn(user="найди Zero Escape", assistant="Нашла два результата.")],
            references=[
                AgentReference(
                    ref="result:1",
                    module_id="video_archiver",
                    item_id="video-1",
                    title="Zero Escape finale",
                )
            ],
        )
        run(CascadeEngine(model, StubBackend()), request)
        state = model.seen_states[0]
        self.assertEqual("что дальше", state["goal"])
        self.assertEqual(1, len(state["conversation"]))
        self.assertEqual("Zero Escape finale", state["references"][0]["title"])

    def test_stalled_turn_only_offers_the_closing_tools(self):
        offered_after_stall: list[list[str]] = []

        class WatchfulModel(ScriptedModel):
            async def next_step(self, message, tools, state):
                offered_after_stall.append([tool.name for tool in tools])
                return await super().next_step(message, tools, state)

        model = WatchfulModel([search(f"запрос {index}") for index in range(6)])
        run(CascadeEngine(model, StubBackend()), AgentTurnRequest(message="ищи"))
        self.assertEqual(
            ["read", "fetch", "act", "ask", "final", "search_global_v1"],
            offered_after_stall[0],
        )
        # after the free steps run out, the model can only wrap up or ask
        self.assertTrue(any(offer == ["ask", "final"] for offer in offered_after_stall[3:]))

    def test_stalled_turn_still_answers_with_the_last_model_step(self):
        class NeverFinishes(ScriptedModel):
            async def next_step(self, message, tools, state):
                self.calls += 1
                names = {tool.name for tool in tools}
                if names <= {"ask", "final"}:
                    return final("Вот что я нашла.", "result:1")
                self.steps = self.steps[1:] if self.steps else []
                return search("ещё раз")

        model = NeverFinishes([search("a"), search("b"), search("c"), search("d")])
        result = run(CascadeEngine(model, StubBackend()))
        self.assertEqual("Вот что я нашла.", result.answer)
        self.assertFalse(result.exhausted)

    def test_queued_calls_run_before_the_model_is_asked_again(self):
        class ParallelModel(ScriptedModel):
            async def next_step(self, message, tools, state):
                if self.calls == 0:
                    self.calls += 1
                    return AgentStep(
                        kind="tool",
                        tool="search_global_v1",
                        arguments={"query": "Zero Escape", "limit": 5},
                        queued=[{"tool": "act", "arguments": {"ref": "result:1", "action": "play"}}],
                    )
                return final("Запускаю и нашлась.")

        model = ParallelModel([])
        backend = StubBackend()
        result = run(CascadeEngine(model, backend))
        self.assertEqual(["search_global_v1", "act"], [record.tool for record in result.steps])
        self.assertEqual("play", result.client_action)
        self.assertEqual(1, model.calls)

    def test_wall_clock_budget_stops_the_loop(self):
        model = ScriptedModel([search("Zero Escape") for _ in range(5)])
        result = run(
            CascadeEngine(model, StubBackend()),
            AgentTurnRequest(message="ищи", budget=AgentBudget(max_steps=5, wall_clock_seconds=5)),
        )
        self.assertTrue(result.exhausted or len(result.steps) == 1)

    def test_backend_outage_is_reported_as_a_failed_turn(self):
        class BrokenBackend(StubBackend):
            async def catalog(self):
                raise AgentBackendUnavailableError("catalog unavailable")

        model = ScriptedModel([final("Ок.")])
        with self.assertRaises(AgentBackendUnavailableError):
            run(CascadeEngine(model, BrokenBackend()))


def _flaky_transport(calls: list[dict]):
    """First reply is cut off mid-reasoning, second one is a real tool call."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            body = {"choices": [{"finish_reason": "length", "message": {"reasoning_content": "thinking"}}]}
        else:
            body = {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {"function": {"name": "final", "arguments": '{"answer":"готово"}'}}
                            ]
                        },
                    }
                ]
            }
        return httpx.Response(200, json=body, request=request)

    return handler


def _flaky_transport(calls: list[dict]):
    """First reply is cut off mid-reasoning, the second one is a real tool call."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            body = {"choices": [{"finish_reason": "length", "message": {"reasoning_content": "thinking"}}]}
        else:
            body = {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {"function": {"name": "final", "arguments": '{"answer":"готово"}'}}
                            ]
                        },
                    }
                ]
            }
        return httpx.Response(200, json=body, request=request)

    return handler


class ModelProtocolTests(unittest.TestCase):
    def test_completion_must_contain_exactly_one_known_tool(self):
        known = {"final", "read"}
        good = {
            "choices": [
                {"message": {"tool_calls": [{"function": {"name": "final", "arguments": '{"answer":"ок"}'}}]}}
            ]
        }
        self.assertEqual("final", step_from_completion(good, known).tool)
        # prose, a terminal call next to another call and truncation have their own tests
        for payload in (
            {"choices": [{"message": {"tool_calls": []}}]},
            {"choices": [{"message": {"tool_calls": [{"function": {"name": "rm_rf", "arguments": "{}"}}]}}]},
            {
                "choices": [
                    {"message": {"tool_calls": [{"function": {"name": "final", "arguments": "не json"}}]}}
                ]
            },
            {},
        ):
            with self.subTest(payload=payload), self.assertRaises(AgentUnavailableError):
                step_from_completion(payload, known)

    def test_truncated_reply_is_retried_once_before_failing(self):
        calls: list[dict] = []

        model = OpenAICompatibleModel(
            "http://local/v1/chat/completions",
            "m",
            transport=httpx.MockTransport(_flaky_transport(calls)),
        )
        step = asyncio.run(model.next_step("привет", TOOLS, {"goal": "привет", "executed": []}))
        self.assertEqual("final", step.tool)
        self.assertEqual(2, len(calls))
        self.assertNotIn(TRUNCATION_NUDGE, calls[0]["messages"][0]["content"])
        self.assertIn(TRUNCATION_NUDGE, calls[1]["messages"][0]["content"])

    def test_prose_answer_is_treated_as_the_final_answer(self):
        step = step_from_completion(
            {"choices": [{"message": {"content": "В главе речь о встрече."}}]},
            {"final"},
        )
        self.assertEqual("final", step.kind)
        self.assertEqual("В главе речь о встрече.", step.arguments["answer"])

    def test_empty_prose_is_still_a_failure(self):
        with self.assertRaises(AgentUnavailableError):
            step_from_completion({"choices": [{"message": {"content": "   "}}]}, {"final"})

    def test_terminal_call_wins_over_extra_calls(self):
        step = step_from_completion(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"function": {"name": "read", "arguments": '{"ref":"result:1"}'}},
                                {"function": {"name": "final", "arguments": '{"answer":"готово"}'}},
                            ]
                        }
                    }
                ]
            },
            {"read", "final"},
        )
        self.assertEqual("final", step.tool)

    def test_two_calls_become_one_step_and_one_queued_call(self):
        step = step_from_completion(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"function": {"name": "read", "arguments": '{"ref":"result:1"}'}},
                                {"function": {"name": "read", "arguments": '{"ref":"result:2"}'}},
                            ]
                        }
                    }
                ]
            },
            {"read"},
        )
        self.assertEqual("read", step.tool)
        self.assertEqual("result:1", step.arguments["ref"])
        self.assertEqual([{"tool": "read", "arguments": {"ref": "result:2"}}], step.queued)

    def test_queued_call_with_an_unknown_tool_is_refused(self):
        with self.assertRaises(AgentUnavailableError):
            step_from_completion(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {"function": {"name": "read", "arguments": '{"ref":"result:1"}'}},
                                    {"function": {"name": "nuke", "arguments": "{}"}},
                                ]
                            }
                        }
                    ]
                },
                {"read"},
            )

    def test_prose_answer_is_truncated(self):
        step = step_from_completion(
            {"choices": [{"message": {"content": "x" * 5_000}}]},
            {"final"},
        )
        self.assertEqual(2_000, len(step.arguments["answer"]))

    def test_final_requires_an_answer(self):
        with self.assertRaises(AgentUnavailableError):
            step_from_completion(
                {
                    "choices": [
                        {"message": {"tool_calls": [{"function": {"name": "final", "arguments": "{}"}}]}}
                    ]
                },
                {"final"},
            )

    def test_tool_schema_is_passed_through_to_the_provider(self):
        tool = next(item for item in TOOLS if item.name == "search_global_v1")
        self.assertEqual("search.query.v1", tool.contract)
        self.assertIn("query", tool.parameters["properties"])
        self.assertIsInstance(tool, AgentTool)

    def test_tool_definition_rejects_unsafe_names(self):
        with self.assertRaises(ValidationError):
            AgentTool(name="Bad Name", description="x", parameters={})


class ReferenceProjectionTests(unittest.TestCase):
    def test_search_contract_projection_numbers_and_trims(self):
        references = project_references("search.query.v1", "search", SEARCH_RESULT)
        self.assertEqual(["result:1", "result:2"], [item.ref for item in references])
        self.assertEqual("video-1", references[0].item_id)
        self.assertTrue(references[0].playable)
        self.assertEqual("/alllib/reader/9", references[1].open_url)

    def test_unknown_contract_projects_nothing(self):
        self.assertEqual([], project_references("something.else.v1", "miku", SEARCH_RESULT))

    def test_library_contract_projection_builds_a_resource_url(self):
        result = {
            "module_id": "alllib",
            "items": [
                {
                    "id": "42",
                    "kind": "novel",
                    "title": "Re:Zero",
                    "description": "Ранобэ",
                    "readable": True,
                    "playable": False,
                }
            ],
        }
        references = project_references("library.viewer.v1", "alllib", result)
        self.assertEqual("/api/miku/resources/alllib/42", references[0].open_url)
        self.assertTrue(references[0].readable)

    def test_reference_numbering_is_bounded(self):
        items = [
            {
                "source_module_id": "m",
                "source_integration_id": "i",
                "document_id": f"d{index}",
                "entity_type": "e",
                "title": f"t{index}",
            }
            for index in range(30)
        ]
        references = project_references("search.query.v1", "search", {"items": items})
        self.assertEqual(20, len(references))


if __name__ == "__main__":
    unittest.main()
