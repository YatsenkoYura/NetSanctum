import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import WebSocketDisconnect
from pydantic import ValidationError

from app.core.security import get_current_user
from app.modules.miku.module import MODULE
from app.modules.miku.router import (
    SOCKET_MESSAGE_LIMIT,
    _send_event,
    miku_socket,
    router,
    websocket_origin_allowed,
    websocket_owner_session,
)
from app.modules.miku.schemas import MikuQuery, MikuSocketMessage
from app.modules.miku.service import MikuQueryError, capabilities, query


class StubRegistry:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = []

    def integration_catalog(self, consumer_id=None):
        if consumer_id != "miku":
            raise AssertionError("MIKU must request a consumer-scoped catalog")
        return [
            {
                "id": "music.library.viewer.v1",
                "contract": "library.viewer.v1",
                "module_id": "music",
                "effects": {"effect": "read", "external_io": False, "idempotent": True},
            },
            {
                "id": "media.audio.import.v1",
                "contract": None,
                "module_id": "music",
                "effects": {"effect": "create", "external_io": True, "idempotent": False},
            },
        ]

    async def invoke_integration(self, integration_id, payload, context):
        self.calls.append((integration_id, payload, context))
        if self.fail:
            from app.core.module_types import IntegrationUnavailableError

            raise IntegrationUnavailableError("private provider error")
        return {
            "module_id": "music",
            "title": "Music",
            "order": 10,
            "items": [
                {
                    "id": "7",
                    "kind": "audio",
                    "title": "Neon Song",
                    "subtitle": "Test Artist",
                    "description": "Synthwave track",
                    "playable": True,
                    "storage_path": "/private/music.mp3",
                },
                {
                    "id": "8",
                    "kind": "audio",
                    "title": "Quiet Piano",
                    "description": "Instrumental",
                },
            ],
        }


class StubWebSocket:
    def __init__(self, messages):
        self.headers = {"origin": "https://netsanctum.local", "host": "netsanctum.local"}
        self.cookies = {"access_token": "session-id"}
        self.messages = iter(messages)
        self.sent = []
        self.accepted = False
        self.close_code = None

    async def accept(self):
        self.accepted = True

    async def close(self, code):
        self.close_code = code

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive_text(self):
        try:
            return next(self.messages)
        except StopIteration as exc:
            raise WebSocketDisconnect() from exc


