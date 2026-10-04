"""Generic, mechanic-derived handling of skills that have no tested profile.

Fixtures are synthetic gems/PoB outputs of the kinds the generator meets (Arc-style chaining spell,
Bane-style damage-over-time hex, Bone-Offering-style minion buff, an unknown multi-minion summon).
None of these names appears in data/mechanic_overrides.json, so every assertion exercises the
derivation, not a profile.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_evaluation import tier_targets
from generation_data import GameData
from mechanics import (delivery, derive_profile, find_override, mechanic_profile, minion_model,
                       minion_population, retarget)
from passive_search import sustained_resource_use, temporary_minion_population
from real_generator import (assess_mechanics, assess_quality, normalize_intent, population_assumptions,
                            sync_permanent_minion_count)

XML = '<PathOfBuilding><Tree><Spec nodes="12"/></Tree></PathOfBuilding>'
CONTEXT = {"tree": {"nodes": {"12": {"name": "Other"}}}}
GOOD_DEFENSE = {"Life": 5000, "EnergyShield": 2500, "TotalEHP": 40000, "FireResist": 75, "ColdResist": 75,
                "LightningResist": 75}
PASSIVES = {"used": 110, "maximum": 115}


def gem(name, tags):
    ident = "Metadata/Items/Gems/SkillGem" + name.replace(" ", "")
    return {"id": ident, "gameId": ident, "name": name, "variantId": name.replace(" ", ""),
            "skillId": name.replace(" ", ""), "tags": dict.fromkeys(tags, True), "support": False, "maxLevel": 20}


def spec_for(name, tags, archetype, **extra):
    spec = {"skill": name, "ascendancy": "Occultist", "archetype": archetype, "focus": "balanced",
            "damageType": "lightning", "skillTags": sorted(tags), **extra}
    spec["minionModel"] = minion_model(spec)
    return spec


def checks_by_name(spec, stats):
    profile = mechanic_profile(spec)
    checks = assess_mechanics(spec, {"stats": stats}, profile, XML, CONTEXT)
    return profile, {check["name"]: check for check in checks}


class UnprofiledSpellCanPass(unittest.TestCase):
    """Arc: chaining lightning spell, no override, must be evaluated and may validate."""

    def setUp(self):
        self.spec = spec_for("Arc", ["spell", "lightning", "chaining"], "spell")
        self.stats = {"FullDPS": 150000, "Speed": 4.0, "ManaCost": 8, "ManaRegen": 40, **GOOD_DEFENSE}

    def test_no_override_exists_and_profile_is_derived(self):
        self.assertIsNone(find_override(self.spec))
        profile = mechanic_profile(self.spec)
        self.assertEqual(profile["name"], "derived_spell")
        self.assertTrue(profile["derived"])
        self.assertIn("lightning damage", profile["scaling"])

    def test_checks_pass_without_a_tested_profile(self):
        _, by_name = checks_by_name(self.spec, self.stats)
        self.assertTrue(all(check["passed"] for check in by_name.values()), by_name)
        self.assertNotIn("Compatible ascendancy", by_name)      # only tested overrides pin ascendancies
        self.assertTrue(by_name["Mechanic profile"]["passed"])

    def test_good_build_is_validated_not_experimental(self):
        profile, by_name = checks_by_name(self.spec, self.stats)
        status, warnings = assess_quality(self.spec, {"stats": self.stats, "passives": PASSIVES}, profile,
                                          list(by_name.values()))
        self.assertEqual((status, warnings), ("validated", []))

    def test_fails_only_for_a_demonstrated_defect(self):
        broken = {**self.stats, "FullDPS": 0}
        profile, by_name = checks_by_name(self.spec, broken)
        self.assertFalse(by_name["Main-skill damage calculated"]["passed"])
        status, warnings = assess_quality(self.spec, {"stats": broken, "passives": PASSIVES}, profile,
                                          list(by_name.values()))
        self.assertEqual(status, "experimental")
        self.assertTrue(any("Main-skill damage calculated" in w for w in warnings))
        self.assertFalse(any("tested" in w.lower() for w in warnings))

    def test_resource_starvation_is_a_real_defect_for_any_spell(self):
        starved = {**self.stats, "ManaCost": 30, "ManaRegen": 20}
        profile, by_name = checks_by_name(self.spec, starved)
        status, warnings = assess_quality(self.spec, {"stats": starved, "passives": PASSIVES}, profile,
                                          list(by_name.values()))
        self.assertEqual(status, "experimental")
        self.assertTrue(any("below estimated skill use" in w for w in warnings))
        self.assertFalse(sustained_resource_use(starved, self.spec)["sustainable"])


class UnprofiledDamageOverTime(unittest.TestCase):
    def test_bane_style_dot_uses_dot_damage(self):
        spec = spec_for("Bane", ["spell", "hex", "chaos", "duration", "trigger", "area"], "dot",
                        damageType="chaos")
        self.assertEqual(delivery(spec), "dot")
        stats = {"FullDotDPS": 90000, "Speed": 1.0, "ManaCost": 5, "ManaRegen": 30, **GOOD_DEFENSE}
        profile, by_name = checks_by_name(spec, stats)
        self.assertEqual(profile["name"], "derived_dot")
        self.assertTrue(all(check["passed"] for check in by_name.values()), by_name)
        status, _ = assess_quality(spec, {"stats": stats, "passives": PASSIVES}, profile, list(by_name.values()))
        self.assertEqual(status, "validated")
        self.assertFalse(checks_by_name(spec, {**stats, "FullDotDPS": 0})[1]["Main-skill damage calculated"]["passed"])

    def test_intent_classifies_dot_from_the_gem_not_from_a_per_skill_branch(self):
        data = GameData({"gems": [gem("Bane", ["spell", "hex", "chaos", "duration", "trigger"]),
                                  gem("Arc", ["spell", "lightning", "chaining"])],
                         "bases": {}, "mods": []})
        data.main_names = ["Arc", "Bane"]
        bane = normalize_intent("level 90 Bane occultist", {}, data, {"divineChaos": 200})
        arc = normalize_intent("level 90 Arc occultist", {}, data, {"divineChaos": 200})
        self.assertEqual((bane["archetype"], bane["mechanicDelivery"]), ("dot", "dot"))
        self.assertEqual((arc["archetype"], arc["mechanicDelivery"]), ("spell", "spell"))
        self.assertIn("chaining", arc["skillTags"])
        self.assertIsNone(arc["minionModel"])


class UnprofiledMinions(unittest.TestCase):
    def test_bone_offering_style_buff_with_no_damage_fails_for_its_defect_only(self):
        spec = spec_for("Bone Offering", ["spell", "minion", "duration"], "minion", minionCount=1,
                        damageType="physical")
        self.assertEqual(minion_model(spec), "temporary")
        stats = {"FullDPS": 0, "Speed": 1.0, "Duration": 10, "ManaCost": 20, "ManaRegen": 25, "ActiveMinionLimit": 1}
        sync_permanent_minion_count(spec, {"stats": stats})
        _, by_name = checks_by_name(spec, stats)
        self.assertEqual([n for n, c in by_name.items() if not c["passed"]], ["Main-skill damage calculated"])

    def test_permanent_minion_without_profile_uses_the_pob_limit(self):
        spec = spec_for("Raise Spectre", ["spell", "minion"], "minion", minionCount=1, damageType="physical",
                        ascendancy="Necromancer")
        self.assertEqual(mechanic_profile(spec)["name"], "derived_minion_permanent")
        stats = {"FullDPS": 200000, "ActiveMinionLimit": 4, "ManaCost": 60, "Speed": 1.5, "ManaRegen": 10,
                 **GOOD_DEFENSE}
        self.assertEqual(minion_population(stats, spec), (4, True))
        self.assertTrue(sync_permanent_minion_count(spec, {"stats": stats}))
        self.assertEqual(spec["minionCount"], 4)
        self.assertEqual(population_assumptions(spec), ["All 4 permanent minions active (PoB-reported limit)"])
        profile, by_name = checks_by_name(spec, stats)
        status, warnings = assess_quality(spec, {"stats": stats, "passives": PASSIVES}, profile, list(by_name.values()))
        self.assertEqual((status, warnings), ("validated", []))      # one-off summon cost is not a sustain defect

    def test_unknown_multi_minion_summon_population_from_pob_outputs(self):
        spec = spec_for("Summon Sentinels", ["spell", "minion", "duration"], "minion", minionCount=1,
                        damageType="physical", ascendancy="Necromancer")
        stats = {"FullDPS": 40000, "Speed": 1.2, "Duration": 20, "ActiveMinionLimit": 6,
                 "SummonedMinionsPerCast": 2, "ManaCost": 30, "ManaRegen": 40, **GOOD_DEFENSE}
        self.assertEqual(temporary_minion_population(stats, spec), (6, True))
        self.assertTrue(sync_permanent_minion_count(spec, {"stats": stats}))
        self.assertTrue(spec["_populationSustainable"])
        _, by_name = checks_by_name(spec, stats)
        self.assertTrue(by_name["Temporary minion population modeled"]["passed"])
        self.assertTrue(by_name["Minion limit calculated"]["passed"])
        starved = {**stats, "ManaRegen": 1.0}
        count, sustainable = minion_population(starved, spec)
        self.assertFalse(sustainable)
        self.assertLess(count, 6)
        starved_spec = {**spec}
        sync_permanent_minion_count(starved_spec, {"stats": starved})
        profile, starved_checks = checks_by_name(starved_spec, starved)
        self.assertFalse(starved_checks["Temporary minion population modeled"]["passed"])
        _, warnings = assess_quality(starved_spec, {"stats": starved, "passives": PASSIVES}, profile,
                                     list(starved_checks.values()))
        self.assertTrue(any("population is not sustained" in w for w in warnings))

    def test_limit_and_duration_come_from_pob_not_constants(self):
        spec = spec_for("Summon Foo", ["spell", "minion", "duration"], "minion", minionCount=1)
        base = {"Speed": 2.0, "Duration": 5.0, "ActiveMinionLimit": 12, "ManaCost": 10, "ManaRegen": 100}
        self.assertEqual(minion_population(base, spec)[0], 10)                              # 2 x 5
        self.assertEqual(minion_population({**base, "Duration": 3.0}, spec)[0], 6)          # duration drives it
        self.assertEqual(minion_population({**base, "ActiveMinionLimit": 4}, spec)[0], 4)   # PoB limit caps it
        self.assertEqual(minion_population({**base, "SummonedMinionsPerCast": 3}, spec)[0], 12)

    def test_retarget_recomputes_mechanics_for_a_stage_skill(self):
        final = spec_for("Summon Raging Spirit", ["spell", "minion", "duration", "fire"], "minion")
        stage = retarget(final, "Fireball", {"spell": True, "fire": True})
        self.assertEqual(minion_model(final), "temporary")
        self.assertIsNone(minion_model(stage))


class DeliveryTiers(unittest.TestCase):
    def test_delivery_is_derived_from_tags(self):
        self.assertEqual(delivery(spec_for("X", ["spell", "brand"], "spell")), "brand")
        self.assertEqual(delivery(spec_for("X", ["spell", "totem"], "spell")), "totem")
        self.assertEqual(delivery(spec_for("X", ["attack", "minion", "duration"], "minion")), "attack_minion")
        self.assertEqual(delivery(spec_for("X", ["attack"], "attack")), "attack")
        self.assertEqual(delivery(spec_for("X", ["spell", "minion"], "minion")), "minion_permanent")

    def test_totem_and_brand_are_evaluated_generically(self):
        for tag in ("totem", "brand", "trap", "mine"):
            spec = spec_for("Unknown " + tag, ["spell", tag], "spell")
            stats = {"FullDPS": 80000, "Speed": 2, "ManaCost": 5, "ManaRegen": 30, **GOOD_DEFENSE}
            profile, by_name = checks_by_name(spec, stats)
            self.assertEqual(profile["name"], "derived_" + tag)
            status, _ = assess_quality(spec, {"stats": stats, "passives": PASSIVES}, profile, list(by_name.values()))
            self.assertEqual(status, "validated", tag)


class TierTargets(unittest.TestCase):
    TARGETS = {"minEndgameDps": {"spell": 40000, "minion": 80000}, "lifePoolMin": 6000, "ehpMin": 15000,
               "resistTarget": 75, "mechanicTiers": {"totem": {"minDps": 25000, "ehpMin": 12000}},
               "referenceBenchmarks": {"builds": [
                   {"archetype": "attack", "fullDps": 10_000_000}, {"archetype": "attack", "fullDps": 30_000_000},
                   {"archetype": "attack", "fullDps": 20_000_000}]},
               "derivedDpsFraction": 0.001, "derivedDpsClamp": [20000, 100000], "defaultMinEndgameDps": 33000}

    def test_resolution_order(self):
        totem = tier_targets(spec_for("T", ["spell", "totem"], "spell"), self.TARGETS)
        self.assertEqual((totem["dpsFloor"], totem["ehpMin"], totem["dpsFloorSource"]), (25000, 12000, "mechanicTiers"))
        self.assertEqual(tier_targets(spec_for("A", ["spell"], "spell"), self.TARGETS)["dpsFloor"], 40000)

    def test_floor_is_derived_from_benchmarks_when_no_explicit_tier(self):
        attack = tier_targets(spec_for("S", ["attack"], "attack"), self.TARGETS)
        self.assertEqual(attack["dpsFloor"], 20000)                       # 0.001 x median 20M
        self.assertEqual(attack["dpsFloorSource"], "derived from reference benchmarks")

    def test_unknown_archetype_uses_the_generic_default(self):
        dot = tier_targets(spec_for("D", ["spell"], "dot"), self.TARGETS)
        self.assertEqual((dot["dpsFloor"], dot["dpsFloorSource"]), (33000, "defaultMinEndgameDps"))

    def test_shipped_quality_targets_floors(self):
        # v4 calibration: spell/ignite/dot floors 60,000 (from the generated-build matrix); minion floor unchanged.
        self.assertEqual(tier_targets(spec_for("Winter Orb", ["spell", "cold"], "spell"))["dpsFloor"], 60000)
        self.assertEqual(tier_targets(spec_for("Raise Zombie", ["spell", "minion"], "minion"))["dpsFloor"], 80000)
        self.assertEqual(tier_targets(spec_for("Summon Skeletons", ["spell", "minion", "duration"], "minion"))["dpsFloor"], 80000)
        self.assertGreater(tier_targets(spec_for("Z", ["spell"], "dot"))["dpsFloor"], 0)


class ProfiledSkillsStillUseOverrides(unittest.TestCase):
    def test_overrides_load_and_keep_their_constraints(self):
        spec = spec_for("Winter Orb", ["spell", "cold", "channelling", "duration"], "spell", damageType="cold",
                        ascendancy="Elementalist", channelled=True)
        profile = mechanic_profile(spec)
        self.assertEqual(profile["name"], "winter_orb_elementalist")
        self.assertTrue(profile["override"])
        self.assertEqual(profile["delivery"], "spell")
        stats = {"FullDPS": 250000, "Speed": 7, "ManaCost": 9, "ManaRegen": 26, **GOOD_DEFENSE}
        _, by_name = checks_by_name(spec, stats)
        self.assertTrue(by_name["Compatible ascendancy"]["passed"])
        wrong = {**spec, "ascendancy": "Necromancer"}
        self.assertEqual(mechanic_profile(wrong)["name"], "derived_spell")   # no override -> derived, still evaluated

    def test_ethereal_knives_override_requires_shaper_of_flames(self):
        spec = spec_for("Ethereal Knives", ["spell", "physical", "projectile"], "ignite", ascendancy="Elementalist",
                        damageType="fire")
        profile, by_name = checks_by_name(spec, {"IgniteDPS": 100})
        self.assertEqual(profile["name"], "ethereal_knives_ignite_elementalist")
        self.assertFalse(by_name["Shaper of Flames allocated"]["passed"])
        self.assertTrue(by_name["Ignite damage active"]["passed"])
        # An ignite skill with no override still gets the generic ignite check.
        other = spec_for("Other Ignite", ["spell", "fire"], "ignite", ascendancy="Occultist", damageType="fire")
        _, other_checks = checks_by_name(other, {"IgniteDPS": 100})
        self.assertTrue(other_checks["Ignite damage active"]["passed"])
        self.assertNotIn("Shaper of Flames allocated", other_checks)

    def test_skeleton_override_and_generic_model_agree(self):
        spec = spec_for("Summon Skeletons", ["spell", "minion", "duration"], "minion", ascendancy="Necromancer",
                        minionCount=1)
        stats = {"Speed": 2.0, "Duration": 33, "ActiveMinionLimit": 8, "SummonedMinionsPerCast": 3,
                 "ManaCost": 20, "ManaRegen": 30}
        self.assertEqual(minion_population(stats, spec), (8, True))
        self.assertEqual(mechanic_profile(spec)["name"], "summon_skeletons_necromancer")
        self.assertEqual(derive_profile(spec)["name"], "derived_minion_temporary")


if __name__ == "__main__":
    unittest.main()
