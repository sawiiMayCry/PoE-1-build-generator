"""Opt-in real-engine regression checks: WITCHCRAFT_TEST_POB_XML=<candidate>."""
import os
import unittest
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from build_progression import combine_loadouts, select_stage
from pob_engine import POB_ENV_KEYS, calculate_with_pob, close_worker, export_with_pob, get_worker


@unittest.skipUnless(os.name == "nt" and os.environ.get("WITCHCRAFT_TEST_POB_XML"),
                     "Set WITCHCRAFT_TEST_POB_XML to run installed-PoB regression checks")
class PersistentPob(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.data = cls.root / "data"
        cls.xml = Path(os.environ["WITCHCRAFT_TEST_POB_XML"]).read_text(encoding="utf-8")

    def tearDown(self):
        close_worker()

    def test_normal_export_saves_stats_and_preserves_build_calculations(self):
        candidate = ET.fromstring(self.xml)
        build = candidate.find("Build")
        for child in list(build):
            if child.tag in {"PlayerStat", "MinionStat", "FullDPSSkill"}:
                build.remove(child)
        minimal = ET.tostring(candidate, encoding="unicode")
        before = calculate_with_pob(minimal, self.root, self.data)
        exported = export_with_pob(minimal, self.root, self.data)
        saved = ET.fromstring(exported.pop("xml"))
        self.assertEqual(exported, before)
        self.assertGreater(len(saved.findall("./Build/PlayerStat")), 0)
        self.assertTrue(saved.findtext("./Tree/Spec/URL"))
        saved_life = next(float(stat.get("value")) for stat in saved.findall("./Build/PlayerStat")
                          if stat.get("stat") == "Life")
        self.assertAlmostEqual(saved_life, before["stats"]["Life"])
        titles = [entry.get("title") for entry in candidate.findall("./Tree/Spec")]
        if len(titles) > 1:
            loadouts = get_worker(self.root, self.data).request("loadouts", xml=self.xml)["loadouts"]
            self.assertTrue(all(title in loadouts for title in titles))

    def test_interleaved_builds_and_threads_do_not_leak_state(self):
        first = calculate_with_pob(self.xml, self.root, self.data)
        changed = ET.fromstring(self.xml)
        changed.find("Build").set("level", "80")
        other_xml = ET.tostring(changed, encoding="unicode")
        other = calculate_with_pob(other_xml, self.root, self.data)
        self.assertNotEqual(first["stats"]["Life"], other["stats"]["Life"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda xml: calculate_with_pob(xml, self.root, self.data), [self.xml, other_xml]))
        self.assertEqual(results, [first, other])
        self.assertEqual(calculate_with_pob(self.xml, self.root, self.data), first)

    def test_lua_error_resets_worker_and_restores_process_context(self):
        first = calculate_with_pob(self.xml, self.root, self.data)
        before_env = {key: os.environ.get(key) for key in POB_ENV_KEYS}
        before_cwd = Path.cwd()
        with self.assertRaisesRegex(RuntimeError, "Unknown worker operation"):
            get_worker(self.root, self.data).request("bad-operation")
        self.assertEqual({key: os.environ.get(key) for key in POB_ENV_KEYS}, before_env)
        self.assertEqual(Path.cwd(), before_cwd)
        self.assertEqual(calculate_with_pob(self.xml, self.root, self.data), first)

    def test_animate_weapon_keeps_its_equipment_after_progression_merge(self):
        worker = get_worker(self.root, self.data)
        gem = next(gem for gem in worker.request("metadata")["gems"] if gem["name"] == "Animate Weapon")
        version = ET.fromstring(self.xml).find("./Tree/Spec").get("treeVersion")
        documents, calculations = [], []
        phases = [{"title": "Act 3 - Level 32"}, {"title": "Act 4 - Level 40"}]
        for damage in (1, 100):
            root = ET.Element("PathOfBuilding")
            ET.SubElement(root, "Build", level="40", characterLevelAutoMode="false", className="Witch",
                          ascendClassName="None", bandit="None", mainSocketGroup="1", targetVersion="3_0")
            tree = ET.SubElement(root, "Tree", activeSpec="1")
            ET.SubElement(tree, "Spec", classId="3", ascendClassId="0", treeVersion=version, nodes="")
            skills = ET.SubElement(root, "Skills", activeSkillSet="1")
            skill_set = ET.SubElement(skills, "SkillSet", id="1")
            skill = ET.SubElement(skill_set, "Skill", enabled="true", includeInFullDPS="true", mainActiveSkill="1")
            ET.SubElement(skill, "Gem", gemId=gem["gameId"], variantId=gem["variantId"],
                          skillId=gem["skillId"], nameSpec=gem["name"], level="1", quality="0", enabled="true", count="1")
            items = ET.SubElement(root, "Items", activeItemSet="1")
            ET.SubElement(items, "Item", id="1").text = (
                f"Rarity: MAGIC\nAnimated test weapon\nGlass Shank\nAdds {damage} to {damage} Physical Damage")
            item_set = ET.SubElement(items, "ItemSet", id="1")
            ET.SubElement(item_set, "Slot", name="Weapon 1", itemId="1")
            config = ET.SubElement(root, "Config", activeConfigSet="1")
            ET.SubElement(config, "ConfigSet", id="1")
            exported = worker.request("export", xml=ET.tostring(root, encoding="unicode"))
            documents.append(exported["xml"])
            calculations.append(exported["stats"])
        self.assertGreater(calculations[1]["FullDPS"], calculations[0]["FullDPS"])
        merged = combine_loadouts(documents, phases, "")
        for index, expected in enumerate(calculations, 1):
            actual = worker.request("calculate", xml=select_stage(merged, index, 40))
            self.assertEqual(actual["stats"], expected)


if __name__ == "__main__":
    unittest.main()
