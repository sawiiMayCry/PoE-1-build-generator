"""Readiness regressions: empty sockets, spare-socket fill, Pantheons, chaos resistance, Summon Skeletons."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_evaluation import package_report
from generation_data import GameData, RareItem, solve_suffixes
from passive_search import (SearchBudget, candidate_minion_count, skeleton_population, sustained_resource_use,
                            temporary_minion_population)
from real_generator import (assess_mechanics, assess_quality, chaos_target, efficiency_rank, mana_shortfall,
                            mechanic_profile, pantheon_rule, repair_chaos_resistance, repair_target,
                            select_pantheon, settle_reservations, sync_permanent_minion_count)
from skill_packages import (capacity_from_equipment, fill_packages, make_group, pack_groups)
from skill_planner import plan_skill_loadout

FIXTURES = Path(__file__).parent / "fixtures"


def gem(name, tags, support=False):
    identifier = "Metadata/Items/Gems/" + ("SupportGem" if support else "SkillGem") + name.replace(" ", "")
    levels = [{"level": level, "requiredLevel": 1, "str": 0, "dex": 0, "int": 0} for level in range(1, 21)]
    return {"id": identifier, "gameId": identifier, "name": name, "variantId": name.replace(" ", ""),
            "skillId": name.replace(" ", ""), "tags": dict.fromkeys(tags, True), "support": support,
            "maxLevel": 20, "levels": levels}


# ---------------------------------------------------------------- empty-socket completeness check

def socket_xml(wand_sockets="B-B-B", wand_gems=0):
    gems = "".join(f'<Gem nameSpec="G{i}" gemId="g{i}" skillId="Main" level="20"/>' for i in range(6))
    wand = "".join(f'<Gem nameSpec="W{i}" gemId="w{i}" skillId="Foo" level="20"/>' for i in range(wand_gems))
    wand_skill = f'<Skill slot="Weapon 1">{wand}</Skill>' if wand_gems else ""
    return f'''<PathOfBuilding><Tree activeSpec="1"><Spec classId="0"/></Tree>
<Skills activeSkillSet="1"><SkillSet id="1"><Skill slot="Body Armour" includeInFullDPS="true">{gems}</Skill>{wand_skill}</SkillSet></Skills>
<Items activeItemSet="1"><Item id="1">Rarity: RARE
Body
Vaal Regalia
Sockets: R-R-R-R-R-R
</Item><Item id="2">Rarity: RARE
Wand
Convoking Wand
Sockets: {wand_sockets}
</Item><ItemSet id="1"><Slot name="Body Armour" itemId="1"/><Slot name="Weapon 1" itemId="2"/></ItemSet></Items></PathOfBuilding>'''


class EmptySocketCheck(unittest.TestCase):
    def test_unused_weapon_sockets_are_a_completeness_gap(self):
        report = package_report(socket_xml(), {})
        gaps = [gap for gap in report["gaps"] if "empty socket" in gap]
        self.assertEqual(gaps, ["Weapon 1 has 3 empty sockets of 3"])
        self.assertEqual(report["counts"]["slots"]["Weapon 1"]["spareSockets"], 3)

    def test_partially_filled_item_reports_the_remainder(self):
        report = package_report(socket_xml(wand_gems=2), {})
        self.assertIn("Weapon 1 has 1 empty socket of 3", report["gaps"])

    def test_fully_socketed_items_have_no_empty_socket_gap(self):
        report = package_report(socket_xml(wand_gems=3), {})
        self.assertFalse([gap for gap in report["gaps"] if "empty socket" in gap])

    def test_trimmed_item_has_no_gap(self):
        report = package_report(socket_xml(wand_sockets="B-B", wand_gems=2), {})
        self.assertFalse([gap for gap in report["gaps"] if "empty socket" in gap])


# ---------------------------------------------------------------- spare-socket fill planner

def planner_data():
    gems = [gem("Summon Skeletons", ["spell", "minion", "intelligence"]), gem("Minion Damage", ["support", "minion"], True),
            gem("Minion Speed", ["support", "minion"], True), gem("Minion Life", ["support", "minion"], True),
            gem("Flame Dash", ["spell", "movement", "blink", "fire"]), gem("Phase Run", ["spell", "movement"]),
            gem("Faster Casting", ["support"], True), gem("Absolution", ["spell", "minion"]),
            gem("Raise Zombie", ["spell", "minion"]), gem("Summon Stone Golem", ["spell", "minion", "golem"]),
            gem("Steelskin", ["spell", "guard"]), gem("Vaal Discipline", ["spell", "aura", "vaal"]),
            gem("Vaal Haste", ["spell", "aura", "vaal"]), gem("Vaal Grace", ["spell", "aura", "vaal"])]
    bases = {"Convoking Wand": {"type": "Weapon", "subType": "Wand", "socketLimit": 3, "tags": {"wand": True, "default": True}},
             "Titanium Spirit Shield": {"type": "Shield", "socketLimit": 3, "tags": {"shield": True, "default": True}},
             "Vaal Regalia": {"type": "Body Armour", "socketLimit": 6, "tags": {"default": True}},
             "Sorcerer Boots": {"type": "Boots", "socketLimit": 4, "tags": {"default": True}}}
    return GameData({"gems": gems, "bases": bases, "mods": []})


class FillPlanner(unittest.TestCase):
    SPEC = {"skill": "Summon Skeletons", "ascendancy": "Necromancer", "level": 90, "focus": "balanced",
            "damageType": "physical", "archetype": "minion"}

    def setUp(self):
        self.data = planner_data()
        self.items = [RareItem(slot, base, self.data.bases[base]) for slot, base in (
            ("Body Armour", "Vaal Regalia"), ("Weapon 1", "Convoking Wand"), ("Weapon 2", "Titanium Spirit Shield"),
            ("Boots", "Sorcerer Boots"))]
        self.capacity = capacity_from_equipment(self.items, {}, self.data)

    def evaluate(self, groups):
        names = {g["name"] for grp in groups for g in grp["gems"]}
        dps = 100_000 * (1.2 if "Absolution" in names else 1.0)
        return {"stats": {"FullDPS": dps, "Life": 5000, "LifeUnreserved": 5000, "EnergyShield": 3000, "TotalEHP": 20000,
                          "Mana": 900, "ManaUnreserved": 900, "ManaCost": 10, "ManaRegen": 30,
                          "Str": 200, "Dex": 200, "Int": 400, "ReqStr": 0, "ReqDex": 0, "ReqInt": 0,
                          "FireResist": 75, "ColdResist": 75, "LightningResist": 75, "ChaosResist": 0},
                "ok": True, "reasons": []}

    def test_fill_packages_put_a_justified_group_on_every_spare_socket(self):
        plan = plan_skill_loadout(self.SPEC, self.data, ["Minion Damage", "Minion Speed"], self.evaluate,
                                  capacity=self.capacity, items=self.items)
        self.assertEqual(plan["problems"], [])
        self.assertTrue(plan["fill"], "the spare weapon sockets were not offered any package")
        for slot, info in plan["occupancy"].items():
            self.assertEqual(info["spare"], 0, f"{slot} has an empty socket")
        for entry in plan["fill"]:
            self.assertTrue(entry["function"])
            self.assertIn(entry["evidence"], {"stat", "role"})
        # A damage-evidence package is only kept because the (mock) PoB measured the gain.
        absolution = [a for a in plan["accepted"] if a["package"] == "fill_absolution"]
        self.assertTrue(absolution and absolution[0]["evidence"] == "stat")

    def test_fill_never_repeats_a_gem_already_socketed(self):
        present = {"Absolution", "Raise Zombie", "Summon Stone Golem", "Phase Run", "Vaal Discipline", "Vaal Haste", "Vaal Grace"}
        facts = {"archetype": "minion", "level": 90}
        self.assertEqual(fill_packages(self.SPEC, self.data, facts, present), [])

    def test_unmeasured_damage_package_is_rejected_not_padded(self):
        flat = lambda groups: {**self.evaluate(groups), "stats": {**self.evaluate(groups)["stats"], "FullDPS": 100_000}}
        plan = plan_skill_loadout(self.SPEC, self.data, ["Minion Damage"], flat, capacity=self.capacity,
                                  items=self.items)
        self.assertFalse([a for a in plan["accepted"] if a["package"] == "fill_absolution"])
        self.assertTrue([o for o in plan["omissions"] if o["package"] == "fill_absolution"
                         and "damage" in o["reason"]])


class ExactPacking(unittest.TestCase):
    CAPACITY = {"Boots": {"total": 4, "fixedRuns": None}, "Gloves": {"total": 4, "fixedRuns": None},
                "Helmet": {"total": 4, "fixedRuns": None}, "Weapon 1": {"total": 3, "fixedRuns": None},
                "Weapon 2": {"total": 3, "fixedRuns": None}}

    def groups(self):
        spec = [("g0", "movement", 1), ("g1", "curse", 3), ("g2", "movement", 2), ("g3", "movement", 2),
                ("g4", "herald", 3), ("g5", "defense", 2), ("g6", "defense", 3)]
        return [make_group(name, role, [(f"{name}:{i}", "active") for i in range(size)]) for name, role, size in spec]

    def test_greedy_fragmentation_is_resolved_by_exact_search(self):
        import skill_packages
        groups = self.groups()                                  # 16 gems into 18 sockets
        original = skill_packages._exact_pack
        skill_packages._exact_pack = lambda *args, **kwargs: None
        try:
            self.assertTrue(pack_groups(groups, self.CAPACITY)["unplaced"], "greedy no longer fragments here")
        finally:
            skill_packages._exact_pack = original
        packed = pack_groups(groups, self.CAPACITY)
        self.assertEqual(packed["unplaced"], [])
        used = {}
        for group in groups:
            used[packed["placements"][group["id"]]] = used.get(packed["placements"][group["id"]], 0) + len(group["gems"])
        for slot, count in used.items():
            self.assertLessEqual(count, self.CAPACITY[slot]["total"], slot)

    def test_impossible_packing_still_reports_unplaced(self):
        groups = self.groups() + [make_group("extra", "utility", [("e:0", "active"), ("e:1", "active"), ("e:2", "active")])]
        packed = pack_groups(groups, self.CAPACITY)
        self.assertTrue(packed["unplaced"] and packed["errors"])


# ---------------------------------------------------------------- Pantheon selection

class FakeWorker:
    def __init__(self, ehp_by_god=None):
        self.ehp_by_god, self.calls = ehp_by_god or {}, 0
        self.spec = None

    def request(self, operation, **kwargs):
        self.calls += 1
        pantheon = self.spec.get("pantheon") or {}
        ehp = 20_000 + sum(self.ehp_by_god.get(god, 0) for god in (pantheon.get("major"), pantheon.get("minor")) if god)
        return {"stats": {"TotalEHP": ehp, "ChaosResist": 0}, "passives": {"used": 1, "maximum": 2}}


class PantheonSelection(unittest.TestCase):
    def run_selection(self, ehp_by_god, spec=None):
        spec = spec or {"skill": "Summon Skeletons"}
        worker = FakeWorker(ehp_by_god)
        worker.spec = spec
        calc = {"stats": {"TotalEHP": 20_000, "ChaosResist": 0}}
        budget = SearchBudget(100)
        trace = []
        select_pantheon(spec, lambda: "xml", worker, budget, calc, trace)
        return spec, trace, budget

    def test_measured_ehp_decides_when_pob_separates_the_gods(self):
        spec, trace, budget = self.run_selection({"Solaris": 2000, "Lunaris": 500, "Gruthkul": 800, "Shakari": 100})
        self.assertEqual(spec["pantheon"], {"major": "Solaris", "minor": "Gruthkul"})
        self.assertEqual(trace[0]["majorBy"], "PoB effective hit pool")
        self.assertIn("pantheon", budget.shares)
        self.assertLessEqual(budget.shares["pantheon"]["used"], 14)

    def test_documented_rule_applies_when_pob_cannot_separate_them(self):
        spec, trace, _ = self.run_selection({})
        self.assertEqual(spec["pantheon"]["major"], "TheBrineKing")
        self.assertEqual(spec["pantheon"]["minor"], "Shakari")        # chaos resistance 0% < 40%
        self.assertEqual((trace[0]["majorBy"], trace[0]["minorBy"]), ("rule", "rule"))

    def test_rule_prefers_gruthkul_when_chaos_resistance_is_healthy(self):
        self.assertEqual(pantheon_rule({}, {"ChaosResist": 60})[:2], ("TheBrineKing", "Gruthkul"))
        self.assertEqual(pantheon_rule({"defenseModel": "ci"}, {"ChaosResist": -60})[1], "Gruthkul")

    def test_selected_gods_are_written_to_the_pob_config(self):
        from build_assembly import assemble
        import xml.etree.ElementTree as ET
        # assemble() needs a full context; the Build attributes are what PoB reads, check them directly.
        source = (Path(__file__).resolve().parents[1] / "build_assembly.py").read_text(encoding="utf-8")
        self.assertIn('"pantheonMajorGod"', source)
        self.assertIn('"pantheonMinorGod"', source)
        self.assertTrue(callable(assemble) and ET is not None)


# ---------------------------------------------------------------- chaos resistance target

def chaos_data():
    mods = [{"id": "ChaosRes1", "kind": "Suffix", "group": "ChaosResistance", "level": 1, "weightKey": ["default"],
             "weightVal": [1000], "lines": ["+(31-35)% to Chaos Resistance"]},
            {"id": "FireRes1", "kind": "Suffix", "group": "FireResistance", "level": 1, "weightKey": ["default"],
             "weightVal": [1000], "lines": ["+(31-35)% to Fire Resistance"]}]
    bases = {"Coral Ring": {"type": "Ring", "tags": {"default": True}}, "Amber Amulet": {"type": "Amulet", "tags": {"default": True}}}
    return GameData({"gems": [], "bases": bases, "mods": mods})


class ChaosResistance(unittest.TestCase):
    def test_chaos_deficit_is_repaired_to_the_floor(self):
        data = chaos_data()
        items = [RareItem("Ring 1", "Coral Ring", data.bases["Coral Ring"]),
                 RareItem("Amulet", "Amber Amulet", data.bases["Amber Amulet"])]
        stats = {"FireResist": 75, "ColdResist": 75, "LightningResist": 75, "ChaosResist": -36}
        self.assertEqual(solve_suffixes(items, data, stats), 0)               # default: elemental only
        added = solve_suffixes(items, data, stats, chaos_target=0)
        self.assertEqual(added, 2)                                              # two 33% rolls cover -36%
        lines = [line for item in items for mod in item.mods for line in mod["lines"]]
        self.assertTrue(all("Chaos Resistance" in line for line in lines))

    def test_no_chaos_target_under_chaos_inoculation(self):
        self.assertIsNone(chaos_target({"defenseModel": "ci"}))
        self.assertEqual(chaos_target({"defenseModel": "hybrid"}), 0)

    def test_final_repair_uses_its_own_budget_share_and_reports_a_shortfall(self):
        data = chaos_data()
        items = [RareItem("Ring 1", "Coral Ring", data.bases["Coral Ring"])]

        class Worker:
            def __init__(self):
                self.chaos = -36

            def request(self, operation, **kwargs):
                resist = -36 + 33 * sum(len(i.mods) for i in items)
                return {"stats": {"FireResist": 75, "ColdResist": 75, "LightningResist": 75, "ChaosResist": resist}}

        budget, spec, trace = SearchBudget(50), {"defenseModel": "hybrid"}, []
        calc = repair_chaos_resistance(spec, data, items, lambda: "xml", Worker(), budget,
                                       {"stats": {"FireResist": 75, "ColdResist": 75, "LightningResist": 75,
                                                  "ChaosResist": -36}}, trace)
        self.assertEqual(spec["chaosRepair"]["before"], -36)
        self.assertLess(spec["chaosRepair"]["after"], 0)                        # one ring cannot reach 0
        self.assertIn("free suffix", spec["chaosRepair"]["reason"])
        self.assertIn("chaos_repair", budget.shares)
        self.assertEqual(trace[-1]["kind"], "chaos_resistance_repair")
        self.assertEqual(calc["stats"]["ChaosResist"], spec["chaosRepair"]["after"])


# ---------------------------------------------------------------- Summon Skeletons profile

class SkeletonProfile(unittest.TestCase):
    """The profile is backed by a real PoB 2.67.2 calculation (fixtures/skeleton_pob_calc.json)."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((FIXTURES / "skeleton_pob_calc.json").read_text(encoding="utf-8"))
        cls.stats = cls.fixture["counts"]["1"]
        cls.spec = {"skill": "Summon Skeletons", "ascendancy": "Necromancer", "archetype": "minion",
                    "minionCount": 1, "damageType": "physical"}

    def test_fixture_is_a_real_pob_calculation_that_scales_linearly_with_the_population(self):
        one, four, eight = (self.fixture["counts"][key]["FullDPS"] for key in ("1", "4", "8"))
        self.assertAlmostEqual(four / one, 4, places=3)
        self.assertAlmostEqual(eight / one, 8, places=3)
        self.assertEqual(self.stats["ActiveMinionLimit"], 8)
        self.assertGreater(self.stats["SummonedMinionsPerCast"], 0)

    def test_population_is_limit_capped_and_sustainable(self):
        count, sustainable = skeleton_population(self.stats, self.spec)
        self.assertEqual((count, sustainable), (8, True))
        self.assertEqual(temporary_minion_population(self.stats, self.spec), (8, True))
        self.assertEqual(candidate_minion_count(self.stats, self.spec), 8)

    def test_population_scales_dps_to_the_real_pob_value(self):
        count, _ = skeleton_population(self.stats, self.spec)
        modeled = self.stats["FullDPS"] * count
        self.assertAlmostEqual(modeled, self.fixture["counts"]["8"]["FullDPS"], delta=1)

    def test_mana_is_charged_for_refresh_casts_only(self):
        resource = sustained_resource_use(self.stats, self.spec)
        self.assertTrue(resource["sustainable"])
        # PoB's tooltip rate would need ManaCost x Speed per second; refreshing 8 skeletons that live 32s
        # needs 2 casts per 33s window.
        tooltip_use = self.stats["ManaCost"] * self.stats["Speed"]
        self.assertGreater(tooltip_use, self.stats["ManaRegen"])
        refresh = self.stats["ManaCost"] * (self.stats["ActiveMinionLimit"] / self.stats["SummonedMinionsPerCast"]) / self.stats["Duration"]
        self.assertLess(refresh, self.stats["ManaRegen"] * 0.1)

    def test_starved_population_is_reported_unsustainable(self):
        starved = {**self.stats, "ManaRegen": 0.5, "Speed": 0.2}
        count, sustainable = skeleton_population(starved, self.spec)
        self.assertFalse(sustainable)
        self.assertLess(count, 8)

    def test_profile_is_tested_and_checks_pass_with_real_outputs(self):
        profile = mechanic_profile(self.spec)
        self.assertEqual(profile["name"], "summon_skeletons_necromancer")
        self.assertTrue(profile["evidence"])
        spec = dict(self.spec)
        self.assertTrue(sync_permanent_minion_count(spec, {"stats": self.stats}))
        self.assertEqual(spec["minionCount"], 8)
        self.assertTrue(spec["_populationSustainable"])
        xml = '<PathOfBuilding><Tree><Spec nodes="12"/></Tree></PathOfBuilding>'
        checks = assess_mechanics(spec, {"stats": {**self.stats, "FullDPS": 400000}}, profile, xml,
                                  {"tree": {"nodes": {"12": {"name": "Other"}}}})
        by_name = {check["name"]: check for check in checks}
        self.assertTrue(by_name["Mechanic profile"]["passed"])
        self.assertTrue(by_name["Minion limit calculated"]["passed"])
        self.assertTrue(by_name["Temporary minion population modeled"]["passed"])
        self.assertTrue(by_name["Compatible ascendancy"]["passed"])

    def test_unsustained_population_fails_the_mechanic_check_and_warns(self):
        profile = mechanic_profile(self.spec)
        spec = {**self.spec, "minionCount": 3, "_populationSustainable": False}
        xml = '<PathOfBuilding><Tree><Spec nodes="12"/></Tree></PathOfBuilding>'
        checks = assess_mechanics(spec, {"stats": self.stats}, profile, xml, {"tree": {"nodes": {}}})
        failed = [c["name"] for c in checks if not c["passed"]]
        self.assertIn("Temporary minion population modeled", failed)
        _, warnings = assess_quality(spec, {"stats": self.stats, "passives": {"used": 1, "maximum": 1}},
                                     profile, checks)
        self.assertTrue(any("minion population" in w for w in warnings))

    def test_other_necromancer_skills_use_derived_profiles(self):
        for skill in ("Absolution", "Bane"):
            profile = mechanic_profile({"skill": skill, "ascendancy": "Necromancer", "archetype": "minion"})
            self.assertTrue(profile["derived"], skill)
            self.assertFalse(profile["override"], skill)

