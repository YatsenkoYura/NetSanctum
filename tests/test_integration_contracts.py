import asyncio
import unittest
from pathlib import Path

from pydantic import BaseModel

from app.core.module_types import (
    IntegrationContext,
    IntegrationSpec,
    IntegrationUnavailableError,
    ModuleSpec,
    UiActionSpec,
)
from app.core.modules import ModuleRecord, ModuleRegistry, ModuleStatus


class ExampleRequest(BaseModel):
    value: str


class ExampleResult(BaseModel):
    echoed: str


async def example_handler(request: ExampleRequest, context: IntegrationContext) -> ExampleResult:
    return ExampleResult(echoed=request.value)


async def example_entity_resolver(session, entity_type: str, entity_id: str) -> dict:
    return {"type": entity_type, "id": entity_id}


def make_registry(status: ModuleStatus = ModuleStatus.ACTIVE) -> ModuleRegistry:
    registry = ModuleRegistry()
    spec = ModuleSpec(
        id="example",
        version="1.0.0",
        title_en="Example",
        title_ru="Example",
        entity_types=("video",),
        entity_resolver="test_integration_contracts:example_entity_resolver",
        integrations=(
            IntegrationSpec(
                id="example.echo.v1",
                handler="test_integration_contracts:example_handler",
                request_model="test_integration_contracts:ExampleRequest",
                result_model="test_integration_contracts:ExampleResult",
            ),
        ),
        uses_integrations=("example.echo.v1",),
        ui_actions=(
            UiActionSpec(
                id="example.echo",
                slot="entity.actions",
                integration="example.echo.v1",
                label_en="Echo",
                label_ru="Повторить",
                entity_types=("video",),
            ),
        ),
    )
    registry._records[spec.id] = ModuleRecord(
        package="example",
        spec=spec,
        status=status,
    )
    return registry


class IntegrationContractTests(unittest.TestCase):
    def test_registry_validates_and_invokes_typed_integration(self):
        registry = make_registry()
        context = IntegrationContext(session=None, user=None, registry=registry)

        result = asyncio.run(registry.invoke_integration("example.echo.v1", {"value": "ok"}, context))

        self.assertEqual({"echoed": "ok"}, result)

    def test_disabled_provider_is_unavailable_and_has_no_ui_action(self):
        registry = make_registry(ModuleStatus.DISABLED)
        context = IntegrationContext(session=None, user=None, registry=registry)

        self.assertFalse(registry.has_integration("example.echo.v1"))
        self.assertEqual(
            [],
            registry.ui_actions(
                "entity.actions",
                {"entity_type": "video", "entity_id": "123"},
            ),
        )
        with self.assertRaises(IntegrationUnavailableError):
            asyncio.run(registry.invoke_integration("example.echo.v1", {"value": "ok"}, context))

    def test_ui_action_is_structured_and_localized(self):
        registry = make_registry()

        actions = registry.ui_actions(
            "entity.actions",
            {"entity_type": "video", "entity_id": "123"},
            lang="ru",
        )

        self.assertEqual(1, len(actions))
        self.assertEqual("Повторить", actions[0]["label"])
        self.assertEqual("/api/integrations/example.echo.v1", actions[0]["href"])
        self.assertEqual(
            {"entity_type": "video", "entity_id": "123"},
            actions[0]["payload"],
        )

    def test_catalog_exposes_typed_contract_and_declared_consumers(self):
        registry = make_registry()

        catalog = registry.integration_catalog()

        self.assertEqual("example.echo.v1", catalog[0]["id"])
        self.assertEqual(["example"], catalog[0]["used_by"])
        self.assertEqual("object", catalog[0]["request_schema"]["type"])

    def test_music_action_follows_module_activation(self):
        enabled = ModuleRegistry.discover({"music", "video_archiver"})
        disabled = ModuleRegistry.discover({"video_archiver"})
        context = {"entity_type": "video", "entity_id": "123"}

        self.assertEqual(1, len(enabled.ui_actions("entity.actions", context)))
        self.assertEqual([], disabled.ui_actions("entity.actions", context))

    def test_video_ui_contains_only_framework_extension_point(self):
        template = Path("app/modules/video_archiver/templates/video_dashboard.html").read_text()

        self.assertIn("<netsanctum-actions", template)
        self.assertNotIn("Send to Music", template)
        self.assertNotIn("music.import_audio", template)


if __name__ == "__main__":
    unittest.main()