class MikuTests(unittest.TestCase):
    def test_manifest_is_read_only_integration_consumer(self):
        self.assertEqual(("library.viewer.v1",), MODULE.uses_integration_contracts)
        self.assertEqual((), MODULE.uses_integrations)
        self.assertEqual((), MODULE.integrations)
        self.assertEqual((), MODULE.browser_policies)
        self.assertIsNone(MODULE.tasks)

    def test_query_rejects_blank_and_overlong_messages(self):
        with self.assertRaises(ValidationError):
            MikuQuery(message="   ")
        with self.assertRaises(ValidationError):
            MikuQuery(message="x" * 501)

    def test_socket_messages_are_bounded_and_typed(self):
        message = MikuSocketMessage(type="query", request_id="turn:1", message="  help  ")
        self.assertEqual("help", message.message)
        self.assertLess(SOCKET_MESSAGE_LIMIT, 16 * 1024)
        with self.assertRaises(ValidationError):
            MikuSocketMessage(type="query", request_id="turn/1", message="help")
        with self.assertRaises(ValidationError):
            MikuSocketMessage(type="ping", request_id="turn:1", message="unexpected")

    def test_capabilities_expose_only_read_library_providers(self):
        result = capabilities(StubRegistry())

        self.assertEqual("read-only", result.mode)
        self.assertEqual(
            ["music.library.viewer.v1"], [provider.integration_id for provider in result.providers]
        )

    def test_find_uses_scoped_integration_and_projects_safe_fields(self):
        registry = StubRegistry()
        result = asyncio.run(
            query(MikuQuery(message="найди synthwave"), None, SimpleNamespace(id=1), registry)
        )

        self.assertEqual("find", result.command)
        self.assertEqual(["Neon Song"], [item.title for item in result.references])
        self.assertEqual("result:1", result.references[0].ref)
        self.assertNotIn("storage_path", result.references[0].model_dump())
        integration_id, payload, context = registry.calls[0]
        self.assertEqual("music.library.viewer.v1", integration_id)
        self.assertEqual(50, payload["limit"])
        self.assertEqual("miku", context.consumer_id)

    def test_result_limit_is_enforced(self):
        result = asyncio.run(query(MikuQuery(message="list music", limit=1), None, None, StubRegistry()))
        self.assertEqual(1, len(result.references))

    def test_provider_failure_is_sanitized(self):
        result = asyncio.run(query(MikuQuery(message="list"), None, None, StubRegistry(fail=True)))
        self.assertEqual(["music is unavailable"], result.warnings)
        self.assertNotIn("private", result.model_dump_json())

    def test_unknown_command_and_provider_are_rejected(self):
        with self.assertRaises(MikuQueryError):
            asyncio.run(query(MikuQuery(message="delete everything"), None, None, StubRegistry()))
        with self.assertRaises(MikuQueryError):
            asyncio.run(query(MikuQuery(message="list vault"), None, None, StubRegistry()))

    def test_dashboard_renders_remote_values_with_text_content(self):
        template = Path("app/modules/miku/templates/miku_dashboard.html").read_text()
        self.assertIn("textContent", template)
        self.assertNotIn("innerHTML", template)

    def test_router_exposes_only_authenticated_read_shell_routes(self):
        routes = {
            (method, route.path)
            for route in router.routes
            for method in (getattr(route, "methods", None) or {"WEBSOCKET"})
        }
        self.assertEqual(
            {
                ("GET", "/miku/dashboard"),
                ("GET", "/api/miku/capabilities"),
                ("POST", "/api/miku/query"),
                ("WEBSOCKET", "/api/miku/ws"),
            },
            routes,
        )
        for route in (route for route in router.routes if getattr(route, "methods", None)):
            self.assertIn(get_current_user, {dependency.call for dependency in route.dependant.dependencies})

    def test_websocket_requires_same_origin_and_owner_session(self):
        websocket = SimpleNamespace(
            headers={"origin": "https://netsanctum.local", "host": "netsanctum.local"},
            cookies={"access_token": "session-id"},
        )
        self.assertTrue(websocket_origin_allowed(websocket))
        websocket.headers["origin"] = "https://attacker.invalid"
        self.assertFalse(websocket_origin_allowed(websocket))

        with patch("app.modules.miku.router.redis_client.get", AsyncMock(return_value="1")) as get:
            user = asyncio.run(websocket_owner_session(websocket))
            self.assertIsNotNone(user)
            assert user is not None
            self.assertEqual(1, user.id)
            get.assert_awaited_once_with("session:session-id")

    def test_socket_events_have_a_stable_envelope(self):
        websocket = SimpleNamespace(send_json=AsyncMock())
        asyncio.run(_send_event(websocket, "turn.started", request_id="turn:1"))
        websocket.send_json.assert_awaited_once_with(
            {"event": "turn.started", "request_id": "turn:1", "data": {}}
        )

    def test_socket_negotiates_protocol_and_answers_ping(self):
        websocket = StubWebSocket(['{"type":"ping","request_id":"ping:1"}'])
        with patch("app.modules.miku.router.redis_client.get", AsyncMock(return_value="1")):
            asyncio.run(miku_socket(websocket))

        self.assertTrue(websocket.accepted)
        self.assertEqual("session.ready", websocket.sent[0]["event"])
        self.assertEqual("session.pong", websocket.sent[1]["event"])
        self.assertEqual("ping:1", websocket.sent[1]["request_id"])


if __name__ == "__main__":
    unittest.main()
