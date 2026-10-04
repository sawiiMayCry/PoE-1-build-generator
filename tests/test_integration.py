"""Integration regressions: degraded Mapping mana, mana-repair trigger, resolver wiring."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import real_generator
from build_progression import _waive_mana


class ManaRepairTests(unittest.TestCase):
    def test_only_repairable_mana_omissions_trigger_repair(self):
        summary = {"omissions": [
            {"package": "herald", "reason": "resource shortfall: mana 180, repairable", "repairable": True},
            {"package": "clarity", "reason": "resource shortfall: mana 450", "repairable": True},
            {"package": "hatred", "reason": "no measured gain", "repairable": False},
            {"package": "grace", "reason": "resource shortfall: mana 900", "repairable": False}]}
        self.assertEqual(real_generator.mana_shortfall(summary), 450.0)
        self.assertEqual(real_generator.mana_shortfall({"omissions": []}), 0.0)
        self.assertEqual(real_generator.mana_shortfall(None), 0.0)

    def test_replan_is_attempted_only_with_a_shortfall(self):
        calls = []

        def fake_once(spec, *args, **kwargs):
            calls.append("plan")
            spec["skillGroups"] = [{"id": "main"}]
            spec["skillPlanSummary"] = {"socketedGems": 12, "omissions": []}
            return {"passives": {}}

        original = real_generator._plan_supporting_skills_once
        real_generator._plan_supporting_skills_once = fake_once
        try:
            spec = {}
            real_generator.plan_supporting_skills(spec, None, None, None, None, [], {}, [], {}, None, context={})
        finally:
            real_generator._plan_supporting_skills_once = original
        self.assertEqual(calls, ["plan"])


class DegradedMappingTests(unittest.TestCase):
    def test_mana_failure_becomes_a_visible_warning_only_when_degraded(self):
        checks = [{"name": "Stage mana", "passed": False, "reason": "x"},
                  {"name": "Stage sockets", "passed": False, "reason": "y"}]
        waived = _waive_mana(checks, "WARNING: short of mana")
        self.assertTrue(waived[0]["passed"] and waived[0]["warning"])
        self.assertIn("WARNING", waived[0]["reason"])
        self.assertFalse(waived[1]["passed"])
        self.assertIs(_waive_mana(checks, None), checks)


if __name__ == "__main__":
    unittest.main()
