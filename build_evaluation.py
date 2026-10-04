"""Authoritative completeness / readiness evaluation derived from the FINAL exported XML.

Everything here reads the exact XML that will be exported, never planned state:
planned-but-removed gems are not counted, item-granted skills are separated from
socketed gems, and the active Tree/Spec, SkillSet and ItemSet are resolved
positionally/by-id the way Path of Building does (Specs need not have ids).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from build_contracts import QualityReport, SkillGroup, GemRecord

TARGETS_PATH = Path(__file__).resolve().parent / "data" / "quality_targets.json"
DEFAULT_TARGETS = {
    "version": 2,
    "scope": "Witch endgame spell/minion builds at level 80-100 against the configured PoB encounter; "
             "numbers are screening targets, not guarantees of a boss kill.",
    "lifePoolMin": 6000, "ehpMin": 15000, "resistTarget": 75,
    "unspentPointsAbsolute": 8, "unspentPointsFraction": 0.15,
    "minSupportedUtilityGroups": 2,
    "minEndgameDps": {"spell": 60000, "minion": 80000, "ignite": 60000, "dot": 60000, "attack": 60000},
    "calibration": "PROVISIONAL: floors are conservative screening values, not yet calibrated against "
                   "same-skill reference builds at matching gear/encounter; replace when references exist.",
    "mainLinkSize": 6,
    "defaultMinEndgameDps": 40000, "derivedDpsFraction": 0.001, "derivedDpsClamp": [20000, 100000],
}


def load_targets() -> dict:
    try:
        return {**DEFAULT_TARGETS, **json.loads(TARGETS_PATH.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULT_TARGETS)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def tier_targets(spec: dict, targets: dict | None = None) -> dict:
    """Quality targets for this build's mechanic tier (spell, minion_temporary, totem ...).

    DPS floor resolution, most specific first: ``mechanicTiers[tier].minDps`` -> ``minEndgameDps[tier]``
    -> ``minEndgameDps[archetype]`` -> derived from the reference benchmarks of the same archetype
    (``derivedDpsFraction`` x their median, clamped to ``derivedDpsClamp``) -> ``defaultMinEndgameDps``.
    Life pool and EHP floors come from ``mechanicTiers[tier]`` when present, otherwise the global
    ``lifePoolMin`` / ``ehpMin``.  No skill name is consulted.
    """
    from mechanics import delivery
    targets = targets or load_targets()
    tier = delivery(spec)
    archetype = spec.get("archetype", "")
    override = (targets.get("mechanicTiers") or {}).get(tier, {})
    floors = targets.get("minEndgameDps", {})
    source = "mechanicTiers" if "minDps" in override else None
    dps = override.get("minDps")
    if dps is None:
        for key in (tier, archetype):
            if key in floors:
                dps, source = floors[key], "minEndgameDps." + key
                break
    if dps is None:
        references = [b["fullDps"] for b in targets.get("referenceBenchmarks", {}).get("builds", [])
                      if b.get("archetype") == archetype and b.get("fullDps")]
        low, high = targets.get("derivedDpsClamp", [20000, 100000])
        if references:
            dps = min(high, max(low, _median(references) * float(targets.get("derivedDpsFraction", 0.001))))
            source = "derived from reference benchmarks"
        else:
            dps, source = targets.get("defaultMinEndgameDps", 40000), "defaultMinEndgameDps"
    # The floor applies at every level but is lower for level 80 characters (fewer passive points, lower
    # item levels): linear from ``levelFloorScale.atLevel80`` at level 80 to 1.0 at ``fullAtLevel``.
    scale_cfg = targets.get("levelFloorScale") or {"atLevel80": 0.5, "fullAtLevel": 90}
    full_at = max(81, int(scale_cfg.get("fullAtLevel", 90)))
    low_scale = float(scale_cfg.get("atLevel80", 0.5))
    level = int(spec.get("level", full_at) or full_at)
    factor = min(1.0, max(low_scale, low_scale + (1.0 - low_scale) * (level - 80) / (full_at - 80)))
    full_floor = float(dps)
    dps = full_floor * factor
    return {"tier": tier, "dpsFloor": int(dps), "dpsFloorFull": int(full_floor), "levelScale": round(factor, 3),
            "dpsFloorSource": source,
            "lifePoolMin": int(override.get("lifePoolMin", targets.get("lifePoolMin", 6000))),
            "ehpMin": int(override.get("ehpMin", targets.get("ehpMin", 15000))),
            "resistTarget": int(override.get("resistTarget", targets.get("resistTarget", 75)))}


def active_tree_spec(root: ET.Element) -> ET.Element | None:
    """Resolve activeSpec by id, then positionally (1-based), then first Spec."""
    tree = root.find("./Tree")
    if tree is None:
        return None
    specs = tree.findall("Spec")
    active = tree.get("activeSpec", "1")
    found = next((entry for entry in specs if entry.get("id") == active), None)
    if found is None and specs and active.isdigit() and 1 <= int(active) <= len(specs):
        found = specs[int(active) - 1]
    return found if found is not None else (specs[0] if specs else None)


def active_skill_set(root: ET.Element) -> ET.Element | None:
    skills = root.find("Skills")
    if skills is None:
        return None
    sets = skills.findall("SkillSet")
    active = skills.get("activeSkillSet", "1")
    found = next((entry for entry in sets if entry.get("id") == active), None)
    if found is None and sets and active.isdigit() and 1 <= int(active) <= len(sets):
        found = sets[int(active) - 1]
    if found is None and sets:
        found = sets[0]
    if found is None:   # legacy flat <Skills><Skill/></Skills>
        return skills
    return found


def active_item_set(root: ET.Element) -> ET.Element | None:
    items = root.find("Items")
    if items is None:
        return None
    sets = items.findall("ItemSet")
    active = items.get("activeItemSet", "1")
    found = next((entry for entry in sets if entry.get("id") == active), None)
    if found is None and sets and active.isdigit() and 1 <= int(active) <= len(sets):
        found = sets[int(active) - 1]
    return found if found is not None else (sets[0] if sets else None)


def socket_link_groups(item_text: str) -> list[int]:
    """Sizes of linked socket groups from a PoB item's `Sockets:` line ("R-G-B B" -> [3, 1])."""
    match = re.search(r"(?im)^Sockets:[ \t]*(.*)$", item_text or "")
    if not match:
        return []
    return [len(re.findall(r"[RGBWAD]", part)) for part in match.group(1).split() if re.search(r"[RGBWAD]", part)]