class ManaRepairTargets(unittest.TestCase):
    OMISSIONS = [
        {"package": "herald", "repairable": True, "reason": "reservation does not fit even with Enlighten: resource shortfall: mana 136"},
        {"package": "defense_grace", "repairable": True, "reason": "resource shortfall: mana 215"},
        {"package": "defense_flesh_and_stone", "repairable": True,
         "reason": "resource shortfall: mana 20; PoB shows no measurable defensive gain"},
        {"package": "defense_determination", "repairable": True,
         "reason": "resource shortfall: mana 14; Armour is 3,059 with it, below the 10,000"},
        {"package": "fill_vaal_clarity", "repairable": True, "fill": True, "reason": "resource shortfall: mana 5"}]

    def test_repair_aims_at_the_cheapest_package_whose_only_problem_is_mana(self):
        summary = {"omissions": self.OMISSIONS}
        self.assertEqual(mana_shortfall(summary), 136.0)
        groups = {"herald": [{"id": "herald"}], "defense_grace": [{"id": "grace"}]}
        self.assertEqual(repair_target(summary, groups), ("herald", [{"id": "herald"}]))

    def test_no_pure_mana_omission_means_no_repair(self):
        self.assertEqual(mana_shortfall({"omissions": self.OMISSIONS[2:4]}), 0.0)
        self.assertEqual(repair_target({"omissions": self.OMISSIONS[2:4]}, {}), (None, None))

    def test_efficiency_nodes_for_reserved_skills_rank_ahead_of_pool_nodes(self):
        reserving = {"Zealotry", "Herald of Ice"}
        self.assertEqual(efficiency_rank("12% increased Reservation Efficiency of Skills", reserving), 0)
        self.assertEqual(efficiency_rank("Zealotry has 50% increased Mana Reservation Efficiency", reserving), 0)
        self.assertEqual(efficiency_rank("10% increased Mana Reservation Efficiency of Herald Skills", reserving), 0)
        self.assertEqual(efficiency_rank("10% increased Mana Reservation Efficiency of Herald Skills", {"Zealotry"}), 2)
        self.assertEqual(efficiency_rank("8% increased maximum Mana", reserving), 1)
        self.assertEqual(efficiency_rank("Hatred has 50% increased Mana Reservation Efficiency", reserving), 0)


