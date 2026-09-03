import unittest

from app.contracts.library_viewer_v1 import CONTRACT_ID
from app.core.module_types import IntegrationResource
from app.core.modules import ModuleRegistry
from app.modules.alllib.integrations import _PlainTextParser


class ComputerCraftIntegrationTests(unittest.TestCase):
    def test_product_modules_register_consumed_library_integrations(self):
        registry = ModuleRegistry.discover({"alllib", "computercraft", "music", "video_archiver"})
        providers = registry.integration_providers(CONTRACT_ID)
        self.assertEqual({"alllib", "music", "video_archiver"}, {record.id for record, _ in providers})
        for item in registry.integration_catalog():
            if item["contract"] == CONTRACT_ID:
                self.assertIn("computercraft", item["used_by"])

    def test_plain_text_resource_removes_markup_and_hidden_content(self):
        parser = _PlainTextParser()
        parser.feed("<h1>Title</h1><p>Hello <b>world</b>.</p><script>hidden</script>")
        self.assertEqual("Title\n\nHello world.", parser.text())

    def test_computercraft_client_is_served_by_its_module(self):
        from app.modules.computercraft.router import router

        route = next(route for route in router.routes if route.path == "/computercraft/client.lua")
        self.assertEqual("get_client", route.name)

    def test_legacy_video_client_routes_remain_available_as_adapters(self):
        from app.modules.video_archiver.router import router

        route_names = {route.name for route in router.routes}
        self.assertIn("legacy_computercraft_frame", route_names)
        self.assertIn("stream_audio", route_names)

    def test_resource_contract_requires_owned_media_path_shape(self):
        with self.assertRaisesRegex(ValueError, "storage path"):
            IntegrationResource(kind="video", title="Broken")


if __name__ == "__main__":
    unittest.main()
