import json
import unittest
from pathlib import Path

from generation_data import GameData, RareItem
from skill_packages import (
    GENERATED_WINTER_ORB_OCCULTIST_BASELINE, REFERENCE_WINTER_ORB_OCCULTIST, apply_stage_caps,
    as_group_dicts, attribute_requirements, build_facts, candidate_packages, capacity_from_equipment,
    gem_level_for, groups_from_xml, has_fixed_sockets, legacy_groups, make_group, package_completeness,
    pack_groups, socket_line, to_contract_groups, trigger_level_legal, validate_groups)
from skill_planner import (fit_levels_to_attributes, judge_package, plan_main_link, plan_skill_loadout,
                           plan_utility_groups, resource_deficits)

FIXTURE = Path(__file__).parent / "fixtures" / "agent2_metadata.json"
MAIN_SUPPORTS = ["Arcane Surge", "Concentrated Effect", "Cold Penetration", "Added Cold Damage", "Cruelty"]


def game_data() -> GameData:
    return GameData(json.loads(FIXTURE.read_text(encoding="utf-8")))


def witch_items(data: GameData) -> list[RareItem]:
    bases = (("Body Armour", "Vaal Regalia"), ("Helmet", "Hubris Circlet"), ("Gloves", "Sorcerer Gloves"),
             ("Boots", "Sorcerer Boots"), ("Weapon 1", "Prophecy Wand"), ("Weapon 2", "Titanium Spirit Shield"),
             ("Amulet", "Amber Amulet"), ("Ring 1", "Coral Ring"), ("Belt", "Leather Belt"))
    return [RareItem(slot, base, data.bases[base]) for slot, base in bases]


SPEC = {"skill": "Winter Orb", "ascendancy": "Occultist", "level": 90, "focus": "damage",
        "damageType": "cold", "archetype": "spell"}
RESERVE = {"Zealotry": 0.30, "Determination": 0.25, "Discipline": 0.25, "Hatred": 0.30, "Clarity": 0.15,
           "Herald of Ice": 0.25, "Grace": 0.25, "Flesh and Stone": 0.25, "Arctic Armour": 0.25,
           "Anger": 0.30, "Wrath": 0.30, "Malevolence": 0.30}
ILLEGAL = {("Frostblink", "Faster Casting")}


class MockPob:
    """Tiny deterministic stand-in for PoB with the mechanics the planner reads."""

    def __init__(self, mana=4000):
        self.mana, self.calls = mana, 0

    def __call__(self, groups):
        self.calls += 1
        reserved = 0.0
        names = set()
        for group in groups:
            names |= {gem["name"] for gem in group["gems"]}
            factor = 0.75 if any(g["name"] == "Enlighten" for g in group["gems"]) else 1.0
            for gem in group["gems"]:
                if gem["kind"] == "active" and gem["enabled"]:
                    reserved += RESERVE.get(gem["name"], 0.0) * factor
        dps = 100_000 * (1.25 if "Zealotry" in names else 1.0)
        ehp = 20_000 * (1.08 if "Determination" in names else 1) * (1.06 if "Discipline" in names else 1) \
            * (1.03 if "Grace" in names else 1) * (1.02 if "Flesh and Stone" in names else 1)
        stats = {"FullDPS": dps, "Life": 3000, "LifeUnreserved": 3000, "EnergyShield": 3300,
                 "TotalEHP": ehp, "Armour": 300 * (1.5 if "Determination" in names else 1),
                 "Mana": self.mana, "ManaUnreserved": self.mana * (1 - reserved), "ManaCost": 15,
                 "ManaRegen": 30, "Str": 150, "Dex": 150, "Int": 400, "ReqStr": 100, "ReqDex": 100, "ReqInt": 100,
                 "FireResist": 75, "ColdResist": 75, "LightningResist": 75, "ChaosResist": 0}
        return {"stats": stats, "ok": True, "reasons": []}

    @staticmethod
    def legality(groups, group):
        active = group.get("mainActive")
        return [f"{gem['name']} cannot support {active}" for gem in group["gems"]
                if gem["kind"] == "support" and (active, gem["name"]) in ILLEGAL]


def plan(data, mana=4000, **kwargs):
    items = witch_items(data)
    capacity = capacity_from_equipment(items, {}, data)
    mock = MockPob(mana)
    result = plan_skill_loadout(SPEC, data, MAIN_SUPPORTS, mock, capacity=capacity, items=items,
                                legality=mock.legality, **kwargs)
    return result, capacity, mock


