import copy
import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from build_assembly import SocketConflict, assemble, assemble_with_report
from generation_data import GameData, RareItem
from loadout_summary import active_spec, loadout_view, parse_item, summarize_loadout
from skill_packages import make_group, to_contract_groups

FIXTURE = Path(__file__).parent / "fixtures" / "agent2_metadata.json"
CONTEXT = {"treeVersion": "3_29", "tree": {"classes": [
    {"name": "Scion", "ascendancies": []},
    {"name": "Witch", "ascendancies": [{"name": "Occultist"}, {"name": "Elementalist"}]}]}}
SPEC = {"skill": "Winter Orb", "ascendancy": "Occultist", "level": 90, "enemyLevel": 83}


def data_and_items():
    data = GameData(json.loads(FIXTURE.read_text(encoding="utf-8")))
    bases = (("Body Armour", "Vaal Regalia"), ("Helmet", "Hubris Circlet"), ("Gloves", "Sorcerer Gloves"),
             ("Boots", "Sorcerer Boots"), ("Weapon 1", "Prophecy Wand"), ("Weapon 2", "Titanium Spirit Shield"),
             ("Amulet", "Amber Amulet"), ("Ring 1", "Coral Ring"))
    return data, [RareItem(slot, base, data.bases[base]) for slot, base in bases]


def gem(group, position, name, kind, level=20, **extra):
    return {"instance": f"{group}:{position}", "name": name, "kind": kind, "level": level,
            "quality": 0, "enabled": True, "count": 1, **extra}


def groups():
    return [
        make_group("main", "main", [gem("main", 1, "Winter Orb", "active"), gem("main", 2, "Arcane Surge", "support", 20),
                                    gem("main", 3, "Concentrated Effect", "support"),
                                    gem("main", 4, "Cold Penetration", "support"), gem("main", 5, "Added Cold Damage", "support"),
                                    gem("main", 6, "Cruelty", "support")],
                   slot="Body Armour", main_active="Winter Orb", include_in_full_dps=True),
        make_group("curse", "curse", [gem("curse", 1, "Frostbite", "active"),
                                      gem("curse", 2, "Arcane Surge", "support", 12)], slot="Gloves"),
        make_group("offerings", "offering", [gem("offerings", 1, "Desecrate", "active"),
                                             gem("offerings", 2, "Flesh Offering", "active"),
                                             gem("offerings", 3, "Bone Offering", "active")], slot="Boots",
                   main_active="Flesh Offering"),
        make_group("move", "movement", [gem("move", 1, "Flame Dash", "active")], slot="Boots"),
        make_group("guard", "guard", [gem("guard", 1, "Steelskin", "active", 10)], slot="Gloves"),
    ]


def build(**kwargs):
    data, items = data_and_items()
    return data, items, assemble_with_report(SPEC, CONTEXT, data, {"1", "2"}, [], items, {}, kwargs.pop("uniques", None),
                                             None, skill_groups=kwargs.pop("skill_groups", groups()), **kwargs)


