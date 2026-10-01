import unittest

from compare_builds import continuous_sustainability, per_paid_point


class CompareBuilds(unittest.TestCase):
    def test_passive_efficiency_uses_the_modeled_damage_and_paid_points(self):
        self.assertEqual(per_paid_point(238_558, 113), 238_558 / 113)
        self.assertEqual(per_paid_point(100, 0), 100)

    def test_continuous_sustain_uses_calculated_cast_rate(self):
        result = continuous_sustainability({"ManaCost": 9, "ManaRegen": 40.1, "Speed": 7.24,
                                            "LifeCost": 0})
        self.assertFalse(result["sustainable"])
        self.assertEqual(result["checks"][0]["resource"], "mana")
        self.assertAlmostEqual(result["checks"][0]["usePerSecond"], 65.16)

    def test_life_cost_support_is_checked_against_life_regeneration(self):
        result = continuous_sustainability({"ManaCost": 0, "LifeCost": 3,
                                            "LifeRegenRecovery": 25, "Speed": 5})
        self.assertTrue(result["sustainable"])
        self.assertEqual(result["checks"][0]["resource"], "life")

    def test_no_recurring_cost_is_sustainable(self):
        result = continuous_sustainability({"ManaCost": 0, "LifeCost": 0, "Speed": 5})
        self.assertTrue(result["sustainable"])
        self.assertEqual(result["checks"], [])