class WinterOrbCompleteness(unittest.TestCase):
    """The user's screenshot comparison: 9 generated socketed gems versus 23 in the reference."""

    def test_reference_and_baseline_fixture_counts(self):
        self.assertEqual(sum(len(v) for v in GENERATED_WINTER_ORB_OCCULTIST_BASELINE["groups"].values()), 9)
        self.assertEqual(sum(len(v) for v in REFERENCE_WINTER_ORB_OCCULTIST["groups"].values()), 23)
        self.assertEqual(GENERATED_WINTER_ORB_OCCULTIST_BASELINE["total"], 9)
        self.assertEqual(REFERENCE_WINTER_ORB_OCCULTIST["total"], 23)

    def groups_from(self, mapping, data):
        groups = []
        for index, (role_name, names) in enumerate(mapping.items()):
            role = {"main": "main", "curse": "curse", "reservation": "aura", "herald": "herald",
                    "utility": "utility", "movement": "movement"}[role_name]
            gems = [(name, "support" if data.gem(name).get("support") else "active") for name in names]
            groups.append(make_group(f"{role}{index}", role, gems))
        return groups

    def test_nine_gem_baseline_and_main_link_only_repair_both_fail_completeness(self):
        data = game_data()
        baseline = self.groups_from(GENERATED_WINTER_ORB_OCCULTIST_BASELINE["groups"], data)
        report = package_completeness(baseline)
        self.assertEqual(report["socketedGems"], 9)
        self.assertFalse(report["complete"])
        # Completing only the six-link adds two gems -> 11, with the same five isolated utility gems.
        repaired = [g for g in baseline if g["role"] != "main"]
        repaired.insert(0, make_group("main", "main", [("Winter Orb", "active")] +
                                      [(name, "support") for name in MAIN_SUPPORTS]))
        repaired_report = package_completeness(repaired)
        self.assertEqual(repaired_report["socketedGems"], 11)
        self.assertFalse(repaired_report["complete"])
        self.assertTrue(any("supported utility group" in gap for gap in repaired_report["gaps"]))

    def test_planner_builds_validated_supported_groups_far_beyond_nine_gems(self):
        data = game_data()
        result, capacity, mock = plan(data)
        self.assertIsNone(result.get("error"))
        self.assertEqual(result["problems"], [])
        self.assertGreaterEqual(result["socketedGemCount"], 15)       # well beyond 9 (and 11)
        self.assertLess(result["socketedGemCount"], 25)               # and no padding to fill every socket
        report = package_completeness(result["groups"], result["omissions"])
        self.assertTrue(report["complete"], report["gaps"])
        self.assertGreaterEqual(report["supportedUtilityGroups"], 2)
        roles = set(result["roles"])
        self.assertTrue({"main", "movement", "curse", "guard", "aura"} <= roles, roles)
        # Occupancy never exceeds physical sockets, and the main link sits in six body sockets.
        for slot, info in result["occupancy"].items():
            self.assertLessEqual(info["used"], info["available"], slot)
        body = next(g for g in result["groups"] if g["id"] == "main")
        self.assertEqual((body["slot"], len(body["gems"])), ("Body Armour", 6))
        # Every accepted utility package states its declared function and evidence type.
        self.assertEqual(len(result["accepted"]), len({a["package"].split("+")[0] for a in result["accepted"]}))
        for accepted in result["accepted"]:
            self.assertTrue(accepted["function"])
            self.assertIn(accepted["evidence"], {"role", "stat"})
        # Omitted references have explicit mechanic/resource reasons (not silence).
        for omission in result["omissions"]:
            self.assertTrue(omission["reason"])

    def test_support_pob_rejects_is_dropped_individually_and_reported(self):
        data = game_data()
        result, _, _ = plan(data)
        blink = next(g for g in result["groups"] if g["id"] == "movement_blink")
        self.assertEqual([gem["name"] for gem in blink["gems"]], ["Frostblink", "Second Wind"])
        self.assertIn({"package": "movement_blink", "support": "Faster Casting",
                       "reason": "PoB: cannot support the group's active skill"}, result["droppedSupports"])

    def test_useless_damage_aura_is_rejected_by_measurement(self):
        data = game_data()
        result, _, _ = plan(data)
        rejected = {o["package"]: o["reason"] for o in result["omissions"]}
        self.assertIn("aura_hatred", rejected)
        self.assertIn("only +0.0% damage", rejected["aura_hatred"])
        self.assertIn("Zealotry", {g["name"] for grp in result["groups"] for g in grp["gems"]})

    def test_low_mana_omits_reserving_packages_with_a_resource_reason_without_forcing_a_quota(self):
        data = game_data()
        rich, _, _ = plan(data, mana=4000)
        poor, _, _ = plan(data, mana=700)
        self.assertLess(poor["socketedGemCount"], rich["socketedGemCount"])
        reasons = [o for o in poor["omissions"] if "resource shortfall" in o["reason"]]
        self.assertTrue(reasons)
        self.assertTrue(all(o.get("repairable") for o in reasons))
        self.assertTrue(any(o["package"] == "herald" for o in poor["omissions"]))

    def test_item_granted_skills_are_not_socketed_gems(self):
        from loadout_summary import summarize_loadout
        xml = '''<PathOfBuilding><Build mainSocketGroup="1"/><Skills activeSkillSet="1"><SkillSet id="1">
          <Skill slot="Body Armour" mainActiveSkill="1"><Gem gemId="Metadata/Items/Gems/SkillGemWinterOrb" nameSpec="Winter Orb" level="20"/>
          <Gem gemId="Metadata/Items/Gems/SupportGemArcaneSurge" nameSpec="Arcane Surge" level="20"/></Skill>
          <Skill source="Item:12:Rathpith" slot="Weapon 2"><Gem nameSpec="EnemyExplode" skillId="EnemyExplode"/></Skill>
          </SkillSet></Skills><Items activeItemSet="1"><Item id="1">Rarity: RARE\nBody\nVaal Regalia\nSockets: B-B-B-B-B-B</Item>
          <ItemSet id="1"><Slot name="Body Armour" itemId="1"/></ItemSet></Items><Tree><Spec nodes="1"/></Tree></PathOfBuilding>'''
        summary = summarize_loadout(xml)
        self.assertEqual(summary["socketedGemCount"], 2)
        self.assertEqual([entry["skills"] for entry in summary["itemGranted"]], [["EnemyExplode"]])


