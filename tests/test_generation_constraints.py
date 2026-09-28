import unittest
import xml.etree.ElementTree as ET

from build_generator import validate_calculation
from prompt_generator import PatternUnusable, _support_options, _target_level, _tree_options


class GenerationConstraints(unittest.TestCase):
    def point_check(self, passives):
        result = validate_calculation({"calculated": True, "stats": {"Life": 4000}, "passives": passives})
        return next(check for check in result if check["name"] == "Legal passive points")

    def test_paid_points_and_secondary_ascendancy_use_pob_counts(self):
        counts = {"used": 111, "maximum": 111, "ascendancy": 8, "secondaryAscendancy": 8}
        self.assertTrue(self.point_check(counts)["passed"])
        self.assertFalse(self.point_check({**counts, "used": 112})["passed"])
        self.assertFalse(self.point_check({**counts, "secondaryAscendancy": 9})["passed"])
        self.assertFalse(self.point_check({})["passed"])

    def test_requested_level_is_not_silently_raised(self):
        self.assertEqual(_target_level("ethereal knives elementalist", 86, 89), 90)
        self.assertEqual(_target_level("level 89 ethereal knives", 86, 89), 89)
        with self.assertRaises(PatternUnusable):
            _target_level("level 86 ethereal knives", 86, 89)
        with self.assertRaises(PatternUnusable):
            _target_level("ethereal knives", 96, 101)

    def test_tree_choices_preserve_attributes_and_resistance_at_point_cap(self):
        root = ET.fromstring('<PathOfBuilding><Build level="100"/><Tree activeSpec="1">'
                             '<Spec nodes="1,2"/></Tree></PathOfBuilding>')
        nodes = {
            "1": {"name": "Agility", "stats": ["+30 to Dexterity"], "out": ["2", "3"]},
            "2": {"name": "Holy Dominion", "stats": ["+12% to all Elemental Resistances"], "out": ["1", "3"]},
            "3": {"name": "Fire Damage", "stats": ["10% increased Fire Damage"], "out": ["1", "2"]},
        }
        context = {"tree": {"nodes": nodes}, "passiveCounts": {"used": 111, "maximum": 111}}
        self.assertEqual(_tree_options(root, context), [])
        options = _tree_options(root, {**context, "passiveCounts": {"used": 111, "maximum": 112}})
        self.assertTrue(options)
        self.assertTrue(all(option["kind"] == "add" for option in options))

    def test_ignite_core_does_not_offer_critical_damage_support(self):
        root = ET.fromstring('<PathOfBuilding><Build ascendClassName="Elementalist"/>'
                             '<Skills activeSkillSet="1"><SkillSet id="1"><Skill>'
                             '<Gem gemId="SkillGemEK" nameSpec="Ethereal Knives"/>'
                             '<Gem gemId="SupportGemBurning" nameSpec="Burning Damage"/>'
                             '<Gem gemId="SupportGemCombustion" nameSpec="Combustion"/>'
                             '</Skill><Skill>'
                             '<Gem gemId="SupportGemCrit" nameSpec="Increased Critical Damage"/>'
                             '<Gem gemId="SupportGemEfficacy" nameSpec="Efficacy"/>'
                             '</Skill></SkillSet></Skills></PathOfBuilding>')
        pattern = {"source": "source", "group": 1, "skill": "Ethereal Knives", "ascendancy": "Elementalist"}
        choices, _ = _support_options(pattern, {"source": root})
        self.assertEqual([choice["name"] for choice in choices], ["Efficacy"])


if __name__ == "__main__":
    unittest.main()
