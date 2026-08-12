import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from app.core.control_center import cancel_tracked_task, tasks_blocking_module_change
from app.core.module_config import (
    load_enabled_module_ids,
    parse_module_ids,
    reset_enabled_module_ids,
    save_enabled_module_ids,
)
from app.core.modules import ModuleRegistry
from app.core.observability import redact_log_message
from app.core.task_dispatch import dispatch_tracked_async, dispatch_tracked_sync


@dataclass
class FakeModuleSettings:
    ENABLED_MODULES: str
    ENABLED_MODULES_FILE: str


class ModuleConfigurationTests(unittest.TestCase):
    def test_file_backed_desired_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = FakeModuleSettings(
                ENABLED_MODULES="",
                ENABLED_MODULES_FILE=str(Path(directory) / "enabled-modules.json"),
            )

            save_enabled_module_ids({"music", "vault"}, settings)
            enabled, source = load_enabled_module_ids(settings)

            self.assertEqual({"music", "vault"}, enabled)
            self.assertEqual("file", source)

            reset_enabled_module_ids(settings)
            self.assertEqual((None, "default"), load_enabled_module_ids(settings))

    def test_environment_module_selection_cannot_be_overwritten(self):
        settings = FakeModuleSettings(
            ENABLED_MODULES="music",
            ENABLED_MODULES_FILE="/unused",
        )

        self.assertEqual(({"music"}, "environment"), load_enabled_module_ids(settings))
        with self.assertRaises(RuntimeError):
            save_enabled_module_ids({"vault"}, settings)

    def test_module_id_parser_handles_default_and_allowlist(self):
        self.assertIsNone(parse_module_ids(""))
        self.assertIsNone(parse_module_ids("all"))
        self.assertEqual({"music", "vault"}, parse_module_ids("music, vault"))


class ObservabilitySecurityTests(unittest.TestCase):
    def test_log_redaction_removes_tokens_and_passwords(self):
        message = (
            "Authorization: Bearer secret-token password=hunter2 "
            "access_token=abc /stream?token=query-secret&quality=720 "
            "eyJabcdefghijk.abcdefghijkl.abcdefghijkl"
        )

        redacted = redact_log_message(message)

        self.assertNotIn("secret-token", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("access_token=abc", redacted)
        self.assertNotIn("query-secret", redacted)
        self.assertIn("quality=720", redacted)
        self.assertNotIn("eyJabcdefghijk", redacted)

    def test_invalid_task_id_is_rejected_before_celery_control(self):
        with self.assertRaises(ValueError):
            asyncio.run(cancel_tracked_task("../../invalid"))

    def test_observability_uses_non_blocking_queue_handler(self):
        import logging
        import logging.handlers

        from app.core.observability import configure_observability

        logger = logging.getLogger("control-center-test")
        configure_observability("test", logger)

        self.assertTrue(
            any(isinstance(handler, logging.handlers.QueueHandler) for handler in logger.handlers)
        )


class ModuleTaskSafetyTests(unittest.TestCase):
    def test_active_tasks_block_disabling_their_module(self):
        import app.core.control_center as control_center

        original_registry = control_center.module_registry
        control_center.module_registry = ModuleRegistry.discover({"music", "vault"})
        try:
            blocked = tasks_blocking_module_change(
                {"vault"},
                [{"module": "music", "task_id": "active"}],
            )
        finally:
            control_center.module_registry = original_registry

        self.assertEqual({"music"}, blocked)


class TrackedDispatchTests(unittest.TestCase):
    def test_sync_tracker_exists_before_broker_dispatch(self):
        state = {}

        class FakeRedis:
            def setex(self, key, ttl, payload):
                state[key] = payload

            def delete(self, key):
                state.pop(key, None)

        class FakeTask:
            def apply_async(self, *, args, kwargs, task_id):
                self.tracker_present = f"music_dl:{task_id}" in state
                return self

        task = FakeTask()
        dispatch_tracked_sync(task, FakeRedis(), "music_dl", {"status": "Queued"})

        self.assertTrue(task.tracker_present)

    def test_async_dispatch_rolls_back_tracker_on_broker_error(self):
        state = {}

        class FakeRedis:
            async def setex(self, key, ttl, payload):
                state[key] = payload

            async def delete(self, key):
                state.pop(key, None)

        class FailingTask:
            def apply_async(self, *, args, kwargs, task_id):
                raise RuntimeError("broker unavailable")

        with self.assertRaises(RuntimeError):
            asyncio.run(
                dispatch_tracked_async(
                    FailingTask(),
                    FakeRedis(),
                    "video_dl",
                    {"status": "Queued"},
                )
            )

        self.assertEqual({}, state)


class ControlCenterContractTests(unittest.TestCase):
    def test_control_endpoints_require_current_user(self):
        from app.core.security import get_current_user
        from app.modules.settings.router import router

        control_routes = [
            route
            for route in router.routes
            if route.path.startswith("/settings/ui/") or route.path == "/settings/control/snapshot"
        ]
        self.assertTrue(control_routes)
        for route in control_routes:
            dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
            self.assertIn(get_current_user, dependency_calls, route.path)

    def test_control_center_templates_compile(self):
        from app.core.templates import templates

        for name in (
            "control_center.html",
            "control_overview.html",
            "control_modules.html",
            "control_tasks.html",
            "control_logs.html",
            "control_settings.html",
        ):
            with self.subTest(template=name):
                self.assertIsNotNone(templates.env.get_template(name))

    def test_module_cancel_endpoints_do_not_purge_shared_queue(self):
        root = Path("app/modules")
        for path in (
            root / "music/router.py",
            root / "video_archiver/router.py",
            root / "alllib/router.py",
        ):
            self.assertNotIn("control.purge(", path.read_text(), str(path))

    def test_celery_preserves_root_observability_handler(self):
        from app.core.scheduler import celery_app

        self.assertFalse(celery_app.conf.worker_hijack_root_logger)


if __name__ == "__main__":
    unittest.main()