class Packing(unittest.TestCase):
    def setUp(self):
        self.data = game_data()
        self.capacity = capacity_from_equipment(witch_items(self.data), {}, self.data)

    def group(self, group_id, role, count, slot=None):
        names = [("Frostbite", "active")] + [("Faster Casting", "support")] * (count - 1)
        gems = [{"instance": f"{group_id}:{i}", "name": n, "kind": k, "level": 20, "quality": 0,
                 "enabled": True, "count": 1} for i, (n, k) in enumerate(names, 1)]
        return make_group(group_id, role, gems, slot=slot)

    def test_physical_limits_per_slot(self):
        self.assertEqual({slot: info["total"] for slot, info in self.capacity.items() if info["total"]},
                         {"Body Armour": 6, "Helmet": 4, "Gloves": 4, "Boots": 4, "Weapon 1": 3, "Weapon 2": 3})

    def test_groups_pack_into_distinct_linked_runs_and_overflow_is_reported(self):
        groups = [self.group("main", "main", 6), self.group("a", "aura", 4), self.group("b", "curse", 3),
                  self.group("c", "movement", 3), self.group("d", "movement", 3), self.group("e", "guard", 2)]
        packing = pack_groups(groups, self.capacity)
        self.assertEqual(packing["errors"], [])
        self.assertEqual(packing["placements"]["main"], "Body Armour")
        per_slot = {}
        for group in groups:
            per_slot.setdefault(packing["placements"][group["id"]], []).append(len(group["gems"]))
        self.assertTrue(all(sum(sizes) <= self.capacity[slot]["total"] for slot, sizes in per_slot.items()))
        too_many = groups + [self.group("f", "aura", 4)]
        failed = pack_groups(too_many, self.capacity)
        self.assertTrue(failed["unplaced"])
        self.assertTrue(failed["errors"])

    def test_lone_gems_share_a_link_run_but_multi_gem_groups_need_their_own(self):
        two_singles = [self.group("s1", "utility", 1, "Helmet"), self.group("s2", "utility", 1, "Helmet")]
        self.assertEqual(pack_groups(two_singles, self.capacity)["errors"], [])
        fixed = {"Helmet": {"total": 4, "fixedRuns": [4], "unique": True, "item": "x"}}
        self.assertEqual(pack_groups([self.group("a", "aura", 2, "Helmet"), self.group("b", "aura", 2, "Helmet")],
                                     fixed)["unplaced"], ["b"])
        self.assertEqual(socket_line([3, 1]), "B-B-B B")
        self.assertEqual(socket_line([2], ["R", "G"], spare=1), "R-G B")

    def test_fixed_socket_uniques_keep_their_layout_and_flexible_uniques_use_the_base_limit(self):
        flexible = "Rarity: UNIQUE\nExample\nSorcerer Gloves\nSockets: B\nImplicits: 0"
        fixed = "Rarity: UNIQUE\nExample\nSorcerer Boots\nSockets: W-W-W-W\nHas 4 Linked Sockets\nImplicits: 0"
        capacity = capacity_from_equipment(witch_items(self.data), {"Gloves": flexible, "Boots": fixed}, self.data)
        self.assertEqual(capacity["Gloves"]["total"], 4)
        self.assertIsNone(capacity["Gloves"]["fixedRuns"])
        self.assertEqual(capacity["Boots"]["fixedRuns"], [4])
        self.assertTrue(has_fixed_sockets(fixed))
        self.assertFalse(has_fixed_sockets(flexible))
        capped = apply_stage_caps(capacity, 2)
        self.assertEqual((capped["Gloves"]["total"], capped["Body Armour"]["total"], capped["Boots"]["total"]),
                         (2, 6, 4))

    def test_validation_catches_duplicates_missing_actives_and_overflow(self):
        bad = self.group("x", "aura", 7, "Helmet")
        problems = validate_groups([bad], self.capacity, self.data)
        self.assertTrue(any("maximum 6" in text or "7 gems" in text for text in problems))
        support_only = make_group("y", "utility", [("Faster Casting", "support")], slot="Gloves")
        self.assertTrue(any("no active gem" in text for text in validate_groups([support_only], self.capacity)))

    def test_contract_round_trip_keeps_per_instance_settings(self):
        groups = [make_group("main", "main", [("Winter Orb", "active"), ("Arcane Surge", "support")], slot="Body Armour"),
                  make_group("curse", "curse", [("Frostbite", "active"), ("Arcane Surge", "support")], slot="Gloves")]
        groups[0]["gems"][1]["level"], groups[1]["gems"][1]["level"] = 20, 12
        contract = to_contract_groups(groups)
        self.assertEqual([g.role for g in contract], ["main", "curse"])
        back = as_group_dicts(contract)
        self.assertEqual([g["gems"][1]["level"] for g in back], [20, 12])
        self.assertEqual(len({gem["instance"] for g in back for gem in g["gems"]}), 4)


