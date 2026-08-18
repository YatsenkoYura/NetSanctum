import ast
import asyncio
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.core.module_types import ShareAsset, ShareRoute, ShareSpec
from app.core.modules import (
    MODULE_API_VERSION,
    ModuleRecord,
    ModuleRegistry,
    ModuleSpec,
    ModuleStatus,
    _installed_module_ids,
)

PUBLIC_MODULES = {
    "alllib",
    "auth",
    "music",
    "settings",
    "sharing",
    "storage",
    "vault",
    "video_archiver",
}

SHUTDOWN_EVENTS: list[str] = []


async def failing_startup() -> None:
    raise RuntimeError("partial startup")


async def record_shutdown() -> None:
    SHUTDOWN_EVENTS.append("shutdown")


class ModuleManifestTests(unittest.TestCase):
    def test_installed_public_modules_have_valid_manifests(self):
        registry = ModuleRegistry.discover()
        records = {record.id: record for record in registry.records}

        self.assertLessEqual(PUBLIC_MODULES, set(records))
        for module_id in PUBLIC_MODULES:
            record = records[module_id]
            self.assertIsNotNone(record.spec)
            assert record.spec is not None
            self.assertEqual(ModuleStatus.ACTIVE, record.status, record.error)
            self.assertEqual(MODULE_API_VERSION, record.spec.api_version)

    def test_allowlist_disables_optional_modules_but_not_required_modules(self):
        registry = ModuleRegistry.discover({"music"})

        self.assertTrue(registry.is_active("auth"))
        self.assertTrue(registry.is_active("settings"))
        self.assertTrue(registry.is_active("sharing"))
        self.assertTrue(registry.is_active("music"))
        self.assertFalse(registry.is_active("vault"))
        self.assertTrue(registry.is_installed("vault"))
        self.assertEqual(["app.modules.music.tasks"], registry.task_modules())
        self.assertIn("video_dl:*", registry.progress_key_patterns())
        self.assertIsNotNone(registry.package_resolver("song_1"))
        self.assertIsNone(registry.package_resolver("vault_all"))
        self.assertIsNotNone(registry.module_cleanup_hook("vault"))
        self.assertIsNotNone(registry.file_cleanup_hook("video_archiver"))
        self.assertEqual("alllib", registry.storage_owner("ranobelib"))
        self.assertIsNone(registry.storage_owner("storage"))
        self.assertIsNone(asyncio.run(registry.resolve_entity("video", "id", None)))

    def test_installed_module_set_controls_runtime_availability(self):
        registry = ModuleRegistry.discover(installed_modules={"music"})

        self.assertTrue(registry.is_active("music"))
        self.assertTrue(registry.is_active("auth"))
        self.assertFalse(registry.is_installed("vault"))
        vault = next(record for record in registry.records if record.id == "vault")
        self.assertEqual(ModuleStatus.UNAVAILABLE, vault.status)

    def test_required_installed_marker_fails_closed_when_missing(self):
        settings = SimpleNamespace(
            INSTALLED_MODULES="",
            INSTALLED_MODULES_FILE="/definitely/missing/installed-modules",
            REQUIRE_INSTALLED_MODULES_MARKER=True,
        )
        with (
            patch("app.core.modules.get_settings", return_value=settings),
            self.assertRaises(RuntimeError),
        ):
            _installed_module_ids()

    def test_explicitly_enabling_uninstalled_module_is_rejected(self):
        with self.assertRaises(RuntimeError):
            ModuleRegistry.discover(
                enabled_modules={"vault"},
                installed_modules={"music"},
                strict_enabled=True,
            )

    def test_committed_module_build_catalog_matches_manifests(self):
        subprocess.run(
            [sys.executable, "scripts/module_build.py", "check"],
            check=True,
        )

    def test_core_build_profile_contains_only_required_modules(self):
        result = subprocess.run(
            [sys.executable, "scripts/module_build.py", "modules", "--modules", "core"],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual("auth,settings,sharing", result.stdout.strip())

    def test_external_module_requires_matching_optional_extra(self):
        from scripts.module_build import resolve_external_modules

        with self.assertRaises(SystemExit):
            resolve_external_modules(Path.cwd(), "missing_external")

    def test_external_entry_point_registers_installed_module(self):
        external_spec = ModuleSpec(
            id="external_example",
            version="1.0.0",
            title_en="External",
            title_ru="External",
        )
        external_entry_point = SimpleNamespace(
            name="external_example",
            module="external_example.module",
            load=lambda: external_spec,
        )

        with patch("app.core.modules.entry_points", return_value=[external_entry_point]):
            registry = ModuleRegistry.discover(installed_modules={"external_example"})

        self.assertTrue(registry.is_active("external_example"))
        self.assertTrue(registry.is_installed("external_example"))

    def test_external_entry_point_not_in_marker_is_unavailable(self):
        external_spec = ModuleSpec(
            id="external_unavailable",
            version="1.0.0",
            title_en="External",
            title_ru="External",
        )
        load = Mock(return_value=external_spec)
        external_entry_point = SimpleNamespace(
            name="external_unavailable",
            module="external_unavailable.module",
            load=load,
        )

        with patch("app.core.modules.entry_points", return_value=[external_entry_point]):
            registry = ModuleRegistry.discover(installed_modules=set())

        self.assertFalse(registry.is_installed("external_unavailable"))
        load.assert_not_called()

    def test_external_entry_point_is_not_loaded_without_marker(self):
        load = Mock()
        external_entry_point = SimpleNamespace(
            name="external_without_marker",
            module="external_without_marker.module",
            load=load,
        )

        with patch("app.core.modules.entry_points", return_value=[external_entry_point]):
            registry = ModuleRegistry.discover()

        self.assertFalse(registry.is_installed("external_without_marker"))
        load.assert_not_called()

    def test_unbundled_module_requires_explicit_installed_marker(self):
        registry = ModuleRegistry()
        spec = ModuleSpec(
            id="local_extension",
            version="1.0.0",
            title_en="Local extension",
            title_ru="Local extension",
            bundled=False,
        )

        registry._register_spec("local_extension", spec)

        self.assertFalse(registry.is_installed("local_extension"))
        self.assertFalse(registry.is_active("local_extension"))

    def test_unknown_enabled_module_is_reported(self):
        registry = ModuleRegistry.discover({"does_not_exist"})

        self.assertEqual(["does_not_exist"], registry.diagnostics()["unknown_enabled_modules"])

    def test_manifest_rejects_invalid_module_id(self):
        with self.assertRaises(ValueError):
            ModuleSpec(
                id="Invalid ID",
                version="0.1.0",
                title_en="Invalid",
                title_ru="Invalid",
            )

    def test_cleanup_hooks_require_storage_namespace(self):
        with self.assertRaises(ValueError):
            ModuleSpec(
                id="invalid_cleanup",
                version="0.1.0",
                title_en="Invalid",
                title_ru="Invalid",
                file_cleanup="invalid:cleanup",
            )

    def test_share_contract_rejects_unsafe_or_duplicate_routes(self):
        with self.assertRaises(ValueError):
            ShareSpec(
                provider="example:PROVIDER",
                selector_key="item_ids",
                dashboard_template="dashboard.html",
                api_prefix="/api/example",
                routes=(
                    ShareRoute(name="items", path="items"),
                    ShareRoute(name="items_duplicate", path="items"),
                ),
            )
        with self.assertRaises(ValueError):
            ShareAsset(name="unsafe", path="../storage/{item_id}")

    def test_video_declares_complete_share_contract(self):
        registry = ModuleRegistry.discover({"video_archiver"})
        spec = registry.share_spec("video_archiver")

        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual("video_ids", spec.selector_key)
        self.assertEqual("video_dashboard.html", spec.dashboard_template)
        self.assertEqual("/api/video-archiver", spec.api_prefix)
        self.assertIn("videos/{video_id}", {route.path for route in spec.routes})
        self.assertIn("videos/{video_id}/stream", {asset.path for asset in spec.assets})

    def test_navigation_reflects_runtime_failure(self):
        registry = ModuleRegistry.discover({"music"})
        music = next(record for record in registry.records if record.id == "music")
        self.assertIn("music", {item["name"] for item in registry.navigation()})

        music.status = ModuleStatus.FAILED

        self.assertNotIn("music", {item["name"] for item in registry.navigation()})

    def test_auxiliary_capability_failure_does_not_disable_module(self):
        registry = ModuleRegistry()
        spec = ModuleSpec(
            id="broken_capability",
            version="0.1.0",
            title_en="Broken",
            title_ru="Broken",
            file_cleanup="missing.module:cleanup",
            storage_namespaces=("broken",),
        )
        record = ModuleRecord(
            package="app.modules.broken_capability",
            spec=spec,
            status=ModuleStatus.ACTIVE,
        )
        registry._records[spec.id] = record

        self.assertIsNone(registry.file_cleanup_hook("broken"))
        self.assertEqual(ModuleStatus.ACTIVE, record.status)
        assert record.component_errors is not None
        self.assertIn("file_cleanup", record.component_errors)

    def test_required_startup_failure_is_fatal_and_marked_for_shutdown(self):
        registry = ModuleRegistry()
        spec = ModuleSpec(
            id="required_failure",
            version="0.1.0",
            title_en="Required",
            title_ru="Required",
            startup="missing.module:start",
            required=True,
        )
        record = ModuleRecord(
            package="app.modules.required_failure",
            spec=spec,
            status=ModuleStatus.ACTIVE,
        )
        registry._records[spec.id] = record

        with self.assertRaises(RuntimeError):
            asyncio.run(registry.run_startup_hooks())

        self.assertTrue(record.started)
        self.assertEqual(ModuleStatus.FAILED, record.status)

    def test_partial_startup_failure_still_runs_shutdown_hook(self):
        SHUTDOWN_EVENTS.clear()
        registry = ModuleRegistry()
        spec = ModuleSpec(
            id="partial_failure",
            version="0.1.0",
            title_en="Partial",
            title_ru="Partial",
            startup="test_module_contracts:failing_startup",
            shutdown="test_module_contracts:record_shutdown",
        )
        record = ModuleRecord(
            package="app.modules.partial_failure",
            spec=spec,
            status=ModuleStatus.ACTIVE,
        )
        registry._records[spec.id] = record

        asyncio.run(registry.run_startup_hooks())
        asyncio.run(registry.run_shutdown_hooks())

        self.assertEqual(["shutdown"], SHUTDOWN_EVENTS)

    def test_component_paths_declared_by_public_modules_are_importable(self):
        registry = ModuleRegistry.discover()
        for record in registry.installed_records():
            if record.id not in PUBLIC_MODULES:
                continue
            spec = record.spec
            assert spec is not None
            for path in (
                spec.router,
                spec.startup,
                spec.shutdown,
                spec.file_cleanup,
                spec.module_cleanup,
                spec.package_resolver,
                spec.share.provider if spec.share else None,
                spec.entity_resolver,
                *(integration.handler for integration in spec.integrations),
                *(integration.request_model for integration in spec.integrations),
                *(integration.result_model for integration in spec.integrations),
            ):
                if path:
                    with self.subTest(module=record.id, component=path):
                        self.assertIsNotNone(registry._load_object(path))
            for path in (spec.models, spec.tasks, spec.i18n):
                if path:
                    with self.subTest(module=record.id, component=path):
                        __import__(path)

    def test_share_providers_follow_framework_contract(self):
        registry = ModuleRegistry.discover()
        for record in registry.active_records():
            spec = record.spec
            if not spec or not spec.share:
                continue
            provider = registry.share_provider(record.id)
            self.assertIsNotNone(provider, record.id)
            assert provider is not None
            for method_name in ("catalog", "selection", "entities", "relations", "asset"):
                with self.subTest(module=record.id, method=method_name):
                    self.assertTrue(callable(getattr(provider, method_name, None)))

            package = __import__(record.package, fromlist=["__file__"])
            assert spec.templates is not None
            template = (
                Path(package.__file__).resolve().parent / spec.templates / spec.share.dashboard_template
            )
            self.assertTrue(template.is_file(), f"Missing shared dashboard template: {template}")

    def test_public_modules_activate_without_registry_failures(self):
        registry = ModuleRegistry.discover(PUBLIC_MODULES)

        registry.template_dirs()
        registry.import_models()
        registry.load_routers()

        for record in registry.active_records():
            self.assertEqual(ModuleStatus.ACTIVE, record.status, record.error)

    def test_product_modules_do_not_import_other_product_modules(self):
        modules_root = Path("app/modules")
        product_modules = {"alllib", "music", "vault", "video_archiver"}

        for source_module in product_modules:
            for path in (modules_root / source_module).glob("*.py"):
                tree = ast.parse(path.read_text(), filename=str(path))
                imported_modules = {
                    node.module.split(".")[2]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.startswith("app.modules.")
                }
                forbidden = imported_modules - {source_module, "settings"}
                self.assertFalse(forbidden, f"{path} imports product modules: {sorted(forbidden)}")


class ModuleActivationSmokeTests(unittest.TestCase):
    def test_failed_module_runtime_guard_returns_service_unavailable(self):
        script = """
import asyncio
from fastapi import HTTPException
from app.core.modules import ModuleStatus, module_registry
from app.main import _module_guard

record = next(record for record in module_registry.records if record.id == "music")
record.status = ModuleStatus.FAILED
try:
    asyncio.run(_module_guard("music")())
except HTTPException as exc:
    print(exc.status_code)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual("503", result.stdout.strip().splitlines()[-1])

    def test_manifest_import_does_not_start_runtime_discovery(self):
        script = """
import sys
import app.modules.music.module
print("app.core.modules" in sys.modules)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual("False", result.stdout.strip())

    def test_main_app_only_mounts_allowlisted_optional_module(self):
        script = """
import json
import sys
from app.main import app
print(json.dumps({
    "paths": sorted(app.openapi()["paths"]),
    "loaded": sorted(name for name in sys.modules if name.startswith("app.modules.")),
}))
"""
        environment = os.environ.copy()
        environment["ENABLED_MODULES"] = "music"
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        smoke_result = json.loads(result.stdout.strip().splitlines()[-1])
        paths = smoke_result["paths"]
        loaded = smoke_result["loaded"]

        self.assertIn("/music/api/playlists", paths)
        self.assertIn("/auth/login", paths)
        self.assertNotIn("/api/vault/items", paths)
        self.assertNotIn("/api/video-archiver/videos", paths)
        self.assertNotIn("app.modules.vault.router", loaded)
        self.assertNotIn("app.modules.video_archiver.router", loaded)
        self.assertNotIn("app.modules.video_archiver.share", loaded)
        self.assertNotIn("app.modules.video_archiver.tasks", loaded)


if __name__ == "__main__":
    unittest.main()
