import unittest
import xml.etree.ElementTree as ET

from build_progression import (add_flasks, ascendancy_order, combine_loadouts, connected_order,
                               gem_level, milestones, select_stage, stage_checks, stage_skill)
from generation_data import GameData, RareItem


class BuildProgression(unittest.TestCase):
    def test_add_flasks_preserves_an_existing_unique_flask_slot(self):
        root = ET.fromstring("""<PathOfBuilding><Items activeItemSet="1"><Item id="1">Rarity: UNIQUE
Rumi's Concoction
Granite Flask</Item><ItemSet id="1"><Slot name="Flask 2" itemId="1" active="false" /></ItemSet></Items></PathOfBuilding>""")
        data = GameData({"gems": [], "mods": [], "bases": {
            name: {"type": "Flask", "req": {"level": 1}}
            for name in ("Greater Life Flask", "Greater Mana Flask", "Quicksilver Flask",
                         "Granite Flask", "Quartz Flask")}})
        add_flasks(root, data, 80)
        item_set = root.find("./Items/ItemSet")
        slots = {slot.get("name"): slot.get("itemId") for slot in item_set.findall("Slot")}
        self.assertEqual(set(slots), {f"Flask {index}" for index in range(1, 6)})
        self.assertEqual(slots["Flask 2"], "1")
        self.assertIn("Rumi's Concoction", root.find("./Items/Item[@id='1']").text)

    def test_add_flasks_repairs_zero_and_unresolved_slot_ids(self):
        root = ET.fromstring('''<PathOfBuilding><Items activeItemSet="1">
          <Item id="1">Rarity: UNIQUE\nRumi's Concoction\nGranite Flask</Item>
          <ItemSet id="1"><Slot name="Flask 1" itemId="0"/>
          <Slot name="Flask 2" itemId="404"/><Slot name="Flask 3" itemId="1"/></ItemSet>
        </Items></PathOfBuilding>''')
        data = GameData({"gems": [], "mods": [], "bases": {
            name: {"type": "Flask", "req": {"level": 1}}
            for name in ("Greater Life Flask", "Greater Mana Flask", "Quicksilver Flask",
                         "Granite Flask", "Quartz Flask")}})
        add_flasks(root, data, 80)
        items = {item.get("id"): item for item in root.findall("./Items/Item")}
        slots = {slot.get("name"): slot.get("itemId") for slot in root.findall("./Items/ItemSet/Slot")}
        self.assertEqual(set(slots), {f"Flask {index}" for index in range(1, 6)})
        for index in range(1, 6):
            self.assertIn(slots[f"Flask {index}"], items)
        self.assertEqual(slots["Flask 3"], "1")

    def test_stage_checks_require_five_resolved_level_legal_flasks(self):
        data = GameData({"gems": [], "bases": {"Granite Flask": {"type": "Flask", "req": {"level": 1}}},
                         "mods": []})
        xml = '''<PathOfBuilding><Build/><Tree><Spec nodes="1"/></Tree><Skills><SkillSet><Skill/></SkillSet></Skills>
          <Items activeItemSet="1"><Item id="1">Rarity: NORMAL\nGranite Flask</Item>
          <ItemSet id="1"><Slot name="Flask 1" itemId="1"/><Slot name="Flask 2" itemId="0"/></ItemSet></Items>
          <Config><ConfigSet/></Config></PathOfBuilding>'''
        calc = {"stats": {}, "passives": {"used": 0, "ascendancy": 0}, "calculated": False}
        checks = stage_checks(xml, {**milestones(90)[-1], "resistanceTarget": 0}, calc, data,
                              {"tree": {"nodes": {"1": {"classStartIndex": 3}}}})
        flask_check = next(check for check in checks if check["name"] == "Five equipped flasks")
        self.assertFalse(flask_check["passed"])

    def test_act_budgets_penalties_and_lab_unlocks(self):
        phases = milestones(89)
        self.assertEqual(len(phases), 13)
        self.assertEqual(phases[-1]["title"], "Endgame - Level 89")
        self.assertEqual(phases[-2]["links"], 5)
        self.assertEqual(phases[4]["ascendancyPoints"], 2)
        self.assertEqual(phases[7]["ascendancyPoints"], 4)
        self.assertEqual(phases[10]["ascendancyPoints"], 6)
        self.assertEqual(phases[5]["resistancePenalty"], -30)
        self.assertEqual(phases[10]["resistancePenalty"], -60)
        self.assertEqual(phases[10]["questPoints"], 24)
        self.assertEqual(phases[1]["level"] - 1 + phases[1]["questPoints"], 13)

    def test_gems_wait_for_unlock_and_never_exceed_character_level(self):
        gem = {"levels": [{"level": 1, "requiredLevel": 28}, {"level": 2, "requiredLevel": 31},
                           {"level": 3, "requiredLevel": 34}]}
        self.assertEqual(gem_level(gem, 27), 0)
        self.assertEqual(gem_level(gem, 32), 2)
        self.assertEqual(gem_level(gem, 90, cap=2), 2)

    def test_transfigured_and_vaal_gems_are_not_required_to_level(self):
        def entry(name, minimum, **extra):
            return {"id": name, "gameId": name, "skillId": name, "name": name, "tags": {},
                    "levels": [{"level": 1, "requiredLevel": minimum}], **extra}
        data = GameData({"gems": [entry("Fireball", 1), entry("Blight", 1), entry("Bane", 24),
                                 entry("Bane of Condemnation", 24, baseName="Bane"),
                                 entry("Summon Skeletons", 10), entry("Vaal Summon Skeletons", 10, baseName="Vaal Summon Skeletons")],
                         "bases": {}, "mods": []})
        spec = {"skill": "Bane of Condemnation", "archetype": "dot", "damageType": "chaos"}
        self.assertEqual(stage_skill(spec, data, milestones(90)[1]), "Blight")
        self.assertEqual(stage_skill(spec, data, milestones(90)[3]), "Bane")
        self.assertEqual(stage_skill(spec, data, milestones(90)[-1]), "Bane of Condemnation")
        minion = {"skill": "Vaal Summon Skeletons", "archetype": "minion", "damageType": "physical"}
        self.assertEqual(stage_skill(minion, data, milestones(90)[1]), "Summon Skeletons")

    def test_leveling_affixes_respect_item_level_and_text(self):
        item = RareItem("Helmet", "Vine Circlet", {"tags": {"helmet": True}}, item_level=12, quality=0)
        mod = {"level": 20, "kind": "Prefix", "group": "Life", "weightKey": ["helmet"], "weightVal": [100]}
        self.assertFalse(item.can_add(mod))
        self.assertIn("Item Level: 12", item.text(3))
        self.assertIn("Quality: 0", item.text(3))

    def test_connected_order_does_not_allocate_mastery_before_its_notable(self):
        nodes = {"1": {"classStartIndex": 3, "out": ["2"]},
                 "2": {"out": ["3"], "stats": []},
                 "3": {"isNotable": True, "group": 8, "stats": []},
                 "4": {"isMastery": True, "group": 8, "stats": ["100% increased Spell Damage"]}}
        order = connected_order(nodes, set(nodes), "1", {"archetype": "spell", "damageType": "cold", "focus": "balanced"}, {"4": 5})
        self.assertEqual(order, ["1", "2", "3", "4"])
        with self.assertRaisesRegex(ValueError, "connected"):
            connected_order(nodes, {"1", "3"}, "1", {}, {})

    def test_each_lab_allocates_a_complete_notable_path(self):
        nodes = {"1": {"isAscendancyStart": True, "out": ["2", "4"]},
                 "2": {"out": ["3"], "stats": ["100% increased Spell Damage"]},
                 "3": {"isNotable": True, "stats": ["20% increased Spell Damage"]},
                 "4": {"out": ["5"], "stats": ["90% increased Spell Damage"]},
                 "5": {"isNotable": True, "stats": ["10% increased Spell Damage"]}}
        for node in nodes.values():
            node["ascendancyName"] = "Elementalist"
        order = ascendancy_order(nodes, set(nodes), "1", {"archetype": "spell", "damageType": "cold", "focus": "balanced"})
        self.assertEqual(order[:3], ["1", "2", "3"])
        self.assertEqual(order[3:], ["4", "5"])

    def test_matching_loadouts_remap_colliding_item_ids_and_select_endgame(self):
        def document(name):
            return f'''<PathOfBuilding><Build level="90"/><Tree activeSpec="1"><Spec nodes="1"/></Tree>
                <Skills activeSkillSet="1"><SkillSet id="1"/></Skills>
                <Items activeItemSet="1"><Item id="1">{name}</Item><ItemSet id="1"><Slot name="Helmet" itemId="1"/></ItemSet></Items>
                <Config activeConfigSet="1"><ConfigSet id="1"/></Config></PathOfBuilding>'''
        phases = [{"title": "Act 1 - Level 12"}, {"title": "Endgame - Level 90"}]
        xml = combine_loadouts([document("Campaign helmet"), document("Endgame helmet")], phases, "Leveling instructions")
        root = ET.fromstring(xml)
        titles = [phase["title"] for phase in phases]
        for path in ("./Tree/Spec", "./Skills/SkillSet", "./Items/ItemSet", "./Config/ConfigSet"):
            self.assertEqual([item.get("title") for item in root.findall(path)], titles)
        ids = {item.get("id"): item.text for item in root.findall("./Items/Item")}
        slots = root.findall("./Items/ItemSet/Slot")
        self.assertEqual([ids[slot.get("itemId")] for slot in slots], ["Campaign helmet", "Endgame helmet"])
        self.assertEqual(root.find("Tree").get("activeSpec"), "2")
        stage = ET.fromstring(select_stage(xml, 1, 12))
        self.assertEqual(stage.find("Build").get("level"), "12")
        self.assertEqual(stage.find("Items").get("activeItemSet"), "1")

    def test_ascendancy_granted_skills_do_not_require_gem_levels_or_sockets(self):
        data = GameData({"gems": [{"id": "Fireball", "gameId": "Fireball", "skillId": "Fireball", "name": "Fireball",
                                  "levels": [{"level": 1, "requiredLevel": 1}]}],
                         "bases": {"Simple Robe": {"req": {"level": 1}}}, "mods": []})
        xml = '''<PathOfBuilding><Build mainSocketGroup="1"/><Tree activeSpec="1"><Spec nodes="1"/></Tree>
          <Skills activeSkillSet="1"><SkillSet id="1"><Skill slot="Body Armour"><Gem gemId="Fireball" nameSpec="Fireball" level="1"/></Skill>
          <Skill><Gem skillId="PrimalAegis" nameSpec="" level="20"/></Skill></SkillSet>
          <SkillSet id="2"><Skill><Gem gemId="Fireball" nameSpec="Fireball" level="20"/></Skill></SkillSet></Skills>
          <Items activeItemSet="1"><Item id="1">Rarity: NORMAL
Simple Robe
Sockets: B</Item><ItemSet id="1"><Slot name="Body Armour" itemId="1"/></ItemSet></Items></PathOfBuilding>'''
        calc = {"calculated": True, "stats": {"Life": 4000, "CombinedDPS": 10, "Str": 100, "Dex": 100, "Int": 100,
                                             "FireResist": 75, "ColdResist": 75, "LightningResist": 75, "ManaUnreserved": 100},
                "passives": {"used": 0, "ascendancy": 0}}
        checks = stage_checks(xml, milestones(90)[-1], calc, data, {"tree": {"nodes": {"1": {"classStartIndex": 3}}}})
        self.assertTrue(next(check["passed"] for check in checks if check["name"] == "Stage gem levels"))
        self.assertTrue(next(check["passed"] for check in checks if check["name"] == "Stage sockets"))

    def test_stage_socket_check_uses_the_selected_merged_item_set(self):
        data = GameData({"gems": [{"id": "Fireball", "gameId": "Fireball", "skillId": "Fireball",
                                   "name": "Fireball", "levels": [{"level": 1, "requiredLevel": 1}]}],
                         "bases": {"Simple Robe": {"req": {"level": 1}}}, "mods": []})
        xml = '''<PathOfBuilding><Build level="90"/><Tree activeSpec="2"><Spec nodes="1"/>
          <Spec nodes="1"/></Tree><Skills activeSkillSet="2"><SkillSet id="1"><Skill slot="Body Armour">
          <Gem gemId="Fireball" nameSpec="Fireball" level="1"/><Gem gemId="Fireball" nameSpec="Fireball" level="1"/></Skill></SkillSet>
          <SkillSet id="2"><Skill slot="Body Armour"><Gem gemId="Fireball" nameSpec="Fireball" level="1"/></Skill></SkillSet></Skills>
          <Items activeItemSet="2"><Item id="1">Rarity: NORMAL
Simple Robe</Item><Item id="2">Rarity: NORMAL
Simple Robe
Sockets: B</Item><ItemSet id="1"><Slot name="Body Armour" itemId="1"/></ItemSet>
          <ItemSet id="2"><Slot name="Body Armour" itemId="2"/></ItemSet></Items>
          <Config activeConfigSet="2"><ConfigSet id="1"/><ConfigSet id="2"/></Config></PathOfBuilding>'''
        calc = {"calculated": True, "stats": {"Life": 4000, "FullDPS": 10, "Str": 100, "Dex": 100,
                  "Int": 100, "FireResist": 75, "ColdResist": 75, "LightningResist": 75,
                  "ManaUnreserved": 100}, "passives": {"used": 0, "ascendancy": 0}}
        checks = stage_checks(xml, milestones(90)[-1], calc, data,
                              {"tree": {"nodes": {"1": {"classStartIndex": 3}}}})
        self.assertTrue(next(check["passed"] for check in checks if check["name"] == "Stage sockets"))

    def test_minion_equipment_references_follow_their_stage_item_set(self):
        def document(set_id):
            return f'''<PathOfBuilding><Build level="90"/><Tree><Spec nodes="1"/></Tree>
              <Skills><SkillSet id="1"><Skill><Gem nameSpec="Animate Weapon"
                skillMinionItemSet="{set_id}" skillMinionItemSetCalcs="{set_id}"/>
                <Gem nameSpec="Minion Damage"/></Skill></SkillSet></Skills>
              <Items><Item id="1">Stage weapon</Item><ItemSet id="{set_id}">
                <Slot name="Weapon 1" itemId="1"/></ItemSet></Items>
              <Config><ConfigSet id="1"/></Config></PathOfBuilding>'''
        phases = [{"title": "Act 3 - Level 32"}, {"title": "Endgame - Level 90"}]
        root = ET.fromstring(combine_loadouts([document(1), document(7)], phases, ""))
        for skill_set, item_set in zip(root.findall("./Skills/SkillSet"), root.findall("./Items/ItemSet")):
            minion, support = skill_set.findall("./Skill/Gem")
            for attr in ("skillMinionItemSet", "skillMinionItemSetCalcs"):
                self.assertEqual(minion.get(attr), item_set.get("id"))
                self.assertIsNone(support.get(attr))


if __name__ == "__main__":
    unittest.main()
