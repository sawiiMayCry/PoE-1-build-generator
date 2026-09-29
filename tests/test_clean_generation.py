import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch, MagicMock

from build_assembly import assemble
from generation_data import GameData, RareItem, eligible, roll_line, solve_suffixes
from passive_search import graph, paths_from, mastery_choices, score, search_tree
from real_generator import budget_from_prompt, current_unique, named_skill, normalize_intent, validate_design, target_dps


def gem(name, tags, support=False):
    identifier = "Metadata/Items/Gems/" + ("SupportGem" if support else "SkillGem") + name.replace(" ", "")
    return {"id": identifier, "gameId": identifier, "name": name, "variantId": name.replace(" ", ""),
            "skillId": name.replace(" ", ""), "tags": dict.fromkeys(tags, True), "support": support, "maxLevel": 20}


def definitions():
    return GameData({"gems": [gem("Ethereal Knives", ["spell", "physical"]), gem("Frenzy", ["attack"]),
        gem("Winter Orb", ["spell", "cold"]), gem("Raise Zombie", ["spell", "minion"]),
        gem("Hatred", ["aura", "cold"]), gem("Flame Dash", ["movement"]), gem("Steelskin", ["guard"]),
        gem("Determination", ["aura"]), gem("Vulnerability", ["curse"]), gem("Frostbite", ["curse"]),
        gem("Malevolence", ["aura"]), gem("Flammability", ["curse"]),
        gem("Bane", ["hex", "chaos", "spell"]), gem("Bane of Condemnation", ["hex", "chaos", "spell"]),
        gem("Added Cold Damage", ["support"], True)], "bases": {}, "mods": []})


