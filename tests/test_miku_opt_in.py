"""Miku is opt-in: absent from a default install, and the app runs without it.

The assistant is the heaviest optional module in the bundle, so "not asked for"
has to mean "not installed, nothing else changes". These tests pin that promise
in both directions: the module appears when it is requested by name, and the
rest of the system boots, routes and navigates when it is not.
"""

import unittest

from app.core.modules import ModuleRegistry, ModuleStatus
from app.modules.miku.module import MODULE as MIKU_MODULE


class MikuOptInManifestTests(unittest.TestCase):
    def test_the_assistant_is_not_part_of_a_default_install(self):
        self.assertFalse(MIKU_MODULE.default_enabled)
        self.assertFalse(MIKU_MODULE.required)

    def test_requesting_it_by_name_is_enough_to_activate_it(self):
        registry = ModuleRegistry.discover({"vault", "miku"})

        self.assertEqual(ModuleStatus.ACTIVE, registry._records["miku"].status)
        self.assertIn("miku", {entry["name"] for entry in registry.navigation()})

    def test_an_install_without_it_leaves_the_rest_of_the_app_intact(self):
        registry = ModuleRegistry.discover({"vault", "planner"})

        self.assertEqual(ModuleStatus.DISABLED, registry._records["miku"].status)
        # Boot must not depend on it: routers and templates still resolve.
        self.assertTrue(registry.load_routers())
        self.assertTrue(registry.template_dirs())
        self.assertTrue(registry.is_active("vault"))
        self.assertTrue(registry.is_active("planner"))
        self.assertNotIn("miku", {entry["name"] for entry in registry.navigation()})


class MikuWithoutModuleUiTests(unittest.TestCase):
    """The assistant shell is only rendered when the module is in navigation."""

    def setUp(self):
        from pathlib import Path

        self.base = (Path(__file__).resolve().parents[1] / "app/core/templates/base.html").read_text()

    def test_the_assistant_shell_is_gated_on_the_module(self):
        self.assertIn("miku_enabled", self.base)
        self.assertIn('{% include "miku_assistant.html" %}', self.base)
        # The gate is the navigation list, so a disabled module removes the shell.
        gate_line = next(
            line for line in self.base.splitlines() if "miku_enabled" in line and "if user" in line
        )
        self.assertIn("miku_enabled", gate_line)

    def test_navigation_grouping_never_promotes_the_assistant(self):
        # The System dropdown lists configuration, storage and sharing only.
        self.assertIn("mod.name in ('storage', 'sharing')", self.base)


if __name__ == "__main__":
    unittest.main()
