import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import WebSocketDisconnect
from pydantic import ValidationError

from app.contracts.vault_capture_v1 import VaultCaptureRequest
from app.core.agent.engine import AgentTurnResult
from app.core.agent.references import AgentReference
from app.core.agent_client import AgentClient
from app.core.module_types import IntegrationContext, IntegrationRejectedError
from app.core.security import get_current_user
from app.modules.miku.models import MikuEpisodeMemory, MikuProfileMemory, MikuTurnAudit
from app.modules.miku.module import MODULE
from app.modules.miku.router import (
    REST_CONTEXT_LOCK_SECONDS,
    SOCKET_MESSAGE_LIMIT,
    _rest_context,
    _save_rest_context,
    _send_event,
    miku_socket,
    router,
    websocket_origin_allowed,
    websocket_owner_session,
)
from app.modules.miku.schemas import (
    MikuEpisodeMemoryItem,
    MikuMemorySnapshot,
    MikuProfileMemoryItem,
    MikuQuery,
    MikuReference,
    MikuReply,
    MikuSessionMemory,
    MikuSocketMessage,
)
from app.modules.miku.service import (
    MikuSessionContext,
    audit_turn,
    capabilities,
    job_status,
    query,
    resolve_resource,
    unavailable_reply,
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
                "request_schema": {"type": "object", "properties": {"operation": {"type": "string"}}},
                "resource_schema": {"type": "object", "properties": {"item_id": {"type": "string"}}},
                "effects": {"effect": "read", "external_io": False, "idempotent": True},
            },
            {
                "id": "media.audio.import.v1",
                "contract": None,
                "module_id": "music",
                "request_schema": {"type": "object", "properties": {}},
                "effects": {"effect": "create", "external_io": True, "idempotent": False},
            },
            {
                "id": "youtube.video_source.v1",
                "contract": "video.source.catalog.v1",
                "module_id": "youtube",
                "request_schema": {"type": "object", "properties": {"operation": {"type": "string"}}},
                "effects": {"effect": "read", "external_io": True, "idempotent": True},
            },
            {
                "id": "media.video.archive.v1",
                "contract": None,
                "module_id": "video_archiver",
                "request_schema": {"type": "object", "properties": {}},
                "effects": {"effect": "create", "external_io": True, "idempotent": False},
            },
            {
                "id": "vault.capture.v1",
                "contract": None,
                "module_id": "vault",
                "request_schema": {"type": "object", "properties": {}},
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
        items = [
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
        ]
        if payload.get("operation") == "search":
            search = payload.get("query", "").casefold()
            items = [
                item
                for item in items
                if search
                in " ".join(
                    str(item.get(key) or "") for key in ("title", "subtitle", "description")
                ).casefold()
            ]
        return {
            "module_id": "music",
            "title": "Music",
            "order": 10,
            "items": items,
        }

    async def resolve_integration_resource(self, integration_id, payload, context):
        from app.core.module_types import IntegrationResource

        return IntegrationResource(kind="audio", title="Neon Song", storage_path=self.resource_path)

    def storage_owner(self, namespace):
        return "music" if namespace == "music" else None

    def validate_integration_request(self, integration_id, payload, context):
        return payload


class GlobalSearchRegistry(StubRegistry):
    def integration_catalog(self, consumer_id=None):
        return [
            *super().integration_catalog(consumer_id),
            {
                "id": "search.global.v1",
                "contract": "search.query.v1",
                "module_id": "search",
                "request_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["query"],
                },
                "effects": {"effect": "read", "external_io": False, "idempotent": True},
            },
        ]

    async def invoke_integration(self, integration_id, payload, context):
        if integration_id != "search.global.v1":
            return await super().invoke_integration(integration_id, payload, context)
        self.calls.append((integration_id, payload, context))
        return {
            "items": [
                {
                    "source_module_id": "video_archiver",
                    "source_integration_id": "video_archiver.search.documents.v1",
                    "document_id": "video-1",
                    "entity_type": "video",
                    "title": "Zero Escape finale",
                    "subtitle": "Archive Channel",
                    "summary": "Final episode",
                    "open_path": "/video-archiver/dashboard?miku_item=video-1",
                    "playable": True,
                    "score": 0.98,
                },
                {
                    "source_module_id": "vault",
                    "source_integration_id": "vault.search.documents.v1",
                    "document_id": "note-2",
                    "entity_type": "note",
                    "title": "Escape notes",
                    "open_path": "/vault/dashboard",
                    "score": 0.4,
                },
            ],
            "warnings": [],
        }


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


