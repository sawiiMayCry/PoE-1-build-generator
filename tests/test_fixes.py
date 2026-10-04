"""Regressions for the matrix findings: crashes, price policy, support selection, fills, floors."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_assembly
import real_generator
from build_evaluation import skill_groups_from_xml, tier_targets
from generation_data import GameData, RareItem, is_main_skill_candidate, rare_templates, support_is_noncombat
from skill_packages import build_facts, candidate_packages, capacity_from_equipment, fill_packages
from skill_planner import plan_skill_loadout
import unique_policy


def gem(name, tags, support=False, **extra):
    identifier = "Metadata/Items/Gems/" + ("SupportGem" if support else "SkillGem") + name.replace(" ", "")
    levels = [{"level": level, "requiredLevel": 1, "str": 0, "dex": 0, "int": 0} for level in range(1, 21)]
    return {"id": identifier, "gameId": identifier, "name": name, "variantId": name.replace(" ", ""),
            "skillId": name.replace(" ", ""), "tags": dict.fromkeys(tags, True), "support": support,
            "maxLevel": 20, "levels": levels, **extra}


BASES = {
    "Prophecy Wand": {"type": "Weapon", "subType": "Wand", "socketLimit": 3, "tags": {"wand": True, "default": True}},
    "Judgement Staff": {"type": "Weapon", "subType": "Staff", "socketLimit": 6,
                        "tags": {"staff": True, "two_hand_weapon": True, "default": True}},
    "Titanium Spirit Shield": {"type": "Shield", "socketLimit": 3, "tags": {"shield": True, "default": True}},
    "Vaal Regalia": {"type": "Body Armour", "socketLimit": 6, "tags": {"default": True}},
    "Leather Belt": {"type": "Belt", "subType": "Belt", "req": {"level": 1}, "tags": {"default": True}},
    "Mechanical Belt": {"type": "Belt", "subType": "Belt", "req": {"level": 60}, "socketLimit": 1,
                        "tags": {"default": True}},
}


def small_data(extra_gems=()):
    gems = [gem("Arc", ["spell", "lightning"]), gem("Shield Charge", ["attack", "movement"]),
            gem("Faster Attacks", ["support"], True), gem("Momentum", ["support"], True),
            gem("Flame Dash", ["spell", "movement", "fire"]), *extra_gems]
    return GameData({"gems": gems, "bases": BASES, "mods": []})


class TwoHandedCapacity(unittest.TestCase):
    """C1/C2: planner and assembler must agree on which slots exist (two-handed Weapon 1 unique)."""

    def test_two_handed_unique_removes_weapon_two_in_the_shared_capacity(self):
        data = small_data()
        items = [RareItem("Weapon 1", "Prophecy Wand", BASES["Prophecy Wand"]),
                 RareItem("Weapon 2", "Titanium Spirit Shield", BASES["Titanium Spirit Shield"]),
                 RareItem("Body Armour", "Vaal Regalia", BASES["Vaal Regalia"])]
        staff = "Rarity: UNIQUE\nCane of Kulemak\nJudgement Staff\nImplicits: 0"
        active, capacity, two_handed = build_assembly.equipment_capacity({}, data, items, {"Weapon 1": staff})
        self.assertTrue(two_handed)
        self.assertNotIn("Weapon 2", capacity)
        self.assertEqual(capacity["Weapon 1"]["total"], 6)
        self.assertEqual([item.slot for item in active], ["Weapon 1", "Body Armour"])

    def test_one_handed_setup_keeps_weapon_two(self):
        data = small_data()
        items = [RareItem("Weapon 1", "Prophecy Wand", BASES["Prophecy Wand"]),
                 RareItem("Weapon 2", "Titanium Spirit Shield", BASES["Titanium Spirit Shield"])]
        _, capacity, two_handed = build_assembly.equipment_capacity({}, data, items, {})
        self.assertFalse(two_handed)
        self.assertEqual(capacity["Weapon 2"]["total"], 3)

    def test_shield_package_needs_a_socketed_weapon_two_and_a_request(self):
        data = small_data()
        items = [RareItem("Weapon 1", "Prophecy Wand", BASES["Prophecy Wand"]),
                 RareItem("Weapon 2", "Titanium Spirit Shield", BASES["Titanium Spirit Shield"]),
                 RareItem("Body Armour", "Vaal Regalia", BASES["Vaal Regalia"])]
        spec = {"skill": "Arc", "level": 90, "focus": "damage", "damageType": "lightning", "archetype": "spell",
                "ascendancy": "Occultist", "requestedUtilities": ["Shield Charge"]}
        capacity = capacity_from_equipment(items, {}, data)
        capacity.pop("Weapon 2")      # as when a two-handed unique displaces it
        calls = []

        def evaluate(groups):
            calls.append([g["id"] for g in groups])
            return {"stats": {"FullDPS": 1000.0, "Life": 4000, "LifeUnreserved": 4000, "Mana": 500,
                              "ManaUnreserved": 500, "ManaCost": 5}, "ok": True, "reasons": []}

        plan = plan_skill_loadout(spec, data, [], evaluate, capacity=capacity, items=items)
        self.assertFalse(any("movement_shield_charge" in ids for ids in calls))
        self.assertEqual(plan["problems"], [])
        # requested + socketed Weapon 2 -> offered
        facts = build_facts(spec, items, None, data)
        self.assertIn("movement_shield_charge", [p["id"] for p in candidate_packages(spec, data, facts)])
        # not requested -> never offered (one movement skill by default)
        plain = {**spec, "requestedUtilities": []}
        self.assertNotIn("movement_shield_charge", [p["id"] for p in candidate_packages(
            plain, data, build_facts(plain, items, None, data))])


class TolerantGemParsing(unittest.TestCase):
    """C4: PoB writes literal nil for unset gem attributes."""

    def test_nil_count_level_and_quality_parse(self):
        xml = ('<PathOfBuilding><Skills activeSkillSet="1"><SkillSet id="1">'
               '<Skill slot="Body Armour" includeInFullDPS="true" mainActiveSkill="nil">'
               '<Gem nameSpec="Spark" gemId="g" skillId="Spark" level="nil" quality="nil" count="nil"/>'
               '</Skill></SkillSet></Skills></PathOfBuilding>')
        groups = skill_groups_from_xml(xml)
        gem_record = groups[0].gems[0]
        self.assertEqual((gem_record.level, gem_record.quality, gem_record.count), (20, 0, 1))


class MainSkillSelection(unittest.TestCase):
    """C5: offerings and other buffs are not damage skills."""

    OFFERING = gem("Bone Offering", ["spell", "minion", "duration"], skillTypes=["Buff", "Minion", "Offering", "Spell"],
                   createsMinions=False, baseEffectiveness=0)
    SPIRIT = gem("Summon Raging Spirit", ["spell", "minion", "duration"],
                 skillTypes=["CreatesMinion", "Minion", "Spell"], createsMinions=True, baseEffectiveness=0)
    BANE = gem("Bane", ["spell", "chaos", "hex", "duration"], skillTypes=["DamageOverTime", "Hex", "Spell"],
               baseEffectiveness=6.3)
    DESPAIR = gem("Despair", ["spell", "chaos", "curse", "hex"], skillTypes=["Hex", "Spell"], baseEffectiveness=0)
    ZOMBIE = gem("Raise Zombie", ["spell", "minion"])   # old metadata without skill types still works

    def test_offering_is_rejected_and_real_summons_are_kept(self):
        self.assertFalse(is_main_skill_candidate("Bone Offering", self.OFFERING))
        self.assertTrue(is_main_skill_candidate("Summon Raging Spirit", self.SPIRIT))
        self.assertTrue(is_main_skill_candidate("Raise Zombie", self.ZOMBIE))

    def test_damaging_hex_allowed_and_plain_curse_rejected(self):
        self.assertTrue(is_main_skill_candidate("Bane", self.BANE))
        self.assertFalse(is_main_skill_candidate("Despair", self.DESPAIR))

    def test_prompt_naming_only_an_offering_fails_with_a_specific_message(self):
        data = GameData({"gems": [self.OFFERING, self.SPIRIT, gem("Raise Zombie", ["spell", "minion"])],
                         "bases": BASES, "mods": []})
        with self.assertRaises(ValueError) as caught:
            real_generator.normalize_intent("Level 90 Bone Offering Necromancer", {}, data, {"divineChaos": 200})
        self.assertIn("Bone Offering", str(caught.exception))
        self.assertIn("no damage of its own", str(caught.exception))

    def test_offering_named_as_a_utility_is_still_allowed(self):
        data = GameData({"gems": [self.OFFERING, self.SPIRIT, gem("Raise Zombie", ["spell", "minion"])],
                         "bases": BASES, "mods": []})
        spec = real_generator.normalize_intent("Level 90 Raise Zombie Necromancer with Bone Offering", {},
                                               data, {"divineChaos": 200})
        self.assertEqual(spec["skill"], "Raise Zombie")


class SupportClassification(unittest.TestCase):
    def test_item_drop_and_legacy_supports_are_not_combat_supports(self):
        quantity = gem("Item Quantity", ["support"], True, statIds=["base_killed_monster_dropped_item_quantity_+%"],
                       legacy=True)
        rarity = gem("Item Rarity", ["support"], True, statIds=["base_killed_monster_dropped_item_rarity_+%"])
        iron_will = gem("Iron Will", ["support"], True, statIds=["spell_damage_+%", "strong_casting"])
        self.assertTrue(support_is_noncombat(quantity))
        self.assertTrue(support_is_noncombat(rarity))
        self.assertFalse(support_is_noncombat(iron_will))
        self.assertFalse(support_is_noncombat(gem("Added Fire Damage", ["support"], True)))   # no metadata -> allowed


class MainLinkPadding(unittest.TestCase):
    """C3: a link that stopped at 5 gems is completed with the best measured compatible support."""

    def test_pad_main_link_fills_to_six(self):
        data = GameData({"gems": [gem("Detonate Dead", ["spell", "fire", "area"]),
                                  gem("Fire Penetration", ["support"], True), gem("Arcane Surge", ["support"], True),
                                  gem("Spell Echo", ["support"], True), gem("Item Quantity", ["support"], True,
                                                                           legacy=True)],
                         "bases": BASES, "mods": []})
        by_name = {g["name"]: g for g in data.gems.values()}
        spec = {"skill": "Detonate Dead", "archetype": "spell", "damageType": "fire", "level": 80,
                "focus": "damage", "skillTags": ["spell", "fire"]}
        dps = {"Spell Echo": 1000.0, "Fire Penetration": 1100.0, "Arcane Surge": 1200.0}

        class Worker:
            def request(self, operation, **kwargs):
                if operation == "supports":
                    return {"supports": [g["id"] for g in data.gems.values() if g.get("support")]}
                if operation == "calculate":
                    return {"stats": {"FullDPS": 900.0, "Life": 4000, "ManaCost": 5, "ManaUnreserved": 100,
                                      "LifeUnreserved": 4000}}
                if operation == "supportScores":
                    return {"candidates": [{"id": identifier, "stats": {
                        "FullDPS": dps.get(data.by_id[identifier]["name"], 900.0), "Life": 4000, "ManaCost": 5,
                        "ManaUnreserved": 100, "LifeUnreserved": 4000}} for identifier in kwargs["candidates"]]}
                raise AssertionError(operation)

        with patch("real_generator.support_mechanism_compatible", return_value=True):
            padded = real_generator.pad_main_link(spec, data, lambda links: "xml", Worker(),
                                                  ["Fire Penetration"], [], None)
        # target is five supports; the available non-combat gem is never used and the best measured one leads
        self.assertNotIn("Item Quantity", padded)
        self.assertEqual(padded[1], "Arcane Surge")
        self.assertEqual(spec["linkFiller"][0]["name"], "Arcane Surge")
        self.assertEqual(padded, ["Fire Penetration", "Arcane Surge", "Spell Echo"])
        self.assertIn("linkShortfall", spec)   # only two usable supports existed: reported, not hidden
        self.assertEqual(by_name["Item Quantity"]["legacy"], True)


class UniquePricePolicy(unittest.TestCase):
    """Superseded by the five-divine standard budget; the new behaviour is tested in test_unique_budget."""

    def test_requested_uniques_are_excluded_from_the_penalty(self):
        requested = {"Shavronne's Wrappings"}
        uniques = {"Body Armour": "Rarity: UNIQUE\nShavronne's Wrappings\nOccultist's Vestment",
                   "Belt": "Rarity: UNIQUE\nUnholy Accomplice\nStygian Vise"}
        prices = real_generator.unrequested_prices(uniques, {"Body Armour": 210.0, "Belt": 3752.0}, requested)
        self.assertEqual(prices, [3752.0])


class SparSocketFill(unittest.TestCase):
    def facts(self, **extra):
        return {"archetype": "spell", "level": 80, "damageType": "fire", **extra}

    def data(self):
        return small_data([gem("Vaal Discipline", ["aura", "vaal"]), gem("Vaal Haste", ["aura", "vaal"]),
                           gem("Vaal Grace", ["aura", "vaal"]), gem("Molten Shell", ["guard", "spell"]),
                           gem("Phase Run", ["movement", "spell"])])

    def test_vaal_gems_with_a_measured_need_rank_before_last_resort_ones(self):
        packages = fill_packages({}, self.data(), self.facts(), set(), stats={"EnergyShield": 4000, "Life": 3000})
        order = [p["id"] for p in packages]
        self.assertLess(order.index("fill_vaal_discipline"), order.index("fill_vaal_grace"))
        grace = next(p for p in packages if p["id"] == "fill_vaal_grace")
        self.assertIn("last-resort", grace["function"])
        discipline = next(p for p in packages if p["id"] == "fill_vaal_discipline")
        self.assertNotIn("last-resort", discipline["function"])

    def test_no_second_movement_skill_when_one_is_placed(self):
        packages = fill_packages({}, self.data(), self.facts(), set(), roles={"movement"})
        self.assertNotIn("fill_phase_run", [p["id"] for p in packages])

    def test_single_gem_filler_exists_for_a_lone_socket(self):
        packages = fill_packages({}, self.data(), self.facts(), set())
        self.assertIn("fill_molten_shell", [p["id"] for p in packages])

    def test_planner_bundles_vaal_fillers_of_one_item_and_keeps_one_movement(self):
        data = self.data()
        items = [RareItem("Body Armour", "Vaal Regalia", BASES["Vaal Regalia"]),
                 RareItem("Weapon 1", "Prophecy Wand", BASES["Prophecy Wand"])]
        capacity = capacity_from_equipment(items, {}, data)
        spec = {"skill": "Arc", "level": 80, "focus": "damage", "damageType": "fire", "archetype": "attack",
                "ascendancy": "Occultist"}

        def evaluate(groups):
            return {"stats": {"FullDPS": 1000.0, "Life": 4000, "LifeUnreserved": 4000, "Mana": 900,
                              "ManaUnreserved": 900, "ManaCost": 5, "EnergyShield": 0}, "ok": True, "reasons": []}

        plan = plan_skill_loadout(spec, data, [], evaluate, capacity=capacity, items=items)
        ids = [g["id"] for g in plan["groups"]]
        movement = [g for g in plan["groups"] if g["role"] == "movement"]
        self.assertEqual(len(movement), 1)
        self.assertLessEqual(sum(1 for i in ids if i.startswith("fill_vaal_") and i != "fill_vaal_utilities"), 1)
        self.assertEqual(plan["problems"], [])


class QualityFloors(unittest.TestCase):
    def test_floor_applies_at_level_80_but_scaled(self):
        spec80 = {"skill": "Contagion", "archetype": "dot", "level": 80, "skillTags": ["spell", "duration"]}
        spec90 = {**spec80, "level": 90}
        low, full = tier_targets(spec80), tier_targets(spec90)
        self.assertEqual(full["dpsFloor"], 60000)
        self.assertEqual(low["dpsFloor"], 30000)
        self.assertGreater(low["dpsFloor"], 16482)      # the matrix's validated 16k Contagion now misses it

    def test_a_weak_level_80_build_is_not_validated(self):
        spec = {"skill": "Contagion", "archetype": "dot", "level": 80, "skillTags": ["spell", "duration"],
                "damageType": "chaos", "focus": "balanced"}
        below, tier = real_generator.below_dps_floor(spec, {"FullDotDPS": 16482.0, "FullDPS": 16482.0})
        self.assertTrue(below)
        strong, _ = real_generator.below_dps_floor(spec, {"FullDotDPS": 90000.0, "FullDPS": 90000.0})
        self.assertFalse(strong)


class SocketlessJewelleryBases(unittest.TestCase):
    def test_leveling_belt_has_no_unusable_socket(self):
        bases = {**BASES, "Hubris Circlet": {"type": "Helmet", "subType": "Evasion", "socketLimit": 4,
                                            "req": {"level": 1}, "tags": {"default": True}},
                 "Sorcerer Gloves": {"type": "Gloves", "socketLimit": 4, "req": {"level": 1}, "tags": {"default": True}},
                 "Sorcerer Boots": {"type": "Boots", "socketLimit": 4, "req": {"level": 1}, "tags": {"default": True}},
                 "Amber Amulet": {"type": "Amulet", "req": {"level": 1}, "tags": {"default": True}},
                 "Coral Ring": {"type": "Ring", "req": {"level": 1}, "tags": {"default": True}}}
        bases["Prophecy Wand"] = {**bases["Prophecy Wand"], "req": {"level": 1}}
        bases["Titanium Spirit Shield"] = {**bases["Titanium Spirit Shield"], "req": {"level": 1}}
        bases["Vaal Regalia"] = {**bases["Vaal Regalia"], "req": {"level": 1}}
        data = GameData({"gems": [], "bases": bases, "mods": []})
        items = rare_templates(data, "spell", "Wand", character_level=70)
        belt = next(item for item in items if item.slot == "Belt")
        self.assertEqual(belt.base, "Leather Belt")


class StageFill(unittest.TestCase):
    def test_stage_fill_places_role_gems_and_labels_what_stays_open(self):
        import build_progression as bp
        from skill_packages import apply_stage_caps, make_group
        data = small_data([gem("Vaal Haste", ["aura", "vaal"]), gem("Molten Shell", ["guard", "spell"]),
                           gem("Phase Run", ["movement", "spell"])])
        items = [RareItem("Body Armour", "Vaal Regalia", BASES["Vaal Regalia"]),
                 RareItem("Weapon 1", "Prophecy Wand", BASES["Prophecy Wand"]),
                 RareItem("Weapon 2", "Titanium Spirit Shield", BASES["Titanium Spirit Shield"])]
        capacity = apply_stage_caps(capacity_from_equipment(items, {}, data), 4, 4)
        main = make_group("main", "main", [("Arc", "active")], slot="Body Armour", main_active="Arc",
                          include_in_full_dps=True)
        spec = {"skill": "Arc", "level": 90, "focus": "damage", "damageType": "lightning",
                "archetype": "spell", "ascendancy": "Occultist", "mainLinks": 4}
        notes = []
        groups = bp._stage_fill_spare(spec, spec, data, {"level": 45, "act": 5}, items, capacity, [main], notes)
        ids = [g["id"] for g in groups]
        self.assertIn("fill_molten_shell", ids)
        self.assertEqual(sum(1 for g in groups if g["role"] == "movement"), 1)
        self.assertTrue(any("left open" in note for note in notes))      # few catalogue gems: labelled, not silent

    def test_experimental_league_bases_are_not_used_for_jewellery_templates(self):
        bases = {**BASES, "Mechanalarm Belt": {"type": "Belt", "subType": "Belt", "req": {"level": 70},
                                                  "tags": {"default": True, "experimental_base": True}},
                 "Hubris Circlet": {"type": "Helmet", "socketLimit": 4, "req": {"level": 1}, "tags": {"default": True}},
                 "Sorcerer Gloves": {"type": "Gloves", "socketLimit": 4, "req": {"level": 1}, "tags": {"default": True}},
                 "Sorcerer Boots": {"type": "Boots", "socketLimit": 4, "req": {"level": 1}, "tags": {"default": True}},
                 "Amber Amulet": {"type": "Amulet", "req": {"level": 1}, "tags": {"default": True}},
                 "Coral Ring": {"type": "Ring", "req": {"level": 1}, "tags": {"default": True}}}
        for name in ("Prophecy Wand", "Titanium Spirit Shield", "Vaal Regalia", "Leather Belt"):
            bases[name] = {**bases[name], "req": {"level": 1}}
        bases["Mechanical Belt"] = {**bases["Mechanical Belt"], "tags": {"default": True, "experimental_base": True}}
        data = GameData({"gems": [], "bases": bases, "mods": []})
        belt = next(i for i in rare_templates(data, "spell", "Wand", character_level=75) if i.slot == "Belt")
        self.assertEqual(belt.base, "Leather Belt")


class ExhaustedFillCatalogue(unittest.TestCase):
    def test_leftover_sockets_after_the_whole_catalogue_are_disclosed_not_a_gap(self):
        from build_evaluation import package_report
        data = small_data()
        items = [RareItem("Body Armour", "Vaal Regalia", BASES["Vaal Regalia"]),
                 RareItem("Weapon 1", "Prophecy Wand", BASES["Prophecy Wand"])]
        capacity = capacity_from_equipment(items, {}, data)
        spec = {"skill": "Arc", "level": 80, "focus": "damage", "damageType": "lightning", "archetype": "spell",
                "ascendancy": "Occultist"}

        def evaluate(groups):
            return {"stats": {"FullDPS": 1000.0, "Life": 4000, "LifeUnreserved": 4000, "Mana": 900,
                              "ManaUnreserved": 900, "ManaCost": 5}, "ok": True, "reasons": []}

        plan = plan_skill_loadout(spec, data, [], evaluate, capacity=capacity, items=items)
        self.assertTrue(plan["spareSockets"])
        self.assertTrue(plan["fillExhausted"])
        gems = "".join(f'<Gem nameSpec="G{i}" gemId="g{i}" skillId="Main" level="20"/>' for i in range(6))
        xml = ('<PathOfBuilding><Tree activeSpec="1"><Spec classId="0"/></Tree><Skills activeSkillSet="1">'
               f'<SkillSet id="1"><Skill slot="Body Armour" includeInFullDPS="true">{gems}</Skill></SkillSet></Skills>'
               '<Items activeItemSet="1"><Item id="1">Rarity: RARE' + chr(10) + 'W' + chr(10) + 'Prophecy Wand' + chr(10) + 'Sockets: B-B-B</Item>'
               '<ItemSet id="1"><Slot name="Weapon 1" itemId="1"/></ItemSet></Items></PathOfBuilding>')
        open_gap = package_report(xml, {})
        self.assertTrue(any("empty socket" in gap for gap in open_gap["gaps"]))
        disclosed = package_report(xml, {"skillPlanSummary": {"fillExhausted": True}})
        self.assertFalse(any("empty socket" in gap for gap in disclosed["gaps"]))
        self.assertTrue(any("no further justified gem" in text for text in disclosed["openSockets"]))


class SupportRelevanceFromStats(unittest.TestCase):
    def test_dot_multiplier_support_outranks_an_unrelated_one_for_a_dot_skill(self):
        spec = {"archetype": "dot", "damageType": "chaos"}
        efficacy = gem("Efficacy", ["support", "duration"], True, statIds=["support_efficacy_damage_over_time_+%_final"])
        other = gem("Faster Projectiles", ["support", "projectile"], True, statIds=["base_projectile_speed_+%"])
        self.assertGreater(real_generator.support_relevance_score(efficacy, spec),
                           real_generator.support_relevance_score(other, spec))


class StageFillAttributes(unittest.TestCase):
    def test_stage_fill_skips_gems_the_character_cannot_meet(self):
        import build_progression as bp
        from skill_packages import apply_stage_caps, make_group
        hard = gem("Molten Shell", ["guard", "spell"])
        hard["levels"] = [{"level": n, "requiredLevel": 1, "str": 500, "dex": 0, "int": 0} for n in range(1, 21)]
        data = small_data([hard])
        items = [RareItem("Body Armour", "Vaal Regalia", BASES["Vaal Regalia"]),
                 RareItem("Weapon 1", "Prophecy Wand", BASES["Prophecy Wand"])]
        capacity = apply_stage_caps(capacity_from_equipment(items, {}, data), 4, 4)
        main = make_group("main", "main", [("Arc", "active")], slot="Body Armour", main_active="Arc",
                          include_in_full_dps=True)
        spec = {"skill": "Arc", "level": 90, "focus": "damage", "damageType": "lightning",
                "archetype": "spell", "ascendancy": "Occultist", "mainLinks": 4}
        groups = bp._stage_fill_spare(spec, spec, data, {"level": 45, "act": 5}, items, capacity, [main], [],
                                      stats={"Str": 100, "Dex": 100, "Int": 300})
        self.assertNotIn("fill_molten_shell", [g["id"] for g in groups])


if __name__ == "__main__":
    unittest.main()
