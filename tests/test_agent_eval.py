"""Acceptance gate for the agent engine, driven by tests/eval_agent.py scenarios."""

import unittest

from tests.eval_agent import report


class AgentEvalTests(unittest.TestCase):
    """The eval scenarios are the regression gate for loop, primitives and cascades."""

    @classmethod
    def setUpClass(cls):
        cls.summary = report()

    def test_supported_scenarios_pass(self):
        self.assertEqual(
            [],
            self.summary["regressions"],
            "engine regressed on: " + ", ".join(self.summary["regressions"]),
        )

    def test_gap_scenarios_are_promoted_explicitly(self):
        closed = [scenario.name for scenario, result in self.summary["gaps"] if result["ok"]]
        self.assertEqual(
            [],
            closed,
            "capability now works but is still declared as a gap: " + ", ".join(closed),
        )

    def test_every_scenario_declares_an_expectation(self):
        for scenario in self.summary["scenarios"]:
            self.assertTrue(scenario.message, scenario.name)
            self.assertIsNotNone(scenario.expect, scenario.name)
            if not scenario.supported:
                self.assertTrue(scenario.gap, f"{scenario.name} must explain its gap")


if __name__ == "__main__":
    unittest.main()
