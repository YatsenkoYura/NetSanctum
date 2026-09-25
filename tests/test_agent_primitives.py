import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from app.contracts.search_query_v1 import GlobalSearchRequest
from app.core.agent.catalog import build_tool_catalog, integration_tools, tool_name
from app.core.agent.fetch import RemoteFetchError, fetch_public_text, html_to_text, plain_to_text
from app.core.agent.primitives import (
    AgentActRequest,
    AgentAskRequest,
    AgentReadRequest,
    AgentStep,
)
from app.core.agent_router import router as agent_router
from app.core.database import get_db
from app.core.module_types import IntegrationResource

SEARCH_ITEM = {
    "id": "search.global.v1",
    "contract": "search.query.v1",
    "module_id": "search",
    "description": "Search every indexed module",
    "request_schema": GlobalSearchRequest.model_json_schema(),
    "resource_schema": None,
    "effects": {"effect": "read", "external_io": False, "idempotent": True},
}

READER_ITEM = {
    "id": "alllib.library.viewer.v1",
    "contract": "library.viewer.v1",
    "module_id": "alllib",
    "description": "Browse and read library items",
    "request_schema": {"type": "object", "properties": {"operation": {"type": "string"}}},
    "resource_schema": {"type": "object", "properties": {"item_id": {"type": "string"}}},
    "effects": {"effect": "read", "external_io": False, "idempotent": True},
}


class StubRegistry:
    def __init__(self, *, resource: IntegrationResource | None = None, fail: bool = False):
        self.resource = resource
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def integration_catalog(self, consumer_id=None):
        if consumer_id != "miku":
            from app.core.module_types import IntegrationUnavailableError

            raise IntegrationUnavailableError("consumer must be active")
        return sorted(
            [
                SEARCH_ITEM,
                READER_ITEM,
                {
                    "id": "vault.capture.v1",
                    "contract": None,
                    "module_id": "vault",
                    "description": "Save a page to Vault",
                    "request_schema": {"type": "object", "properties": {"url": {"type": "string"}}},
                    "resource_schema": None,
                    "effects": {"effect": "create", "external_io": False, "idempotent": False},
                },
            ],
            key=lambda item: item["id"],
        )

    def validate_integration_request(self, integration_id, payload, context):
        self.calls.append((integration_id, payload))
        if self.fail:
            from app.core.module_types import IntegrationRejectedError

            raise IntegrationRejectedError("nope")
        return payload

    async def invoke_integration(self, integration_id, payload, context):
        self.calls.append((integration_id, payload))
        return {"ok": True, "integration_id": integration_id}

    async def resolve_integration_resource(self, integration_id, payload, context):
        self.calls.append((integration_id, payload))
        return self.resource

    def storage_owner(self, namespace):
        return "alllib"


