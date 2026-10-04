"""Regression tests for unique-selection safety found while widening the unique search."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import real_generator
import unique_policy
from build_generator import validate_calculation


def calculation(**stats):
    base = {"Life": 5000, "EnergyShield": 500, "FullDPS": 100000, "FireResist": 75, "ColdResist": 75,
            "LightningResist": 75, "Str": 100, "Dex": 100, "Int": 100}
    return {"calculated": True, "stats": {**base, **stats},
            "passives": {"used": 100, "maximum": 100, "ascendancy": 8, "secondaryAscendancy": 0}}


class LifeReservation(unittest.TestCase):
    """Malachai's Simula's Blood Magic reserved 92% of life in a measured Arc build; that is not a unique upgrade."""

    def check(self, **stats):
        return next(entry for entry in validate_calculation(calculation(**stats)) if entry["name"] == "Life reservation")

    def test_normal_builds_pass(self):
        self.assertTrue(self.check()["passed"])
        self.assertTrue(self.check(LifeReserved=1000)["passed"])

    def test_blood_magic_style_reservation_fails(self):
        self.assertFalse(self.check(LifeReserved=4479)["passed"])

    def test_chaos_inoculation_pool_is_not_affected(self):
        self.assertTrue(self.check(Life=1, LifeReserved=0)["passed"])


class UnobtainableUniques(unittest.TestCase):
    def test_no_longer_obtainable_uniques_are_found_in_the_pob_data(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "Data" / "Uniques").mkdir(parents=True)
            (home / "Data" / "Uniques" / "jewel.lua").write_text(
                "return {\n[[\nClear Mind\nCobalt Jewel\nSource: No longer obtainable\nLimited to: 1\n]],[[\n"
                "Pure Talent\nViridian Jewel\nLimited to: 1\n]],\n}\n", encoding="utf-8")
            names = real_generator.unobtainable_unique_names({"pobHome": home})
        self.assertEqual(names, frozenset({"Clear Mind"}))

    def test_jewel_shortlist_skips_unobtainable_and_rotating_buff_uniques(self):
        spec = {"budgetChaos": None, "archetype": "spell", "damageType": "fire", "focus": "balanced"}
        unique_policy.apply_default_budget(spec, {"divineChaos": 300})
        definitions = [
            {"name": "Clear Mind", "type": "Jewel", "raw": "Clear Mind\nspell damage"},
            {"name": "Rotating", "type": "Jewel", "raw": "Rotating\nEvery 5 seconds, gain one of the following"},
            {"name": "Fine Jewel", "type": "Jewel", "raw": "Fine Jewel\n20% increased spell damage"},
        ]
        chosen = real_generator.unique_jewel_shortlist(definitions, False, set(), spec, None,
                                                       frozenset({"Clear Mind"}))
        self.assertEqual([entry["name"] for entry in chosen], ["Fine Jewel"])

    def test_requested_jewel_is_never_filtered(self):
        definitions = [{"name": "Clear Mind", "type": "Jewel", "raw": "Clear Mind"}]
        chosen = real_generator.unique_jewel_shortlist(definitions, False, {"Clear Mind"}, {}, None,
                                                       frozenset({"Clear Mind"}))
        self.assertEqual(len(chosen), 1)


class MinimumGain(unittest.TestCase):
    def test_noise_level_gains_do_not_buy_uniques(self):
        spec = {"budgetChaos": None}
        unique_policy.apply_default_budget(spec, {"divineChaos": 300})
        self.assertFalse(unique_policy.worth_price(spec, 0.004, [30.0]))     # +0.4% is noise, not a reason to spend
        self.assertTrue(unique_policy.worth_price(spec, unique_policy.MIN_UNIQUE_GAIN, [30.0]))

    def test_unquoted_uniques_need_a_clearly_higher_gain(self):
        spec = {"budgetChaos": None}
        unique_policy.apply_default_budget(spec, {"divineChaos": 300})
        self.assertFalse(unique_policy.worth_price(spec, 0.03, [None]))
        self.assertTrue(unique_policy.worth_price(spec, unique_policy.UNPRICED_MIN_GAIN, [None]))
        self.assertEqual(unique_policy.min_gain(12.0), unique_policy.MIN_UNIQUE_GAIN)
        self.assertEqual(unique_policy.min_gain(None), unique_policy.UNPRICED_MIN_GAIN)
        # a pair is judged by its least certain member
        self.assertFalse(unique_policy.worth_price(spec, 0.03, [12.0, None]))


class AttributeRepairInPlanner(unittest.TestCase):
    # Flame Dash is the movement package's active gem here and needs more Dex than the build has.
    """Utility packages needing Dex must be able to get it from the rare gear (a unique had cost the Dex)."""

    def setUp(self):
        from test_fixes import BASES, gem
        phase = gem("Flame Dash", ["spell", "movement", "fire"])
        for row in phase["levels"]:
            row["dex"] = 60
        from generation_data import GameData, RareItem
        self.bases = BASES
        self.data = GameData({"gems": [gem("Arc", ["spell", "lightning"]), phase], "bases": BASES, "mods": []})
        self.items = [RareItem("Body Armour", "Vaal Regalia", BASES["Vaal Regalia"]),
                      RareItem("Weapon 1", "Prophecy Wand", BASES["Prophecy Wand"])]
        from skill_packages import capacity_from_equipment
        self.capacity = capacity_from_equipment(self.items, {}, self.data)
        self.spec = {"skill": "Arc", "level": 80, "focus": "damage", "damageType": "lightning",
                     "archetype": "spell", "ascendancy": "Elementalist"}

    def plan(self, repair, ok=True):
        from skill_planner import plan_skill_loadout

        def evaluate(groups):
            return {"stats": {"FullDPS": 1000.0, "Life": 4000, "LifeUnreserved": 4000, "Mana": 900,
                              "ManaUnreserved": 900, "ManaCost": 5, "Dex": 24},
                    "ok": ok or len(groups) == 1, "reasons": [] if ok or len(groups) == 1 else ["x"]}
        return plan_skill_loadout(self.spec, self.data, [], evaluate, capacity=self.capacity, items=self.items,
                                  attribute_repair=repair)

    def movement_ids(self, plan):
        return [group["id"] for group in plan["groups"] if group["role"] == "movement"]

    def test_spare_socket_fillers_never_trigger_a_gear_repair(self):
        from test_fixes import gem
        phase = gem("Phase Run", ["spell", "movement"])
        for row in phase["levels"]:
            row["dex"] = 60
        self.data = type(self.data)({"gems": [*self.data_gems(), phase], "bases": self.bases, "mods": []})
        calls = {"repair": 0}

        def repair(groups):
            calls["repair"] += 1
            return {"stats": {"Dex": 80}, "rollback": lambda: None}
        plan = self.plan(repair)
        self.assertEqual(self.movement_ids(plan), [])
        self.assertEqual(calls["repair"], 0)

    def data_gems(self):
        from test_fixes import gem
        return [gem("Arc", ["spell", "lightning"])]

    def test_without_repair_the_dex_gem_is_omitted(self):
        plan = self.plan(None)
        self.assertEqual(self.movement_ids(plan), [])
        self.assertTrue(any("attribute requirements" in row["reason"] for row in plan["omissions"]))

    def test_repair_unlocks_the_package(self):
        calls = {"rollback": 0}
        plan = self.plan(lambda groups: {"stats": {"Dex": 80, "Str": 100, "Int": 300},
                                         "rollback": lambda: calls.__setitem__("rollback", calls["rollback"] + 1)})
        self.assertEqual(len(self.movement_ids(plan)), 1)
        self.assertEqual(calls["rollback"], 0)

    def test_repair_that_finds_nothing_leaves_the_omission(self):
        plan = self.plan(lambda groups: None)
        self.assertEqual(self.movement_ids(plan), [])

    def test_gear_change_is_rolled_back_when_the_package_is_still_dropped(self):
        calls = {"rollback": 0}
        plan = self.plan(lambda groups: {"stats": {"Dex": 80, "Str": 100, "Int": 300},
                                         "rollback": lambda: calls.__setitem__("rollback", calls["rollback"] + 1)},
                         ok=False)
        self.assertEqual(self.movement_ids(plan), [])
        self.assertGreaterEqual(calls["rollback"], 1)


class OptionalSupportDropped(unittest.TestCase):
    """The movement package must survive an optional support (Second Wind) the attributes cannot carry."""

    def test_flame_dash_alone_when_second_wind_does_not_fit(self):
        from test_fixes import BASES, gem
        from generation_data import GameData, RareItem
        from skill_packages import capacity_from_equipment
        from skill_planner import plan_skill_loadout
        flame = gem("Flame Dash", ["spell", "movement", "fire"])
        second = gem("Second Wind", ["support"], True)
        for row in second["levels"]:
            row["dex"] = 60
        data = GameData({"gems": [gem("Fireball", ["spell", "fire"]), flame, second], "bases": BASES, "mods": []})
        items = [RareItem("Body Armour", "Vaal Regalia", BASES["Vaal Regalia"]),
                 RareItem("Weapon 1", "Prophecy Wand", BASES["Prophecy Wand"])]
        capacity = capacity_from_equipment(items, {}, data)
        spec = {"skill": "Fireball", "level": 80, "focus": "damage", "damageType": "fire", "archetype": "spell",
                "ascendancy": "Elementalist"}

        def evaluate(groups):
            return {"stats": {"FullDPS": 1000.0, "Life": 4000, "LifeUnreserved": 4000, "Mana": 900,
                              "ManaUnreserved": 900, "ManaCost": 5, "Dex": 24}, "ok": True, "reasons": []}
        plan = plan_skill_loadout(spec, data, [], evaluate, capacity=capacity, items=items)
        movement = [group for group in plan["groups"] if group["role"] == "movement"]
        self.assertEqual(len(movement), 1)
        self.assertEqual([entry["name"] for entry in movement[0]["gems"]], ["Flame Dash"])
        self.assertIn("Second Wind", [entry.get("support") for entry in plan["droppedSupports"]])


if __name__ == "__main__":
    unittest.main()
