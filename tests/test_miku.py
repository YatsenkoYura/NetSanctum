import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from app.core.security import get_current_user
from app.modules.miku.module import MODULE
from app.modules.miku.router import router
from app.modules.miku.schemas import MikuQuery
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
        routes = {(method, route.path) for route in router.routes for method in route.methods}
        self.assertEqual(
            {
                ("GET", "/miku/dashboard"),
                ("GET", "/api/miku/capabilities"),
                ("POST", "/api/miku/query"),
            },
            routes,
        )
        for route in router.routes:
            self.assertIn(get_current_user, {dependency.call for dependency in route.dependant.dependencies})


if __name__ == "__main__":
    unittest.main()
