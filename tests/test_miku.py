import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import WebSocketDisconnect
from pydantic import ValidationError

from app.contracts.vault_capture_v1 import VaultCaptureRequest
from app.core.module_types import IntegrationContext, IntegrationRejectedError
from app.core.security import get_current_user
from app.modules.miku.models import MikuTurnAudit
from app.modules.miku.module import MODULE
from app.modules.miku.router import (
    SOCKET_MESSAGE_LIMIT,
    _rest_context,
    _save_rest_context,
    _send_event,
    miku_socket,
    router,
    websocket_origin_allowed,
    websocket_owner_session,
)
from app.modules.miku.schemas import MikuDecision, MikuQuery, MikuReference, MikuReply, MikuSocketMessage
from app.modules.miku.service import (
    MikuActionSigner,
    MikuQueryError,
    MikuSessionContext,
    audit_turn,
    capabilities,
    confirm_action,
    job_status,
    query,
    resolve_resource,
)
from app.modules.vault.integrations import capture_item


class StubRegistry:
    def __init__(self, *, fail: bool = False, resource_path: str = "music/song.mp3"):
        self.fail = fail
        self.resource_path = resource_path
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
            {
                "id": "youtube.video_source.v1",
                "contract": "video.source.catalog.v1",
                "module_id": "youtube",
                "effects": {"effect": "read", "external_io": True, "idempotent": True},
            },
            {
                "id": "media.video.archive.v1",
                "contract": None,
                "module_id": "video_archiver",
                "effects": {"effect": "create", "external_io": True, "idempotent": False},
            },
            {
                "id": "vault.capture.v1",
                "contract": None,
                "module_id": "vault",
                "effects": {"effect": "create", "external_io": False, "idempotent": False},
            },
        ]

    async def invoke_integration(self, integration_id, payload, context):
        self.calls.append((integration_id, payload, context))
        if self.fail:
            from app.core.module_types import IntegrationUnavailableError

            raise IntegrationUnavailableError("private provider error")
        if integration_id == "youtube.video_source.v1":
            return {
                "title": "YouTube",
                "items": [
                    {
                        "entity_type": "youtube_video",
                        "entity_id": "abc123",
                        "kind": "video",
                        "title": "Neon Mix",
                        "description": "Night drive",
                        "channel_title": "Test Channel",
                        "source_url": "https://www.youtube.com/watch?v=abc123",
                    },
                    {
                        "entity_type": "youtube_video",
                        "entity_id": "def456",
                        "kind": "video",
                        "title": "Night Mix",
                        "description": "Ambient mix",
                        "source_url": "https://www.youtube.com/watch?v=def456",
                    },
                ],
            }
        if integration_id == "media.video.archive.v1":
            return {
                "status": "dispatched",
                "task_id": "job-1",
                "platform": "youtube",
                "message": "Video archive queued",
            }
        if integration_id == "vault.capture.v1":
            return {
                "status": "completed",
                "item_id": 11,
                "kind": payload["kind"],
                "title": payload["title"],
                "message": f"Saved {payload['kind']} to Vault",
            }
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

    async def resolve_integration_resource(self, integration_id, payload, context):
        from app.core.module_types import IntegrationResource

        return IntegrationResource(kind="audio", title="Neon Song", storage_path=self.resource_path)

    def storage_owner(self, namespace):
        return "music" if namespace == "music" else None


class StubWebSocket:
    def __init__(self, messages):
        self.headers = {"origin": "https://netsanctum.local", "host": "netsanctum.local"}
        self.cookies = {"access_token": "session-id"}
        self.query_params = {"context_id": "test-session"}
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


class StubPlanner:
    async def decide(self, message):
        return MikuDecision(command="list", argument="music")


class StubTokenStore:
    def __init__(self):
        self.keys = set()

    async def set(self, key, value, *, ex, nx):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


