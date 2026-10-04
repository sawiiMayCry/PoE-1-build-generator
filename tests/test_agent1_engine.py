"""Engine regressions: contracts, six-link repair, CI defense, drawbacks, jewel sockets, report."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_contracts import Candidate, GemRecord, SkillGroup, groups_from_legacy, legacy_from_groups
from build_evaluation import (active_tree_spec, mastery_count, package_report, socket_link_groups)
from generation_data import GameData
from passive_search import SearchBudget, heuristic, initial_nodes, score, tree_defenses_met
from real_generator import (defense_model_checks, jewel_socket_options, normalize_intent,
                            repair_link_resources, search_links, unique_drawback_checks)
import xml.etree.ElementTree as ET


def gem(name, tags, support=False):
    identifier = "Metadata/Items/Gems/" + ("SupportGem" if support else "SkillGem") + name.replace(" ", "")
    return {"id": identifier, "gameId": identifier, "name": name, "variantId": name.replace(" ", ""),
            "skillId": name.replace(" ", ""), "tags": dict.fromkeys(tags, True), "support": support, "maxLevel": 20}


SPEC = {"skill": "Winter Orb", "channelled": True, "archetype": "spell", "focus": "damage", "damageType": "cold",
        "baseDamageType": "cold"}


class ContractTests(unittest.TestCase):
    def test_clone_isolates_utility_choices_and_signature_includes_them(self):
        base = Candidate(spec={"skill": "X", "utility": {"A": "Boots"}}, nodes={"1"},
                         groups=groups_from_legacy({"skill": "X", "utility": {"A": "Boots"}}, ["S1"]))
        other = base.clone()
        other.groups[1].gems[0].level = 10
        other.spec["utility"]["B"] = "Gloves"
        other.nodes.add("2")
        self.assertEqual(base.spec["utility"], {"A": "Boots"})
        self.assertEqual(base.groups[1].gems[0].level, 20)
        self.assertNotEqual(base.signature(), other.signature())
        supports, utility = legacy_from_groups(base.groups)
        self.assertEqual((supports, utility), (["S1"], {"A": "Boots"}))

    def test_same_gem_in_two_groups_keeps_separate_instance_settings(self):
        a = SkillGroup("g1", "main", "Body Armour", gems=[GemRecord("Arcane Surge", True, 20, instance_id="g1:0")])
        b = SkillGroup("g2", "curse", "Helmet", gems=[GemRecord("Arcane Surge", True, 1, enabled=False, instance_id="g2:0")])
        self.assertNotEqual(SkillGroup.from_dict(a.to_dict()).gems[0], SkillGroup.from_dict(b.to_dict()).gems[0])


class SixLinkTests(unittest.TestCase):
    def _data(self, count=6):
        supports = [gem(f"Support {i}", ["support"], True) for i in range(count)]
        return GameData({"gems": [gem("Winter Orb", ["spell", "cold"]), *supports], "bases": {}, "mods": []}), supports

    def test_resource_infeasible_best_supports_are_repaired_not_truncated(self):
        data, supports = self._data(7)
        ids = [entry["id"] for entry in supports]
        costly = {ids[0], ids[1]}      # supports 0 and 1 raise cost beyond mana when linked together

        class Worker:
            def request(self, operation, **kwargs):
                links = int(kwargs.get("xml", "0:").split(":")[0] or 0) if False else 0
                xml = kwargs.get("xml", "")
                chosen = set(xml.split("|")[1].split(",")) if "|" in xml and xml.split("|")[1] else set()
                def stats(extra=()):
                    names = chosen | set(extra)
                    cost = 10 + 20 * len([n for n in names if n in {"Support 0", "Support 1"}])
                    return {"FullDPS": 1000 + 100 * len(names) + (500 if "Support 0" in names else 0),
                            "ManaUnreserved": 50, "ManaCost": cost, "ManaRegen": 100,
                            "LifeUnreserved": 100, "Life": 100, "Speed": 1}
                if operation == "supports":
                    return {"supports": ids}
                if operation == "calculate":
                    return {"stats": stats()}
                if operation == "supportScores":
                    by_id = {entry["id"]: entry["name"] for entry in supports}
                    return {"candidates": [{"id": i, "stats": stats([by_id[i]])} for i in kwargs["candidates"]]}
                raise AssertionError(operation)

        spec = dict(SPEC)
        selected = search_links(spec, data, lambda links: "<x>|" + ",".join(links), Worker(), lambda _: None,
                                trace=[], budget=SearchBudget(2000))
        self.assertEqual(len(selected), 5)
        self.assertNotIn("linkShortfall", spec)
        self.assertLessEqual(len({"Support 0", "Support 1"} & set(selected)), 1)
        self.assertNotIn("linkResourceDeficit", spec)

    def test_inert_supports_are_not_added_as_filler(self):
        data, supports = self._data(5)
        ids = [entry["id"] for entry in supports]

        class Worker:
            def request(self, operation, **kwargs):
                if operation == "supports":
                    return {"supports": ids}
                base = {"FullDPS": 100, "ManaUnreserved": 100, "ManaCost": 1, "ManaRegen": 10,
                        "LifeUnreserved": 100, "Life": 100, "Speed": 1}
                if operation == "calculate":
                    return {"stats": base}
                return {"candidates": [{"id": i, "stats": dict(base)} for i in kwargs["candidates"]]}

        spec = dict(SPEC)
        with self.assertRaises(ValueError):
            search_links(spec, data, lambda links: "<x/>", Worker(), lambda _: None)
        self.assertIn("filler", spec["linkShortfall"])


class DefenseTests(unittest.TestCase):
    NODES = {"10": {"name": "Chaos Inoculation", "isKeystone": True}}

    def test_ci_with_one_life_passes_when_protection_and_recovery_work(self):
        spec = {"defenseModel": "ci", "focus": "damage"}
        stats = {"Life": 1, "EnergyShield": 7000, "EnergyShieldRecharge": 900}
        checks = defense_model_checks(spec, stats, {"10"}, self.NODES)
        self.assertTrue(all(c["passed"] for c in checks), checks)

    def test_one_life_without_ci_or_without_es_fails_specifically(self):
        spec = {"defenseModel": "hybrid", "focus": "damage"}
        failed = [c["name"] for c in defense_model_checks(spec, {"Life": 1, "EnergyShield": 3000}, set(), self.NODES)
                  if not c["passed"]]
        self.assertEqual(failed, ["No unprotected 1-life character"])
        ci = {"defenseModel": "ci", "focus": "damage"}
        failed = [c["name"] for c in defense_model_checks(ci, {"Life": 1, "EnergyShield": 1500, "EnergyShieldRecharge": 100},
                                                          {"10"}, self.NODES) if not c["passed"]]
        self.assertEqual(failed, ["CI energy shield pool"])

    def test_ci_does_not_reward_life_or_chaos_resistance(self):
        life_node = {"stats": ["+30 to maximum Life"]}
        es_node = {"stats": ["+30 to maximum Energy Shield"]}
        ci = {"archetype": "spell", "damageType": "cold", "focus": "damage", "defenseModel": "ci"}
        hybrid = {**ci, "defenseModel": "hybrid"}
        self.assertEqual(heuristic(life_node, ci), 0)
        self.assertGreater(heuristic(life_node, hybrid), 0)
        self.assertGreater(heuristic(es_node, ci), heuristic(es_node, hybrid))

    def test_ci_pool_target_counts_only_energy_shield(self):
        spec = {"defenseModel": "ci", "focus": "damage"}
        self.assertTrue(tree_defenses_met({"Life": 1, "EnergyShield": 6500, "TotalEHP": 20000}, spec))
        self.assertFalse(tree_defenses_met({"Life": 5000, "EnergyShield": 1000, "TotalEHP": 20000}, spec))

    def test_ci_prompt_is_supported_and_low_life_remains_unsupported(self):
        data = GameData({"gems": [gem("Winter Orb", ["spell", "cold"])], "bases": {}, "mods": []})
        spec = normalize_intent("level 90 winter orb elementalist CI", {}, data, {"divineChaos": 200})
        self.assertEqual(spec["defenseModel"], "ci")
        with self.assertRaisesRegex(ValueError, "low life"):
            normalize_intent("winter orb low life", {}, data, {"divineChaos": 200})


class CadenceTests(unittest.TestCase):
    def test_channelled_skill_is_paid_per_channel_cycle_not_per_tooltip_cast(self):
        from passive_search import paid_use_rate, sustained_resource_use
        stats = {"Speed": 6.6, "ManaCost": 20, "ManaRegen": 30}
        self.assertEqual(paid_use_rate(stats, {"channelled": True}), 1.0)
        self.assertEqual(paid_use_rate(stats, {}), 6.6)
        spec = {**SPEC, "skill": "Winter Orb"}
        self.assertTrue(sustained_resource_use(stats, spec)["sustainable"])            # 20/s need vs 25.5/s net
        self.assertFalse(sustained_resource_use(stats, {**spec, "channelled": False})["sustainable"])  # 132/s


class DrawbackTests(unittest.TestCase):
    RATHPITH = ("Rarity: UNIQUE\nRathpith Globe\nTitanium Spirit Shield\n"
                "Sacrifice 10% of your Life when you Use or Trigger a Spell Skill\n")

    def test_sacrifice_unique_is_rejected_when_recovery_cannot_cover_it(self):
        stats = {"Life": 3000, "LifeCost": 0, "Speed": 6.6, "LifeRegenRecovery": 80, "LifeLeechRate": 0}
        checks = unique_drawback_checks({"Weapon 2": self.RATHPITH}, stats, SPEC)
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0]["passed"])
        self.assertIn("uses/s", checks[0]["reason"])

    def test_sacrifice_unique_passes_with_enough_recovery_and_unknown_is_not_safe(self):
        stats = {"Life": 3000, "Speed": 6.6, "LifeRegenRecovery": 400}
        self.assertTrue(unique_drawback_checks({"Weapon 2": self.RATHPITH}, stats, SPEC)[0]["passed"])
        self.assertFalse(unique_drawback_checks({"Weapon 2": self.RATHPITH}, {"Life": 3000, "Speed": 6.6}, SPEC)[0]["passed"])


class ReportTests(unittest.TestCase):
    def xml(self, main_gems, body_links="R-R-R-R-R-R"):
        gems = "".join(f'<Gem gemId="g{i}" nameSpec="G{i}" level="20" skillId="{"SupportG" if i else "Main"}"/>' for i in range(main_gems))
        return f'''<PathOfBuilding><Tree activeSpec="2"><Spec id="" classId="0" masteryEffects="{{1,2}},{{3,4}}"/>
<Spec classId="0" masteryEffects="{{1,2}},{{3,4}},{{5,6}},{{7,8}},{{9,10}}"/></Tree>
<Skills activeSkillSet="1"><SkillSet id="1"><Skill slot="Body Armour" includeInFullDPS="true">{gems}</Skill>
<Skill slot="Boots"><Gem gemId="x" nameSpec="Flame Dash" level="20"/></Skill>
<Skill source="Item:Explode"><Gem nameSpec="EnemyExplode"/></Skill></SkillSet></Skills>
<Items activeItemSet="1"><Item id="1">Rarity: RARE
Body
Vaal Regalia
Sockets: {body_links}
</Item><Item id="2">Rarity: RARE
Boot
Sorcerer Boots
Sockets: R-R-R
</Item><ItemSet id="1"><Slot name="Body Armour" itemId="1"/><Slot name="Boots" itemId="2"/></ItemSet></Items></PathOfBuilding>'''

    def test_positional_active_spec_counts_masteries_without_ids(self):
        root = ET.fromstring(self.xml(6))
        self.assertEqual(mastery_count(root), 5)
        self.assertIs(active_tree_spec(root), root.findall("./Tree/Spec")[1])

    def test_four_gem_main_link_is_incomplete_and_six_is_complete(self):
        four = package_report(self.xml(4), {})
        self.assertFalse(four["mainLinkComplete"])
        self.assertTrue(any("main link has 4 of 6" in gap for gap in four["gaps"]))
        six = package_report(self.xml(6), {})
        self.assertTrue(six["mainLinkComplete"])
        self.assertEqual(six["counts"]["socketedGems"], 7)          # item-granted EnemyExplode excluded
        self.assertEqual(six["counts"]["itemGrantedSkills"], 1)
        self.assertTrue(any("supporting skill package is sparse" in gap for gap in six["gaps"]))

    def test_unlinked_body_armour_does_not_satisfy_six_link(self):
        report = package_report(self.xml(6, "R-R-R-R R-R"), {})
        self.assertFalse(report["mainLinkComplete"])
        self.assertEqual(socket_link_groups("Sockets: R-R-R-R R-R"), [4, 2])


class EvaluationTests(unittest.TestCase):
    CALC = {"calculated": True, "stats": {"Life": 1, "EnergyShield": 7000, "FullDPS": 1000, "Str": 100, "Dex": 100, "Int": 100,
                                           "FireResist": 75, "ColdResist": 75, "LightningResist": 75,
                                           "ManaUnreserved": 100, "ManaCost": 10, "EnergyShieldRecharge": 500},
            "passives": {"used": 100, "maximum": 100, "ascendancy": 8, "secondaryAscendancy": 0}}

    def test_evaluate_calculation_separates_dimensions_and_ci_is_feasible(self):
        from real_generator import evaluate_calculation
        context = {"tree": {"nodes": {"10": {"name": "Chaos Inoculation", "isKeystone": True}}}}
        spec = {**SPEC, "defenseModel": "ci", "focus": "damage"}
        result = evaluate_calculation(self.CALC, spec, context, {"10"}, {})
        self.assertTrue(result.feasible, result.reasons)
        broken = evaluate_calculation(self.CALC, spec, context, set(), {})   # CI keystone missing
        self.assertFalse(broken.feasible)
        self.assertTrue(any("Chaos Inoculation allocated" in reason for reason in broken.reasons))

    def test_report_cannot_upgrade_an_assessed_experimental_status(self):
        from build_evaluation import final_quality_report
        xml = ReportTests().xml(6)
        report = final_quality_report(xml, {"level": 90}, self.CALC, [], {"completeness": {"gaps": []},
                                      "encounterReadiness": {"gaps": ["chaos resistance is -30% (0% target)"]}},
                                      assessed_status="experimental")
        self.assertEqual(report.status, "experimental")
        # chaos resistance is now a hard repair target: a remaining deficit blocks readiness
        self.assertEqual(report.encounter_readiness, "not_ready")
        pantheon = final_quality_report(xml, {"level": 90}, self.CALC, [], {"completeness": {"gaps": []},
                                        "encounterReadiness": {"gaps": ["major and minor Pantheons are unselected"]}})
        self.assertEqual(pantheon.encounter_readiness, "not_ready")
        hard = final_quality_report(xml, {"level": 90}, self.CALC, [], {"completeness": {"gaps": []},
                                    "encounterReadiness": {"gaps": ["damage 10 is below the 40,000 screening target"]}})
        self.assertEqual(hard.encounter_readiness, "not_ready")
        self.assertIn("incomplete", final_quality_report(xml, {"level": 90}, self.CALC, [],
                      {"completeness": {"gaps": []}, "encounterReadiness": {"gaps": []}}).completeness)


class QuoteParityTests(unittest.TestCase):
    def test_candidate_quote_equals_resolver_quote_for_the_equipped_item(self):
        from real_generator import unique_market_price
        from unique_pricing import entry_from_text, resolve_unique
        market = {"league": "L", "updated": 1, "listings": {"Test Robe": [
            {"chaos": 5, "links": 0}, {"chaos": 90, "links": 6}]}}
        definition = {"name": "Test Robe", "type": "Body Armour",
                      "raw": "Rarity: UNIQUE" + chr(10) + "Test Robe" + chr(10) + "Vaal Regalia" + chr(10) + "Sockets: B-B-B-B-B-B" + chr(10)}
        selection = unique_market_price(definition, market, 6, "Body Armour")
        equipped = resolve_unique(entry_from_text(definition["raw"], "Body Armour"), market)
        self.assertEqual(selection, equipped["chaos"])
        self.assertEqual(selection, 90)       # a six-link is never priced with the unlinked listing


class ProtectionTests(unittest.TestCase):
    def test_equipped_socket_path_is_protected_from_pruning_and_keystones_survive(self):
        from passive_search import jewel_socket_paths
        nodes = {"1": {"classStartIndex": 3, "out": ["2"]}, "2": {"in": ["1"], "out": ["3", "5"]},
                 "3": {"in": ["2"], "out": ["4"]}, "4": {"in": ["3"], "isJewelSocket": True},
                 "5": {"in": ["2"]}}
        protected = jewel_socket_paths({"tree": {"nodes": nodes}}, {"1", "2", "3", "4", "5"}, {"4"})
        self.assertTrue({"2", "3"} <= protected)
        self.assertNotIn("5", protected)

    def test_reroutes_never_trade_away_an_allocated_keystone(self):
        from real_generator import tree_reroute_removals
        nodes = {"1": {"classStartIndex": 3, "out": ["2"]}, "2": {"in": ["1"], "out": ["3"]},
                 "3": {"in": ["2"], "isKeystone": True}}
        self.assertEqual(tree_reroute_removals(nodes, {"1", "2", "3"}), [])


class JewelSocketTests(unittest.TestCase):
    def test_allocated_empty_socket_needs_no_path_and_rerouting_is_offered_at_the_cap(self):
        nodes = {"1": {"classStartIndex": 3, "out": ["2"]},
                 "2": {"in": ["1"], "out": ["3"], "isJewelSocket": True},
                 "3": {"in": ["2"], "out": ["4"]},
                 "4": {"in": ["3"], "isJewelSocket": True}}
        from passive_search import graph
        adjacency = graph(nodes, lambda node: True)
        options, summary = jewel_socket_options({"tree": {"nodes": nodes}}, {"archetype": "spell", "damageType": "cold",
                                                                              "skill": "x", "focus": "damage"},
                                                {"1", "2"}, {}, adjacency, remaining_points=2)
        self.assertEqual(options[0][:2], ("allocated", "2"))
        self.assertEqual(summary["allocatedEmpty"], 1)
        self.assertTrue(any(kind == "path" and key == "4" for kind, key, _, _ in options))


if __name__ == "__main__":
    unittest.main()