class StructuredAssembly(unittest.TestCase):
    def test_real_linked_groups_per_slot_with_runs_from_the_groups(self):
        data, items, (xml, report) = build()
        root = ET.fromstring(xml)
        self.assertEqual(root.find("Build").get("className"), "Witch")
        self.assertEqual(root.find("./Tree/Spec").get("classId"), "1")
        summary = summarize_loadout(xml)
        self.assertEqual(summary["socketedGemCount"], 6 + 2 + 3 + 1 + 1)
        body = summary["bySlot"]["Body Armour"]
        self.assertEqual((body["linkedRuns"], body["socketsUsed"]), ([6], 6))
        boots = summary["bySlot"]["Boots"]
        self.assertEqual(boots["linkedRuns"], [3, 1])        # one run for the 3-gem group, one socket per lone gem
        self.assertEqual(boots["socketsSpare"], 0)
        self.assertEqual(summary["bySlot"]["Gloves"]["linkedRuns"], [2, 1])
        self.assertEqual(summary["unplacedGroups"], [])
        self.assertEqual(report["socketedGemCount"], 13)
        self.assertEqual(report["slots"]["Boots"]["linkedRuns"], [3, 1])

    def test_main_active_skill_is_the_selected_active_gem_and_main_group_is_first(self):
        _, _, (xml, _) = build()
        root = ET.fromstring(xml)
        self.assertEqual(root.find("Build").get("mainSocketGroup"), "1")
        skills = root.findall("./Skills/SkillSet/Skill")
        offerings = next(s for s in skills if s.get("label") == "Offerings")
        self.assertEqual(offerings.get("mainActiveSkill"), "2")        # Flesh Offering is the 2nd active gem
        self.assertEqual(skills[0].get("includeInFullDPS"), "true")
        self.assertTrue(all(s.get("includeInFullDPS") == "false" for s in skills[1:]))
        _, _, (probe, _) = build(main_group_id="curse")
        self.assertEqual(ET.fromstring(probe).find("Build").get("mainSocketGroup"),
                         str(1 + [s.get("label") for s in skills].index("Curse")))

    def test_repeated_supports_keep_independent_per_instance_levels(self):
        _, _, (xml, _) = build()
        levels = [(g.get("nameSpec"), g.get("level")) for g in ET.fromstring(xml).iter("Gem")
                  if g.get("nameSpec") == "Arcane Surge"]
        self.assertEqual(levels, [("Arcane Surge", "20"), ("Arcane Surge", "12")])
        steelskin = next(g for g in ET.fromstring(xml).iter("Gem") if g.get("nameSpec") == "Steelskin")
        self.assertEqual(steelskin.get("level"), "10")

    def test_assembly_is_pure(self):
        data, items = data_and_items()
        spec, planned = copy.deepcopy(SPEC), groups()
        snapshot = (copy.deepcopy(spec), copy.deepcopy(planned), [(i.slot, i.base, list(i.mods)) for i in items])
        first = assemble(spec, CONTEXT, data, {"1"}, [], items, skill_groups=planned)
        second = assemble(spec, CONTEXT, data, {"1"}, [], items, skill_groups=planned)
        self.assertEqual(first, second)
        self.assertEqual(snapshot, (spec, planned, [(i.slot, i.base, list(i.mods)) for i in items]))

    def test_physically_impossible_groups_fail_with_specific_socket_conflicts(self):
        too_big = groups() + [make_group("extra", "aura", [gem("extra", i, n, "active") for i, n in
                                                           enumerate(("Clarity", "Hatred", "Zealotry", "Grace"), 1)],
                                         slot="Helmet"), make_group("extra2", "aura", [gem("extra2", i, n, "active") for i, n in
                                                           enumerate(("Clarity", "Hatred", "Zealotry", "Grace", "Wrath"), 1)],
                                         slot="Helmet")]
        data, items = data_and_items()
        with self.assertRaises(SocketConflict) as caught:
            assemble(SPEC, CONTEXT, data, {"1"}, [], items, skill_groups=too_big)
        self.assertIn("extra2", str(caught.exception))
        self.assertTrue(caught.exception.problems)
        # Jewellery has no sockets at all.
        amulet = [groups()[0], make_group("a", "utility", [gem("a", 1, "Clarity", "active")], slot="Amulet")]
        with self.assertRaises(SocketConflict):
            assemble(SPEC, CONTEXT, data, {"1"}, [], items, skill_groups=amulet)

    def test_unplaced_groups_are_packed_into_slots_with_room(self):
        data, items = data_and_items()
        floating = [groups()[0], make_group("aura", "aura", [gem("aura", 1, "Zealotry", "active"),
                                                              gem("aura", 2, "Determination", "active"),
                                                              gem("aura", 3, "Enlighten", "support", 3)])]
        xml, report = assemble_with_report(SPEC, CONTEXT, data, {"1"}, [], items, skill_groups=floating)
        self.assertIn(report["placements"]["aura"], {"Helmet", "Gloves", "Boots"})   # armour before weapons
        self.assertEqual(summarize_loadout(xml)["unplacedGroups"], [])

    def test_contract_skill_groups_are_accepted(self):
        data, items = data_and_items()
        xml = assemble(SPEC, CONTEXT, data, {"1"}, [], items, skill_groups=to_contract_groups(groups()))
        self.assertEqual(summarize_loadout(xml)["socketedGemCount"], 13)

    def test_flexible_unique_is_relinked_but_fixed_socket_unique_is_kept(self):
        data, items = data_and_items()
        boots = "Rarity: UNIQUE\nExample Boots\nSorcerer Boots\nSockets: B\nImplicits: 0\n+30 to maximum Life"
        gloves = ("Rarity: UNIQUE\nFixed Gloves\nSorcerer Gloves\nSockets: W-W\nHas 2 Linked Sockets\nImplicits: 0\n"
                  "+30 to maximum Life")
        xml, report = assemble_with_report(SPEC, CONTEXT, data, {"1"}, [], items,
                                           uniques={"Boots": boots, "Gloves": gloves}, skill_groups=[
                                               groups()[0], groups()[2],
                                               make_group("p", "aura", [gem("p", 1, "Clarity", "active"),
                                                                        gem("p", 2, "Discipline", "active")],
                                                          slot="Gloves")])
        summary = summarize_loadout(xml)
        self.assertEqual(summary["bySlot"]["Boots"]["linkedRuns"], [3])
        self.assertEqual(summary["bySlot"]["Gloves"]["linkedRuns"], [2])
        self.assertEqual(summary["bySlot"]["Boots"]["item"]["name"], "Example Boots")
        # Fixed layout cannot take a 3-gem group.
        with self.assertRaises(SocketConflict):
            assemble(SPEC, CONTEXT, data, {"1"}, [], items, uniques={"Gloves": gloves},
                     skill_groups=[groups()[0], make_group("q", "aura", [gem("q", i, n, "active") for i, n in
                                                                         enumerate(("Clarity", "Hatred", "Grace"), 1)],
                                                           slot="Gloves")])

    def test_item_granted_skills_are_never_emitted_as_socketed_groups(self):
        _, _, (xml, report) = build()
        root = ET.fromstring(xml)
        self.assertTrue(all(skill.get("source") is None for skill in root.findall("./Skills/SkillSet/Skill")))
        self.assertEqual(report["itemGrantedSkillsEmitted"], 0)

    def test_body_armour_run_extends_to_the_main_link_policy(self):
        data, items = data_and_items()
        short = [make_group("main", "main", [gem("main", 1, "Winter Orb", "active"), gem("main", 2, "Arcane Surge", "support")],
                            slot="Body Armour", main_active="Winter Orb")]
        xml, _ = assemble_with_report({**SPEC, "mainLinks": 6}, CONTEXT, data, {"1"}, [], items, skill_groups=short)
        view = summarize_loadout(xml)
        self.assertEqual(view["bySlot"]["Body Armour"]["linkedRuns"], [6])
        self.assertEqual(view["bySlot"]["Body Armour"]["socketsSpare"], 4)    # surfaced as a gap by completeness

    def test_loadout_view_is_compact_json_and_counts_jewels_and_uniques(self):
        data, items = data_and_items()
        unique_jewel = "Rarity: UNIQUE\nThe Unnatural Instinct\nCobalt Jewel\nImplicits: 0\nAllocates nodes"
        xml = assemble(SPEC, CONTEXT, data, {"1", "2"}, [], items, {}, {"Amulet": "Rarity: UNIQUE\nAstramentis\nStellar Amulet"},
                       {"2": unique_jewel}, skill_groups=groups())
        view = loadout_view(xml)
        json.dumps(view)
        self.assertEqual(view["counts"]["socketedGems"], 13)
        self.assertEqual(view["counts"]["jewels"], 1)
        self.assertEqual(view["jewels"][0]["name"], "The Unnatural Instinct")
        self.assertTrue(view["jewels"][0]["isUnique"])
        self.assertEqual(view["counts"]["uniques"], 2)
        self.assertEqual(view["slots"]["Body Armour"]["groups"], [1])
        self.assertIn("Amulet", [row["slot"] for row in view["gear"] if row["isUnique"]])

    def test_active_spec_is_positional_and_does_not_need_ids(self):
        root = ET.fromstring('<PathOfBuilding><Tree activeSpec="2"><Spec title="a" nodes="1"/><Spec title="b" nodes="2"/></Tree></PathOfBuilding>')
        self.assertEqual(active_spec(root).get("title"), "b")

    def test_legacy_inputs_still_assemble(self):
        data, items = data_and_items()
        spec = {**SPEC, "utility": {"Flame Dash": "Boots", "Steelskin": "Gloves"}}
        xml = assemble(spec, CONTEXT, data, {"1"}, ["Arcane Surge", "Cold Penetration"], items)
        summary = summarize_loadout(xml)
        self.assertEqual(summary["mainLinkGems"], ["Winter Orb", "Arcane Surge", "Cold Penetration"])
        self.assertEqual(summary["socketedGemCount"], 5)
        self.assertEqual(summary["bySlot"]["Body Armour"]["linkedRuns"], [3])
        self.assertEqual(parse_item(ET.fromstring(xml).find("./Items/Item").text)["rarity"], "rare")


if __name__ == "__main__":
    unittest.main()