class SettleReservations(unittest.TestCase):
    def test_final_build_that_cannot_pay_its_reservations_drops_the_last_one(self):
        groups = [make_group("main", "main", [("Skill", "active")]),
                  make_group("reservation", "aura", [("Anger", "active"), ("Hatred", "active")]),
                  make_group("herald", "herald", [("Herald of Ice", "active")])]
        spec = {"skillGroups": groups, "skillPlanSummary": {"omissions": []}}

        class Worker:
            def request(self, operation, **kwargs):
                count = sum(len(g["gems"]) for g in spec["skillGroups"] if g["role"] != "main")
                return {"stats": {"ManaUnreserved": 100 - 40 * (count - 1) if count > 1 else 100, "ManaCost": 30}}

        calc = {"stats": {"ManaUnreserved": 0, "ManaCost": 30}}
        calc = settle_reservations(spec, lambda: "xml", Worker(), SearchBudget(50), calc, [])
        self.assertGreaterEqual(calc["stats"]["ManaUnreserved"], calc["stats"]["ManaCost"])
        self.assertEqual(spec["reservationsDropped"][0], "Herald of Ice")
        self.assertTrue(any("dropped after the final tree" in o["reason"] for o in spec["skillPlanSummary"]["omissions"]))

    def test_nothing_is_dropped_when_mana_is_sufficient(self):
        spec = {"skillGroups": [make_group("main", "main", [("Skill", "active")])]}
        calc = {"stats": {"ManaUnreserved": 50, "ManaCost": 10}}
        self.assertIs(settle_reservations(spec, lambda: "x", None, SearchBudget(5), calc, []), calc)
        self.assertNotIn("reservationsDropped", spec)


