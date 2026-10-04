"""Opt-in real-PoB check of the skill planner on a saved generated build (never publishes).

  python validate_skill_packages.py data/generated/<id>.json

Re-plans the build's six-link and supporting groups on its final gear/tree with
the installed PoB, then prints socketed-gem counts, groups, omissions and the
exported loadout summary.  Requires the installed PoB and the development
context snapshot (data/development_context.json).
"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from build_assembly import assemble_with_report
from generation_data import GameData, RareItem
from loadout_summary import active_item_set, active_spec, parse_item, summarize_loadout
from pob_engine import export_with_pob, find_pob_installation, get_worker
from skill_packages import capacity_from_equipment, package_completeness
from skill_planner import PobGroupEvaluator, PobMainLinkOps, plan_main_link, plan_skill_loadout

ROOT = Path(__file__).resolve().parent


def main(path: str) -> int:
    worker = get_worker(ROOT, ROOT / "data")
    data = GameData(worker.request("metadata"))
    context = json.loads((ROOT / "data" / "development_context.json").read_text(encoding="utf-8"))
    context["pobHome"] = find_pob_installation()
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    spec = dict(saved["recipe"]["constraints"])
    spec["enemyLevel"] = 83
    root = ET.fromstring(saved["_xml"])
    tree_spec = root.findall("./Tree/Spec")[-1]
    nodes = set(tree_spec.get("nodes").split(","))
    masteries = {a: int(b) for a, b in re.findall(r"\{(\d+),(\d+)\}", tree_spec.get("masteryEffects", ""))}
    by_id = {i.get("id"): i.text for i in root.findall("./Items/Item")}
    uniques, items = {}, []
    for slot in root.findall("./Items/ItemSet")[-1].findall("Slot"):
        text = by_id.get(slot.get("itemId"))
        if not text or slot.get("name").endswith("Swap") or slot.get("name").startswith(("Graft", "Belt Abyssal")):
            continue
        uniques[slot.get("name")] = text.strip()
        if not slot.get("name").startswith("Flask"):
            base = parse_item(text)["base"]
            items.append(RareItem(slot.get("name"), base, data.bases[base]))

    def render(groups, **kw):
        return assemble_with_report(spec, context, data, nodes, [], items, masteries, uniques, None,
                                    skill_groups=groups, **kw)[0]

    link = plan_main_link(spec, data, PobMainLinkOps(worker, data, render, spec, int(spec["level"])))
    print("main link complete:", link["complete"], link["supports"], link["reason"])
    evaluator = PobGroupEvaluator(worker, data, render)
    plan = plan_skill_loadout(spec, data, link["supports"], evaluator, capacity=capacity_from_equipment(items, uniques, data),
                              items=items, uniques=uniques, legality=evaluator.legality)
    for group in plan["groups"]:
        print(f"  {group['slot']:<12} {group['role']:<9}", " + ".join(f"{g['name']} {g['level']}" for g in group["gems"]))
    for omission in plan["omissions"]:
        print("  omitted:", omission["package"], "-", omission["reason"])
    print("completeness:", package_completeness(plan["groups"], plan["omissions"]))
    exported = export_with_pob(render(plan["groups"]), ROOT, ROOT / "data")
    summary = summarize_loadout(exported["xml"], data.gems)
    print("exported socketed gems:", summary["socketedGemCount"], "supported groups:", summary["supportedGroupCount"],
          "FullDPS:", round(exported["stats"].get("FullDPS", 0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
