"""Compare two PoB exports at identical level, config and minion assumptions."""
from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from build_generator import quote, validate_structure
from build_progression import select_stage
from mechanics import FALLBACK_MINION_MODEL
from passive_search import temporary_minion_population
from pob_engine import calculate_with_pob

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def comparable(xml: str, level: int, skill: str, minions: int) -> str:
    root = ET.fromstring(xml)
    stage_count = len(root.findall("./Tree/Spec"))
    if stage_count > 1:
        xml = select_stage(xml, stage_count, level)
        root = ET.fromstring(xml)
    root.find("Build").set("level", str(level))
    skill_set_id = root.find("Skills").get("activeSkillSet")
    active_set = root.find(f"./Skills/SkillSet[@id='{skill_set_id}']")
    main = next((group for group in active_set.findall("Skill")
                 if group.get("includeInFullDPS") == "true"), None)
    if main is None or not any(gem.get("nameSpec") == skill for gem in main.findall("Gem")):
        raise ValueError(f"Export does not have {skill} in its main damage group")
    for gem in main.findall("Gem"):
        if gem.get("nameSpec") == skill:
            gem.set("count", str(minions))
    config = root.find("Config/ConfigSet")
    if config is None:
        raise ValueError("PoB export has no configuration set")
    for input_node in list(config.findall("Input")):
        config.remove(input_node)
    ET.SubElement(config, "Input", name="enemyLevel", number="83")
    return ET.tostring(root, encoding="unicode")


def continuous_sustainability(output: dict) -> dict:
    """Report PoB's ongoing mana and life use per second for a caster."""
    speed = max(0.0, float(output.get("Speed", 0) or 0))
    checks = []
    for label, cost_key, regen_key in (("mana", "ManaCost", "ManaRegen"),
                                       ("life", "LifeCost", "LifeRegenRecovery")):
        use_rate = max(0.0, float(output.get(cost_key, 0) or 0)) * speed
        available = max(0.0, float(output.get(regen_key, 0) or 0))
        if use_rate > 0:
            checks.append({"resource": label, "usePerSecond": use_rate,
                           "availablePerSecond": available,
                           "sustainable": available + 1e-6 >= use_rate})
    return {"mode": "continuous casting", "sustainable": all(item["sustainable"] for item in checks),
            "castRatePerSecond": speed, "checks": checks}


def per_paid_point(damage: float, used: int) -> float:
    return float(damage) / max(1, int(used))


def summarize(path: Path, level: int, skill: str, minions: int) -> dict:
    source = path.read_text(encoding="utf-8")
    xml = comparable(source, level, skill, minions)
    calculation = calculate_with_pob(xml, ROOT, DATA)
    root = ET.fromstring(xml)
    spec = root.find("Build")
    context = json.loads((DATA / "development_context.json").read_text(encoding="utf-8"))
    context["pobHome"] = Path(context["pobHome"])
    _, details = validate_structure(xml, context, spec.get("ascendClassName", "None"), skill)
    market = json.loads((DATA / "development_market.json").read_text(encoding="utf-8"))
    prices = quote(details["gear"], market, 10_000_000)
    used, maximum = calculation["passives"]["used"], calculation["passives"]["maximum"]
    if used > maximum:
        raise ValueError(f"{path} is not a legal level-{level} benchmark: it allocates {used} passive points, "
                         f"but only {maximum} are available")
    stats = calculation["stats"]
    # No gem metadata here: fall back to PoB's own outputs when the skill is not in the known table.
    pob_model = (("temporary" if float(stats.get("Duration", 0) or 0) > 0 else "permanent")
                 if float(stats.get("ActiveMinionLimit", 0) or 0) > 0 else None)
    population = temporary_minion_population(
        stats, {"skill": skill, "minionCount": minions,
                "minionModel": FALLBACK_MINION_MODEL.get(skill) or pob_model})
    output = calculation["stats"]
    sustainability = None
    if population is not None:
        sustainable_count, sustainable = population
        sustained_calculation = (calculation if sustainable_count == minions else calculate_with_pob(
            comparable(source, level, skill, sustainable_count), ROOT, DATA))
        sustainability = {
            "modeledSummons": sustainable_count,
            "sustainable": sustainable,
            "skillCost": calculation["stats"].get("ManaCost", 0),
            "manaRegenPerSecond": calculation["stats"].get("ManaRegen", 0),
            "lifeCost": calculation["stats"].get("LifeCost", 0),
            "lifeRegenRecoveryPerSecond": calculation["stats"].get("LifeRegenRecovery", 0),
            "castRatePerSecond": calculation["stats"].get("Speed", 0),
            "durationSeconds": calculation["stats"].get("Duration", 0),
            "FullDPSAtModeledPopulation": sustained_calculation["stats"].get("FullDPS", 0),
            "FullDotDPSAtModeledPopulation": sustained_calculation["stats"].get("FullDotDPS", 0),
            "FullDPSPerPaidPointAtModeledPopulation": per_paid_point(
                sustained_calculation["stats"].get("FullDPS", 0), calculation["passives"].get("used", 0)),
        }
    elif skill != "Raise Zombie":
        sustainability = continuous_sustainability(output)
    return {"file": str(path.resolve()), "stats": calculation["stats"],
            "passives": calculation["passives"], "skill": skill,
            "equipment": [{"slot": item["slot"], "rarity": item["rarity"],
                           "name": item["name"], "base": item["base"],
                           "variant": item.get("variant"), "links": item.get("links")}
                          for item in details["gear"]],
            "passiveUse": {"used": used, "available": maximum, "unspent": max(0, maximum - used),
            "fullDPSPerPaidPoint": per_paid_point(calculation["stats"].get("FullDPS", 0), used)},
            "priceCoverage": {"pricedSubtotalChaos": prices["pricedSubtotalChaos"],
                              "quotedSlots": len(prices["priced"]),
                              "unknownSlots": [entry["slot"] for entry in prices["unknown"]]},
            "summonCountAssumption": minions,
            "sustainability": sustainability}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--level", type=int, required=True)
    parser.add_argument("--skill", required=True)
    parser.add_argument("--summon-count", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = {"comparisonAssumptions": {"level": args.level, "enemyLevel": 83,
                                         "skill": args.skill, "summonCount": args.summon_count,
                                         "conditionalConfigInputs": "removed from both exports"},
               "before": summarize(args.before, args.level, args.skill, args.summon_count),
               "after": summarize(args.after, args.level, args.skill, args.summon_count)}
    fields = ("FullDPS", "FullDotDPS", "IgniteDPS", "Life", "EnergyShield", "Armour", "Evasion",
              "TotalEHP", "FireResist", "ColdResist", "LightningResist", "ChaosResist",
              "ManaUnreserved", "ManaCost", "ManaRegen")
    results["delta"] = {key: results["after"]["stats"].get(key, 0) - results["before"]["stats"].get(key, 0)
                        for key in fields}
    rendered = json.dumps(results, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