class Triggers(unittest.TestCase):
    def test_cast_when_damage_taken_caps_the_supported_gem_requirement(self):
        data = game_data()
        ok, _ = trigger_level_legal(data, "Cast when Damage Taken", 20, "Frostbite", 20)
        self.assertTrue(ok)
        # CwDT level 4 (requires 44) cannot trigger Frostbite level 8 (requires 45).
        ok, why = trigger_level_legal(data, "Cast when Damage Taken", 4, "Frostbite", 8)
        self.assertFalse(ok)
        self.assertIn("exceeds", why)

    def test_plan_keeps_a_manual_curse_plan_when_the_trigger_is_illegal(self):
        data = game_data()
        items = witch_items(data)
        capacity = capacity_from_equipment(items, {}, data)
        spec = {**SPEC, "level": 45}
        mock = MockPob(4000)
        result = plan_skill_loadout(spec, data, MAIN_SUPPORTS[:2], mock, capacity=capacity, items=items,
                                    legality=mock.legality)
        curse = next(g for g in result["groups"] if g["id"] == "curse")
        self.assertEqual(curse["delivery"], "manual")
        self.assertNotIn("Cast when Damage Taken", [gem["name"] for gem in curse["gems"]])
        self.assertTrue(any(row["kind"] == "trigger_dropped" for row in result["trace"]))


