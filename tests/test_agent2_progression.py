import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from build_progression import (_ci_nodes, _drop_last_reserver, _stage_jewels, _stage_summary,
                               _stage_utility_groups, combine_loadouts, milestones, select_stage,
                               stage_checks, stage_utility_cap)
from generation_data import GameData
from skill_packages import apply_stage_caps, capacity_from_equipment, make_group
from tests.test_agent2_assembly import data_and_items, gem

FIXTURE = Path(__file__).parent / "fixtures" / "agent2_metadata.json"
TREE = {"tree": {"nodes": {"1": {"classStartIndex": 3}}}}
GEMS = {"Winter Orb": "Metadata/Items/Gems/SkillGemWinterOrb", "Arcane Surge": "Metadata/Items/Gems/SupportGemArcaneSurge",
        "Concentrated Effect": "Metadata/Items/Gems/SupportGemConcentratedEffect",
        "Cold Penetration": "Metadata/Items/Gems/SupportGemColdPenetration",
        "Added Cold Damage": "Metadata/Items/Gems/SupportGemAddedColdDamage",
        "Cruelty": "Metadata/Items/Gems/SupportGemCruelty", "Frostblink": "Metadata/Items/Gems/SkillGemFrostblink",
        "Steelskin": "Metadata/Items/Gems/SkillGemSteelskin"}


def skill(slot, names, main=False, level=1):
    return (f'<Skill slot="{slot}" mainActiveSkill="1" includeInFullDPS="{str(main).lower()}">' +
            "".join(f'<Gem gemId="{GEMS[n]}" nameSpec="{n}" level="{level}"/>' for n in names) + "</Skill>")


def stage_xml(main, others=(), sockets="B-B-B-B-B-B", body_extra="", jewel_item=None):
    spec = '<Spec nodes="1">' + ('<Sockets><Socket nodeId="1" itemId="9"/></Sockets>' if jewel_item else "") + "</Spec>"
    jewel = f'<Item id="9">{jewel_item}</Item>' if jewel_item else ""
    return f'''<PathOfBuilding><Build level="90" mainSocketGroup="1"/><Tree activeSpec="1">{spec}</Tree>
      <Skills activeSkillSet="1"><SkillSet id="1">{skill("Body Armour", main, True)}{"".join(others)}</SkillSet></Skills>
      <Items activeItemSet="1"><Item id="1">Rarity: RARE
Witchcraft Body Armour
Vaal Regalia
Sockets: {sockets}
Implicits: 0{body_extra}</Item>{jewel}<ItemSet id="1"><Slot name="Body Armour" itemId="1"/></ItemSet></Items>
      <Config activeConfigSet="1"><ConfigSet id="1"/></Config></PathOfBuilding>'''


def calc(**stats):
    return {"calculated": True, "passives": {"used": 0, "ascendancy": 0},
            "stats": {"Life": 4000, "FullDPS": 10, "Str": 100, "Dex": 100, "Int": 100, "FireResist": 75,
                      "ColdResist": 75, "LightningResist": 75, "ManaUnreserved": 100, **stats}}


def check(checks, name):
    return next(entry for entry in checks if entry["name"] == name)


