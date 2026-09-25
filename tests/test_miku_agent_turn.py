import asyncio
import json
import unittest
from types import SimpleNamespace

import httpx

from app.core.agent.engine import AgentTurnResult
from app.core.agent_client import AgentClient
from app.modules.miku.agent_turn import (
    history_for_agent,
    run_agent_turn,
    to_agent_profile,
    to_agent_references,
    to_miku_reply,
)
from app.modules.miku.schemas import MikuConversationTurn, MikuQuery, MikuReference
from app.modules.miku.service import MikuSessionContext

RUNTIME_TOKEN = "runtime-secret"

TURN_RESULT = {
    "answer": "В третьей главе Ремна встречает Экземпляр.",
    "mood": "neutral",
    "refs": ["result:2"],
    "client_action": None,
    "question": None,
    "question_options": [],
    "steps": [
        {
            "tool": "search_global_v1",
            "status": "success",
            "summary": "Найдено результатов: 2.",
            "arguments": {"query": "Re:Zero"},
            "reference_refs": ["result:1", "result:2"],
        }
    ],
    "skeleton": ["search", "read"],
    "references": [
        {
            "ref": "result:1",
            "module_id": "video_archiver",
            "item_id": "video-1",
            "kind": "video",
            "title": "Zero Escape finale",
            "playable": True,
            "readable": False,
            "open_url": "/video-archiver/dashboard?miku_item=video-1",
        },
        {
            "ref": "result:2",
            "module_id": "alllib",
            "item_id": "chapter-3",
            "kind": "novel",
            "title": "Re:Zero chapter 3",
            "playable": False,
            "readable": True,
            "open_url": "/alllib/reader/9",
        },
    ],
    "exhausted": False,
}


