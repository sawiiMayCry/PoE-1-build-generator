"""Run a local generation diagnostic from saved snapshots; never publish it.

Example:
  python diagnose_generation.py --prompt "Level 90 SRS Necromancer, 2000 chaos"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

from build_evaluation import final_quality_report
from loadout_summary import loadout_view
from generation_data import GameData
from pob_engine import find_pob_installation
from pob_engine import get_worker
from real_generator import (assess_mechanics, assess_quality, build_design, mechanic_profile,
                            mentioned_uniques, normalize_intent)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def read_json(path: Path):
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True, help="The build request to diagnose")
    parser.add_argument("--context", type=Path, default=DATA / "development_context.json")
    parser.add_argument("--market", type=Path, default=DATA / "development_market.json")
    parser.add_argument("--metadata", type=Path, default=DATA / "pob_metadata.json")
    parser.add_argument("--expected-pob-version", help="Fail if the installed PoB version differs")
    parser.add_argument("--output-dir", type=Path, default=DATA / "diagnostics")
    args = parser.parse_args()
    started = time.perf_counter()

    context, context_hash = read_json(args.context)
    market, market_hash = read_json(args.market)
    metadata, metadata_hash = read_json(args.metadata)
    if context.get("treeVersion") not in str(context.get("officialRelease", "")).replace(".", "_"):
        raise SystemExit("The context tree version does not match its official release label")
    if context.get("league") != market.get("league"):
        raise SystemExit("The context and market snapshots are from different leagues")
    home = context.get("pobHome")
    context["pobHome"] = Path(home) if home else find_pob_installation()
    if context["pobHome"] is None or not context["pobHome"].is_dir():
        raise SystemExit("The PoB installation recorded by the context snapshot is unavailable")

    # Versioned development snapshots can predate PoB's gem-level tables. Keep
    # the snapshot intact and its hash in the report, while filling only the
    # progression tables from the installed PoB after the caller has pinned
    # the expected version.
    if (any(not gem.get("levels") for gem in metadata.get("gems", []))
            or any("skillTypes" not in gem for gem in metadata.get("gems", []))
            or not metadata.get("jewelMods")):
        live_metadata = get_worker(ROOT, DATA).request("metadata")
        live_by_id = {gem["id"]: gem for gem in live_metadata.get("gems", [])}
        for gem in metadata.get("gems", []):
            live_gem = live_by_id.get(gem["id"])
            if live_gem:
                gem["levels"] = live_gem.get("levels", [])
                for key in ("skillTypes", "createsMinions", "baseEffectiveness", "statIds", "legacy"):
                    if key in live_gem:
                        gem[key] = live_gem[key]
        if not metadata.get("jewelMods"):
            metadata["jewelMods"] = live_metadata.get("jewelMods", [])
        print("Filled missing installed gem progression or jewel modifier data", flush=True)
    supplemented = (["gem levels", "jewelMods"] if "live_metadata" in locals() else [])
    data = GameData(metadata)
    data.unique_items = get_worker(ROOT, DATA).request("uniques").get("items", [])
    effective_inputs = {
        "effectiveMetadataSha256": hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest(),
        "metadataSupplementedFromLivePob": supplemented,
        "jewelModCount": len(metadata.get("jewelMods", [])),
        "uniqueCatalogueCount": len(data.unique_items),
        "uniqueCatalogueSha256": hashlib.sha256(json.dumps(
            [(u.get("name"), u.get("base"), u.get("selectedVersion"), u.get("selectedVariant"))
             for u in data.unique_items], sort_keys=True).encode()).hexdigest(),
        "marketSchema": {"hasListings": bool(market.get("listings")), "priceKeys": sorted(market.keys())[:12],
                         "updated": market.get("updated")},
        "league": context.get("league"),
    }
    spec = normalize_intent(args.prompt, {}, data, market)
    spec["requestedUniques"] = mentioned_uniques(args.prompt, context, data.unique_items)
    trace = []
    def stage(message: str) -> None:
        print(message, flush=True)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        xml, recipe, checks, details, calculation, price = build_design(
            spec, context, market, ROOT, DATA, stage, data, trace=trace)
    except Exception as exc:
        # Preserve the decisions leading to a failed candidate. Without this,
        # the most important optimizer/export failures disappear with the
        # diagnostic process and cannot be reproduced from its output.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        failure_path = args.output_dir / f"{stamp}-failed-generation.json"
        failure_path.write_text(json.dumps({
            "published": False, "error": f"{type(exc).__name__}: {exc}",
            "runtimeSeconds": round(time.perf_counter() - started, 3),
            "snapshotHashes": {"context": context_hash, "market": market_hash,
                               "metadata": metadata_hash},
            "treeVersion": context["treeVersion"], "pobHome": str(context["pobHome"]),
            "spec": spec, "candidateTrace": trace,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Failed diagnostic trace: {failure_path.resolve()}", flush=True)
        raise
    pob_version = calculation.get("version")
    if args.expected_pob_version and pob_version != args.expected_pob_version:
        raise SystemExit(f"PoB version mismatch: expected {args.expected_pob_version}, got {pob_version}")
    mechanic_checks = assess_mechanics(spec, calculation, recipe.get("mechanics") or mechanic_profile(spec),
                                       xml, context)
    status, warnings = assess_quality(spec, calculation, recipe.get("mechanics") or mechanic_profile(spec),
                                      mechanic_checks, bool(recipe.get("searchLimitWarning")),
                                      recipe.get("qualityDiagnostics"))
    quality_report = final_quality_report(xml, spec, calculation, mechanic_checks,
                                          recipe.get("qualityDiagnostics", {}), data,
                                          search_limited=bool(recipe.get("searchLimitWarning")),
                                          assessed_status=status).to_dict()
    status = quality_report["status"] if status == "validated" else status
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", spec["skill"].lower()).strip("-")[:36] or "build"
    stem = f"{stamp}-{slug}-{spec['level']}"
    xml_path = args.output_dir / f"{stem}.xml"
    report_path = args.output_dir / f"{stem}.json"
    xml_path.write_text(xml, encoding="utf-8")
    report = {
        "published": False,
        "createdAt": int(time.time()),
        "snapshotHashes": {"context": context_hash, "market": market_hash, "metadata": metadata_hash},
        "treeVersion": context["treeVersion"], "officialTreeRelease": context["officialRelease"],
        "pobVersion": pob_version, "skill": spec["skill"], "ascendancy": spec["ascendancy"],
        "level": spec["level"], "spec": spec, "qualityStatus": status,
        "effectiveInputs": effective_inputs,
        "searchShares": recipe.get("searchShares"),
        "qualityWarnings": warnings, "qualityReport": quality_report, "recipe": recipe, "validation": checks,
        "completeness": recipe.get("qualityDiagnostics", {}).get("completeness"),
        "encounterReadiness": recipe.get("qualityDiagnostics", {}).get("encounterReadiness"),
        "priceCoverage": recipe.get("qualityDiagnostics", {}).get("priceCoverage"),
        "mechanicChecks": mechanic_checks,
        "stats": calculation.get("stats", {}), "quote": price,
        "gear": details.get("gear", []),
        "loadout": loadout_view(xml, data.gems), "progression": recipe.get("progression", []),
        "evaluationCount": recipe.get("designEvaluations", 0),
        "pobCalls": recipe.get("evaluations", 0),
        "runtimeSeconds": round(time.perf_counter() - started, 3),
        "candidateTrace": trace, "xmlFile": str(xml_path.resolve()),
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Diagnostic XML: {xml_path.resolve()}")
    print(f"Diagnostic report: {report_path.resolve()}")
    print(f"PoB {pob_version}; quality {status}; {len(checks)} legality checks; {len(trace)} trace records; not published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