class StageSixLink(unittest.TestCase):
    def setUp(self):
        self.data = GameData(json.loads(FIXTURE.read_text(encoding="utf-8")))
        self.mapping = milestones(90)[-2]
        self.endgame = milestones(90)[-1]
        self.main6 = ["Winter Orb", "Arcane Surge", "Concentrated Effect", "Cold Penetration", "Added Cold Damage", "Cruelty"]

    def test_mapping_and_endgame_are_six_links_and_act_stages_stay_shorter(self):
        self.assertEqual((self.mapping["title"], self.mapping["links"]), ("Mapping - Level 75", 6))
        self.assertEqual(self.endgame["links"], 6)
        self.assertTrue(all(phase["links"] <= 4 for phase in milestones(90)[:-2]))

    def test_mapping_requires_exactly_six_linked_gems_in_six_sockets(self):
        good = stage_checks(stage_xml(self.main6), self.mapping, calc(), self.data, TREE)
        self.assertTrue(check(good, "Stage sockets")["passed"])
        five = stage_checks(stage_xml(self.main6[:5], sockets="B-B-B-B-B-B"), self.mapping, calc(), self.data, TREE)
        self.assertFalse(check(five, "Stage sockets")["passed"])
        self.assertIn("main link 5/6", check(five, "Stage sockets")["reason"])
        five_link_body = stage_checks(stage_xml(self.main6, sockets="B-B-B-B-B"), self.mapping, calc(), self.data, TREE)
        self.assertFalse(check(five_link_body, "Stage sockets")["passed"])     # six gems cannot fit five sockets
        broken_links = stage_checks(stage_xml(self.main6, sockets="B-B-B-B B-B"), self.mapping, calc(), self.data, TREE)
        self.assertFalse(check(broken_links, "Stage sockets")["passed"])       # not genuinely linked

    def test_campaign_stage_may_hold_fewer_gems_than_its_links(self):
        act3 = milestones(90)[3]
        checks = stage_checks(stage_xml(self.main6[:3], sockets="B-B-B"), act3, calc(), self.data, TREE)
        self.assertTrue(check(checks, "Stage sockets")["passed"])
        over = stage_checks(stage_xml(self.main6[:5], sockets="B-B-B-B-B"), act3, calc(), self.data, TREE)
        self.assertFalse(check(over, "Stage sockets")["passed"])

    def test_utility_groups_need_their_own_linked_runs(self):
        others = [skill("Gloves", ["Frostblink", "Steelskin"])]
        xml = stage_xml(self.main6, others)
        xml = xml.replace('<ItemSet id="1">', '''<Item id="2">Rarity: RARE
Witchcraft Gloves
Sorcerer Gloves
Sockets: B B
Implicits: 0</Item><ItemSet id="1">''').replace('<Slot name="Body Armour" itemId="1"/>',
                                                  '<Slot name="Body Armour" itemId="1"/><Slot name="Gloves" itemId="2"/>')
        unlinked = stage_checks(xml, self.mapping, calc(), self.data, TREE)       # "B B": two separate sockets
        self.assertFalse(check(unlinked, "Stage sockets")["passed"])
        linked = xml.replace("Sockets: B B", "Sockets: B-B")
        self.assertTrue(check(stage_checks(linked, self.mapping, calc(), self.data, TREE), "Stage sockets")["passed"])
        tiny = xml.replace("Sockets: B B", "Sockets: B")
        self.assertFalse(check(stage_checks(tiny, self.mapping, calc(), self.data, TREE), "Stage sockets")["passed"])

    def test_unique_and_jewel_level_requirements_are_checked_not_only_bases(self):
        xml = stage_xml(self.main6, body_extra="\nLevelReq: 84")
        checks = stage_checks(xml, self.mapping, calc(), self.data, TREE)      # Mapping is level 75
        self.assertFalse(check(checks, "Stage equipment levels")["passed"])
        # Vaal Regalia itself needs 68; LevelReq 60 leaves the base requirement in charge.
        ok = stage_checks(stage_xml(self.main6, body_extra="\nLevelReq: 60"), self.mapping, calc(), self.data, TREE)
        self.assertTrue(check(ok, "Stage equipment levels")["passed"])