def frame(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def turn_stream(
    result: dict | None = None, *, progress: list[dict] | None = None, fail: bool = False
) -> bytes:
    lines = [
        frame(
            {
                "type": "step",
                "tool": "search_global_v1",
                "status": "success",
                "summary": "Найдено результатов: 2.",
                "result_count": 2,
            }
        )
    ]
    lines.extend(frame(item) for item in (progress or []))
    if fail:
        lines.append(frame({"type": "failed", "reason": "RuntimeError"}))
    else:
        lines.append(frame({"type": "done", "result": result or TURN_RESULT}))
    return ("\n".join(lines) + "\n").encode()


class AgentBridgeTests(unittest.TestCase):
    def _client(self, handler) -> AgentClient:
        return AgentClient(
            enabled=True,
            url="http://agent-runtime:8780",
            token=RUNTIME_TOKEN,
            transport=httpx.MockTransport(handler),
        )

    def _handler(self, body: bytes, status: int = 200):
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(RUNTIME_TOKEN, request.headers.get("X-Agent-Runtime-Token"))
            return httpx.Response(status, content=body, request=request)

        return handler

    def test_agent_reply_keeps_only_referenced_results(self):
        reply = to_miku_reply(AgentTurnResult.model_validate(TURN_RESULT))
        self.assertEqual("В третьей главе Ремна встречает Экземпляр.", reply.text)
        self.assertEqual(["result:2"], [item.ref for item in reply.references])
        self.assertEqual("Re:Zero chapter 3", reply.references[0].title)
        self.assertIsNone(reply.client_action)

    def test_client_action_becomes_the_reply_command(self):
        result = {**TURN_RESULT, "client_action": "play", "refs": ["result:1"]}
        reply = to_miku_reply(AgentTurnResult.model_validate(result))
        self.assertEqual("play", reply.command)
        self.assertEqual("play", reply.client_action)

    def test_question_becomes_a_clarification_reply(self):
        result = {
            **TURN_RESULT,
            "answer": "",
            "question": "Какую главу прочитать?",
            "question_options": ["Третью", "Пятую"],
        }
        reply = to_miku_reply(AgentTurnResult.model_validate(result))
        self.assertEqual("Какую главу прочитать?", reply.text)
        self.assertEqual("Какую главу прочитать?", reply.question)
        self.assertEqual(["Третью", "Пятую"], reply.question_options)

    def test_only_known_context_reaches_the_model(self):
        context = MikuSessionContext(
            references=[
                MikuReference(
                    ref="result:1",
                    module_id="alllib",
                    item_id="9",
                    kind="novel",
                    title="Re:Zero",
                    readable=True,
                )
            ],
            history=[MikuConversationTurn(user="найди Re:Zero", assistant="Нашла.")],
        )
        projected = to_agent_references(context.references)
        self.assertEqual(1, len(projected))
        self.assertEqual("/api/miku/resources/alllib/9", projected[0].open_url)
        self.assertEqual(
            [("найди Re:Zero", "Нашла.")],
            [(turn.user, turn.assistant) for turn in history_for_agent(context)],
        )

    def test_turn_streams_progress_before_the_result(self):
        client = self._client(self._handler(turn_stream()))

        async def run():
            async for item in client.turn(message="прочитай третью главу"):
                if isinstance(item, AgentTurnResult):
                    return item
            return None

        result = asyncio.run(run())
        self.assertEqual("В третьей главе Ремна встречает Экземпляр.", result.answer)
        self.assertEqual("search_global_v1", result.steps[0].tool)

    def test_bridge_reports_progress_to_the_caller(self):
        events: list[tuple[str, dict]] = []

        async def on_event(phase, data):
            events.append((phase, data))

        client = self._client(self._handler(turn_stream()))

        async def run():
            return await run_agent_turn(
                MikuQuery(message="прочитай третью главу"),
                MikuSessionContext(),
                client=client,
                on_event=on_event,
            )

        reply = asyncio.run(run())
        self.assertIsNotNone(reply)
        self.assertIn("acknowledgement", [phase for phase, _ in events])
        self.assertIn("tool_result", [phase for phase, _ in events])

    def test_unusable_provider_values_never_kill_a_turn(self):
        """A stray type in provider settings must not turn a turn into an error."""
        broken = SimpleNamespace(url=None, model=object(), api_key=123, mode="api", server_callable=True)
        dropped = to_agent_profile(broken)
        self.assertEqual("", dropped.url)
        self.assertEqual("", dropped.model)
        self.assertEqual("", dropped.api_key)
        usable = SimpleNamespace(url="https://x/v1", model="m", api_key="k", mode="api", server_callable=True)
        profile = to_agent_profile(usable)
        self.assertEqual("https://x/v1", profile.url)
        self.assertIsNone(to_agent_profile(SimpleNamespace(server_callable=False)))
        self.assertIsNone(to_agent_profile(None))

    def test_turn_is_reported_to_the_caller_after_the_reply_is_built(self):
        seen: list[tuple] = []

        async def on_turn(turn, reply):
            seen.append((turn.answer, reply.text))

        client = self._client(self._handler(turn_stream()))

        async def run():
            return await run_agent_turn(
                MikuQuery(message="найди Re:Zero"),
                MikuSessionContext(),
                client=client,
                on_turn=on_turn,
            )

        reply = asyncio.run(run())
        self.assertIsNotNone(reply)
        self.assertEqual(1, len(seen))
        self.assertEqual(reply.text, seen[0][1])

    def test_a_failing_recorder_does_not_break_the_turn(self):
        async def on_turn(turn, reply):
            raise RuntimeError("storage is down")

        client = self._client(self._handler(turn_stream()))
        reply = asyncio.run(
            run_agent_turn(
                MikuQuery(message="найди Re:Zero"),
                MikuSessionContext(),
                client=client,
                on_turn=on_turn,
            )
        )
        self.assertIsNotNone(reply)

    def test_disabled_agent_never_calls_the_runtime(self):
        calls: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, content=turn_stream(), request=request)

        client = AgentClient(
            enabled=False,
            url="http://agent-runtime:8780",
            token=RUNTIME_TOKEN,
            transport=httpx.MockTransport(handler),
        )
        reply = asyncio.run(run_agent_turn(MikuQuery(message="привет"), MikuSessionContext(), client=client))
        self.assertIsNone(reply)
        self.assertEqual([], calls)

    def test_runtime_outage_falls_back_instead_of_failing(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, request=request)

        client = self._client(handler)
        reply = asyncio.run(run_agent_turn(MikuQuery(message="привет"), MikuSessionContext(), client=client))
        self.assertIsNone(reply)

    def test_failed_cascade_falls_back(self):
        client = self._client(self._handler(turn_stream(fail=True)))
        reply = asyncio.run(run_agent_turn(MikuQuery(message="привет"), MikuSessionContext(), client=client))
        self.assertIsNone(reply)

    def test_unreachable_runtime_falls_back(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        client = self._client(handler)
        reply = asyncio.run(run_agent_turn(MikuQuery(message="привет"), MikuSessionContext(), client=client))
        self.assertIsNone(reply)

    def test_garbage_frames_are_ignored_not_fatal(self):
        body = "not json\n\n" + frame({"type": "done", "result": TURN_RESULT}) + "\n"
        client = self._client(self._handler(body.encode()))
        result = asyncio.run(_first_turn(client))
        self.assertEqual("В третьей главе Ремна встречает Экземпляр.", result.answer)

    def test_reference_without_open_url_is_still_reported(self):
        result = {
            **TURN_RESULT,
            "refs": ["result:1"],
            "references": [{**TURN_RESULT["references"][0], "open_url": None}],
        }
        reply = to_miku_reply(AgentTurnResult.model_validate(result))
        self.assertEqual("/api/miku/resources/video_archiver/video-1", reply.references[0].resource_url)


async def _first_turn(client: AgentClient) -> AgentTurnResult:
    async for item in client.turn(message="привет"):
        if isinstance(item, AgentTurnResult):
            return item
    raise AssertionError("no result")


if __name__ == "__main__":
    unittest.main()
