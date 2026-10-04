"""Regression tests: five-divine standard unique budget, wider unique coverage, tree sanity."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import real_generator
import tree_sanity
import unique_policy
from generation_data import GameData


def spec_with(divine_chaos=327.0, **extra):
    spec = {"budgetChaos": None, "archetype": "spell", "damageType": "lightning", "focus": "balanced", **extra}
    unique_policy.apply_default_budget(spec, {"divineChaos": divine_chaos, "source": "poe.ninja economy API"})
    return spec


class StandardBudget(unittest.TestCase):
    def test_standard_budget_is_five_divine_at_the_market_rate(self):
        spec = spec_with(327.0)
        self.assertEqual(unique_policy.budget_cap(spec), 1635.0)
        self.assertEqual(spec["policyBudget"]["divineChaosRate"], 327.0)
        self.assertEqual(spec["policyBudget"]["divine"], 5.0)

    def test_snapshot_rate_converts_the_same_way(self):
        self.assertEqual(unique_policy.budget_cap(spec_with(232.6)), 1163.0)

    def test_documented_constant_only_without_a_market_rate(self):
        spec = {"budgetChaos": None}
        unique_policy.apply_default_budget(spec, {"divineChaos": None})
        self.assertEqual(unique_policy.budget_cap(spec), 5 * unique_policy.FALLBACK_DIVINE_CHAOS)
        self.assertIn("fallback", spec["policyBudget"]["rateSource"])

    def test_explicit_budget_wins(self):
        spec = spec_with(budgetChaos=40.0)
        self.assertEqual(unique_policy.budget_cap(spec), 40.0)
        self.assertTrue(unique_policy.is_stated(spec))
        self.assertEqual(unique_policy.policy_summary(spec)["mode"], "stated budget")
        # a stated budget still needs known quotes
        self.assertFalse(unique_policy.within_budget(spec, [], [None]))
        self.assertFalse(unique_policy.within_budget(spec, [30.0], [20.0]))
        self.assertTrue(unique_policy.within_budget(spec, [30.0], [10.0]))

    def test_no_price_penalty_inside_the_budget(self):
        spec = spec_with()
        # the old policy rejected a 600c item that gains 3%; inside 5 divine it is simply affordable
        self.assertTrue(unique_policy.worth_price(spec, 0.03, [600.0]))
        self.assertTrue(unique_policy.worth_price(spec, 0.03, [1500.0], [100.0]))
        self.assertFalse(unique_policy.worth_price(spec, 0.03, [3752.0]))     # beyond the budget
        self.assertFalse(unique_policy.worth_price(spec, 0.0, [5.0]))          # no gain, no purchase

    def test_budget_can_be_filled_exactly_but_never_exceeded(self):
        spec = spec_with()
        self.assertTrue(unique_policy.within_budget(spec, [1000.0, 400.0], [235.0]))
        self.assertFalse(unique_policy.within_budget(spec, [1000.0, 400.0], [235.1]))

    def test_unpriced_uniques_are_assumed_counted_and_limited(self):
        spec = spec_with()
        assumed = unique_policy.assumed_unpriced(spec)
        self.assertAlmostEqual(assumed, 163.5)
        self.assertTrue(unique_policy.within_budget(spec, [], [None]))          # not an automatic block
        self.assertFalse(unique_policy.within_budget(spec, [1500.0], [None]))   # but its assumed cost counts
        self.assertTrue(unique_policy.within_budget(spec, [None], [None]))
        self.assertFalse(unique_policy.within_budget(spec, [None, None], [None]))   # at most two unquoted

    def test_quoted_items_rank_before_unquoted_ones_of_equal_gain(self):
        spec = spec_with()
        self.assertGreater(unique_policy.gain_per_cost(0.05, 100.0, spec),
                           unique_policy.gain_per_cost(0.05, None, spec))
        self.assertGreater(unique_policy.gain_per_cost(0.05, 2.0, spec),
                           unique_policy.gain_per_cost(0.06, 400.0, spec))

    def test_mapping_gets_a_proportional_share(self):
        spec = spec_with()
        self.assertAlmostEqual(unique_policy.mapping_cap(spec), 1635.0 * unique_policy.MAPPING_BUDGET_SHARE)
        self.assertEqual(unique_policy.mapping_cap(spec_with(budgetChaos=40.0)), 40.0)

    def test_report_shows_budget_spent_remaining_and_assumptions(self):
        spec = spec_with()
        report = unique_policy.budget_report(spec, [
            {"slot": "Belt", "name": "Cheap Belt", "chaos": 5.0},
            {"slot": "Amulet", "name": "Mystery", "chaos": None},
            {"slot": "Weapon 1", "name": "Wand", "chaos": 800.0, "requested": True}])
        self.assertEqual(report["budgetChaos"], 1635.0)
        self.assertEqual(report["budgetDivine"], 5.0)
        self.assertAlmostEqual(report["spentChaos"], 968.5)
        self.assertAlmostEqual(report["remainingChaos"], 666.5)
        self.assertTrue(report["withinBudget"])
        self.assertEqual(report["unpricedAssumed"], ["Mystery"])
        self.assertTrue(next(row for row in report["uniques"] if row["name"] == "Mystery")["assumed"])
        over = unique_policy.budget_report(spec, [{"slot": "Belt", "name": "Huge", "chaos": 2000.0}])
        self.assertFalse(over["withinBudget"])

    def test_policy_text_states_divine_rate_and_assumption(self):
        text = unique_policy.policy_summary(spec_with())["text"]
        self.assertIn("5 divine", text)
        self.assertIn("1635", text)
        self.assertIn("assumed", text)

    def test_package_fits_uses_the_policy_and_displaced_slot(self):
        spec = spec_with()
        self.assertTrue(real_generator.package_fits(spec, [900.0, 700.0], [35.0]))
        self.assertFalse(real_generator.package_fits(spec, [900.0, 700.0], [36.0]))


def unique(name, text="", price=1.0, slot="Belt"):
    return (name, slot, f"Rarity: UNIQUE\n{name}\nLeather Belt\n{text}", price)


class CandidateCoverage(unittest.TestCase):
    def test_shortlist_is_wider_and_keeps_cheap_relevant_uniques(self):
        spec = spec_with()
        entries = [unique(f"Strong {index:02d}", "spell damage cast speed lightning damage maximum life resistance",
                          price=900.0) for index in range(14)]
        entries.append(unique("Cheap Gem", "spell damage", price=2.0))
        entries.append(unique("Cheap Junk", "nothing relevant", price=1.0))
        chosen = [entry[0] for entry in real_generator.shortlist_slot_uniques(entries, spec)]
        self.assertGreater(len(chosen), 4)               # was four per slot
        self.assertIn("Cheap Gem", chosen)               # cheap, relevant, beyond the top ten
        self.assertNotIn("Cheap Junk", chosen)           # relevance zero: no padding
        self.assertEqual(len([name for name in chosen if name.startswith("Strong")]),
                         real_generator.UNIQUES_PER_SLOT)

    def test_flask_shortlist_dedupes_and_is_small(self):
        spec = spec_with()
        entries = [unique(f"Flask {index}", "life", slot="Flask 1") for index in range(20)]
        entries += [unique("Flask 3", "life", slot="Flask 1")]
        chosen = real_generator.shortlist_slot_uniques(entries, spec)
        self.assertEqual(len(chosen), real_generator.FLASK_UNIQUES_TESTED)
        self.assertEqual(len({entry[0] for entry in chosen}), len(chosen))

    def test_unique_options_drop_items_above_the_standard_budget(self):
        # the single-item cap is the standard budget; priced above it can never be bought
        spec = spec_with()
        self.assertTrue(unique_policy.budget_cap(spec) < 3752.0)
        self.assertFalse(real_generator.package_fits(spec, [], [3752.0]))

    def test_mapping_packages_are_a_cheap_subset(self):
        from build_progression import mapping_unique_packages
        data = GameData({"gems": [], "bases": {"Leather Belt": {"type": "Belt", "req": {"level": 1}, "tags": {}},
                                               "Coral Ring": {"type": "Ring", "req": {"level": 1}, "tags": {}}},
                         "mods": []})
        ring = "Rarity: UNIQUE\nRing\nCoral Ring\nImplicits: 0"
        belt = "Rarity: UNIQUE\nBelt\nLeather Belt\nImplicits: 0"
        spec = spec_with(uniqueScreenGain={"Costly Belt": 0.2, "Cheap Ring": 0.05})
        options = [("Costly Belt", "Belt", belt, 1200.0), ("Cheap Ring", "Ring 1", ring, 50.0)]
        names = [[entry[0] for entry in package] for package in mapping_unique_packages(options, spec, data, 75)]
        self.assertIn(["Cheap Ring"], names)
        self.assertNotIn(["Costly Belt"], names)                       # 1200 > 25% of 1635
        self.assertNotIn(["Costly Belt", "Cheap Ring"], names)
        stated = spec_with(budgetChaos=5000.0, uniqueScreenGain={"Costly Belt": 0.2})
        self.assertIn(["Costly Belt"], [[entry[0] for entry in package]
                                        for package in mapping_unique_packages(options, stated, data, 75)])


def tiny_tree():
    def node(key, neighbours, **extra):
        return {"name": f"n{key}", "in": [], "out": [str(n) for n in neighbours], **extra}
    nodes = {
        "1": node(1, [2], classStartIndex=3, name="Witch Start"),
        "2": node(2, [3, 4]),
        "3": node(3, [5], isNotable=True, group=7),
        "4": node(4, [6]),
        "5": node(5, [], isKeystone=True, name="Chaos Inoculation"),
        "6": node(6, [7]),
        "7": node(7, [], isNotable=True, group=9),
        "8": node(8, [], name="floating"),
        "20": node(20, [21], ascendancyName="Occultist", isAscendancyStart=True),
        "21": node(21, [22], ascendancyName="Occultist", isNotable=True),
        "22": node(22, [], ascendancyName="Occultist"),
        "30": node(30, [], isMastery=True, group=7,
                   masteryEffects=[{"effect": 111}, {"effect": 112}], name="Mastery"),
    }
    return nodes


class TreeSanity(unittest.TestCase):
    def run_checks(self, allocated, masteries=None, passives=None, spec=None, level=90):
        checks, metrics = tree_sanity.structural_checks(
            tiny_tree(), spec or {"defenseModel": "hybrid"}, set(allocated), masteries or {}, {},
            passives or {"used": 7, "maximum": 7}, "Occultist", level)
        return {check["name"]: check for check in checks}, metrics

    def test_connected_tree_passes_structure(self):
        checks, metrics = self.run_checks({"1", "2", "3", "20", "21", "22"}, passives={"used": 6, "maximum": 6},
                                          level=33)
        self.assertTrue(checks["Tree: every node connected to its start"]["passed"])
        self.assertTrue(checks["Tree: ascendancy points full"]["passed"])    # 2 points at level 33
        self.assertEqual(metrics["orphans"], [])

    def test_orphan_and_unknown_nodes_fail(self):
        checks, metrics = self.run_checks({"1", "2", "3", "8", "999", "20", "21", "22"})
        self.assertFalse(checks["Tree: every node connected to its start"]["passed"])
        self.assertEqual(metrics["orphans"], ["8"])

    def test_ascendancy_points_must_be_full(self):
        checks, _ = self.run_checks({"1", "2", "3", "20", "21"}, level=90)
        self.assertFalse(checks["Tree: ascendancy points full"]["passed"])     # 8 expected at level 90

    def test_unspent_points_near_zero(self):
        ok, _ = self.run_checks({"1", "2"}, passives={"used": 98, "maximum": 100})
        bad, _ = self.run_checks({"1", "2"}, passives={"used": 90, "maximum": 100})
        self.assertTrue(ok["Tree: unspent points near zero"]["passed"])
        self.assertFalse(bad["Tree: unspent points near zero"]["passed"])
        over, _ = self.run_checks({"1", "2"}, passives={"used": 101, "maximum": 100})
        self.assertFalse(over["Tree: points within budget"]["passed"])

    def test_keystone_must_match_the_defence_model(self):
        wrong, _ = self.run_checks({"1", "2", "3", "5"}, spec={"defenseModel": "hybrid"})
        right, _ = self.run_checks({"1", "2", "3", "5"}, spec={"defenseModel": "ci"})
        self.assertFalse(wrong["Tree: keystone legality"]["passed"])
        self.assertTrue(right["Tree: keystone legality"]["passed"])
        missing, _ = self.run_checks({"1", "2", "3"}, spec={"defenseModel": "ci"})
        self.assertFalse(missing["Tree: keystone legality"]["passed"])

    def test_mastery_needs_group_notable_and_valid_effect(self):
        good, _ = self.run_checks({"1", "2", "3", "30"}, masteries={"30": 111})
        no_notable, _ = self.run_checks({"1", "2", "4", "30"}, masteries={"30": 111})
        bad_effect, _ = self.run_checks({"1", "2", "3", "30"}, masteries={"30": 999})
        unassigned, _ = self.run_checks({"1", "2", "3", "30"})
        self.assertTrue(good["Tree: mastery legality"]["passed"])
        self.assertFalse(no_notable["Tree: mastery legality"]["passed"])
        self.assertFalse(bad_effect["Tree: mastery legality"]["passed"])
        self.assertFalse(unassigned["Tree: mastery legality"]["passed"])

    def test_waste_report_flags_branches_that_do_not_help(self):
        nodes = tiny_tree()
        losses = {frozenset({"4", "6", "7"}): 0.0001, frozenset({"3"}): 0.05}
        wasted = tree_sanity.waste_report(lambda branch: 1.0 - losses[frozenset(branch)], 1.0,
                                          [{"4", "6", "7"}, {"3"}], nodes)
        self.assertEqual(len(wasted), 1)
        self.assertEqual(wasted[0]["points"], 3)
        self.assertEqual(wasted[0]["notables"], ["n7"])

    def test_report_fails_when_waste_is_found(self):
        nodes = tiny_tree()
        report = tree_sanity.sanity_report(nodes, {"defenseModel": "hybrid"}, {"1", "2", "3", "20", "21", "22"},
                                           {}, {}, {"used": 6, "maximum": 6}, "Occultist", 33,
                                           wasted=[{"nodes": ["3"], "points": 1, "names": ["n3"],
                                                    "objectiveLoss": 0.0, "notables": []}], waste_measured=True)
        self.assertFalse(report["passed"])
        self.assertIn("Tree: no wasted travel or unrelated clusters", [check["name"] for check in report["checks"]
                                                                       if not check["passed"]])


class UnmodeledUptime(unittest.TestCase):
    def test_rotating_random_buffs_are_not_candidates(self):
        bound_fate = "Every 5 seconds, gain one of the following for 5 seconds:\nYour Hits are always Critical Strikes"
        self.assertTrue(real_generator.UNMODELED_UPTIME_PATTERN.search(bound_fate))
        self.assertFalse(real_generator.UNMODELED_UPTIME_PATTERN.search("+(60-80) to maximum Life"))


class CoverageReport(unittest.TestCase):
    def test_slots_with_and_without_uniques_are_explained(self):
        spec = spec_with(uniqueScreenGain={"Good Belt": 0.04})
        belt = "Rarity: UNIQUE\nGood Belt\nLeather Belt\nImplicits: 0"
        trace = [{"kind": "unique_candidate", "name": "Bad Amulet", "slot": "Amulet", "score_delta": -0.02,
                  "failed_checks": ["Resistances"]},
                 {"kind": "unique_candidate_skipped", "name": "Pricey Helm", "slot": "Helmet",
                  "reason": "does not fit the unique budget on its own"}]
        report = real_generator.unique_coverage_report(
            spec, {"gear": [{"slot": "Weapon 2"}]}, {"Belt": belt}, {}, {"priced": [
                {"slot": "Belt", "name": "Good Belt", "chaos": 3.0}], "unknown": []}, trace)
        rows = {row["slot"]: row for row in report["slots"]}
        self.assertEqual(rows["Belt"]["unique"], "Good Belt")
        self.assertIn("measured gain", rows["Belt"]["reason"])
        self.assertIn("broke", rows["Amulet"]["reason"])
        self.assertIn("none improved", rows["Amulet"]["reason"])
        self.assertIn("compatible", rows["Gloves"]["reason"])
        self.assertIn("not modeled", rows["Flask 1"]["reason"])
        self.assertEqual(report["equipmentSlotsWithUniques"], 1)


if __name__ == "__main__":
    unittest.main()
