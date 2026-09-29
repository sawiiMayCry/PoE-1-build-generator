"""Generate locally and compare to a reference; never publishes a build.

By default uses live version-checked context. --snapshot explicitly opts into
the development data written by inspect_generation.py for offline iteration.
"""
import argparse
import json
import re
import time
from pathlib import Path

from build_generator import _main_group, offense_value
import xml.etree.ElementTree as ET
from generation_data import GameData
from pob_engine import calculate_with_pob, get_worker
from real_generator import build_design, normalize_intent, parse_intent
from services import game_context, market_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt")
    parser.add_argument("--model")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if args.snapshot:
        context = json.loads((root / "data/development_context.json").read_text(encoding="utf-8"))
        context["pobHome"] = Path(context["pobHome"])
        market = json.loads((root / "data/development_market.json").read_text(encoding="utf-8"))
    else:
        context = game_context()
        market = market_data(context["league"])
    data = GameData(get_worker(root, root / "data").request("metadata"))
    spec = (parse_intent(args.prompt, args.model, data, market) if args.model else
            normalize_intent(args.prompt, {}, data, market))
    print(json.dumps(spec), flush=True)
    started = time.perf_counter()
    xml, recipe, checks, details, calculation, price = build_design(
        spec, context, market, root, root / "data", lambda message: print(message, flush=True), data)
    output = root / "data" / "verification"
    output.mkdir(parents=True, exist_ok=True)
    filename = re.sub(r"[^a-z0-9_-]", "_", spec["skill"].lower())
    (output / (filename + ".xml")).write_text(xml, encoding="utf-8")
    report = {"prompt": args.prompt, "spec": spec, "seconds": time.perf_counter() - started,
              "recipe": recipe, "validation": checks, "details": details,
              "calculation": calculation, "quote": price}
    if args.reference:
        reference_xml = args.reference.read_text(encoding="utf-8")
        reference_skill = _main_group(ET.fromstring(reference_xml))[1].find("Gem").get("nameSpec")
        if reference_skill != spec["skill"]:
            raise ValueError(f"Reference uses {reference_skill}, expected {spec['skill']}; compare the same main skill")
        reference = calculate_with_pob(reference_xml, root, root / "data")
        report["reference"] = {"file": str(args.reference), "stats": reference["stats"],
            "dpsRatio": offense_value(calculation["stats"]) / max(1, offense_value(reference["stats"])),
            "poolRatio": (calculation["stats"].get("Life", 0) + calculation["stats"].get("EnergyShield", 0)) /
                         max(1, reference["stats"].get("Life", 0) + reference["stats"].get("EnergyShield", 0))}
    (output / (filename + ".json")).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"seconds": report["seconds"], "stats": calculation["stats"],
                      "links": details["gems"], "reference": report.get("reference"),
                      "xml": str(output / (filename + ".xml"))}, indent=2), flush=True)


if __name__ == "__main__":
    main()
