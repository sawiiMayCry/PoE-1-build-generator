import unittest
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
import tempfile
from unittest.mock import patch, MagicMock

from build_assembly import assemble, disable_conditional_item_skill_groups
from build_progression import mapping_unique_packages
from generation_data import GameData, RareItem, eligible, rare_templates, roll_line, solve_suffixes
from passive_search import (SearchBudget, graph, paths_from, initial_nodes, mastery_choices, score,
                            recounted_stats, search_tree, tree_pool_target, resource_deficit, heuristic,
                            temporary_minion_population)
from real_generator import (assess_mechanics, assess_quality, budget_from_prompt, current_unique, mechanic_profile,
                            calculate_finalist, named_skill, normalize_intent, ordinary_jewel_templates,
                            sync_permanent_minion_count, choose_support_candidate, search_curse, search_links,
                            missing_unique_reason, unique_jewel_shortlist, unique_options, mentioned_uniques,
                            release_repairable_suffixes, unique_pair_shortlist, tree_reroute_removals,
                            support_candidate_shortlist, support_refinement_order, reconcile_unique_package_trace,
                            support_gain_is_meaningful, support_mechanism_compatible, validate_design,
                            mapping_support_plan, clear_coverage_role, target_dps, complete_design_search,
                            sustained_resource_use, mana_utility_levels, search_mana_utility,
                            unique_pair_has_weapon_conflict, unique_package_within_budget,
                            mastery_reserve_for, keystone_reserve_for, keystone_package_candidates,
                            build_quality_report)


def gem(name, tags, support=False):
    identifier = "Metadata/Items/Gems/" + ("SupportGem" if support else "SkillGem") + name.replace(" ", "")
    return {"id": identifier, "gameId": identifier, "name": name, "variantId": name.replace(" ", ""),
            "skillId": name.replace(" ", ""), "tags": dict.fromkeys(tags, True), "support": support, "maxLevel": 20}


def definitions():
    return GameData({"gems": [gem("Ethereal Knives", ["spell", "physical"]), gem("Frenzy", ["attack"]),
        gem("Winter Orb", ["spell", "cold"]), gem("Raise Zombie", ["spell", "minion"]),
        gem("Summon Raging Spirit", ["spell", "minion", "fire"]),
        gem("Hatred", ["aura", "cold"]), gem("Flame Dash", ["movement"]), gem("Steelskin", ["guard"]),
        gem("Determination", ["aura"]), gem("Vulnerability", ["curse"]), gem("Frostbite", ["curse"]),
        gem("Malevolence", ["aura"]), gem("Flammability", ["curse"]),
        gem("Bane", ["hex", "chaos", "spell"]), gem("Bane of Condemnation", ["hex", "chaos", "spell"]),
        gem("Despair", ["curse", "chaos"]),
        gem("Added Cold Damage", ["support"], True)], "bases": {}, "mods": []})