class CleanGeneration(unittest.TestCase):
    def test_main_skill_detection_excludes_utility_and_frenzy_charges(self):
        data = definitions()
        self.assertEqual(named_skill("ethereal knives with Hatred", data), "Ethereal Knives")
        self.assertIsNone(named_skill("cheap build with frenzy charges and hatred", data))
        self.assertEqual(named_skill("EK elementalist", data), "Ethereal Knives")
        self.assertEqual(named_skill("zombie necromancer", data), "Raise Zombie")
        self.assertEqual(named_skill("Bane of Condemnation Occultist", data), "Bane of Condemnation")

    def test_intent_preserves_explicit_skill_ascendancy_level_budget(self):
        spec = normalize_intent("level 89 Ethereal Knives elementalist, tanky, 20 div",
                                {"skill": "Winter Orb", "ascendancy": "Occultist", "focus": "garbage"},
                                definitions(), {"divineChaos": 200})
        self.assertEqual((spec["skill"], spec["ascendancy"], spec["level"], spec["budgetChaos"], spec["focus"]),
                         ("Ethereal Knives", "Elementalist", 89, 4000, "defense"))
        self.assertIsNone(budget_from_prompt("cheap build", 200))
        self.assertEqual(budget_from_prompt("20 chaos", 200), 20)

    def test_invalid_model_reply_has_a_deterministic_valid_fallback(self):
        spec = normalize_intent("cheap cold witch build", {"skill": "unknown", "ascendancy": "bad"},
                                definitions(), {"divineChaos": 200})
        self.assertEqual((spec["skill"], spec["ascendancy"]), ("Winter Orb", "Elementalist"))
        typed = normalize_intent("cheap cold witch build", {"skill": [], "ascendancy": [], "focus": {}},
                                 definitions(), {"divineChaos": 200})
        self.assertEqual((typed["skill"], typed["ascendancy"], typed["focus"]), ("Winter Orb", "Elementalist", "balanced"))
        with self.assertRaisesRegex(ValueError, "level"):
            normalize_intent("level 79 Winter Orb", {}, definitions(), {"divineChaos": 200})

    def test_requested_aura_replaces_default_and_unsupported_mechanics_fail(self):
        spec = normalize_intent("Ethereal Knives with Hatred", {}, definitions(), {"divineChaos": 200})
        self.assertIn("Hatred", spec["utility"])
        self.assertNotIn("Determination", spec["utility"])
        with self.assertRaisesRegex(ValueError, "Chaos Inoculation"):
            normalize_intent("Winter Orb chaos inoculation", {}, definitions(), {"divineChaos": 200})

    def test_graph_is_bidirectional_and_excludes_synthetic_root(self):
        nodes = {"root": {"out": ["1"]}, "1": {"out": ["2"]}, "2": {"in": ["1"], "out": ["3"]}, "3": {}}
        adjacency = graph(nodes, lambda _: True)
        self.assertNotIn("root", adjacency)
        self.assertEqual(paths_from({"1"}, adjacency)["3"], ["2", "3"])
        self.assertEqual(paths_from({"3"}, adjacency)["1"], ["2", "1"])

    def test_mastery_needs_an_allocated_notable_in_its_group(self):
        tree = {"nodes": {"1": {"group": 7, "isNotable": True}, "2": {"group": 7},
                          "3": {"group": 7, "isMastery": True, "masteryEffects": [{"effect": 12}]}}}
        self.assertEqual(mastery_choices({"tree": tree}, {"2"}), [])
        self.assertEqual(mastery_choices({"tree": tree}, {"1"}), [("3", [{"effect": 12}])])

    def test_affix_eligibility_uses_first_matching_weight_and_level(self):
        base = {"tags": {"weapon": True, "default": True}}
        mod = {"kind": "Prefix", "level": 1, "weightKey": ["weapon", "default"], "weightVal": [0, 1000]}
        self.assertFalse(eligible(mod, base))
        self.assertTrue(eligible(mod, {"tags": {"default": True}}))
        self.assertFalse(eligible({**mod, "level": 81}, {"tags": {"default": True}}))
        self.assertEqual(roll_line("+(30-35)% to Fire Resistance"), "+32% to Fire Resistance")

    def test_suffix_solver_never_overfills_or_duplicates_modifier_groups(self):
        mods = [{"id": str(index), "kind": "Suffix", "group": element, "level": 1,
                 "weightKey": ["default"], "weightVal": [1000], "lines": [f"+35% to {element} Resistance"]}
                for index, element in enumerate(("Fire", "Cold", "Lightning"))]
        data = GameData({"gems": [], "bases": {}, "mods": mods})
        items = [RareItem("Helmet", "base", {"tags": {"default": True}}),
                 RareItem("Boots", "base", {"tags": {"default": True}})]
        solve_suffixes(items, data, {"FireResist": 5, "ColdResist": 5, "LightningResist": 5})
        self.assertTrue(all(len(item.mods) == 3 for item in items))
        self.assertTrue(all(len({mod["group"] for mod in item.mods}) == 3 for item in items))
        self.assertEqual(solve_suffixes(items, data, {}), 0)

    def test_clean_assembly_uses_only_design_mechanics(self):
        data = definitions()
        context = {"treeVersion": "3_29", "tree": {"classes": [
            {"name": "Witch", "ascendancies": [{"name": "Elementalist"}]}]}}
        spec = {"skill": "Ethereal Knives", "ascendancy": "Elementalist", "level": 89, "utility": {}}
        item = RareItem("Body Armour", "Vaal Regalia", {"socketLimit": 6})
        root = ET.fromstring(assemble(spec, context, data, {"1", "2"}, ["Added Cold Damage"], [item]))
        self.assertEqual(root.find("Build").get("level"), "89")
        self.assertEqual(root.find("./Tree/Spec").get("nodes"), "1,2")
        self.assertEqual(len(root.findall("./Skills/SkillSet/Skill/Gem")), 2)
        self.assertEqual(root.find("./Items/ItemSet/Slot").get("itemId"), "1")
        self.assertFalse(root.findall(".//PlayerStat"))
        self.assertFalse(root.findall(".//Notes"))

    def test_current_unique_selects_current_base_and_mods(self):
        raw = "Example\n{variant:1}Old Base\n{variant:2}New Base\nVariant: Old\nVariant: Current\nImplicits: 2\n{variant:1}10% increased Damage\n{variant:2}20% increased Damage\n+(30-40) to maximum Life"
        text = current_unique(raw)
        self.assertIn("Example\nNew Base", text)
        self.assertNotIn("Old Base", text)
        self.assertIn("Implicits: 1\n20% increased Damage", text)
        self.assertIn("+35 to maximum Life", text)

    def test_tree_objective_prioritizes_measured_requirements(self):
        spec = {"archetype": "spell", "focus": "balanced"}
        stats = {"Life": 4000, "TotalDPS": 10000, "FireResist": 75, "ColdResist": 75, "LightningResist": 75,
                 "ReqDex": 150, "Dex": 150}
        self.assertGreater(score(stats, spec), score({**stats, "Dex": 50}, spec))

    def test_ignite_objective_does_not_optimize_unrequested_poison_damage(self):
        self.assertEqual(target_dps({"IgniteDPS": 100, "FullDotDPS": 1000}, {"archetype": "ignite"}), 100)

    def test_final_tree_search_respects_points_already_spent_on_masteries(self):
        # The raw regular-node count understates paid points after masteries.
        # PoB's count is authoritative, including additional mastery points.
        class Worker:
            def request(self, operation, **kwargs):
                self.assert_calculation(operation)
                return {"passives": {"used": 112, "maximum": 112}, "stats": {}}

            @staticmethod
            def assert_calculation(operation):
                if operation != "calculate":
                    raise AssertionError("A full tree must not score or add another node")

        nodes = {"1": {"classStartIndex": 3, "out": ["2"]},
                 "2": {"isNotable": True, "stats": ["10% increased maximum Life"]},
                 "3": {"isMastery": True}}
        allocated, calc = search_tree({"tree": {"nodes": nodes}}, {}, {"1", "3"},
                                       lambda _: "xml", Worker(), lambda _: None, reserve=0)
        self.assertEqual(allocated, {"1", "3"})
        self.assertEqual(calc["passives"]["used"], 112)

    def test_generated_tree_validation_rejects_disconnections_and_locked_masteries(self):
        nodes = {"1": {"classStartIndex": 3, "out": ["2"]},
                 "2": {"isNotable": True, "group": 7},
                 "3": {"isMastery": True, "group": 7, "masteryEffects": [{"effect": 12}]},
                 "4": {"isAscendancyStart": True, "ascendancyName": "Elementalist", "out": ["5"]},
                 "5": {"ascendancyName": "Elementalist"}, "6": {}}
        spec = {"ascendancy": "Elementalist", "utility": {}}
        checks = validate_design({"tree": {"nodes": nodes}}, spec, {"1", "2", "3", "4", "5"},
                                 {"3": 12}, [], {}, definitions())
        self.assertTrue(all(check["passed"] for check in checks))
        broken = validate_design({"tree": {"nodes": nodes}}, spec, {"1", "3", "4", "5", "6"},
                                 {"3": 12}, [], {}, definitions())
        self.assertFalse(next(check["passed"] for check in broken if check["name"] == "Connected passive tree"))
        self.assertFalse(next(check["passed"] for check in broken if check["name"] == "Legal mastery effects"))

    def test_public_generation_contract_and_export_roundtrip_without_references(self):
        from prompt_generator import generate
        from build_generator import decode_pob, encode_pob, mechanics_fingerprint
        from server import public
        spec = normalize_intent("Winter Orb elementalist", {}, definitions(), {"divineChaos": 200})
        xml = '<PathOfBuilding><Build name="Generated" level="90"/></PathOfBuilding>'
        details = {"level": 90, "gems": ["Winter Orb"], "treeNodes": 111, "ascendancyPoints": 8, "gear": []}
        worker = MagicMock()
        worker.request.return_value = {"gems": list(definitions().gems.values()), "bases": {}, "mods": []}
        context = {"league": "Test", "treeVersion": "3_29", "officialRelease": "3.29.1"}
        with patch("real_generator.game_context", return_value=context), \
                patch("real_generator.market_data", return_value={"divineChaos": 200}), \
                patch("real_generator.get_worker", return_value=worker), \
                patch("real_generator.parse_intent", return_value=spec), \
                patch("real_generator.build_design", return_value=(xml, {}, [], details, {"stats": {"Life": 4000}}, {})), \
                patch("prompt_generator.catalog", side_effect=AssertionError("Generation must not load references")):
            result = generate({"prompt": "Winter Orb elementalist", "model": "test-model"}, Path("."), Path("data"), lambda _: None)
        required = {"id", "name", "class", "ascendancy", "mainSkill", "level", "gems", "treeNodes", "ascendancyPoints",
                    "gear", "validation", "stats", "quote", "recipe", "modelUsed", "prompt", "league", "treeVersion",
                    "officialTreeRelease", "createdAt", "shareStatus", "shareUrl", "_xml", "_fingerprint"}
        self.assertTrue(required <= result.keys())
        self.assertRegex(result["id"], r"^g[0-9a-f]{20}$")
        self.assertEqual(result["mainSkill"], "Winter Orb")
        self.assertEqual(decode_pob(encode_pob(result["_xml"])), xml)
        self.assertEqual(mechanics_fingerprint(xml), result["_fingerprint"])
        self.assertNotIn("_xml", public(result))


if __name__ == "__main__":
    unittest.main()