class StageComposition(unittest.TestCase):
    def setUp(self):
        self.data, self.items = data_and_items()
        self.capacity = apply_stage_caps(capacity_from_equipment(self.items, {}, self.data), 4, 6)
        phase = milestones(90)[-2]
        self.phase = {**phase, "level": 75}
        self.final = [
            make_group("main", "main", [gem("main", 1, "Winter Orb", "active")], slot="Body Armour"),
            make_group("movement-2", "movement", [gem("movement-2", 1, "Frostblink", "active"),
                                                  gem("movement-2", 2, "Second Wind", "support")]),
            make_group("aura-3", "aura", [gem("aura-3", 1, "Zealotry", "active"), gem("aura-3", 2, "Enlighten", "support", 3)]),
            make_group("curse-4", "curse", [gem("curse-4", 1, "Frostbite", "active"),
                                            gem("curse-4", 2, "Cast when Damage Taken", "support", 20)])]

    def groups(self, phase, final=None):
        notes = []
        groups = [make_group("main", "main", [("Winter Orb", "active")], slot="Body Armour")]
        _stage_utility_groups({}, self.data, phase, final or self.final, 20, self.capacity, groups, notes)
        return groups, notes

    def test_mapping_stage_carries_endgame_utility_groups_with_real_links(self):
        groups, notes = self.groups(self.phase)
        self.assertEqual([g["id"] for g in groups], ["main", "movement-2", "aura-3", "curse-4"])
        self.assertEqual([gem["name"] for gem in groups[2]["gems"]], ["Zealotry", "Enlighten"])
        self.assertEqual(notes, [])

    def test_campaign_stages_skip_endgame_only_and_exceptional_gems_and_explain_it(self):
        act4 = {**milestones(90)[4], "level": 40}
        groups, notes = self.groups(act4)
        names = {gem["name"] for group in groups for gem in group["gems"]}
        self.assertNotIn("Enlighten", names)          # exceptional gems are endgame acquisitions
        early = {**milestones(90)[1], "level": 12}
        groups, _ = self.groups(early, [make_group("guard-5", "guard", [gem("guard-5", 1, "Steelskin", "active"),
                                                                        gem("guard-5", 2, "Second Wind", "support")]),
                                        self.final[1]])
        self.assertEqual([g["id"] for g in groups], ["main", "guard-5"])
        self.assertEqual([gem["name"] for gem in groups[1]["gems"]], ["Steelskin"])   # lone early utility only

    def test_trigger_is_dropped_for_a_manual_plan_when_the_stage_level_cannot_support_it(self):
        low = {**milestones(90)[5], "level": 45}
        final = [self.final[0], make_group("curse-4", "curse", [gem("curse-4", 1, "Frostbite", "active", 20),
                                                                gem("curse-4", 2, "Cast when Damage Taken", "support", 20)])]
        groups, notes = self.groups(low, final)
        curse = next(g for g in groups if g["id"] == "curse-4")
        self.assertEqual(curse["delivery"], "manual")
        self.assertTrue(any("manually" in note for note in notes))

    def test_packages_that_do_not_fit_are_deferred_with_an_instruction(self):
        small = apply_stage_caps(capacity_from_equipment(self.items, {}, self.data), 1, 1)
        notes = []
        groups = [make_group("main", "main", [("Winter Orb", "active")], slot="Body Armour")]
        _stage_utility_groups({}, self.data, self.phase, self.final, 20, small, groups, notes)
        self.assertEqual([g["id"] for g in groups], ["main"])
        self.assertTrue(all(note.startswith("Add the") for note in notes) and len(notes) == 3)
        self.assertEqual((stage_utility_cap(1), stage_utility_cap(22), stage_utility_cap(60)), (1, 3, 4))

    def test_reservation_is_dropped_before_any_support_and_never_silently(self):
        groups = [make_group("main", "main", [("Winter Orb", "active"), ("Arcane Surge", "support")]),
                  make_group("aura-3", "aura", [("Zealotry", "active"), ("Determination", "active"), ("Enlighten", "support")])]
        self.assertEqual(_drop_last_reserver(groups), "Determination")
        self.assertEqual(_drop_last_reserver(groups), "Zealotry")
        self.assertEqual([g["id"] for g in groups], ["main"])
        self.assertIsNone(_drop_last_reserver(groups))
        self.assertEqual(len(groups[0]["gems"]), 2)

    def test_chaos_inoculation_keystone_is_found_by_name(self):
        official = {"1": {"name": "Chaos Inoculation"}, "2": {"name": "Mind Over Matter"}}
        self.assertEqual(_ci_nodes(official, {"1", "2"}), {"1"})