class MikuTests(unittest.TestCase):
    def test_manifest_declares_bounded_read_and_confirmed_action_integrations(self):
        self.assertEqual(("library.viewer.v1", "video.source.catalog.v1"), MODULE.uses_integration_contracts)
        self.assertEqual(("media.video.archive.v1", "vault.capture.v1"), MODULE.uses_integrations)
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

    def test_capabilities_expose_only_declared_read_providers(self):
        result = capabilities(StubRegistry())

        self.assertEqual("guarded", result.mode)
        self.assertEqual(
            ["music.library.viewer.v1", "youtube.video_source.v1"],
            [provider.integration_id for provider in result.providers],
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

    def test_query_executes_only_the_runtime_structured_decision(self):
        registry = StubRegistry()
        result = asyncio.run(
            query(
                MikuQuery(message="show something useful"),
                None,
                None,
                registry,
                runtime=StubPlanner(),
            )
        )
        self.assertEqual("list", result.command)
        self.assertEqual("music.library.viewer.v1", registry.calls[0][0])

    def test_repeat_uses_only_bounded_socket_context(self):
        registry = StubRegistry()
        context = MikuSessionContext()
        first = asyncio.run(query(MikuQuery(message="find neon"), None, None, registry, context=context))
        reply = asyncio.run(query(MikuQuery(message="repeat"), None, None, registry, context=context))

        self.assertEqual("repeat", reply.command)
        self.assertEqual(first.references, reply.references)
        self.assertEqual(1, len(registry.calls))

    def test_repeat_without_socket_context_is_rejected(self):
        with self.assertRaises(MikuQueryError):
            asyncio.run(query(MikuQuery(message="repeat"), None, None, StubRegistry()))

    def test_rest_context_is_owner_scoped_bounded_and_ephemeral(self):
        context = MikuSessionContext(
            references=[
                MikuReference(
                    ref="result:1",
                    module_id="music",
                    item_id="7",
                    kind="audio",
                    title="Neon Song",
                )
            ]
        )
        with patch("app.modules.miku.router.redis_client.setex", AsyncMock()) as setex:
            asyncio.run(_save_rest_context("session-1", 7, context))
        key, ttl, serialized = setex.await_args_list[0].args
        self.assertEqual("miku:context:7:session-1", key)
        self.assertEqual(900, ttl)

        with patch("app.modules.miku.router.redis_client.get", AsyncMock(return_value=serialized)):
            restored = asyncio.run(_rest_context("session-1", 7))
        self.assertIsNotNone(restored)
        assert restored is not None and restored.references is not None
        self.assertEqual("Neon Song", restored.references[0].title)

    def test_discover_preview_and_confirm_are_separate_bounded_steps(self):
        registry = StubRegistry()
        context = MikuSessionContext()
        user = SimpleNamespace(id=1)
        discovered = asyncio.run(
            query(MikuQuery(message="discover neon"), None, user, registry, context=context)
        )
        preview = asyncio.run(
            query(MikuQuery(message="archive result:1"), None, user, registry, context=context)
        )

        self.assertEqual("youtube", discovered.references[0].module_id)
        self.assertEqual("youtube_video", discovered.references[0].entity_type)
        self.assertEqual(["result:1", "result:2"], [item.ref for item in discovered.references])
        self.assertIsNotNone(preview.pending_action)
        assert preview.pending_action is not None
        token_store = StubTokenStore()
        result = asyncio.run(
            confirm_action(preview.pending_action.confirmation_token, None, user, registry, token_store)
        )

        self.assertEqual("job-1", result.task_id)
        integration_id, payload, invocation = registry.calls[-1]
        self.assertEqual("media.video.archive.v1", integration_id)
        self.assertEqual("youtube_video", payload["entity_type"])
        self.assertEqual("miku", invocation.consumer_id)
        with self.assertRaises(MikuQueryError):
            asyncio.run(
                confirm_action(
                    preview.pending_action.confirmation_token,
                    None,
                    user,
                    registry,
                    token_store,
                )
            )

    def test_action_confirmation_is_user_bound_and_tamper_evident(self):
        reference = MikuReference(
            ref="result:1",
            module_id="youtube",
            item_id="abc123",
            kind="video",
            title="Neon Mix",
            entity_type="youtube_video",
        )
        signer = MikuActionSigner(secret="test-secret", ttl_seconds=60)
        token = signer.create(1, reference)

        self.assertEqual("abc123", signer.verify(token, 1)["entity_id"])
        with self.assertRaises(MikuQueryError):
            signer.verify(token, 2)
        with self.assertRaises(MikuQueryError):
            signer.verify(f"{token[:-1]}x", 1)
        with self.assertRaises(MikuQueryError):
            signer.verify(f"{token}!!!!", 1)

    def test_vault_note_requires_preview_and_single_use_confirmation(self):
        registry = StubRegistry()
        user = SimpleNamespace(id=1)
        preview = asyncio.run(query(MikuQuery(message="note buy tea"), None, user, registry))
        self.assertIsNotNone(preview.pending_action)
        assert preview.pending_action is not None
        action = preview.pending_action
        self.assertEqual("note", action.action)

        token_store = StubTokenStore()
        result = asyncio.run(
            confirm_action(
                action.confirmation_token,
                None,
                user,
                registry,
                token_store,
            )
        )
        self.assertEqual("completed", result.status)
        self.assertEqual("vault.capture.v1", registry.calls[-1][0])
        self.assertEqual("buy tea", registry.calls[-1][1]["content"])
        with self.assertRaises(MikuQueryError):
            asyncio.run(
                confirm_action(
                    action.confirmation_token,
                    None,
                    user,
                    registry,
                    token_store,
                )
            )

    def test_vault_bookmark_rejects_non_http_urls(self):
        for url in ("file:///etc/passwd", "https://user:secret@example.com"):
            with self.subTest(url=url), self.assertRaises(MikuQueryError):
                asyncio.run(
                    query(
                        MikuQuery(message=f"bookmark {url}"),
                        None,
                        SimpleNamespace(id=1),
                        StubRegistry(),
                    )
                )

    def test_vault_capture_integration_disables_remote_metadata_fetch(self):
        created = SimpleNamespace(id=11, title="Example")
        context = IntegrationContext(session=object(), user=None, registry=None, consumer_id="miku")
        with patch(
            "app.modules.vault.integrations.create_vault_item",
            AsyncMock(return_value=created),
        ) as create:
            result = asyncio.run(
                capture_item(
                    VaultCaptureRequest.model_validate(
                        {"kind": "bookmark", "title": "Example", "url": "https://example.com"}
                    ),
                    context,
                )
            )

        item = create.await_args_list[0].args[1]
        self.assertFalse(item.auto_fetch_og)
        self.assertEqual("completed", result.status)

    def test_open_and_play_only_use_current_session_references(self):
        context = MikuSessionContext(
            references=[
                MikuReference(
                    ref="result:1",
                    module_id="music",
                    item_id="7",
                    kind="audio",
                    title="Neon Song",
                    playable=True,
                    open_url="/api/miku/resources/music/7",
                    resource_url="/api/miku/resources/music/7",
                )
            ]
        )
        reply = asyncio.run(
            query(MikuQuery(message="play result:1"), None, None, StubRegistry(), context=context)
        )

        self.assertEqual("play", reply.client_action)
        with self.assertRaises(MikuQueryError):
            asyncio.run(
                query(MikuQuery(message="open result:2"), None, None, StubRegistry(), context=context)
            )

    def test_resource_resolution_stays_consumer_scoped(self):
        registry = StubRegistry()
        resource = asyncio.run(resolve_resource("music", "7", None, None, None, None, registry))

        self.assertEqual("audio", resource.kind)
        with self.assertRaises(IntegrationRejectedError):
            asyncio.run(
                resolve_resource(
                    "music",
                    "7",
                    None,
                    None,
                    None,
                    None,
                    StubRegistry(resource_path="music/../vault/private"),
                )
            )

    def test_job_status_projects_only_safe_tracker_fields(self):
        task = {
            "task_id": "job-1",
            "module": "video_archiver",
            "status": "Downloading",
            "progress": "20%",
            "title": "Neon Mix",
            "url": "https://private.example/video",
        }
        with patch("app.modules.miku.service.tracked_tasks", AsyncMock(return_value=[task])):
            status = asyncio.run(job_status("job-1"))

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual("20%", status.progress)
        self.assertNotIn("url", status.model_dump())

    def test_turn_audit_contains_only_bounded_metadata(self):
        self.assertEqual(
            {
                "id",
                "user_id",
                "request_id",
                "transport",
                "command",
                "result_count",
                "warning_count",
                "created_at",
            },
            set(MikuTurnAudit.__table__.columns.keys()),
        )
        events = []
        db = SimpleNamespace(add=events.append)
        reply = MikuReply(
            command="find",
            text="Found 1 item(s).",
            references=[
                MikuReference(
                    ref="result:1",
                    module_id="music",
                    item_id="7",
                    kind="audio",
                    title="Neon Song",
                )
            ],
        )

        audit_turn(db, SimpleNamespace(id=1), "turn:1", "websocket", reply)

        self.assertEqual(1, events[0].user_id)
        self.assertEqual("turn:1", events[0].request_id)
        self.assertEqual("find", events[0].command)
        self.assertEqual(1, events[0].result_count)

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

    def test_router_exposes_only_authenticated_assistant_routes(self):
        routes = {
            (method, route.path)
            for route in router.routes
            for method in (getattr(route, "methods", None) or {"WEBSOCKET"})
        }
        self.assertEqual(
            {
                ("GET", "/miku/dashboard"),
                ("GET", "/api/miku/capabilities"),
                ("GET", "/api/miku/runtime"),
                ("POST", "/api/miku/query"),
                ("POST", "/api/miku/transcribe"),
                ("POST", "/api/miku/speech"),
                ("POST", "/api/miku/actions/confirm"),
                ("GET", "/api/miku/jobs/{task_id}"),
                ("GET", "/api/miku/resources/{module_id}/{item_id}"),
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