def equipped_items(root: ET.Element) -> dict[str, str]:
    """slot -> raw item text for the active item set (flasks/jewel sockets excluded)."""
    by_id = {item.get("id"): item.text or "" for item in root.findall("./Items/Item")}
    item_set = active_item_set(root)
    result = {}
    if item_set is not None:
        for slot in item_set.findall("Slot"):
            if slot.get("itemId") in by_id:
                result[slot.get("name")] = by_id[slot.get("itemId")]
    return result


def _item_title_and_rarity(text: str) -> tuple[str, str, str]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    rarity = next((line.split(":", 1)[1].strip().lower() for line in lines if line.lower().startswith("rarity:")), "")
    start = next((i for i, line in enumerate(lines) if line.lower().startswith("rarity:")), -1)
    name = lines[start + 1] if start >= 0 and start + 1 < len(lines) else ""
    base = lines[start + 2] if start >= 0 and start + 2 < len(lines) else ""
    return name, base, rarity


def _int_attr(value, default: int) -> int:
    """Tolerant integer attribute: PoB writes ``nil``/empty for unset gem fields."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default


def skill_groups_from_xml(xml: str, data=None) -> list[SkillGroup]:
    """Structured SkillGroups for the active skill set of the final XML."""
    root = ET.fromstring(xml)
    skill_set = active_skill_set(root)
    groups: list[SkillGroup] = []
    if skill_set is None:
        return groups
    counters: dict[str, int] = {}
    for index, skill in enumerate(skill_set.findall("Skill")):
        gems = []
        for position, gem in enumerate(skill.findall("Gem")):
            if not gem.get("gemId") and not gem.get("nameSpec"):
                continue
            name = gem.get("nameSpec", "")
            support = False
            if data is not None and name in getattr(data, "gems", {}):
                support = bool(data.gems[name].get("support"))
            elif gem.get("skillId", "").startswith("Support"):
                support = True
            gems.append(GemRecord(name=name, support=support, level=_int_attr(gem.get("level"), 20) or 20,
                                  quality=_int_attr(gem.get("quality"), 0),
                                  enabled=gem.get("enabled", "true").lower() != "false",
                                  count=_int_attr(gem.get("count"), 1) or 1,
                                  instance_id=f"g{index}:{position}"))
        source = skill.get("source", "")
        slot = skill.get("slot", "")
        granted = bool(source) or (not slot and not any(g.name for g in gems if g.support))
        role = "main" if skill.get("includeInFullDPS") == "true" else "other"
        key = slot or source or "unslotted"
        counters[key] = counters.get(key, 0) + 1
        main_index = _int_attr(skill.get("mainActiveSkill"), 1) or 1
        actives = [g for g in gems if not g.support]
        groups.append(SkillGroup(
            id=f"{key.lower().replace(' ', '-')}-{counters[key]}", role=role, slot=slot,
            link_group=counters[key] - 1, gems=gems,
            main_active=(actives[min(main_index, len(actives)) - 1].name if actives else ""),
            delivery="item_granted" if granted else "socketed",
            include_in_full_dps=skill.get("includeInFullDPS") == "true",
            enabled=skill.get("enabled", "true").lower() != "false", source=source))
    return groups


def package_report(xml: str, spec: dict, data=None, targets: dict | None = None) -> dict:
    """Counts, per-slot occupancy, link validity and skill-package completeness from final XML."""
    targets = targets or load_targets()
    root = ET.fromstring(xml)
    groups = skill_groups_from_xml(xml, data)
    items = equipped_items(root)
    socketed = [g for g in groups if g.delivery == "socketed"]
    granted = [g for g in groups if g.delivery == "item_granted"]
    main = next((g for g in socketed if g.include_in_full_dps), None)

    slots = {}
    for slot, text in items.items():
        links = socket_link_groups(text)
        used = sum(len(g.gems) for g in socketed if g.slot == slot)
        slots[slot] = {"linkGroups": links, "sockets": sum(links), "gemsSocketed": used,
                       "spareSockets": max(0, sum(links) - used)}
    gaps = []
    body = slots.get("Body Armour", {})
    body_links = max(body.get("linkGroups", [0]) or [0])
    main_gems = len([g for g in main.gems if g.enabled]) if main else 0
    required = targets["mainLinkSize"]
    main_ok = main is not None and main_gems >= required and body_links >= required
    # Gems of the main group must be physically linked: group size <= largest link group.
    if main is not None and main_gems > body_links:
        gaps.append(f"main group has {main_gems} gems but the body armour's largest link is {body_links}")
    if main is None:
        gaps.append("no main damage skill group")
    elif main_gems < required:
        gaps.append(f"main link has {main_gems} of {required} gems")
    elif body_links < required:
        gaps.append(f"body armour has no {required}-link (largest link {body_links})")
    # Per-item overflow: a group cannot occupy more sockets than the item's link allows.
    for slot, info in slots.items():
        per_group = [len(g.gems) for g in socketed if g.slot == slot]
        if per_group and max(per_group) > max(info["linkGroups"] or [0]):
            gaps.append(f"{slot} has a {max(per_group)}-gem group in sockets linked at most {max(info['linkGroups'] or [0])}")
        if info["gemsSocketed"] > info["sockets"]:
            gaps.append(f"{slot} holds {info['gemsSocketed']} gems in {info['sockets']} sockets")
    # Every socket on every equipped item must hold a gem. PoB gives an item without a `Sockets:`
    # line all of its base's sockets, so an unused socket is always real and always a gap.
    # When the planner tried the whole fill catalogue and no level-, attribute- and resource-legal gem
    # with a stated role remains, the sockets are disclosed (``openSockets``), not counted as a gap.
    fill_exhausted = bool(((spec or {}).get("skillPlanSummary") or {}).get("fillExhausted"))
    open_sockets = []
    for slot, info in sorted(slots.items()):
        if info["spareSockets"] > 0 and info["sockets"] > 0:
            text = (f"{slot} has {info['spareSockets']} empty socket{'s' if info['spareSockets'] != 1 else ''} "
                    f"of {info['sockets']}")
            if fill_exhausted:
                open_sockets.append(text + " (no further justified gem is legal for this build)")
            else:
                gaps.append(text)
    utility = [g for g in socketed if not g.include_in_full_dps]
    supported_utility = [g for g in utility if g.supports and g.actives]
    if len(supported_utility) < targets["minSupportedUtilityGroups"]:
        gaps.append(f"supporting skill package is sparse: {len(supported_utility)} supported utility group(s), "
                    f"{len(utility)} utility groups, {sum(len(g.gems) for g in utility)} utility gems "
                    f"(target {targets['minSupportedUtilityGroups']} supported groups)")
    total_gems = sum(1 for g in socketed for gem in g.gems)
    return {"groups": [g.to_dict() for g in groups],
            "counts": {"socketedGems": total_gems, "itemGrantedSkills": len(granted),
                       "utilityGroups": len(utility), "supportedUtilityGroups": len(supported_utility),
                       "mainLinkGems": main_gems, "bodyArmourLargestLink": body_links,
                       "slots": slots},
            "mainLinkComplete": main_ok, "gaps": gaps, "openSockets": open_sockets}


def jewel_report(root: ET.Element, spec: dict, data=None) -> dict:
    tree_spec = active_tree_spec(root)
    by_id = {item.get("id"): item.text or "" for item in root.findall("./Items/Item")}
    jewels = []
    if tree_spec is not None:
        for socket in tree_spec.findall("./Sockets/Socket"):
            item_id = socket.get("itemId")
            if item_id in by_id:
                name, base, rarity = _item_title_and_rarity(by_id[item_id])
                jewels.append({"node": socket.get("nodeId"), "name": name, "base": base,
                               "rarity": rarity, "itemId": item_id})
    return {"equipped": jewels, "count": len(jewels),
            "uniqueJewels": [j for j in jewels if j["rarity"] == "unique"]}


def mastery_count(root: ET.Element) -> int:
    tree_spec = active_tree_spec(root)
    return len(re.findall(r"\{\d+,\d+\}", tree_spec.get("masteryEffects", ""))) if tree_spec is not None else 0


def unique_summary(root: ET.Element) -> list[dict]:
    result = []
    for slot, text in equipped_items(root).items():
        name, base, rarity = _item_title_and_rarity(text)
        if rarity == "unique":
            result.append({"slot": slot, "name": name, "base": base})
    by_id = {item.get("id"): item.text or "" for item in root.findall("./Items/Item")}
    tree_spec = active_tree_spec(root)
    if tree_spec is not None:
        for socket in tree_spec.findall("./Sockets/Socket"):
            text = by_id.get(socket.get("itemId"))
            if text:
                name, base, rarity = _item_title_and_rarity(text)
                if rarity == "unique":
                    result.append({"slot": "Jewel " + str(socket.get("nodeId")), "name": name, "base": base})
    return result


SOFT_READINESS_PREFIXES = ()   # chaos resistance, Pantheons and the Determination armour check are hard gaps


def final_quality_report(xml: str, spec: dict, calculation: dict, mechanic_checks: list[dict],
                         diagnostics: dict, data=None, search_limited: bool = False,
                         legality_failures: list[str] | None = None, targets: dict | None = None,
                         assessed_status: str | None = None) -> QualityReport:
    """Combine legality, mechanic validity, completeness, readiness and price coverage."""
    targets = targets or load_targets()
    root = ET.fromstring(xml)
    package = package_report(xml, spec, data, targets)
    completeness = diagnostics.get("completeness", {})
    readiness = diagnostics.get("encounterReadiness", {})
    report = QualityReport()
    report.legality = "fail" if legality_failures else "pass"
    failed_mech = [c for c in mechanic_checks if not c.get("passed")]
    report.mechanics = "pass" if not failed_mech else "fail"
    gaps = list(completeness.get("gaps", [])) + [g for g in package["gaps"]
                                                   if g not in completeness.get("gaps", [])]
    report.completeness = "complete" if not gaps and package["mainLinkComplete"] else "incomplete"
    readiness_gaps = [g for g in readiness.get("gaps", []) if g not in gaps]
    # Every readiness gap blocks readiness: chaos resistance and Pantheons are now repaired by the
    # generator, so a remaining gap is a real gap (damage, pool, EHP, resistance, armour, search limit).
    hard_readiness = [g for g in readiness_gaps if not g.startswith(SOFT_READINESS_PREFIXES)]
    report.encounter_readiness = "ready" if not hard_readiness and not search_limited else "not_ready"
    report.price_coverage = diagnostics.get("priceCoverage", {})
    report.gaps = gaps + readiness_gaps + [f"mechanic check failed: {c.get('name')}" for c in failed_mech]
    if legality_failures:
        report.gaps = list(legality_failures) + report.gaps
    jewels = jewel_report(root, spec, data)
    uniques = unique_summary(root)
    stats = calculation.get("stats", {})
    passives = calculation.get("passives", {})
    report.counts = {**package["counts"], "masteries": mastery_count(root), "jewels": jewels["count"],
                     "uniqueJewels": len(jewels["uniqueJewels"]), "uniques": len(uniques),
                     "unspentPoints": max(0, int(passives.get("maximum", 0) or 0) - int(passives.get("used", 0) or 0))}
    report.groups = package["groups"]
    report.notes = [f"search limit reached: {spec.get('_searchLimitNote')}" if search_limited else "",
                    "main link: " + ("complete" if package["mainLinkComplete"] else "incomplete")]
    report.notes = [n for n in report.notes if n]
    report.repairs = repairs_for(report, spec)
    # One status: the report can never upgrade the assessed badge (and sharing/retry cannot
    # upgrade it either); "validated" requires every dimension plus the assessed badge.
    report.status = "validated" if report.validated() and assessed_status in (None, "validated") else "experimental"
    if report.legality == "fail":
        report.status = "failed"
    return report


def repairs_for(report: QualityReport, spec: dict) -> list[str]:
    repairs = []
    if any("main link" in gap or "-link" in gap for gap in report.gaps):
        repairs.append("complete the six-link: whole-build repair of supports, resources and body armour")
    if any("supporting skill package" in gap for gap in report.gaps):
        repairs.append("add supported utility groups (movement, reservation, curse, herald) on spare sockets")
    if spec.get("linkResourceDeficit"):
        repairs.append("raise unreserved mana or reduce skill cost; the link was kept at full size")
    return repairs