class StubTokenStore:
    def __init__(self):
        self.keys = set()

    async def set(self, key, value, *, ex, nx):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


class MikuTests(unittest.TestCase):
    def test_manifest_declares_bounded_read_and_memory_integrations(self):
        # library.viewer is declared for server-side resource resolution only;
        # discovery is still exposed solely via search.global.v1 (see
        # test_global_search_is_the_only_local_material_read_tool).
        self.assertEqual(("library.viewer.v1", "video.source.catalog.v1"), MODULE.uses_integration_contracts)
        self.assertEqual(
            (
                "media.video.archive.v1",
                "miku.conversation.note.search.v1",
                "miku.conversation.note.write.v1",
                "miku.memory.search.v1",
                "miku.memory.undo.v1",
                "miku.memory.write.v1",
                "search.global.v1",
                "vault.capture.v1",
            ),
            MODULE.uses_integrations,
        )
        # Memory is reachable as a tool, never as a hardcoded command word. So are the
        # notes the agent keeps inside one conversation.
        self.assertEqual(
            (
                "miku.conversation.note.search.v1",
                "miku.conversation.note.undo.v1",
                "miku.conversation.note.write.v1",
                "miku.memory.search.v1",
                "miku.memory.undo.v1",
                "miku.memory.write.v1",
            ),
            tuple(sorted(item.id for item in MODULE.integrations)),
        )
        reversible = {
            item.id: item.effects.undo_integration for item in MODULE.integrations if item.effects.reversible
        }
        self.assertEqual(
            {
                "miku.conversation.note.write.v1": "miku.conversation.note.undo.v1",
                "miku.memory.write.v1": "miku.memory.undo.v1",
            },
            reversible,
        )
        self.assertEqual((), MODULE.browser_policies)
        self.assertEqual("app.modules.miku.tasks", MODULE.tasks)

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
        self.assertEqual(3, result.protocol_version)
        self.assertEqual(
            ["read", "fetch", "act", "ask", "final"],
            result.commands[:5],
        )
        self.assertIn("music.library.viewer.v1", result.commands)
        self.assertIn(
            "music.library.viewer.v1",
            [provider.integration_id for provider in result.providers],
        )

    def test_query_delegates_to_the_agent_and_remembers_the_turn(self):
        events: list[tuple[str, dict]] = []

        async def on_event(phase, data):
            events.append((phase, data))

        reply = MikuReply(
            command="find",
            text="Нашла финал.",
            references=[
                MikuReference(
                    ref="result:1", module_id="alllib", item_id="9", kind="novel", title="Re:Zero 3"
                )
            ],
        )
        context = MikuSessionContext()
        with patch(
            "app.modules.miku.service.run_agent_turn",
            AsyncMock(return_value=reply),
        ) as run:
            result = asyncio.run(
                query(
                    MikuQuery(message="найди финал"),
                    None,
                    SimpleNamespace(id=1),
                    StubRegistry(),
                    context=context,
                    on_event=on_event,
                )
            )
        self.assertIs(result, reply)
        self.assertIsNotNone(run.await_args)
        self.assertEqual("найди финал", run.await_args.args[0].message)
        self.assertIs(context, run.await_args.args[1])
        self.assertEqual(1, len(context.history))
        self.assertEqual("найди финал", context.history[0].user)
        self.assertEqual("Нашла финал.", context.history[0].assistant)
        self.assertEqual([item.ref for item in reply.references], [item.ref for item in result.references])

    def test_history_stays_bounded_to_six_turns(self):
        context = MikuSessionContext(history=[])
        for index in range(9):
            with patch(
                "app.modules.miku.service.run_agent_turn",
                AsyncMock(return_value=MikuReply(command="respond", text=f"ответ {index}")),
            ):
                asyncio.run(
                    query(
                        MikuQuery(message=f"вопрос {index}"),
                        None,
                        SimpleNamespace(id=1),
                        StubRegistry(),
                        context=context,
                    )
                )
        self.assertEqual(6, len(context.history or []))
        self.assertEqual("ответ 8", (context.history or [])[-1].assistant)

    def test_missing_agent_runtime_says_so_instead_of_guessing(self):
        with patch(
            "app.modules.miku.service.run_agent_turn",
            AsyncMock(return_value=None),
        ):
            result = asyncio.run(
                query(MikuQuery(message="найди финал"), None, SimpleNamespace(id=1), StubRegistry())
            )
        self.assertEqual("respond", result.command)
        self.assertIn("недоступен", result.text)
        self.assertEqual([], result.references)
        self.assertEqual("status", result.segments[0].kind)

    def test_unavailable_reply_is_always_speakable_and_plain(self):
        reply = unavailable_reply()
        self.assertTrue(reply.segments[0].speak or reply.segments[0].kind == "status")
        self.assertNotIn("\U0001f600", reply.text)

    def test_capabilities_list_primitives_before_integrations(self):
        result = capabilities(StubRegistry())
        self.assertEqual(["read", "fetch", "act", "ask", "final"], result.commands[:5])
        self.assertTrue(all(name.endswith(".v1") for name in result.commands[5:]))

    def test_assistant_output_strips_emoji(self):
        reply = MikuReply(command="respond", text="Привет \U0001f44b \u2728")
        reference = MikuReference(
            ref="result:1",
            module_id="music",
            item_id="1",
            kind="audio",
            title="Track \U0001f3b5",
        )

        self.assertEqual("Привет", reply.text)
        self.assertEqual("Привет", reply.segments[0].text)
        self.assertEqual("Track", reference.title)

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

    def test_memory_schemas_split_session_profile_and_episodic_layers(self):
        session = MikuSessionMemory(
            summary="Обсуждали Zero Escape и Re:Zero.",
            recent_topics=["Zero Escape", "Re:Zero"],
            active_references=[
                MikuReference(
                    ref="result:1",
                    module_id="video_archiver",
                    item_id="video-1",
                    kind="video",
                    title="Zero Escape walkthrough 1",
                    playable=True,
                )
            ],
        )
        profile = MikuProfileMemoryItem(
            memory_key="preferences.voice.language",
            value={"value": "ru"},
            source="explicit",
            confidence=1.0,
        )
        episodic = MikuEpisodeMemoryItem(
            summary="User watched Zero Escape part 1.",
            subject="Zero Escape",
            tags=["video", "playback"],
            source_module_id="video_archiver",
            source_item_id="video-1",
        )
        snapshot = MikuMemorySnapshot(session=session, profile=[profile], episodic=[episodic])

        self.assertEqual(900, snapshot.session.ttl_seconds)
        self.assertEqual("preferences.voice.language", snapshot.profile[0].memory_key)
        self.assertEqual(["video", "playback"], snapshot.episodic[0].tags)

    def test_memory_models_expose_audit_profile_and_episode_tables(self):
        self.assertEqual(
            {
                "miku_turn_audit",
                "miku_profile_memory",
                "miku_episode_memory",
            },
            {
                MikuTurnAudit.__tablename__,
                MikuProfileMemory.__tablename__,
                MikuEpisodeMemory.__tablename__,
            },
        )

    def test_dashboard_renders_remote_values_with_text_content(self):
        dashboard = Path("app/modules/miku/templates/miku_dashboard.html").read_text()
        assistant = Path("static/miku-assistant.js").read_text()
        self.assertIn("textContent", dashboard)
        self.assertIn("textContent", assistant)
        self.assertNotIn("innerHTML", assistant)
        self.assertIn("segment.speak", assistant)
        self.assertNotIn("miku-video", dashboard)
        self.assertNotIn('value="{{ provider.model }}" required', dashboard)
        self.assertIn("payload.references?.length === 1", assistant)

    def test_rest_calls_survive_a_dead_connection_and_report_it_readably(self):
        assistant = Path("static/miku-assistant.js").read_text()
        # A restart leaves the browser on a dead pooled socket, so every REST call
        # retries once and asks for a fresh connection instead of surfacing the
        # browser's bare "Failed to fetch" to the user.
        self.assertIn("async function postJson(", assistant)
        self.assertIn("cache: 'no-store'", assistant)
        self.assertIn("Нет связи с сервером", assistant)
        self.assertNotIn("line(error.message || 'Request failed', 'error')", assistant)
        self.assertIn("connect();\n            return await send();", assistant)

    def test_the_drawer_offers_the_stored_conversations(self):
        assistant = Path("static/miku-assistant.js").read_text()
        markup = Path("app/modules/miku/templates/miku_assistant.html").read_text()
        for element in (
            'id="miku-conversation"',
            'id="miku-conversation-new"',
            'id="miku-conversation-rename"',
            'id="miku-conversation-delete"',
        ):
            self.assertIn(element, markup)
        # Both transports have to name the thread, or a turn is stored nowhere.
        self.assertEqual(3, assistant.count("conversation_id: conversationId"))
        self.assertIn("/api/miku/conversations", assistant)
        # Remote text is rendered as text, never as markup.
        self.assertIn("row.textContent = item.role === 'user'", assistant)

    def test_video_archive_miku_link_opens_without_artificial_delay(self):
        dashboard = Path("app/modules/video_archiver/templates/video_dashboard.html").read_text()

        self.assertIn("if (mikuItem) {\n            await window.playVideoInLibrary(mikuItem);", dashboard)
        self.assertNotIn("// 2-second Preload Delay", dashboard)

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
                ("PUT", "/api/miku/providers"),
                ("POST", "/api/miku/query"),
                ("POST", "/api/miku/transcribe"),
                ("POST", "/api/miku/speech"),
                ("GET", "/api/miku/memory"),
                ("GET", "/api/miku/cascades"),
                ("POST", "/api/miku/cascades/{cascade_id}/undo/{step_index}"),
                ("GET", "/api/miku/jobs/{task_id}"),
                ("GET", "/api/miku/resources/{module_id}/{item_id}"),
                ("GET", "/api/miku/conversations"),
                ("POST", "/api/miku/conversations"),
                ("GET", "/api/miku/conversations/{conversation_id}"),
                ("PUT", "/api/miku/conversations/{conversation_id}"),
                ("DELETE", "/api/miku/conversations/{conversation_id}"),
                ("GET", "/api/miku/conversations/{conversation_id}/notes"),
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

    def test_socket_cancel_message_is_typed(self):
        message = MikuSocketMessage(type="cancel", request_id="turn:1")
        self.assertEqual("cancel", message.type)
        with self.assertRaises(ValidationError):
            MikuSocketMessage(type="cancel", request_id="turn:1", message="unexpected")
        with self.assertRaises(ValidationError):
            MikuSocketMessage(type="ping", request_id="ping:1", message="unexpected")

    def test_socket_voice_message_is_typed(self):
        message = MikuSocketMessage(
            type="voice",
            request_id="turn:2",
            audio="ZmFrZS1hdWRpbw==",
            audio_content_type="audio/webm",
        )
        self.assertEqual("voice", message.type)
        with self.assertRaises(ValidationError):
            MikuSocketMessage(type="voice", request_id="turn:2")
        with self.assertRaises(ValidationError):
            MikuSocketMessage(
                type="voice",
                request_id="turn:2",
                audio="ZmFrZS1hdWRpbw==",
                audio_content_type="",
            )
        with self.assertRaises(ValidationError):
            MikuSocketMessage(
                type="query",
                request_id="turn:3",
                message="hello",
                audio="ZmFrZS1hdWRpbw==",
            )

    def test_socket_cancel_without_active_turn_is_acknowledged(self):
        websocket = StubWebSocket(['{"type":"cancel","request_id":"turn:9"}'])
        with patch("app.modules.miku.router.redis_client.get", AsyncMock(return_value="1")):
            asyncio.run(miku_socket(websocket))

        events = [(item["event"], item["request_id"]) for item in websocket.sent]
        self.assertIn(("session.ready", None), events)
        self.assertIn(("turn.cancelled", "turn:9"), events)

    def test_socket_turn_streams_partial_events_before_result(self):
        class SlowDisconnectStub(StubWebSocket):
            async def receive_text(self):
                try:
                    return next(self.messages)
                except StopIteration:
                    await asyncio.sleep(3)
                    raise WebSocketDisconnect()

        websocket = SlowDisconnectStub(['{"type":"query","request_id":"turn:1","message":"hello"}'])

        async def fake_query(*args, on_event=None, **kwargs):
            assert on_event is not None
            await on_event("acknowledgement", {"text": "Сейчас найду!"})
            await on_event(
                "tool_result",
                {"integration_id": "search.global.v1", "status": "success", "result_count": 1},
            )
            return MikuReply(command="respond", text="Готово.")

        class _NullDB:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def commit(self):
                return None

            async def rollback(self):
                return None

            def add(self, *args, **kwargs):
                return None

        async def _redis_get(key):
            if key.startswith("session:"):
                return "1"
            return None

        with (
            patch("app.modules.miku.router.redis_client.get", side_effect=_redis_get),
            patch("app.modules.miku.router.query", fake_query),
            patch("app.modules.miku.router._acquire_context_lock", AsyncMock(return_value=None)),
            patch("app.modules.miku.router._release_context_lock", AsyncMock()),
            patch("app.modules.miku.router._save_rest_context", AsyncMock()),
            patch("app.modules.miku.router.AsyncSessionLocal", return_value=_NullDB()),
            patch("app.modules.miku.router.audit_turn", return_value=None),
        ):
            asyncio.run(miku_socket(websocket))

        kinds = [(item["event"], item.get("data", {}).get("phase")) for item in websocket.sent]
        self.assertEqual("session.ready", kinds[0][0])
        self.assertEqual("turn.started", kinds[1][0])
        partials = [item for item in websocket.sent if item["event"] == "turn.partial"]
        self.assertEqual(
            ["acknowledgement", "tool_result"],
            [item["data"]["phase"] for item in partials],
        )
        self.assertEqual("Сейчас найду!", partials[0]["data"]["text"])
        result_index = next(
            index for index, item in enumerate(websocket.sent) if item["event"] == "turn.result"
        )
        completed_index = next(
            index for index, item in enumerate(websocket.sent) if item["event"] == "turn.completed"
        )
        self.assertLess(result_index, completed_index)
        self.assertLess(
            websocket.sent.index(partials[-1]),
            result_index,
        )

    def test_socket_turn_runs_the_real_cascade_over_a_stored_context(self):
        """The socket path must survive a session context restored from Redis."""
        stored = json.dumps(
            {
                "references": [
                    {
                        "ref": "result:1",
                        "module_id": "alllib",
                        "item_id": "9",
                        "kind": "novel",
                        "title": "Re:Zero",
                        "readable": True,
                    }
                ],
                "history": [{"user": "привет", "assistant": "Привет!"}],
            }
        )

        class SlowDisconnectStub(StubWebSocket):
            async def receive_text(self):
                try:
                    return next(self.messages)
                except StopIteration:
                    await asyncio.sleep(3)
                    raise WebSocketDisconnect()

        websocket = SlowDisconnectStub(['{"type":"query","request_id":"turn:9","message":"что дальше?"}'])
        seen: dict = {}

        class FakeAgent:
            enabled = True

            async def turn(self, **kwargs):
                seen.update(kwargs)
                yield AgentTurnResult(
                    answer="Продолжаю с третьей главы.",
                    refs=["result:1"],
                    references=[
                        AgentReference(
                            ref="result:1",
                            module_id="alllib",
                            item_id="9",
                            kind="novel",
                            title="Re:Zero",
                            readable=True,
                        )
                    ],
                )

        class _NullDB:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def commit(self):
                return None

            async def rollback(self):
                return None

            async def flush(self):
                return None

            def add(self, *args, **kwargs):
                return None

        async def _redis_get(key):
            if key.startswith("session:"):
                return "1"
            if key.startswith("miku:context:"):
                return stored
            return None

        with (
            patch("app.modules.miku.router.redis_client.get", side_effect=_redis_get),
            patch("app.modules.miku.router.redis_client.setex", AsyncMock()),
            patch("app.modules.miku.router._acquire_context_lock", AsyncMock(return_value=None)),
            patch("app.modules.miku.router._release_context_lock", AsyncMock()),
            patch("app.modules.miku.router.AsyncSessionLocal", return_value=_NullDB()),
            patch("app.modules.miku.router.audit_turn", return_value=None),
            patch(
                "app.modules.miku.service.load_provider_bundle",
                AsyncMock(return_value=SimpleNamespace(llm=None, stt=None, tts=None)),
            ),
            patch("app.modules.miku.agent_turn.agent_client", return_value=FakeAgent()),
        ):
            asyncio.run(miku_socket(websocket))

        self.assertEqual(
            [],
            [item for item in websocket.sent if item["event"] == "turn.error"],
            f"events: {[(i['event'], i.get('data', {}).get('code')) for i in websocket.sent]}",
        )
        replies = [item for item in websocket.sent if item["event"] == "turn.result"]
        self.assertEqual(1, len(replies))
        self.assertEqual("Продолжаю с третьей главы.", replies[0]["data"]["text"])
        self.assertEqual("что дальше?", seen["message"])
        self.assertEqual(1, len(seen["references"]))
        self.assertEqual(1, len(seen["history"]))

    def test_socket_voice_turn_transcribes_then_runs_turn(self):
        import base64 as stdlib_base64

        class SlowDisconnectStub(StubWebSocket):
            async def receive_text(self):
                try:
                    return next(self.messages)
                except StopIteration:
                    await asyncio.sleep(3)
                    raise WebSocketDisconnect()

        audio = stdlib_base64.b64encode(b"fake-utterance").decode()
        websocket = SlowDisconnectStub(
            [
                '{"type":"voice","request_id":"turn:7","audio":"'
                + audio
                + '","audio_content_type":"audio/webm"}'
            ]
        )

        async def fake_query(*args, **kwargs):
            return MikuReply(command="respond", text="Готово.")

        class _NullDB:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def commit(self):
                return None

            async def rollback(self):
                return None

            def add(self, *args, **kwargs):
                return None

        async def _redis_get(key):
            if key.startswith("session:"):
                return "1"
            return None

        with (
            patch("app.modules.miku.router.redis_client.get", side_effect=_redis_get),
            patch("app.modules.miku.router.query", fake_query),
            patch("app.modules.miku.router._acquire_context_lock", AsyncMock(return_value=None)),
            patch("app.modules.miku.router._release_context_lock", AsyncMock()),
            patch("app.modules.miku.router._save_rest_context", AsyncMock()),
            patch("app.modules.miku.router.AsyncSessionLocal", return_value=_NullDB()),
            patch("app.modules.miku.router.audit_turn", return_value=None),
            patch(
                "app.modules.miku.router.load_provider_bundle",
                AsyncMock(return_value=SimpleNamespace(stt=SimpleNamespace())),
            ),
            patch.object(AgentClient, "transcribe", AsyncMock(return_value="включи zero escape")),
        ):
            asyncio.run(miku_socket(websocket))

        partials = [item for item in websocket.sent if item["event"] == "turn.partial"]
        self.assertEqual("transcript", partials[0]["data"]["phase"])
        self.assertEqual("включи zero escape", partials[0]["data"]["text"])
        self.assertIn("turn.result", [item["event"] for item in websocket.sent])
        self.assertIn("turn.completed", [item["event"] for item in websocket.sent])

    def test_socket_voice_rejects_unsupported_audio(self):
        import base64 as stdlib_base64

        audio = stdlib_base64.b64encode(b"fake-utterance").decode()
        websocket = StubWebSocket(
            [
                '{"type":"voice","request_id":"turn:8","audio":"'
                + audio
                + '","audio_content_type":"audio/bogus"}'
            ]
        )

        async def _redis_get(key):
            if key.startswith("session:"):
                return "1"
            return None

        with patch("app.modules.miku.router.redis_client.get", side_effect=_redis_get):
            asyncio.run(miku_socket(websocket))

        errors = [item for item in websocket.sent if item["event"] == "turn.error"]
        self.assertEqual("unsupported_audio", errors[0]["data"]["code"])

    def test_rest_context_is_owner_scoped_bounded_and_ephemeral(self):
        self.assertGreaterEqual(REST_CONTEXT_LOCK_SECONDS, 120)
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


if __name__ == "__main__":
    unittest.main()