class AttributesAndLevels(unittest.TestCase):
    def test_new_gems_are_lowered_to_the_highest_level_whose_attributes_fit(self):
        data = game_data()
        gem = {"instance": "g:1", "name": "Grace", "kind": "active", "level": 20, "quality": 0, "enabled": True, "count": 1}
        group = make_group("g", "defense", [gem])
        need = attribute_requirements(data, "Grace", 20)
        stats = {"Str": 150, "Dex": need["dex"] - 20, "Int": 400}
        self.assertEqual(fit_levels_to_attributes([group], set(), stats, data), [])
        self.assertLess(group["gems"][0]["level"], 20)
        self.assertLessEqual(attribute_requirements(data, "Grace", group["gems"][0]["level"])["dex"], stats["Dex"])
        known = make_group("k", "aura", [dict(gem, instance="k:1")])
        fit_levels_to_attributes([known], {"k:1"}, stats, data)
        self.assertEqual(known["gems"][0]["level"], 20)
        impossible = make_group("i", "aura", [dict(gem, instance="i:1")])
        self.assertEqual(fit_levels_to_attributes([impossible], set(), {"Str": 0, "Dex": 0, "Int": 0}, data), ["Grace"])

    def test_gem_levels_follow_character_level(self):
        data = game_data()
        self.assertEqual(gem_level_for(data, "Winter Orb", 90), 20)
        self.assertLess(gem_level_for(data, "Frostbite", 45), 20)
        self.assertEqual(gem_level_for(data, "Frostbite", 5), 0)

    def test_judge_rejects_new_resource_shortfalls_and_missing_effects(self):
        pkg = {"id": "aura_x", "role": "aura", "evidence": "stat:damage"}
        before = {"FullDPS": 100.0, "ManaUnreserved": 100, "ManaCost": 10}
        no_gain = judge_package(pkg, before, dict(before))
        self.assertFalse(no_gain["ok"])
        starved = judge_package(pkg, before, {"FullDPS": 130.0, "ManaUnreserved": 5, "ManaCost": 10})
        self.assertFalse(starved["ok"])
        self.assertIn("resource shortfall", starved["problems"][0])
        self.assertTrue(judge_package(pkg, before, {"FullDPS": 130.0, "ManaUnreserved": 50, "ManaCost": 10})["ok"])
        self.assertEqual(resource_deficits({"ManaCost": 30, "ManaUnreserved": 10}), {"mana": 20.0})


class MockLinkOps:
    GAINS = {"Arcane Surge": .45, "Concentrated Effect": .40, "Cold Penetration": .45, "Added Cold Damage": .25,
             "Cruelty": .25, "Elemental Focus": .30, "Pierce": 0.0, "Chain": 0.0, "Culling Strike": 0.0,
             "Increased Area of Effect": 0.0, "Greater Multiple Projectiles": .02}
    COST = {"Concentrated Effect": 1.6, "Arcane Surge": 1.3, "Greater Multiple Projectiles": 1.5}

    def __init__(self, mana=1000, names=None):
        self.mana, self.names, self.calls = mana, names or list(self.GAINS), 0

    def stats(self, supports):
        self.calls += 1
        dps, cost = 100_000.0, 20.0
        for name in supports:
            dps *= 1 + self.GAINS[name]
            cost *= self.COST.get(name, 1.0)
        return {"FullDPS": dps, "ManaCost": cost, "ManaUnreserved": self.mana, "Life": 3000, "LifeUnreserved": 3000}

    def candidates(self, supports):
        return list(self.names)

    def score(self, supports, names):
        return {name: self.stats([*supports, name]) for name in names}