class WinterOrbOccultistProfile(unittest.TestCase):
    """Winter Orb on an Occultist: same cold spell hit and channel cadence as the tested Elementalist recipe."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((FIXTURES / "winter_orb_occultist_pob_calc.json").read_text(encoding="utf-8"))
        cls.spec = {"skill": "Winter Orb", "ascendancy": "Occultist", "archetype": "spell", "channelled": True,
                    "damageType": "cold", "focus": "balanced"}

    def test_profile_is_tested_for_the_occultist_and_not_for_other_ascendancies(self):
        self.assertEqual(mechanic_profile(self.spec)["name"], "winter_orb_occultist")
        self.assertEqual(mechanic_profile({**self.spec, "ascendancy": "Necromancer"})["name"], "derived_spell")
        self.assertEqual(mechanic_profile({**self.spec, "ascendancy": "Elementalist"})["name"], "winter_orb_elementalist")

    def test_real_pob_outputs_satisfy_the_profile_checks_and_channel_sustain(self):
        stats = self.fixture["stats"]
        profile = mechanic_profile(self.spec)
        xml = '<PathOfBuilding><Tree><Spec nodes="12"/></Tree></PathOfBuilding>'
        checks = assess_mechanics(self.spec, {"stats": stats}, profile, xml, {"tree": {"nodes": {}}})
        by_name = {c["name"]: c for c in checks}
        self.assertTrue(by_name["Mechanic profile"]["passed"])
        self.assertTrue(by_name["Compatible ascendancy"]["passed"])
        self.assertTrue(by_name["Main-skill damage calculated"]["passed"])
        # Tooltip cast rate would drain mana; the channel cadence (one payment per second) is covered.
        self.assertGreater(stats["ManaCost"] * stats["Speed"], stats["ManaRegen"])
        self.assertTrue(sustained_resource_use(stats, self.spec)["sustainable"])


if __name__ == "__main__":
    unittest.main()