class CleanGeneration(unittest.TestCase):
    def test_support_selection_trace_reports_sustain_candidates(self):
        supports = [gem(f"Support {index}", ["support"], True) for index in range(5)]
        data = GameData({"gems": [gem("Winter Orb", ["spell", "cold"]), *supports],
                         "bases": {}, "mods": []})
        identifiers = [entry["id"] for entry in supports]

        class Worker:
            def request(self, operation, **kwargs):
                if operation == "supports":
                    return {"supports": identifiers}
                if operation == "calculate":
                    return {"stats": {"FullDPS": 100, "ManaUnreserved": 100,
                                       "ManaCost": 1, "ManaRegen": 10,
                                       "LifeUnreserved": 100, "Life": 100, "Speed": 1}}
                if operation == "supportScores":
                    return {"candidates": [{"id": identifier,
                                             "stats": {"FullDPS": 1000, "ManaUnreserved": 100,
                                                       "ManaCost": 1, "ManaRegen": 10,
                                                       "LifeUnreserved": 100, "Life": 100, "Speed": 1}}
                                            for identifier in kwargs["candidates"]]}
                raise AssertionError(operation)

        trace = []
        selected = search_links({"skill": "Winter Orb", "archetype": "spell", "focus": "damage",
                                 "damageType": "cold", "baseDamageType": "cold"},
                                data, lambda links: "<test />", Worker(), lambda _: None, trace=trace)
        self.assertEqual(len(selected), 5)
        self.assertEqual(len(trace), 5)
        self.assertEqual(trace[0]["sustainableCandidates"], 5)

    def test_support_selection_stops_before_a_compatible_but_useless_support(self):
        supports = [gem(f"Support {index}", ["support"], True) for index in range(5)]
        data = GameData({"gems": [gem("Winter Orb", ["spell", "cold"]), *supports],
                         "bases": {}, "mods": []})
        identifiers = [entry["id"] for entry in supports]

        class Worker:
            def request(self, operation, **kwargs):
                count = int(re.search(r"count='(\d+)'", kwargs.get("xml", "<x count='0'/>"))[1])
                baseline = 100 + count * 100
                if operation == "supports":
                    return {"supports": identifiers}
                if operation == "calculate":
                    return {"stats": {"FullDPS": baseline, "ManaUnreserved": 100,
                                       "ManaCost": 1, "ManaRegen": 10, "LifeUnreserved": 100,
                                       "Life": 100, "Speed": 1}}
                if operation == "supportScores":
                    gain = 0 if count >= 3 else 100
                    return {"candidates": [{"id": identifier,
                                             "stats": {"FullDPS": baseline + gain,
                                                       "ManaUnreserved": 100, "ManaCost": 1,
                                                       "ManaRegen": 10, "LifeUnreserved": 100,
                                                       "Life": 100, "Speed": 1}}
                                            for identifier in kwargs["candidates"]]}
                raise AssertionError(operation)

        trace = []
        selected = search_links({"skill": "Winter Orb", "archetype": "spell", "focus": "damage",
                                 "damageType": "cold", "baseDamageType": "cold"},
                                data, lambda links: f"<Link count='{len(links)}' />", Worker(),
                                lambda _: None, trace=trace)
        self.assertEqual(len(selected), 3)
        self.assertIn("at least 0.5%", trace[-1]["reason"])

    def test_exact_exported_srs_regression_fixture_captures_unsupported_link(self):
        fixture = Path(__file__).parent / "fixtures" / "srs_generated_20261001.xml"
        root = ET.parse(fixture).getroot()
        active = root.find("Skills").get("activeSkillSet")
        skill_set = next(entry for entry in root.findall("./Skills/SkillSet") if entry.get("id") == active)
        main = next(entry for entry in skill_set.findall("Skill")
                    if entry.get("includeInFullDPS") == "true")
        names = [gem.get("nameSpec") for gem in main.findall("Gem")]
        self.assertEqual(names, ["Summon Raging Spirit", "Added Chaos Damage", "Chance to Poison",
                                 "Block Chance Reduction", "Inspiration"])
        utilities = [gem.get("nameSpec") for entry in skill_set.findall("Skill") if entry is not main
                     for gem in entry.findall("Gem")]
        self.assertIn("Flammability", utilities)
        self.assertFalse(support_gain_is_meaningful(264735.38, 265681.29))
        self.assertFalse(support_mechanism_compatible(
            gem("Chance to Poison", ["support"], True),
            {"damageMechanism": "minion_hit"}))
        population = temporary_minion_population(
            {"ManaCost": 20, "ManaRegen": 32.1, "Speed": 2.92, "Duration": 8.217,
             "ActiveMinionLimit": 20},
            {"skill": "Summon Raging Spirit", "resourceReserveFraction": 0.15})
        self.assertEqual(population, (11, True))

    def test_mapping_coverage_is_reported_separately_from_boss_support_damage(self):
        plan = mapping_support_plan([{"kind": "support_selection", "link_index": 2,
            "selected": "Minion Damage", "candidates": [
                {"name": "Minion Damage", "damage": 100000, "sustainedDamage": 100000, "feasible": True},
                {"name": "Melee Splash", "damage": 80000, "sustainedDamage": 80000, "feasible": True},
                {"name": "Block Chance Reduction", "damage": 0, "sustainedDamage": 0, "feasible": True}]}],
            ["Minion Damage"])
        self.assertEqual(clear_coverage_role("Melee Splash"), "area coverage")
        self.assertEqual(plan["bossLink"], ["Minion Damage"])
        self.assertEqual(plan["mappingAlternatives"][0]["support"], "Melee Splash")
        self.assertEqual(plan["mappingAlternatives"][0]["singleTargetRetention"], 0.8)
        self.assertIn("not converted into single-target DPS", plan["coverageModel"])

    def test_damage_focus_support_choice_accounts_for_long_run_cast_uptime(self):
        entries = [
            {"id": "damage", "stats": {"FullDPS": 100_000, "ManaCost": 10,
                                            "ManaRegen": 60, "Speed": 10}},
            {"id": "sustain", "stats": {"FullDPS": 60_000, "ManaCost": 5,
                                            "ManaRegen": 60, "Speed": 10}},
        ]
        selected, sustain, accepted = choose_support_candidate(
            entries, {"skill": "Winter Orb", "archetype": "spell", "focus": "damage"})
        self.assertFalse(accepted)
        self.assertEqual(selected["id"], "sustain")
        self.assertFalse(sustain["damage"]["sustainable"])
        self.assertTrue(sustain["sustain"]["sustainable"])

    def test_support_shortlist_uses_installed_gem_tags_before_full_scoring(self):
        data = GameData({"gems": [
            gem("Raise Zombie", ["spell", "minion", "attack", "melee"]),
            gem("Ethereal Knives", ["spell", "projectile", "physical"]),
            gem("Minion Damage", ["minion", "support"], True),
            gem("Melee Physical Damage", ["attack", "melee", "physical", "support"], True),
            gem("Chance to Poison", ["chaos", "support"], True),
            gem("Sadism", ["support"], True),
            gem("Cold Penetration", ["cold", "support"], True),
            gem("Item Rarity", ["support"], True)], "bases": {}, "mods": []})
        ids = [entry["id"] for entry in data.gems.values() if entry["support"]]
        chosen = support_candidate_shortlist(ids, data, {"skill": "Raise Zombie", "archetype": "minion",
                                                           "damageType": "physical"}, limit=2)
        names = [data.by_id[identifier]["name"] for identifier in chosen]
        self.assertEqual(names, ["Melee Physical Damage", "Minion Damage"])
        expanded = support_candidate_shortlist(ids, data, {"skill": "Raise Zombie", "archetype": "minion",
                                                            "damageType": "physical"}, limit=3)
        self.assertIn("Chance to Poison", [data.by_id[identifier]["name"] for identifier in expanded])
        ignite = support_candidate_shortlist(ids, data, {"skill": "Ethereal Knives", "archetype": "ignite",
                                                          "damageType": "fire"}, limit=2)
        self.assertIn("Sadism", [data.by_id[identifier]["name"] for identifier in ignite])

    def test_late_support_refinement_keeps_resource_options_and_ranks_sustained_damage(self):
        supports = [gem(name, ["support"], True) for name in
                    ("High Raw Damage", "Lifetap", "Inspiration", "Sustained Damage")]
        data = GameData({"gems": supports, "bases": {}, "mods": []})
        identifiers = [entry["id"] for entry in supports]
        initial = [
            {"name": "High Raw Damage", "damage": 100_000,
             "sustainability": {"sustainable": False, "checks": [
                 {"usePerSecond": 100, "availablePerSecond": 10}]}},
            {"name": "Sustained Damage", "damage": 20_000,
             "sustainability": {"sustainable": True, "checks": []}},
        ]
        ordered = support_refinement_order(identifiers, data, initial, limit=4)
        names = [data.by_id[identifier]["name"] for identifier in ordered]
        self.assertEqual(names[:2], ["Inspiration", "Lifetap"])
        self.assertEqual(names[2:], ["Sustained Damage", "High Raw Damage"])

    def test_sustained_resource_use_uses_cast_rate_and_temporary_population_model(self):
        unsustainable_spell = sustained_resource_use(
            {"ManaCost": 10, "ManaRegen": 24.2, "Speed": 7},
            {"skill": "Winter Orb", "archetype": "spell"})
        self.assertFalse(unsustainable_spell["sustainable"])
        self.assertAlmostEqual(unsustainable_spell["checks"][0]["usePerSecond"], 70)

        life_sustained = sustained_resource_use(
            {"ManaCost": 0, "LifeCost": 5, "LifeRegenRecovery": 20, "Speed": 3},
            {"skill": "Ethereal Knives", "archetype": "ignite"})
        self.assertTrue(life_sustained["sustainable"])

        srs = sustained_resource_use(
            {"ManaCost": 35, "ManaRegen": 35.7, "Speed": 2.26, "Duration": 8.2,
             "ActiveMinionLimit": 20},
            {"skill": "Summon Raging Spirit", "archetype": "minion", "minionCount": 20})
        self.assertTrue(srs["sustainable"])
        self.assertEqual(srs["population"], 7)

    def test_clarity_search_chooses_lowest_level_that_sustains_mana(self):
        clarity = gem("Clarity", ["aura", "spell"])
        clarity["levels"] = [{"level": level, "requiredLevel": level, "int": 0, "dex": 0, "str": 0}
                              for level in range(1, 6)]
        data = GameData({"gems": [clarity], "bases": {}, "mods": []})
        item = RareItem("Helmet", "Hubris Circlet", {"socketLimit": 4})
        spec = {"skill": "Winter Orb", "archetype": "spell", "damageType": "cold",
                "focus": "damage", "level": 90, "utility": {"Malevolence": "Helmet"}}

        def stats(level):
            return {"Life": 4000, "EnergyShield": 2000, "TotalEHP": 20000,
                    "FullDPS": 10000, "ManaCost": 10, "ManaRegen": 15 + level * 10,
                    "Speed": 5, "ManaUnreserved": 50 - level * 10, "LifeCost": 0,
                    "LifeRegenRecovery": 50, "FireResist": 75, "ColdResist": 75,
                    "LightningResist": 75, "Str": 100, "Dex": 100, "Int": 100,
                    "ReqStr": 0, "ReqDex": 0, "ReqInt": 0}

        baseline = {"calculated": True, "stats": stats(0),
                    "passives": {"used": 30, "maximum": 100, "ascendancy": 8, "secondaryAscendancy": 0}}

        class Worker:
            def request(self, operation, **kwargs):
                level = int(kwargs["xml"].split("=", 1)[1])
                return {"calculated": True, "stats": stats(level), "passives": baseline["passives"]}

        budget, trace = SearchBudget(100), []
        result = search_mana_utility(spec, data, [item],
                                     lambda: f"level={spec['gemLevels']['Clarity']}",
                                     Worker(), baseline, budget, trace)
        self.assertEqual(spec["utility"]["Clarity"], "Helmet")
        self.assertEqual(spec["gemLevels"]["Clarity"], 4)
        self.assertEqual(result["stats"]["ManaRegen"], 55)
        selected = next(entry for entry in trace if entry["kind"] == "resource_utility_search")
        self.assertFalse(selected["sustainable"])
        self.assertEqual(selected["selectedLevel"], 4)
        self.assertEqual(budget.used, 5)

    def test_clarity_search_repairs_unpayable_cast_even_if_score_falls(self):
        clarity = gem("Clarity", ["aura", "spell"])
        clarity["levels"] = [{"level": level, "requiredLevel": level, "int": 0, "dex": 0, "str": 0}
                              for level in range(1, 6)]
        data = GameData({"gems": [clarity], "bases": {}, "mods": []})
        item = RareItem("Helmet", "Hubris Circlet", {"socketLimit": 4})
        spec = {"skill": "Winter Orb", "archetype": "spell", "damageType": "cold",
                "focus": "damage", "level": 90, "utility": {"Malevolence": "Helmet"},
                "gemLevels": {"Clarity": 20}, "_automaticClarity": True}

        def stats(level):
            return {"Life": 4000, "EnergyShield": 2000, "TotalEHP": 20000,
                    "FullDPS": 10000, "ManaCost": 10, "ManaRegen": 15 + level * 10,
                    "Speed": 5, "ManaUnreserved": max(0, 50 - level * 10), "LifeCost": 0,
                    "LifeRegenRecovery": 50, "FireResist": 75, "ColdResist": 75,
                    "LightningResist": 75, "Str": 100, "Dex": 100, "Int": 100,
                    "ReqStr": 0, "ReqDex": 0, "ReqInt": 0}

        baseline_stats = stats(20)
        baseline_stats["ManaRegen"] = 1000
        baseline_stats["ManaUnreserved"] = 0
        baseline = {"calculated": True, "stats": baseline_stats,
                    "passives": {"used": 30, "maximum": 100, "ascendancy": 8,
                                 "secondaryAscendancy": 0}}

        class Worker:
            def request(self, operation, **kwargs):
                level = int(kwargs["xml"].split("=", 1)[1])
                return {"calculated": True, "stats": stats(level),
                        "passives": baseline["passives"]}

        trace = []
        result = search_mana_utility(spec, data, [item],
                                     lambda: f"level={spec['gemLevels']['Clarity']}",
                                     Worker(), baseline, SearchBudget(100), trace)
        self.assertEqual(spec["gemLevels"]["Clarity"], 4)
        self.assertGreaterEqual(result["stats"]["ManaUnreserved"], result["stats"]["ManaCost"])
        selected = next(entry for entry in trace if entry["kind"] == "resource_utility_search")
        self.assertEqual(selected["reason"], "restored a payable main-skill cast")

    def test_tree_objective_values_sustain_and_prioritizes_relevant_passives(self):
        spec = {"skill": "Winter Orb", "archetype": "spell", "focus": "balanced",
                "damageType": "cold"}
        base = {"FullDPS": 100000, "Life": 4000, "EnergyShield": 2500, "TotalEHP": 22000,
                "ManaCost": 10, "Speed": 7, "ManaRegen": 24, "LifeCost": 0,
                "FireResist": 75, "ColdResist": 75, "LightningResist": 75}
        sustained = dict(base, ManaRegen=100)
        self.assertIsNotNone(resource_deficit(base, spec))
        self.assertIsNone(resource_deficit(sustained, spec))
        self.assertGreater(score(sustained, spec), score(base, spec))
        self.assertGreater(score(sustained, spec) - score(base, spec), 0.8,
                           "sustained damage should materially outweigh mana-starved tooltip DPS")
        regen_node = {"stats": ["20% increased Mana Regeneration Rate"]}
        self.assertGreater(heuristic(regen_node, {**spec, "_resourceSustainGap": True}),
                           heuristic(regen_node, spec))

    def test_spell_rare_templates_add_skill_specific_offense_from_pob_data(self):
        metadata = json.loads((Path(__file__).resolve().parents[1] / "data" / "pob_metadata.json")
                              .read_text(encoding="utf-8"))
        data = GameData(metadata)
        cases = (("ignite", "fire", "physical", "Physical", "Fire", "Damage over Time Multiplier"),
                 ("spell", "cold", "cold", "Cold", "Cold", "Spell Damage"))
        for archetype, damage_type, base_type, gem_type, element, amulet_mod in cases:
            with self.subTest(skill_type=archetype):
                items = rare_templates(data, archetype, "Wand", damage_type=damage_type,
                                       base_damage_type=base_type, focus="damage")
                weapon = next(item for item in items if item.slot == "Weapon 1")
                shield = next(item for item in items if item.slot == "Weapon 2")
                amulet = next(item for item in items if item.slot == "Amulet")
                ring = next(item for item in items if item.slot == "Ring 1")
                weapon_lines = " ".join(line for mod in weapon.mods for line in mod["lines"])
                shield_lines = " ".join(line for mod in shield.mods for line in mod["lines"])
                amulet_lines = " ".join(line for mod in amulet.mods for line in mod["lines"])
                ring_lines = " ".join(line for mod in ring.mods for line in mod["lines"])
                self.assertIn(f"Level of all {gem_type} Spell Skill Gems", weapon_lines)
                self.assertIn(f"increased {element} Damage", weapon_lines)
                self.assertIn(f"increased {element} Damage", shield_lines)
                self.assertIn(f"Level of all {gem_type} Skill Gems", amulet_lines)
                self.assertIn(amulet_mod, amulet_lines)
                self.assertIn(f"increased {element} Damage", ring_lines)
                self.assertLessEqual(sum(mod["kind"] == "Prefix" for mod in weapon.mods), 3)
                self.assertLessEqual(sum(mod["kind"] == "Suffix" for mod in weapon.mods), 3)

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
        for phrase, expected in (("damage focus", "damage"), ("focus on damage", "damage"),
                                 ("defense focus", "defense"), ("focused on defense", "defense")):
            with self.subTest(focus_phrase=phrase):
                parsed = normalize_intent(f"Level 100 Summon Raging Spirit Necromancer, {phrase}", {},
                                          definitions(), {"divineChaos": 200})
                self.assertEqual(parsed["focus"], expected)

    def test_rare_only_wording_disables_unique_search(self):
        for wording in ("rare items only", "rares only", "rare-only", "no unique items"):
            with self.subTest(wording=wording):
                spec = normalize_intent(f"Level 90 Winter Orb Elementalist, {wording}", {}, definitions(),
                                        {"divineChaos": 200})
                self.assertTrue(spec["noUniques"])
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

    def test_curse_choices_follow_the_requested_damage_mechanism(self):
        cold = normalize_intent("Level 90 Winter Orb Elementalist", {}, definitions(),
                                {"divineChaos": 200})
        self.assertIn("Frostbite", cold["utility"])
        self.assertNotIn("Flammability", cold["utility"])
        minion = normalize_intent("Level 90 Raise Zombie Necromancer", {}, definitions(),
                                  {"divineChaos": 200})
        self.assertIn("Vulnerability", minion["utility"])
        self.assertNotIn("Frostbite", minion["utility"])

    def test_srs_hit_and_poison_requests_get_distinct_mechanism_packages(self):
        hit = normalize_intent("Level 90 Summon Raging Spirit Necromancer", {}, definitions(),
                               {"divineChaos": 200})
        poison = normalize_intent("Level 90 Summon Raging Spirit Necromancer poison", {}, definitions(),
                                  {"divineChaos": 200})
        self.assertEqual(hit["damageMechanism"], "minion_hit")
        self.assertEqual(hit["damageType"], "fire")
        self.assertIn("Flammability", hit["utility"])
        self.assertEqual(poison["damageMechanism"], "poison")
        self.assertEqual(poison["baseDamageType"], "fire")
        self.assertEqual(poison["damageType"], "chaos")
        self.assertIn("Despair", poison["utility"])
        poison_support = gem("Chance to Poison", ["support"], True)
        self.assertFalse(support_mechanism_compatible(poison_support, hit))
        self.assertTrue(support_mechanism_compatible(poison_support, poison))

    def test_curse_search_uses_measured_damage_and_keeps_one_curse_slot(self):
        data = GameData({"gems": [gem(name, tags) for name, tags in (
            ("Flammability", ["curse"]), ("Despair", ["curse", "chaos"]),
            ("Vulnerability", ["curse"]))], "bases": {}, "mods": []})
        spec = {"skill": "Winter Orb", "archetype": "spell", "focus": "balanced",
                "damageType": "fire", "damageMechanism": "spell_hit", "expectedCurse": "Flammability",
                "utility": {"Flammability": "Gloves"}}
        scores = {"Flammability": 100.0, "Despair": 120.0, "Vulnerability": 90.0}

        class Worker:
            def request(self, operation, **kwargs):
                root = ET.fromstring(kwargs["xml"])
                name = next(gem.get("nameSpec") for gem in root.findall("./Skills/SkillSet/Skill/Gem"))
                return {"calculated": True, "stats": {"FullDPS": scores[name], "ManaCost": 1,
                         "ManaRegen": 10, "Speed": 1, "ManaUnreserved": 100}}

        def render():
            return f'''<PathOfBuilding><Skills><SkillSet id="1"><Skill><Gem nameSpec="{next(iter(spec['utility']))}"/></Skill>
              </SkillSet></Skills></PathOfBuilding>'''

        trace = []
        selected = search_curse(spec, data, render, Worker(),
                                {"calculated": True, "stats": {"FullDPS": 100.0,
                                 "ManaCost": 1, "ManaRegen": 10, "Speed": 1, "ManaUnreserved": 100}},
                                trace=trace)
        self.assertEqual(spec["expectedCurse"], "Despair")
        self.assertEqual(spec["utility"], {"Despair": "Gloves"})
        self.assertEqual(selected["stats"]["FullDPS"], 120.0)
        self.assertEqual(trace[0]["selected"], "Despair")

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

    def test_ascendancy_does_not_fill_missing_profile_notables_arbitrarily(self):
        nodes = {"1": {"classStartIndex": 3},
                 "10": {"ascendancyName": "Elementalist", "isAscendancyStart": True, "out": ["11"]},
                 "11": {"ascendancyName": "Elementalist", "isNotable": True, "name": "Unrelated notable",
                        "in": ["10"]}}
        allocated = initial_nodes({"tree": {"nodes": nodes}},
                                  {"ascendancy": "Elementalist", "damageType": "cold", "archetype": "spell"})
        self.assertEqual(allocated, {"1", "10"})

    def test_physical_occultist_does_not_take_cold_frigid_wake(self):
        names = ("Frigid Wake", "Profane Bloom", "Unholy Authority", "Withering Presence", "Void Beacon")
        nodes = {"1": {"classStartIndex": 3},
                 "10": {"ascendancyName": "Occultist", "isAscendancyStart": True,
                        "out": [str(value) for value in range(11, 16)]}}
        for value, name in enumerate(names, 11):
            nodes[str(value)] = {"ascendancyName": "Occultist", "name": name,
                                 "isNotable": True, "in": ["10"]}
        allocated = initial_nodes({"tree": {"nodes": nodes}},
                                  {"ascendancy": "Occultist", "damageType": "physical", "archetype": "minion"})
        self.assertNotIn("11", allocated)
        self.assertIn("12", allocated)

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

    def test_assembly_equips_requested_unique_flask_as_inactive(self):
        data = definitions()
        flask_base = {"type": "Flask", "subType": "Utility", "socketLimit": 0, "tags": {}}
        data.bases["Granite Flask"] = flask_base
        context = {"treeVersion": "3_29", "tree": {"classes": [
            {"name": "Witch", "ascendancies": [{"name": "Elementalist"}]}]}}
        spec = {"skill": "Ethereal Knives", "ascendancy": "Elementalist", "level": 89, "utility": {}}
        flask = "Rarity: UNIQUE\nRumi's Concoction\nGranite Flask\nImplicits: 0"
        items = [RareItem("Body Armour", "Vaal Regalia", {"socketLimit": 6}),
                 RareItem("Flask 2", "Granite Flask", flask_base)]
        root = ET.fromstring(assemble(spec, context, data, {"1"}, [], items,
                                     uniques={"Flask 2": flask},
                                     jewels={"40": "Rarity: UNIQUE\nThe Unnatural Instinct\nCobalt Jewel"}))
        slot = root.find("./Items/ItemSet/Slot[@name='Flask 2']")
        self.assertEqual(slot.get("active"), "false")
        item = next(item for item in root.findall("./Items/Item")
                    if (item.text or "").startswith("Rarity: UNIQUE"))
        self.assertIn("Rumi's Concoction", item.text)
        socket = root.find("./Tree/Spec/Sockets/Socket")
        self.assertNotEqual(socket.get("itemId"), item.get("id"))
        self.assertEqual(root.find(f"./Items/Item[@id='{socket.get('itemId')}']").text.splitlines()[1],
                         "The Unnatural Instinct")
        self.assertEqual(len(root.findall("./Items/ItemSet/Slot")), 2)

    def test_two_handed_unique_removes_or_rejects_offhand_equipment(self):
        data = definitions()
        data.bases.update({"Judgement Staff": {"type": "Staff", "tags": {"two_hand_weapon": True},
                                                "socketLimit": 6},
                           "Titanium Spirit Shield": {"type": "Shield", "tags": {"shield": True},
                                                       "socketLimit": 3}})
        context = {"treeVersion": "3_29", "tree": {"classes": [
            {"name": "Witch", "ascendancies": [{"name": "Elementalist"}]}]}}
        spec = {"skill": "Winter Orb", "ascendancy": "Elementalist", "level": 90,
                "utility": {}}
        staff = "Rarity: UNIQUE\nExample Staff\nJudgement Staff"
        items = [RareItem("Weapon 1", "Judgement Staff", data.bases["Judgement Staff"]),
                 RareItem("Weapon 2", "Titanium Spirit Shield", data.bases["Titanium Spirit Shield"])]
        xml = assemble(spec, context, data, {"1", "2"}, ["Added Cold Damage"], items,
                       uniques={"Weapon 1": staff})
        slots = ET.fromstring(xml).findall("./Items/ItemSet/Slot")
        self.assertEqual([slot.get("name") for slot in slots], ["Weapon 1"])
        with self.assertRaisesRegex(ValueError, "two-handed Weapon 1"):
            assemble(spec, context, data, {"1", "2"}, ["Added Cold Damage"], items,
                     uniques={"Weapon 1": staff,
                              "Weapon 2": "Rarity: UNIQUE\nExample Shield\nTitanium Spirit Shield"})
        pair = (("Example Staff", "Weapon 1", staff, None, False),
                ("Example Shield", "Weapon 2", "Rarity: UNIQUE\nExample Shield\nTitanium Spirit Shield",
                 None, False))
        self.assertTrue(unique_pair_has_weapon_conflict(*pair, data))

    def test_current_unique_selects_current_base_and_mods(self):
        raw = "Example\n{variant:1}Old Base\n{variant:2}New Base\nVariant: Old\nVariant: Current\nImplicits: 2\n{variant:1}10% increased Damage\n{variant:2}20% increased Damage\n+(30-40) to maximum Life"
        text = current_unique(raw)
        self.assertIn("Example\nNew Base", text)
        self.assertNotIn("Old Base", text)
        self.assertIn("Implicits: 1\n20% increased Damage", text)
        self.assertIn("+35 to maximum Life", text)

    def test_unique_candidates_cover_matching_slots_and_respect_explicit_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            unique_dir = home / "Data" / "Uniques"
            unique_dir.mkdir(parents=True)
            (unique_dir / "UniqueRings.lua").write_text(
                'itemBases = { [[Example\nIron Ring\nImplicits: 0]] }', encoding="utf-8")
            base = {"type": "Ring", "subType": "Ring", "socketLimit": 0}
            data = GameData({"gems": [], "bases": {"Iron Ring": base}, "mods": []})
            items = [RareItem(slot, "Iron Ring", base) for slot in ("Ring 1", "Ring 2")]
            context = {"pobHome": home}
            market = {"prices": {"Example": [12.0]},
                      "listings": {"Example": [{"chaos": 12.0, "variant": None, "links": None}]}}
            self.assertEqual(mentioned_uniques("Use Example", context), ["Example"])
            self.assertEqual(mentioned_uniques(
                "Raise Zombies with Queen's Hunger",
                context,
                [{"name": "The Queen's Hunger"}]), ["The Queen's Hunger"])
            affordable = unique_options(context, {"noUniques": False, "budgetChaos": 20}, market, items, data)
            self.assertEqual([entry[1] for entry in affordable], ["Ring 1", "Ring 2"])
            unaffordable = unique_options(context, {"noUniques": False, "budgetChaos": 10}, market, items, data)
            self.assertEqual(unaffordable, [])

    def test_unique_weapon_search_keeps_all_skill_compatible_weapon_subtypes(self):
        data = GameData({"gems": [], "bases": {
            "Prophecy Wand": {"type": "Wand", "socketLimit": 3},
            "Imperial Skean": {"type": "Dagger", "subType": "Rune Dagger", "socketLimit": 3},
        }, "mods": []})
        weapon = RareItem("Weapon 1", "Prophecy Wand", data.bases["Prophecy Wand"])
        divinarius = {"name": "Divinarius", "base": "Imperial Skean", "type": "Dagger",
                      "subType": "Rune Dagger", "raw": "Rarity: UNIQUE\nDivinarius\nImperial Skean"}
        context = {"pobHome": Path(".")}
        market = {"listings": {}}
        unrestricted = unique_options(context, {"noUniques": False, "budgetChaos": None,
                                                "weaponTypes": []}, market, [weapon], data,
                                     unique_defs=[divinarius])
        self.assertEqual([(entry[0], entry[1]) for entry in unrestricted], [("Divinarius", "Weapon 1")])
        wand_only = unique_options(context, {"noUniques": False, "budgetChaos": None,
                                             "weaponTypes": ["Wand"]}, market, [weapon], data,
                                  unique_defs=[divinarius])
        self.assertEqual(wand_only, [])

    def test_unique_package_shortlist_includes_synergy_and_distinct_slots(self):
        option = lambda name, slot, stats="": (name, slot,
            f"Rarity: UNIQUE\n{name}\n{slot} Base\n{stats}", None)
        candidates = [option("Aegis", "Helmet", "Reservation Efficiency"),
                      option("Conflux", "Ring 1", "Minions deal increased Damage"),
                      option("Conflux II", "Ring 1"), option("Ashes", "Amulet", "Skill Reservation"),
                      option("The Baron", "Helmet", "Strength grants Minions increased Damage"),
                      option("Shaper's Touch", "Gloves", "Evasion Rating per Strength")]
        packages = unique_pair_shortlist(candidates, {}, "minion", "physical", limit=20)
        names = [{first[0], second[0]} for first, second in packages]
        self.assertEqual(names[0], {"The Baron", "Shaper's Touch"})
        self.assertIn({"Aegis", "Conflux"}, names)
        self.assertNotIn({"Conflux", "Conflux II"}, names)
        duplicated = unique_pair_shortlist(
            [option("Conflux", "Ring 1"), option("Conflux", "Ring 2"),
             option("Ashes", "Amulet")], {}, "minion", "physical", limit=20)
        self.assertFalse(any(first[0] == second[0] for first, second in duplicated))
        selected = {"Helmet": "Rarity: UNIQUE\nAegis\nHelmet Base"}
        anchored = unique_pair_shortlist(candidates, selected, "minion", "physical", limit=20)
        self.assertTrue(any({first[0], second[0]} == {"Aegis", "Conflux"}
                            for first, second in anchored))

    def test_unique_package_budget_is_cumulative_and_unknown_prices_stay_unknown(self):
        self.assertTrue(unique_package_within_budget([60], [35], 100))
        self.assertFalse(unique_package_within_budget([60], [41], 100))
        self.assertFalse(unique_package_within_budget([60], [None], 100))
        self.assertTrue(unique_package_within_budget([None], [None], None))

    def test_unique_package_trace_reports_final_retention_not_seed_status(self):
        trace = [{"kind": "unique_package", "items": ["The Queen's Hunger", "The Devouring Diadem"],
                  "evaluated": True, "eligible": True, "bestPackage": True, "selected": False}]
        reconcile_unique_package_trace(trace, {"The Queen's Hunger"})
        self.assertFalse(trace[0]["selected"])
        self.assertIn("not retained", trace[0]["reason"])
        reconcile_unique_package_trace(trace, {"The Queen's Hunger", "The Devouring Diadem"})
        self.assertTrue(trace[0]["selected"])
        self.assertIn("verified complete design", trace[0]["reason"])

    def test_unique_package_repair_releases_only_rebuildable_suffixes(self):
        item = RareItem("Ring 1", "Coral Ring", {"tags": {}})
        item.mods = [
            {"kind": "Suffix", "lines": ["+32% to Fire Resistance"]},
            {"kind": "Suffix", "lines": ["+46 to Strength"]},
            {"kind": "Suffix", "lines": ["Minions have 15% increased Attack and Cast Speed"]},
        ]
        release_repairable_suffixes([item])
        self.assertEqual([mod["lines"][0] for mod in item.mods],
                         ["Minions have 15% increased Attack and Cast Speed"])

    def test_tree_reroute_removals_include_disconnected_branches_and_protect_jewels(self):
        nodes = {
            "1": {"classStartIndex": 3, "out": ["2"]},
            "2": {"in": ["1"], "out": ["3", "4"]},
            "3": {"in": ["2"]},
            "4": {"in": ["2"], "out": ["5"]},
            "5": {"in": ["4"], "isJewelSocket": True},
        }
        options = tree_reroute_removals(nodes, {"1", "2", "3", "4", "5"})
        self.assertIn({"4", "5"}, options)
        protected = tree_reroute_removals(nodes, {"1", "2", "3", "4", "5"}, {"5": "Rare Jewel"})
        self.assertNotIn({"4", "5"}, protected)

    def test_unique_jewel_shortlist_obeys_rares_only_and_required_names(self):
        catalog = [{"name": "Direct Jewel", "type": "Jewel"},
                   {"name": "Ordinary Helmet", "type": "Helmet"},
                   {"name": "Transformed Jewel", "type": "Jewel"}]
        self.assertEqual(unique_jewel_shortlist(catalog, True, {"Direct Jewel"}), [])
        self.assertEqual([item["name"] for item in unique_jewel_shortlist(
            catalog, False, {"Transformed Jewel"})], ["Transformed Jewel"])

    def test_missing_unique_reports_budget_quote_reason(self):
        ring = {"name": "Example Ring", "type": "Ring"}
        self.assertIn("no usable variant-specific market quote",
                      missing_unique_reason("Example Ring", [ring], {"listings": {}}, 10))

    def test_unique_flasks_are_candidates_for_each_flask_slot(self):
        base = {"type": "Flask", "subType": "Utility", "socketLimit": 0, "tags": {}}
        data = GameData({"gems": [], "bases": {"Granite Flask": base}, "mods": []})
        raw = "Rarity: UNIQUE\nRumi's Concoction\nGranite Flask\nImplicits: 0"
        definition = {"name": "Rumi's Concoction", "base": "Granite Flask", "type": "Flask",
                      "subType": "Utility", "raw": raw, "selectedVariantLabel": "Current"}
        items = [RareItem(f"Flask {index}", "Granite Flask", base) for index in range(1, 6)]
        spec = {"noUniques": False, "budgetChaos": None, "mainLinks": 6, "utility": {},
                "utilitySockets": 4, "weaponTypes": [], "damageType": "physical"}
        options = unique_options({}, spec, {"listings": {}}, items, data, [definition])
        self.assertEqual([option[1] for option in options], [f"Flask {index}" for index in range(1, 6)])

    def test_armor_unique_slot_does_not_require_matching_base_subtype(self):
        base = {"type": "Helmet", "subType": "Intelligence", "socketLimit": 4}
        close_helmet = {"type": "Helmet", "subType": "Strength", "socketLimit": 4}
        data = GameData({"gems": [], "bases": {"Hubris Circlet": base, "Close Helmet": close_helmet}, "mods": []})
        items = [RareItem("Helmet", "Hubris Circlet", base)]
        definition = {"name": "The Baron", "base": "Close Helmet", "type": "Helmet",
                      "subType": "Strength", "raw": "Rarity: UNIQUE\nThe Baron\nClose Helmet"}
        result = unique_options({"pobHome": Path("." )}, {"noUniques": False, "budgetChaos": None},
                                {}, items, data, [definition])
        self.assertEqual([(entry[0], entry[1]) for entry in result], [("The Baron", "Helmet")])

    def test_unique_candidate_is_rejected_when_its_actual_sockets_cannot_fit_utility_gems(self):
        base = {"type": "Helmet", "subType": "Intelligence", "socketLimit": 4}
        data = GameData({"gems": [], "bases": {"Hubris Circlet": base}, "mods": []})
        item = RareItem("Helmet", "Hubris Circlet", base)
        spec = {"noUniques": False, "budgetChaos": None, "mainLinks": 6,
                "utility": {"Hatred": "Helmet", "Clarity": "Helmet"},
                "utilitySockets": 4, "weaponTypes": [], "damageType": "cold"}
        unique = {"name": "One Socket Crown", "base": "Hubris Circlet", "type": "Helmet",
                  "subType": "Intelligence", "raw": "Rarity: UNIQUE\nOne Socket Crown\nHubris Circlet\nSockets: B"}
        self.assertEqual(unique_options({}, spec, {}, [item], data, [unique]), [])

    def test_ordinary_jewel_templates_use_pob_jewel_affixes_and_four_mod_cap(self):
        base = {"type": "Jewel", "tags": {"default": True, "jewel": True}}
        metadata = {"gems": [], "bases": {"Cobalt Jewel": base, "Timeless Jewel": base}, "mods": [],
                    "jewelMods": [
                        {"id": "spell", "kind": "Prefix", "group": "SpellDamage", "level": 1,
                         "weightKey": ["jewel"], "weightVal": [1000], "lines": ["(10-12)% increased Spell Damage"]},
                        {"id": "cold", "kind": "Prefix", "group": "ColdDamage", "level": 1,
                         "weightKey": ["jewel"], "weightVal": [1000], "lines": ["(10-12)% increased Cold Damage"]},
                        {"id": "life", "kind": "Suffix", "group": "Life", "level": 1,
                         "weightKey": ["jewel"], "weightVal": [1000], "lines": ["+(10-12) to maximum Life"]}]}
        jewels = ordinary_jewel_templates(GameData(metadata),
                                          {"skill": "Winter Orb", "archetype": "spell",
                                           "damageType": "cold", "focus": "balanced"}, 90)
        self.assertEqual([jewel.base for jewel in jewels], ["Cobalt Jewel"])
        self.assertEqual({mod["id"] for mod in jewels[0].mods}, {"spell", "cold", "life"})
        self.assertFalse(jewels[0].can_add({"kind": "Prefix", "group": "extra", "level": 1,
                                            "weightKey": ["jewel"], "weightVal": [1000]}))

    def test_unique_quote_matches_variant_and_link_count(self):
        from build_generator import quote
        gear = [{"slot": "Body Armour", "rarity": "unique", "name": "Example",
                 "base": "Vaal Regalia", "variant": None, "links": 6}]
        market = {"prices": {"Example": [20.0, 300.0]}, "listings": {"Example": [
            {"chaos": 20.0, "variant": None, "links": 0},
            {"chaos": 300.0, "variant": None, "links": 6}]},
            "divineChaos": 100, "errors": [], "source": "test", "updated": 0, "league": "Test"}
        result = quote(gear, market, 500)
        self.assertEqual(result["pricedSubtotalChaos"], 300)
        self.assertEqual(result["priced"][0]["links"], 6)
        market["listings"]["Example"] = [{"chaos": 20.0, "variant": "Legacy", "links": 6}]
        unmatched = quote(gear, market, 500)
        self.assertEqual(unmatched["unknown"][0]["reason"],
                         "No quote matches the equipped unique variant and links")

    def test_tree_objective_prioritizes_measured_requirements(self):
        spec = {"archetype": "spell", "focus": "balanced"}
        stats = {"Life": 4000, "TotalDPS": 10000, "FireResist": 75, "ColdResist": 75, "LightningResist": 75,
                 "ReqDex": 150, "Dex": 150}
        self.assertGreater(score(stats, spec), score({**stats, "Dex": 50}, spec))

    def test_tree_objective_values_calculated_effective_hit_pool(self):
        spec = {"archetype": "spell", "focus": "balanced"}
        baseline = {"Life": 4000, "EnergyShield": 2000, "FullDPS": 100000,
                    "TotalEHP": 15000}
        self.assertGreater(score({**baseline, "TotalEHP": 20000}, spec), score(baseline, spec))

    def test_defense_focus_keeps_measuring_large_health_pool_improvements(self):
        spec = {"archetype": "spell", "focus": "defense"}
        baseline = {"Life": 3490, "EnergyShield": 3368, "FullDPS": 60000,
                    "FireResist": 75, "ColdResist": 75, "LightningResist": 75}
        improved = {**baseline, "Life": 5683, "EnergyShield": 3051}
        self.assertGreater(score(improved, spec), score(baseline, spec))

    def test_balanced_objective_keeps_measuring_pool_above_focus_target(self):
        spec = {"archetype": "minion", "focus": "balanced"}
        baseline = {"Life": 4500, "EnergyShield": 2500, "FullDPS": 60000,
                    "FireResist": 75, "ColdResist": 75, "LightningResist": 75}
        tougher = {**baseline, "Life": 5500, "EnergyShield": 3000}
        self.assertGreater(score(tougher, spec), score(baseline, spec))

    def test_balanced_tree_heuristic_uses_measured_defensive_baseline(self):
        self.assertEqual(tree_pool_target({"focus": "balanced"}), 6500)
        self.assertEqual(tree_pool_target({"focus": "damage"}), 6000)

    def test_candidate_dps_uses_each_zombie_limit(self):
        stats = recounted_stats({"FullDPS": 1000, "ActiveMinionLimit": 8},
                                {"skill": "Raise Zombie", "minionCount": 7})
        self.assertEqual(stats["FullDPS"], 1000 * 8 / 7)

    def test_candidate_srs_dps_uses_each_mana_limited_population(self):
        spec = {"skill": "Summon Raging Spirit", "minionCount": 2}
        stats = recounted_stats({"FullDPS": 1000, "Speed": 2.5, "ManaCost": 50,
                                 "ManaRegen": 25, "Duration": 8.2, "ActiveMinionLimit": 20}, spec)
        self.assertEqual(stats["FullDPS"], 1500)
        unsustainable = recounted_stats({"FullDPS": 1000, "Speed": 2.5, "ManaCost": 50,
                                          "ManaRegen": 0, "Duration": 8.2, "ActiveMinionLimit": 20}, spec)
        self.assertEqual(unsustainable["FullDPS"], 500)
        life_limited = recounted_stats({"FullDPS": 1000, "Speed": 2.5, "ManaCost": 0,
                                        "LifeCost": 100, "LifeRegenRecovery": 50,
                                        "Duration": 8.2, "ActiveMinionLimit": 20}, spec)
        self.assertEqual(life_limited["FullDPS"], 1500,
                         "Lifetap uses life regeneration when estimating temporary spirits")
        unsustainable_life = {"skill": "Summon Raging Spirit", "minionCount": 2}
        self.assertTrue(sync_permanent_minion_count(
            unsustainable_life, {"stats": {"Speed": 2.5, "ManaCost": 0, "ManaRegen": 0,
                                            "LifeCost": 100, "LifeRegenRecovery": 0,
                                            "Duration": 8.2, "ActiveMinionLimit": 20}}))
        self.assertEqual(unsustainable_life["minionCount"], 1)
        self.assertFalse(unsustainable_life["_srsPopulationSustainable"])

    def test_ignite_objective_does_not_optimize_unrequested_poison_damage(self):
        self.assertEqual(target_dps({"IgniteDPS": 100, "FullDotDPS": 1000}, {"archetype": "ignite"}), 100)

    def test_mechanic_profiles_report_known_recipes_and_resource_warnings(self):
        profile = mechanic_profile({"ascendancy": "Necromancer", "skill": "Summon Raging Spirit",
                                   "archetype": "minion", "damageType": "fire"})
        self.assertEqual(profile["name"], "srs_necromancer")
        self.assertIn("spirit duration", profile["requiredInteractions"])
        self.assertEqual(profile["compatibleAscendancies"], ["Necromancer"])
        self.assertIn("Convocation", profile["compatibleUtilityChoices"])
        status, warnings = assess_quality({"skill": "Summon Raging Spirit"},
                                          {"stats": {"ManaCost": 30, "Speed": 3, "ManaRegen": 20}},
                                          profile)
        self.assertEqual(status, "experimental")
        self.assertTrue(any("below estimated skill use" in warning for warning in warnings))
        self.assertTrue(any("Temporary SRS population" in warning for warning in warnings))
        sustained_spec = {"skill": "Summon Raging Spirit", "_srsPopulationSustainable": True}
        _, sustained_warnings = assess_quality(
            sustained_spec, {"stats": {"ManaCost": 56, "Speed": 2.26, "ManaRegen": 14.5}}, profile)
        self.assertFalse(any("below estimated skill use" in warning for warning in sustained_warnings))
        zombie_profile = mechanic_profile({"ascendancy": "Necromancer", "skill": "Raise Zombie",
                                           "archetype": "minion", "damageType": "physical"})
        _, zombie_warnings = assess_quality(
            {"skill": "Raise Zombie"}, {"stats": {"ManaCost": 104, "Speed": 1.77, "ManaRegen": 21.6}},
            zombie_profile)
        self.assertFalse(any("below estimated skill use" in warning or "zombie counts" in warning.lower()
                             for warning in zombie_warnings))

    def test_quality_validation_requires_mechanic_defense_and_search_gates(self):
        spec = {"ascendancy": "Necromancer", "skill": "Raise Zombie", "archetype": "minion",
                "damageType": "physical", "minionCount": 7}
        profile = mechanic_profile(spec)
        calculation = {"stats": {"FullDPS": 100000, "Life": 5000, "EnergyShield": 2500,
                                  "TotalEHP": 20000, "FireResist": 75, "ColdResist": 75,
                                  "LightningResist": 75, "ActiveMinionLimit": 7},
                       "passives": {"used": 100, "maximum": 112}}
        checks = [{"name": "Zombie limit calculated", "passed": True}]
        status, warnings = assess_quality(spec, calculation, profile, checks)
        self.assertEqual(status, "validated")
        self.assertEqual(warnings, [])
        limited, limited_warnings = assess_quality(spec, calculation, profile, checks, search_limited=True)
        self.assertEqual(limited, "experimental")
        self.assertTrue(any("2,000-evaluation limit" in warning for warning in limited_warnings))
        missing, missing_warnings = assess_quality(spec, calculation, profile)
        self.assertEqual(missing, "experimental")
        self.assertTrue(any("checks were not supplied" in warning for warning in missing_warnings))
        winter_orb = {"ascendancy": "Elementalist", "skill": "Winter Orb", "archetype": "spell",
                      "damageType": "cold"}
        winter_profile = mechanic_profile(winter_orb)
        unsustained = {"stats": {**calculation["stats"], "ManaCost": 14, "Speed": 6.28,
                                  "ManaRegen": 32.8}, "passives": calculation["passives"]}
        winter_status, winter_warnings = assess_quality(
            winter_orb, unsustained, winter_profile, checks)
        self.assertEqual(winter_status, "experimental")
        self.assertTrue(any("below estimated skill use" in warning for warning in winter_warnings))
        generic = {"ascendancy": "Elementalist", "skill": "Arc", "archetype": "spell",
                   "focus": "damage", "damageType": "lightning"}
        generic_profile = mechanic_profile(generic)
        generic_status, generic_warnings = assess_quality(
            generic, {"stats": {"FullDPS": 100000, "Life": 2500, "EnergyShield": 1500,
                                "TotalEHP": 9000, "FireResist": 70, "ColdResist": 75,
                                "LightningResist": 75, "ManaCost": 10, "Speed": 7,
                                "ManaRegen": 20},
                       "passives": {"used": 70, "maximum": 113}},
            generic_profile, search_limited=True)
        self.assertEqual(generic_status, "experimental")
        for phrase in ("No tested mechanic profile", "Mana regeneration", "life plus energy shield",
                       "effective hit pool", "Fire resistance", "passive points remain unspent",
                       "2,000-evaluation limit"):
            self.assertTrue(any(phrase in warning for warning in generic_warnings), phrase)

    def test_mechanic_activation_checks_are_separate_from_quality_warnings(self):
        spec = {"skill": "Ethereal Knives", "ascendancy": "Elementalist", "archetype": "ignite"}
        profile = mechanic_profile(spec)
        checks = assess_mechanics(spec, {"stats": {"IgniteDPS": 100}}, profile,
                                  '<PathOfBuilding><Tree><Spec nodes="12"/></Tree></PathOfBuilding>',
                                  {"tree": {"nodes": {"12": {"name": "Other"}}}})
        self.assertEqual([check["name"] for check in checks], [
            "Tested mechanic profile", "Main-skill damage calculated", "Compatible ascendancy",
            "Compatible utility choices", "Shaper of Flames allocated", "Ignite damage active"])
        self.assertTrue(checks[1]["passed"])
        self.assertTrue(checks[2]["passed"])
        self.assertTrue(checks[3]["passed"])
        self.assertFalse(checks[4]["passed"])
        self.assertTrue(checks[5]["passed"])
        incompatible_utility = {**spec, "utility": {"Wrath": "Helmet"}}
        incompatible_checks = assess_mechanics(
            incompatible_utility, {"stats": {"IgniteDPS": 100}}, profile,
            '<PathOfBuilding><Tree><Spec nodes="12"/></Tree></PathOfBuilding>',
            {"tree": {"nodes": {"12": {"name": "Other"}}}})
        self.assertFalse(next(check for check in incompatible_checks
                              if check["name"] == "Compatible utility choices")["passed"])
        progression_xml = ('<PathOfBuilding><Tree activeSpec="2">'
                           '<Spec id="1" nodes="12"/><Spec id="2" nodes="12,99"/>'
                           '</Tree></PathOfBuilding>')
        progression_checks = assess_mechanics(
            spec, {"stats": {"IgniteDPS": 100}}, profile, progression_xml,
            {"tree": {"nodes": {"12": {"name": "Other"}, "99": {"name": "Shaper of Flames"}}}})
        self.assertTrue(progression_checks[4]["passed"],
                        "mechanic checks must inspect the active endgame progression spec")

    def test_zombie_population_is_synchronized_after_candidate_calculations(self):
        spec = {"skill": "Raise Zombie", "minionCount": 3}
        self.assertTrue(sync_permanent_minion_count(spec, {"stats": {"ActiveMinionLimit": 5}}))
        self.assertEqual(spec["minionCount"], 5)
        self.assertFalse(sync_permanent_minion_count(spec, {"stats": {"ActiveMinionLimit": 5}}))

    def test_srs_population_uses_duration_cast_rate_mana_and_limit(self):
        spec = {"skill": "Summon Raging Spirit", "minionCount": 1}
        calculation = {"stats": {"Speed": 3.0, "ManaCost": 10.0, "ManaRegen": 12.0,
                                  "Duration": 5.0, "ActiveMinionLimit": 20}}
        self.assertTrue(sync_permanent_minion_count(spec, calculation))
        self.assertEqual(spec["minionCount"], 5)
        calculation["stats"]["ManaRegen"] = 3.9
        self.assertTrue(sync_permanent_minion_count(spec, calculation))
        self.assertEqual(spec["minionCount"], 1)
        calculation["stats"]["ManaRegen"] = 0
        self.assertFalse(sync_permanent_minion_count(spec, calculation))
        self.assertEqual(spec["minionCount"], 1)

    def test_animate_weapon_population_uses_duration_cast_rate_mana_and_limit(self):
        from passive_search import candidate_minion_count
        stats = {"FullDPS": 1000, "Speed": 1.83, "ManaCost": 49,
                 "ManaRegen": 16.2, "Duration": 65.6, "ActiveMinionLimit": 15}
        spec = {"skill": "Animate Weapon", "minionCount": 1, "archetype": "minion",
                "ascendancy": "Occultist", "damageType": "physical"}
        self.assertEqual(candidate_minion_count(stats, spec), 15)
        self.assertTrue(sync_permanent_minion_count(spec, {"stats": stats}))
        self.assertEqual(spec["minionCount"], 15)
        self.assertTrue(spec["_temporaryPopulationSustainable"])
        recounted = recounted_stats(stats, {**spec, "minionCount": 1})
        self.assertEqual(recounted["FullDPS"], 15000)

        profile = mechanic_profile({"ascendancy": "Occultist", "skill": "Animate Weapon",
                                    "archetype": "minion", "damageType": "physical"})
        self.assertEqual(profile["name"], "generic_experimental")
        self.assertIn("weapon duration", profile["requiredInteractions"])
        _, warnings = assess_quality(spec, {"stats": stats}, profile)
        self.assertFalse(any("below estimated skill use" in warning for warning in warnings))
        checks = assess_mechanics(spec, {"stats": stats}, profile,
                                  "<PathOfBuilding><Tree><Spec nodes=\"1\"/></Tree></PathOfBuilding>",
                                  {"tree": {"nodes": {"1": {"name": "Witch Start"}}}})
        self.assertTrue(next(check["passed"] for check in checks
                             if check["name"] == "Temporary weapon population modeled"))

        unsustainable = {**spec, "minionCount": 1, "_temporaryPopulationSustainable": False}
        low_regen = {**stats, "ManaRegen": 0}
        sync_permanent_minion_count(unsustainable, {"stats": low_regen})
        self.assertFalse(unsustainable["_temporaryPopulationSustainable"])
        _, warnings = assess_quality(unsustainable, {"stats": low_regen}, profile)
        self.assertTrue(any("not derived from summon rate" in warning for warning in warnings))

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

    def test_tree_search_reuses_existing_baseline_when_budget_is_exhausted(self):
        class Worker:
            def request(self, operation, **kwargs):
                raise AssertionError("Exhausted tree search must not calculate another baseline")

        budget = SearchBudget(1)
        budget.used = 1
        baseline = {"passives": {"used": 1, "maximum": 1}, "stats": {"Life": 1000}}
        allocated, calculation = search_tree(
            {"tree": {"nodes": {"1": {"classStartIndex": 3}}}}, {}, {"1"}, lambda _: "xml",
            Worker(), lambda _: None, budget=budget, baseline_calc=baseline)
        self.assertEqual(allocated, {"1"})
        self.assertIs(calculation, baseline)

    def test_reserved_search_budget_is_not_reported_as_hard_exhaustion(self):
        budget = SearchBudget(2000)
        budget.used = 1350
        self.assertEqual(budget.claim(10, reserve=650), 0)
        self.assertTrue(budget.reserve_blocked)
        self.assertFalse(budget.exhausted)

    def test_mastery_reserve_preserves_final_refinement_and_scales_to_budget(self):
        self.assertEqual(mastery_reserve_for(SearchBudget(2000)), 120)
        self.assertEqual(mastery_reserve_for(SearchBudget(940)), 120)
        self.assertEqual(mastery_reserve_for(SearchBudget(939)), 119)
        self.assertEqual(mastery_reserve_for(SearchBudget(820)), 0)

    def test_conditional_queen_hunger_offerings_are_disabled_idempotently(self):
        xml = '''<PathOfBuilding><Skills><SkillSet><Skill source="Item:1:The Queen's Hunger, Vaal Regalia" enabled="true">
          <Gem nameSpec="Bone Offering" enabled="true"/><Gem nameSpec="Flesh Offering" enabled="true"/>
        </Skill><Skill enabled="true"><Gem nameSpec="Desecrate" enabled="true"/></Skill></SkillSet></Skills></PathOfBuilding>'''
        safe, disabled = disable_conditional_item_skill_groups(xml)
        root = ET.fromstring(safe)
        grants = root.findall("./Skills/SkillSet/Skill")
        self.assertEqual({entry["skill"] for entry in disabled}, {"Bone Offering", "Flesh Offering"})
        self.assertEqual(grants[0].get("enabled"), "false")
        self.assertTrue(all(gem.get("enabled") == "false" for gem in grants[0].findall("Gem")))
        self.assertEqual(grants[1].get("enabled"), "true")
        self.assertEqual(disable_conditional_item_skill_groups(safe), (safe, []))

    def test_mapping_packages_are_level_legal_and_fit_the_stage_budget(self):
        data = GameData({"gems": [], "mods": [], "bases": {
            "Iron Hat": {"type": "Helmet", "req": {"level": 20}},
            "Gold Ring": {"type": "Ring", "req": {"level": 10}},
            "Vaal Regalia": {"type": "Body Armour", "req": {"level": 70}}}})
        options = [
            ("Cinder Hood", "Helmet", "Rarity: UNIQUE\nCinder Hood\nIron Hat", 5),
            ("Ash Ring", "Ring 1", "Rarity: UNIQUE\nAsh Ring\nGold Ring", 8),
            ("Late Armour", "Body Armour", "Rarity: UNIQUE\nLate Armour\nVaal Regalia", 1),
        ]
        packages = mapping_unique_packages(options, {"budgetChaos": 15, "requestedUniques": [],
                                                      "archetype": "spell", "damageType": "fire"}, data, 50)
        names = [set(item[0] for item in package) for package in packages]
        self.assertTrue(any(package == {"Cinder Hood", "Ash Ring"} for package in names))
        self.assertFalse(any("Late Armour" in package for package in names))
        self.assertFalse(any(len(package) > 1 and "Cinder Hood" not in package and "Ash Ring" not in package
                             for package in names))

    def test_keystone_packages_require_a_matching_mechanic(self):
        fireball = gem("Fireball", ["spell", "projectile", "fire"])
        srs = gem("Summon Raging Spirit", ["spell", "minion", "fire"])
        data = GameData({"gems": [fireball, srs], "mods": [], "bases": {}})
        context = {"tree": {"nodes": {
            "1": {"classStartIndex": 3, "out": ["2"]},
            "2": {"in": ["1"], "out": ["3"]},
            "3": {"in": ["2"], "isKeystone": True, "name": "Elemental Overload"}}}}
        calc = {"stats": {}, "passives": {"used": 3, "maximum": 5}}
        spell = {"skill": "Fireball", "archetype": "spell", "damageType": "fire", "focus": "damage"}
        minion = {**spell, "skill": "Summon Raging Spirit", "archetype": "minion"}
        found = keystone_package_candidates(context, spell, calc, data, [], {"1"})
        self.assertEqual([entry[0] for entry in found], ["Elemental Overload"])
        self.assertEqual(keystone_package_candidates(context, minion, calc, data, [], {"1"}), [])
        self.assertEqual(keystone_reserve_for(SearchBudget(2000)), 80)

    def test_keystone_package_can_trade_a_low_value_branch_for_its_connected_path(self):
        data = GameData({"gems": [gem("Fireball", ["spell", "fire"])], "mods": [], "bases": {}})
        context = {"tree": {"nodes": {
            "1": {"classStartIndex": 3, "out": ["2", "3"]},
            "2": {"in": ["1"]},
            "3": {"in": ["1"], "out": ["4"], "isNotable": True, "group": 7},
            "4": {"in": ["3"], "isKeystone": True, "name": "Elemental Overload"}}}}
        spec = {"skill": "Fireball", "archetype": "spell", "damageType": "fire", "focus": "damage"}
        packages = keystone_package_candidates(context, spec,
                    {"stats": {}, "passives": {"used": 3, "maximum": 3}}, data, [], {"1", "2", "3"})
        self.assertTrue(any(entry[0] == "Elemental Overload" and entry[4] == ["2"] and entry[2] == ["4"]
                            for entry in packages))

    def test_quality_report_separates_readiness_and_price_coverage(self):
        data = GameData({"gems": [], "mods": [], "bases": {}})
        xml = '<PathOfBuilding><Build pantheonMajorGod="None" pantheonMinorGod="None"/><Skills><SkillSet><Skill includeInFullDPS="true"><Gem gemId="Fireball"/></Skill></SkillSet></Skills></PathOfBuilding>'
        report = build_quality_report(xml, {"level": 90, "utility": {}, "expectedCurse": None}, {},
                                      {"stats": {"Life": 4000, "EnergyShield": 0, "ChaosResist": -36,
                                                  "TotalEHP": 10000, "Armour": 1000},
                                       "passives": {"used": 100, "maximum": 113}},
                                      data, {"complete": False, "unknown": [{"slot": "Ring 1"}],
                                             "pricedSubtotalChaos": 0, "budgetStatus": "unverified"}, [])
        self.assertEqual(report["completeness"]["status"], "gaps")
        self.assertEqual(report["encounterReadiness"]["status"], "review_gaps")
        self.assertIn("major and minor Pantheons are unselected", report["encounterReadiness"]["gaps"])
        self.assertFalse(report["priceCoverage"]["complete"])
        self.assertNotIn("validation", report)

    def test_fully_consumed_search_budget_is_reported_as_exhausted(self):
        budget = SearchBudget(2000)
        self.assertEqual(budget.claim(650), 650)
        self.assertEqual(budget.claim(1350), 1350)
        self.assertTrue(budget.exhausted)

    def test_tree_search_scores_later_candidate_batches_before_choosing(self):
        nodes = {"1": {"classStartIndex": 3, "out": [str(value) for value in range(2, 41)]}}
        nodes.update({str(value): {"in": ["1"], "isNotable": True,
                                  "stats": ["10% increased Cold Damage"]}
                      for value in range(2, 41)})

        class Worker:
            calls = 0

            def request(self, operation, **kwargs):
                if operation == "nodes":
                    return {"candidates": [{"id": item["id"], "stats": {
                        "Life": 4000, "ColdResist": 75, "FireResist": 75, "LightningResist": 75,
                        "TotalDPS": 100000 if item["id"] == "40" else 1000}}
                        for item in kwargs["candidates"]]}
                self.calls += 1
                xml = kwargs["xml"]
                selected = "40" in xml
                return {"passives": {"used": int(selected), "maximum": 1},
                        "stats": {"Life": 4000, "ColdResist": 75, "FireResist": 75,
                                  "LightningResist": 75, "TotalDPS": 100000 if selected else 1000}}

        worker = Worker()
        allocated, _ = search_tree({"tree": {"nodes": nodes}},
                                   {"archetype": "spell", "damageType": "cold", "focus": "damage"},
                                   {"1"}, lambda selected: ",".join(sorted(selected)), worker,
                                   lambda _: None, reserve=0)
        self.assertIn("40", allocated)

    def test_tree_search_can_reach_a_high_value_nonnotable_minion_node(self):
        nodes = {"1": {"classStartIndex": 3, "out": ["2"]},
                 "2": {"in": ["1"], "out": ["3"], "stats": ["+10 to Strength"]},
                 "3": {"in": ["2"], "stats": ["Minions deal 16% increased Damage"]}}

        class Worker:
            def request(self, operation, **kwargs):
                if operation == "nodes":
                    return {"candidates": [{"id": item["id"], "stats": {
                        "Life": 4000, "FullDPS": 100000 if item["id"] == "3" else 1000,
                        "FireResist": 75, "ColdResist": 75, "LightningResist": 75}}
                        for item in kwargs["candidates"]]}
                selected = "3" in kwargs["xml"]
                return {"passives": {"used": 2 if selected else 0, "maximum": 3},
                        "stats": {"Life": 4000, "FullDPS": 100000 if selected else 1000,
                                  "FireResist": 75, "ColdResist": 75, "LightningResist": 75}}

        allocated, _ = search_tree({"tree": {"nodes": nodes}},
                                   {"skill": "Raise Zombie", "archetype": "minion",
                                    "damageType": "physical", "focus": "damage"},
                                   {"1"}, lambda selected: ",".join(sorted(selected)), Worker(),
                                   lambda _: None, reserve=0)
        self.assertEqual(allocated, {"1", "2", "3"})

    def test_tree_search_scores_zombie_limit_candidates_with_their_own_population(self):
        nodes = {"1": {"classStartIndex": 3, "out": ["2", "3"]},
                 "2": {"isNotable": True, "stats": ["10% increased Minion Damage"]},
                 "3": {"isNotable": True, "stats": ["10% increased Minion Damage"]}}

        class Worker:
            def request(self, operation, **kwargs):
                if operation == "nodes":
                    return {"candidates": [
                        {"id": item["id"], "stats": {"Life": 4000, "FullDPS": 1000,
                         "ActiveMinionLimit": 8 if item["id"] == "3" else 7,
                         "FireResist": 75, "ColdResist": 75, "LightningResist": 75}}
                        for item in kwargs["candidates"]]}
                selected = "3" in kwargs["xml"]
                return {"passives": {"used": int(selected), "maximum": 1},
                        "stats": {"Life": 4000, "FullDPS": 1000,
                                  "ActiveMinionLimit": 8 if selected else 7,
                                  "FireResist": 75, "ColdResist": 75, "LightningResist": 75}}

        spec = {"skill": "Raise Zombie", "minionCount": 7, "archetype": "minion",
                "damageType": "physical", "focus": "damage"}
        allocated, _ = search_tree({"tree": {"nodes": nodes}}, spec, {"1"},
                                   lambda selected: ",".join(sorted(selected)), Worker(),
                                   lambda _: None, reserve=0)
        self.assertIn("3", allocated)
        self.assertEqual(spec["minionCount"], 8)

    def test_complete_design_sweeps_refine_links_and_tree_within_shared_budget(self):
        data = GameData({"gems": [
            {"id": "Metadata/SupportStrong", "gameId": "Metadata/SupportStrong", "skillId": "SupportStrong",
             "name": "Strong Support", "tags": {"support": True}},
            {"id": "Metadata/SupportWeak", "gameId": "Metadata/SupportWeak", "skillId": "SupportWeak",
             "name": "Weak Support", "tags": {"support": True}}], "bases": {}, "mods": []})
        context = {"tree": {"nodes": {
            "1": {"classStartIndex": 3, "out": ["2"]},
            "2": {"in": ["1"], "out": ["3", "4"], "isNotable": True,
                  "stats": ["10% increased Fire Damage"]},
            "3": {"in": ["2"], "isNotable": True, "stats": ["10% increased Fire Damage"]},
            "4": {"in": ["2"], "isNotable": True, "stats": ["10% increased Fire Damage"]}}}}
        spec = {"skill": "Fireball", "archetype": "spell", "damageType": "fire", "focus": "damage"}
        repair_state = {"applied": False}

        def fake_assemble(_spec, _context, _data, nodes, supports, _items, _masteries, _uniques, _jewels):
            unique_names = ",".join(text.splitlines()[1] for text in _uniques.values())
            return ("nodes=" + ",".join(sorted(nodes, key=int)) + ";supports=" +
                    ",".join(supports) + ";uniques=" + unique_names)

        def stats(dps, fire=75):
            return {"Life": 4000, "EnergyShield": 0, "FullDPS": dps,
                    "FireResist": fire, "ColdResist": 75, "LightningResist": 75,
                    "Str": 100, "Dex": 100, "Int": 100}

        def repair_suffixes(_items, _data, output):
            if output.get("FireResist", 75) < 75:
                repair_state["applied"] = True
                return 1
            return 0

        class Worker:
            def request(self, operation, **kwargs):
                xml = kwargs.get("xml", "")
                if operation == "supports":
                    return {"supports": ["Metadata/SupportStrong", "Metadata/SupportWeak"]}
                if operation == "supportScores":
                    return {"candidates": [{"id": identifier,
                        "stats": stats(6000 if identifier.endswith("SupportStrong") else 1000)}
                        for identifier in kwargs["candidates"]]}
                if operation == "nodes":
                    base = 5000 if "Strong Support" in xml else 0
                    existing = set(xml.split(";", 1)[0].removeprefix("nodes=").split(","))
                    weights = {"2": 100, "3": 200, "4": 150}
                    return {"candidates": [{"id": candidate["id"],
                        "stats": stats(1000 + base + sum(weights.get(node, 0) for node in
                                                            existing | set(candidate["nodes"])))}
                        for candidate in kwargs["candidates"]]}
                nodes_text = xml.split(";", 1)[0]
                links_text = xml.split(";supports=", 1)[1]
                selected = set(nodes_text.removeprefix("nodes=").split(","))
                dps = (1000 + (5000 if "Strong Support" in links_text else 0)
                       + sum({"2": 100, "3": 200, "4": 150}.get(node, 0) for node in selected))
                gear_deficit = "New Candidate" in xml and not repair_state["applied"]
                return {"calculated": True, "stats": stats(dps, 60 if gear_deficit else 75),
                        "passives": {"used": sum(node in selected for node in ("2", "3", "4")), "maximum": 5,
                                     "ascendancy": 0, "secondaryAscendancy": 0}}

        equipped_unique = "Rarity: UNIQUE\nAlready Equipped\nLeather Cap"
        initial = {"nodes": {"1"}, "supports": ["Weak Support"], "items": [], "masteries": {},
                   "uniques": {"Helmet": equipped_unique}, "unique_prices": {}, "jewels": {}, "minion_count": None,
                   "calc": {"calculated": True, "stats": stats(1000),
                            "passives": {"used": 0, "maximum": 5,
                                         "ascendancy": 0, "secondaryAscendancy": 0}}, "score": 0}
        rare_a = RareItem("Ring 1", "Gold Ring", {},
                          mods=[{"id": "ModDex", "lines": ["+5 to Dexterity"],
                                 "kind": "suffix", "group": "Dexterity"}])
        rare_b = RareItem("Ring 1", "Gold Ring", {},
                          mods=[{"id": "ModLife", "lines": ["+25 to maximum Life"],
                                 "kind": "prefix", "group": "Life"}])
        gear_options = [("Already Equipped", f"Slot {index}", equipped_unique, None)
                        for index in range(8)]
        gear_options.append(("New Candidate", "Belt", "Rarity: UNIQUE\nNew Candidate\nLeather Belt", None))
        budget, trace = SearchBudget(30), []
        with patch("real_generator.assemble", side_effect=fake_assemble), \
                patch("real_generator.solve_suffixes", side_effect=repair_suffixes):
            best = complete_design_search(context, spec, data, Worker(), lambda _: None, trace,
                                          budget, initial, gear_seeds=[
                                              {**initial, "items": [rare_a]},
                                              {**initial, "items": [rare_b]}],
                                          unique_options=gear_options)
        self.assertIn("Strong Support", best["supports"])
        self.assertIn("2", best["nodes"])
        self.assertIn("3", best["nodes"])
        self.assertIn("4", best["nodes"])
        self.assertTrue(any(item.get("kind") == "complete_design_tree_refinement"
                            and item.get("passivePathsAdded", 0) > 1 for item in trace))
        self.assertEqual([item["dimension"] for item in trace if item["kind"] == "complete_design_sweep"],
                         ["gear", "links", "tree"])
        gear_sweep = next(item for item in trace
                          if item.get("kind") == "complete_design_sweep" and item.get("sweep") == 1)
        self.assertEqual(gear_sweep["retained"], 6)
        self.assertGreater(gear_sweep["candidates"], 0)
        link_sweep = next(item for item in trace
                          if item.get("kind") == "complete_design_sweep" and item.get("sweep") == 2)
        tree_sweep = next(item for item in trace
                          if item.get("kind") == "complete_design_sweep" and item.get("sweep") == 3)
        self.assertEqual(gear_sweep["reservedForLaterSweeps"], 25)
        self.assertEqual(link_sweep["reservedForLaterSweeps"], 18)
        self.assertEqual(tree_sweep["reservedForLaterSweeps"], 0)
        self.assertLessEqual(gear_sweep["evaluations"] - gear_sweep["budgetAtStart"], 5)
        self.assertLessEqual(link_sweep["evaluations"] - link_sweep["budgetAtStart"], 10)
        self.assertTrue(repair_state["applied"], "unique must be repaired before legality rejection")
        self.assertTrue(all(item["retained"] <= 6 for item in trace if item["kind"] == "complete_design_sweep"))
        self.assertLessEqual(budget.used, budget.limit)
        unique_result = next(item for item in trace if item.get("kind") == "complete_design_unique_result"
                             and item.get("name") == "New Candidate")
        self.assertTrue(unique_result["evaluated"])
        self.assertIn("verified complete design", unique_result["reason"])

    def test_finalist_calculation_restarts_stale_pob_tree_state(self):
        class Worker:
            stale = True
            closes = 0

            def request(self, operation, **kwargs):
                return {"passives": {"used": 0 if self.stale else 61}, "stats": {"Life": 3500}}

            def close(self):
                self.stale = False
                self.closes += 1

        worker = Worker()
        trace = []
        result = calculate_finalist(worker, "candidate xml", 71, trace)
        self.assertEqual(result["passives"]["used"], 61)
        self.assertEqual(worker.closes, 1)
        self.assertEqual(trace[0]["kind"], "pob_worker_reset")

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
                    "gear", "validation", "mechanicChecks", "stats", "quote", "recipe", "modelUsed", "prompt", "league", "treeVersion",
                    "officialTreeRelease", "qualityStatus", "qualityWarnings", "createdAt", "shareStatus", "shareUrl",
                    "_xml", "_fingerprint"}
        self.assertTrue(required <= result.keys())
        self.assertEqual(result["qualityStatus"], "experimental")
        self.assertTrue(result["qualityWarnings"])
        self.assertRegex(result["id"], r"^g[0-9a-f]{20}$")
        self.assertEqual(result["mainSkill"], "Winter Orb")
        self.assertEqual(decode_pob(encode_pob(result["_xml"])), xml)
        self.assertEqual(mechanics_fingerprint(xml), result["_fingerprint"])
        self.assertNotIn("_xml", public(result))


if __name__ == "__main__":
    unittest.main()