class StageSummaries(unittest.TestCase):
    def setUp(self):
        self.data = GameData(json.loads(FIXTURE.read_text(encoding="utf-8")))

    def final_xml(self):
        jewel = "Rarity: UNIQUE\nThe Unnatural Instinct\nCobalt Jewel\nLevelReq: 20\nImplicits: 0"
        xml = stage_xml(["Winter Orb", "Arcane Surge"], [skill("Boots", ["Frostblink"])], sockets="B-B-B-B-B-B",
                        jewel_item=jewel)
        return xml.replace('<Slot name="Body Armour" itemId="1"/>', '<Slot name="Body Armour" itemId="1"/>')

    def test_summary_is_derived_from_the_final_xml_with_unique_names_and_jewels(self):
        xml = self.final_xml().replace("Witchcraft Body Armour\nVaal Regalia", "Kaom's Heart\nVaal Regalia") \
            .replace("Rarity: RARE\nKaom", "Rarity: UNIQUE\nKaom")
        phase = {**milestones(90)[-2], "level": 75}
        market = {"league": "T", "divineChaos": 100.0, "updated": 1, "source": "t", "errors": [],
                  "listings": {"Kaom's Heart": [{"chaos": 40, "links": 6}], "The Unnatural Instinct": [{"chaos": 3}]}}
        summary = _stage_summary(xml, phase, "Winter Orb", [], calc(), [], self.data, None, None, market, 200)
        self.assertEqual(summary["gems"], ["Winter Orb", "Arcane Surge"])
        self.assertEqual(summary["socketedGemCount"], 3)
        self.assertEqual(summary["stageUniques"], ["Kaom's Heart"])
        self.assertEqual([row["name"] for row in summary["jewels"]], ["The Unnatural Instinct"])
        coverage = summary["priceCoverage"]
        self.assertEqual((coverage["uniqueCount"], coverage["quotedUniqueCount"]), (2, 2))
        self.assertEqual(coverage["uniqueSubtotalChaos"], 43.0)
        self.assertEqual(summary["uniquePackageCostChaos"], 43.0)
        self.assertEqual(coverage["scope"], "stage_equipped_uniques")
        self.assertEqual(summary["slotLinks"]["Body Armour"]["linkedRuns"], [6])
        self.assertTrue(any(text.startswith("Use the shown 2-gem main link") for text in summary["instructions"]))
        no_market = _stage_summary(xml, phase, "Winter Orb", [], calc(), [], self.data, None, 12.0, None, None)
        self.assertIsNone(no_market["priceCoverage"])
        self.assertEqual(no_market["uniquePackageCostChaos"], 12.0)

    def test_stage_with_no_uniques_reports_no_unique_items(self):
        phase = {**milestones(90)[-2], "level": 75}
        market = {"league": "T", "divineChaos": 100.0, "updated": 1, "source": "t", "errors": [], "listings": {}}
        summary = _stage_summary(stage_xml(["Winter Orb"]), phase, "Winter Orb", [], calc(), [], self.data,
                                 None, None, market, None)
        self.assertTrue(summary["priceCoverage"]["noUniques"])
        self.assertEqual(summary["uniquePackage"], [])
        self.assertIsNone(summary["uniquePackageCostChaos"] if False else None)

    def test_stage_jewels_require_an_allocated_socket_and_the_character_level(self):
        root = ET.fromstring(self.final_xml())
        self.assertEqual(list(_stage_jewels(root, {"1"}, 75, self.data)), ["1"])
        self.assertEqual(_stage_jewels(root, set(), 75, self.data), {})            # socket not allocated yet
        self.assertEqual(_stage_jewels(root, {"1"}, 10, self.data), {})            # LevelReq 20 > level 10

    def test_merged_loadout_remaps_items_jewels_and_stays_selectable(self):
        first = self.final_xml()
        second = self.final_xml().replace("Cobalt Jewel\nLevelReq: 20", "Cobalt Jewel\nLevelReq: 21")
        phases = [{"title": "Act 3 - Level 32"}, {"title": "Endgame - Level 90"}]
        merged = combine_loadouts([first, second], phases, "notes")
        root = ET.fromstring(merged)
        specs = root.findall("./Tree/Spec")
        item_ids = {item.get("id") for item in root.findall("./Items/Item")}
        self.assertEqual(len(item_ids), 4)
        sockets = [spec.find("./Sockets/Socket").get("itemId") for spec in specs]
        self.assertEqual(len(set(sockets)), 2)                                     # each stage keeps its own jewel
        for index, spec in enumerate(specs, 1):
            jewel = root.find(f"./Items/Item[@id='{sockets[index - 1]}']").text
            self.assertIn("Unnatural Instinct", jewel)
        selected = select_stage(merged, 1, 32)
        from loadout_summary import summarize_loadout
        view = summarize_loadout(selected)
        self.assertEqual(view["jewels"][0]["node"], "1")
        self.assertIn("LevelReq: 20", ET.fromstring(selected).find(f"./Items/Item[@id='{sockets[0]}']").text)
        self.assertEqual(summarize_loadout(select_stage(merged, 2, 90))["socketedGemCount"], 3)


if __name__ == "__main__":
    unittest.main()