class MainLinkPlanning(unittest.TestCase):
    def test_complete_six_link_with_every_support_justified(self):
        data = game_data()
        result = plan_main_link(SPEC, data, MockLinkOps())
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["supports"]), 5)
        self.assertTrue({"Arcane Surge", "Concentrated Effect", "Cold Penetration"} <= set(result["supports"]))
        self.assertTrue(all(MockLinkOps.GAINS[name] > 0.05 for name in result["supports"]))
        self.assertTrue(all(row["role"] for row in result["supportRoles"]))
        self.assertEqual(result["bossLink"], result["supports"])

    def test_zero_effect_supports_are_not_used_as_padding(self):
        data = game_data()
        # Only three useful supports exist; compatible but useless ones must not pad the link.
        ops = MockLinkOps(names=["Arcane Surge", "Cold Penetration", "Added Cold Damage", "Culling Strike", "Pierce"])
        result = plan_main_link({**SPEC, "archetype": "spell"}, data, ops)
        self.assertFalse(result["complete"])
        self.assertNotIn("Culling Strike", result["supports"])
        self.assertIn("filler", result["reason"])
        self.assertTrue(any(row["kind"] == "main_link_filler" for row in result["trace"]))

    def test_mana_pressure_keeps_the_sixth_support_as_a_repair_not_a_removal(self):
        data = game_data()
        result = plan_main_link(SPEC, data, MockLinkOps(mana=30))
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["supports"]), 5)
        self.assertTrue(result["repairNeeded"] or result["alternatives"])
        deficits = [resource_deficits({"ManaCost": 20, "ManaUnreserved": 30})]
        self.assertEqual(deficits, [{}])

    def test_mapping_link_swaps_in_a_support_with_a_real_interaction(self):
        data = game_data()
        ops = MockLinkOps()
        result = plan_main_link(SPEC, data, ops)
        mapping = result["mappingLink"]
        # Winter Orb is a projectile/area skill: Greater Multiple Projectiles, Chain, Pierce or area apply.
        self.assertIsNotNone(mapping["swap"])
        self.assertGreaterEqual(mapping["singleTargetRetention"], 0.6)
        self.assertEqual(len(mapping["supports"]), 5)
        # A cold hit skill gets no Ignite Proliferation "coverage".
        self.assertNotIn("Ignite Proliferation", " ".join(mapping["swap"].split()))

    def test_catalogue_is_role_justified_and_requires_equipment_facts(self):
        data = game_data()
        facts = build_facts(SPEC, witch_items(data), None, data)
        ids = [pkg["id"] for pkg in candidate_packages(SPEC, data, facts)]
        # Shield Charge is a second movement skill: only offered when requested by name.
        self.assertNotIn("movement_shield_charge", ids)
        requested = {**SPEC, "requestedUtilities": ["Shield Charge"]}
        self.assertIn("movement_shield_charge", [p["id"] for p in candidate_packages(
            requested, data, build_facts(requested, witch_items(data), None, data))])
        no_shield = [item for item in witch_items(data) if item.slot != "Weapon 2"]
        facts2 = build_facts(requested, no_shield, None, data)
        self.assertNotIn("movement_shield_charge", [p["id"] for p in candidate_packages(requested, data, facts2)])
        minion = {**SPEC, "archetype": "minion", "skill": "Summon Raging Spirit", "damageType": "fire"}
        self.assertIn("minion_offerings", [p["id"] for p in candidate_packages(
            minion, data, build_facts(minion, witch_items(data), None, data))])
        for pkg in candidate_packages(SPEC, data, facts):
            self.assertTrue(pkg["function"] and pkg["evidence"])


class LegacyAndRecovery(unittest.TestCase):
    def test_legacy_shape_converts_and_recovers_from_xml(self):
        data = game_data()
        spec = {"skill": "Winter Orb", "utility": {"Flame Dash": "Boots", "Steelskin": "Gloves", "Ghost": "Helmet"}}
        groups = legacy_groups(spec, ["Arcane Surge"], data)
        self.assertEqual([g["role"] for g in groups], ["main", "movement", "guard"])
        self.assertEqual([g["slot"] for g in groups], ["Body Armour", "Boots", "Gloves"])

    def test_groups_from_xml_recovers_roles_main_and_instances(self):
        data = game_data()
        xml = '''<PathOfBuilding><Build mainSocketGroup="2"/><Skills activeSkillSet="1"><SkillSet id="1">
          <Skill slot="Boots"><Gem gemId="Metadata/Items/Gems/SkillGemFrostblink" nameSpec="Frostblink" level="20"/></Skill>
          <Skill slot="Body Armour" mainActiveSkill="1"><Gem gemId="Metadata/Items/Gems/SkillGemWinterOrb" nameSpec="Winter Orb" level="20"/>
            <Gem gemId="Metadata/Items/Gems/SupportGemArcaneSurge" nameSpec="Arcane Surge" level="20"/></Skill>
          <Skill source="Item:1:x"><Gem nameSpec="EnemyExplode"/></Skill></SkillSet></Skills>
          <Items activeItemSet="1"/><Tree><Spec nodes="1"/></Tree></PathOfBuilding>'''
        groups = groups_from_xml(xml, data)
        self.assertEqual([(g["id"], g["role"]) for g in groups], [("movement-1", "movement"), ("main", "main")])
        self.assertEqual(len({gem["instance"] for g in groups for gem in g["gems"]}), 3)


if __name__ == "__main__":
    unittest.main()