class AgentPrimitiveTests(unittest.TestCase):
    def test_catalog_exposes_primitives_before_integrations(self):
        tools = build_tool_catalog([SEARCH_ITEM])
        self.assertEqual(["read", "fetch", "act", "ask", "final"], [tool.name for tool in tools[:5]])
        self.assertTrue(all(tool.kind == "primitive" for tool in tools[:5]))
        self.assertEqual("search_global_v1", tools[5].name)
        self.assertEqual("search.global.v1", tools[5].integration_id)

    def test_tool_names_are_model_safe_and_unique(self):
        self.assertEqual("a_b_c_v1", tool_name("a.b-c.v1"))
        names = [tool.name for tool in build_tool_catalog([SEARCH_ITEM])]
        self.assertEqual(len(names), len(set(names)))

    def test_tool_schema_keeps_property_names_and_drops_prose(self):
        tools = {tool.name: tool for tool in build_tool_catalog([SEARCH_ITEM])}
        schema = tools["search_global_v1"].parameters
        self.assertIn("query", schema["properties"])
        self.assertIn("required_terms", schema["properties"])
        self.assertEqual(["query"], schema["required"])
        self.assertNotIn("title", schema)
        self.assertNotIn("description", schema["properties"]["query"])
        self.assertNotIn("maxLength", schema["properties"]["query"])

    def test_primitive_schemas_match_their_models(self):
        tools = {tool.name: tool for tool in build_tool_catalog([])}
        for name, model in (("read", AgentReadRequest), ("act", AgentActRequest), ("ask", AgentAskRequest)):
            schema = tools[name].parameters
            with self.subTest(primitive=name):
                self.assertEqual(
                    sorted(field for field, spec in model.model_fields.items() if spec.is_required()),
                    sorted(schema["required"]),
                )
                self.assertEqual(sorted(model.model_fields), sorted(schema["properties"]))

    def test_every_integration_becomes_a_tool_without_code_changes(self):
        tools = integration_tools([SEARCH_ITEM])
        self.assertEqual(1, len(tools))
        self.assertEqual("read", tools[0].effect)
        self.assertFalse(tools[0].external_io)

    def test_step_requires_a_known_shape(self):
        self.assertEqual("final", AgentStep(kind="final", arguments={"answer": "готово"}).kind)
        self.assertEqual("ask", AgentStep(kind="ask", arguments={"question": "какой?"}).kind)
        self.assertEqual("read", AgentStep(kind="tool", tool="read", arguments={"ref": "result:1"}).tool)
        with self.assertRaises(ValidationError):
            AgentStep(kind="tool", tool="read", arguments={})
        with self.assertRaises(ValidationError):
            AgentStep(kind="tool", arguments={"ref": "result:1"})
        with self.assertRaises(ValidationError):
            AgentStep(kind="final", arguments={"answer": ""})
        with self.assertRaises(ValidationError):
            AgentStep(kind="ask", arguments={"question": "x" * 400})

    def test_html_becomes_readable_text_without_scripts_or_markup(self):
        title, text = html_to_text(
            "<html><head><title>Глава 3</title><style>p{color:red}</style></head>"
            "<body><script>alert(1)</script><h1>Глава 3</h1><p>Первый абзац.</p>"
            "<p>Второй   абзац.</p></body></html>"
        )
        self.assertEqual("Глава 3", title)
        self.assertIn("Первый абзац.", text)
        self.assertIn("Второй абзац.", text)
        self.assertNotIn("alert(1)", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("<p>", text)

    def test_plain_text_is_collapsed(self):
        _, text = plain_to_text("Первая  строка\n\n\n\nВторая\tстрока")
        self.assertEqual("Первая строка\n\nВторая строка", text)

    def test_fetch_refuses_private_and_credentialed_targets(self):
        for url in (
            "https://127.0.0.1/admin",
            "https://localhost/admin",
            "https://user:secret@example.org/a",
            "http://example.org/a",
        ):
            with self.subTest(url=url), self.assertRaises(RemoteFetchError):
                asyncio.run(fetch_public_text(url))


class AgentRouterTests(unittest.TestCase):
    def _client(self, registry: StubRegistry, key: str) -> httpx.AsyncClient:
        app = FastAPI()
        app.include_router(agent_router)
        app.dependency_overrides[get_db] = lambda: None
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://web")
        client.headers.update({"X-Agent-Key": key})
        return client

    def _settings(self, key: str = "agent-secret"):
        return patch(
            "app.core.agent_router.get_settings",
            lambda: SimpleNamespace(AGENT_INTERNAL_KEY=key, AGENT_CONSUMER_ID="miku"),
        )

    def test_internal_routes_require_the_agent_key(self):
        async def call():
            with self._settings(), patch("app.core.agent_router.module_registry", StubRegistry()):
                async with self._client(StubRegistry(), "") as client:
                    return (
                        await client.get("/internal/agent/catalog"),
                        await client.post("/internal/agent/invoke", json={"integration_id": "x.v1"}),
                    )

        catalog, invoke = asyncio.run(call())
        self.assertEqual(403, catalog.status_code)
        self.assertEqual(403, invoke.status_code)

    def test_catalog_returns_primitives_and_integrations(self):
        async def call():
            with self._settings(), patch("app.core.agent_router.module_registry", StubRegistry()):
                async with self._client(StubRegistry(), "agent-secret") as client:
                    return await client.get("/internal/agent/catalog")

        response = asyncio.run(call())
        self.assertEqual(200, response.status_code)
        names = [tool["name"] for tool in response.json()["tools"]]
        self.assertEqual(
            [
                "read",
                "fetch",
                "act",
                "ask",
                "final",
                "alllib_library_viewer_v1",
                "search_global_v1",
                "vault_capture_v1",
            ],
            names,
        )

    def test_invoke_returns_the_integration_result(self):
        registry = StubRegistry()

        async def call():
            with self._settings(), patch("app.core.agent_router.module_registry", registry):
                async with self._client(registry, "agent-secret") as client:
                    return await client.post(
                        "/internal/agent/invoke",
                        json={"integration_id": "search.global.v1", "parameters": {"query": "x"}},
                    )

        response = asyncio.run(call())
        self.assertEqual(200, response.status_code)
        self.assertEqual({"integration_id": "search.global.v1", "ok": True}, response.json()["result"])

    def test_rejected_request_is_a_422_not_a_crash(self):
        registry = StubRegistry(fail=True)

        async def call():
            with self._settings(), patch("app.core.agent_router.module_registry", registry):
                async with self._client(registry, "agent-secret") as client:
                    return await client.post(
                        "/internal/agent/invoke",
                        json={"integration_id": "search.global.v1", "parameters": {"query": "x"}},
                    )

        self.assertEqual(422, asyncio.run(call()).status_code)

    def test_resource_reads_text_and_truncates(self):
        registry = StubRegistry(resource=IntegrationResource(kind="text", title="Глава 3", text="а" * 9_000))

        async def call(max_chars: int):
            with self._settings(), patch("app.core.agent_router.module_registry", registry):
                async with self._client(registry, "agent-secret") as client:
                    return await client.post(
                        "/internal/agent/resource",
                        json={
                            "module_id": "alllib",
                            "item_id": "42",
                            "ref": "result:1",
                            "max_chars": max_chars,
                        },
                    )

        response = asyncio.run(call(8_000))
        payload = response.json()
        self.assertEqual(200, response.status_code)
        self.assertEqual("result:1", payload["ref"])
        self.assertEqual("Глава 3", payload["title"])
        self.assertEqual(8_000, len(payload["text"]))
        self.assertTrue(payload["truncated"])

    def test_module_without_a_resolver_is_a_404(self):
        async def call():
            with self._settings(), patch("app.core.agent_router.module_registry", StubRegistry()):
                async with self._client(StubRegistry(), "agent-secret") as client:
                    return await client.post(
                        "/internal/agent/resource",
                        json={"module_id": "youtube", "item_id": "1", "ref": "result:1"},
                    )

        self.assertEqual(404, asyncio.run(call()).status_code)


if __name__ == "__main__":
    unittest.main()
