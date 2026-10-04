"""Structured, read-only summaries of a PoB XML's *active* loadout.

Everything the app, progression notes and completeness checks claim about
skills, sockets, uniques and jewels is derived here from the final exported
XML, never from a plan.  Item-granted skills (for example ``EnemyExplode``
from an item or tree node) are kept separate from gems that occupy sockets.

The module has no dependency on the generator, PoB or the network.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

GEM_COLORS = frozenset("RGBW")
SLOT_ORDER = ("Weapon 1", "Weapon 2", "Helmet", "Body Armour", "Gloves", "Boots",
              "Amulet", "Ring 1", "Ring 2", "Belt")


def active_spec(root: ET.Element) -> ET.Element | None:
    """PoB's ``activeSpec`` is positional; IDs are optional in saved Specs."""
    tree = root.find("Tree")
    if tree is None:
        return None
    specs = tree.findall("Spec")
    if not specs:
        return None
    active = tree.get("activeSpec", "1")
    if active.isdigit() and 0 < int(active) <= len(specs):
        return specs[int(active) - 1]
    return next((spec for spec in specs if spec.get("id") == active), specs[0])


def _active_by_id(root: ET.Element, section: str, child: str, attribute: str) -> ET.Element | None:
    container = root.find(section)
    if container is None:
        return None
    entries = container.findall(child)
    if not entries:
        return None
    active = container.get(attribute, "1")
    return next((entry for entry in entries if entry.get("id") == active), entries[0])


def active_skill_set(root: ET.Element) -> ET.Element | None:
    return _active_by_id(root, "Skills", "SkillSet", "activeSkillSet")


def active_item_set(root: ET.Element) -> ET.Element | None:
    return _active_by_id(root, "Items", "ItemSet", "activeItemSet")


def parse_item(text: str | None) -> dict:
    """Identity, sockets and flags of one PoB item text."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    index = next((i for i, line in enumerate(lines) if line.upper().startswith("RARITY:")), None)
    if index is None or index + 1 >= len(lines):
        return {"rarity": None, "name": None, "base": None, "sockets": [], "linked": 0,
                "variant": None, "corrupted": False, "league": None, "text": text or ""}
    rarity = lines[index].split(":", 1)[1].strip().lower()
    name = lines[index + 1]
    has_base = rarity in {"rare", "unique", "magic", "relic"} and index + 2 < len(lines)
    base = lines[index + 2] if has_base else name
    sockets = []
    for line in lines:
        if line.startswith("Sockets:"):
            for group in line.split(":", 1)[1].split():
                colors = [color for color in re.split(r"-", group) if color in GEM_COLORS]
                # An all-abyssal group (A) has no gem sockets and is dropped.
                if colors:
                    sockets.append(colors)
            break
    variants = re.findall(r"(?m)^Variant:\s*(.+?)\s*$", text or "")
    selected = re.search(r"(?m)^Selected Variant:\s*(\d+)\s*$", text or "")
    variant = None
    if selected and variants and 0 < int(selected.group(1)) <= len(variants):
        label = variants[int(selected.group(1)) - 1]
        variant = None if label == "Current" else label
    league = re.search(r"(?m)^League:\s*(.+?)\s*$", text or "")
    return {"rarity": rarity, "name": name, "base": base, "sockets": sockets,
            "linked": max((len(group) for group in sockets), default=0),
            "socketCount": sum(len(group) for group in sockets),
            "variant": variant,
            "corrupted": bool(re.search(r"(?mi)^Corrupted\s*$", text or "")),
            "league": league.group(1) if league else None, "text": text or ""}


def _gem_kind(gem: ET.Element, catalogue: dict | None) -> str | None:
    name = gem.get("nameSpec", "")
    if catalogue and name in catalogue:
        return "support" if catalogue[name].get("support") else "active"
    identifier = (gem.get("gemId") or "") + " " + (gem.get("skillId") or "")
    if not gem.get("gemId"):
        return None
    return "support" if "Support" in identifier else "active"


def _pack(group_sizes: list[int], sockets: list[int]) -> list[int | None]:
    """Assign gem groups to the item's linked-socket runs.

    A group of two or more gems needs a whole run of its own (otherwise its
    supports would also affect a neighbour).  A lone gem needs only one free
    socket anywhere, because nothing is linked to it.
    """
    capacity = list(sockets)
    taken = [False] * len(sockets)
    assignment: list[int | None] = [None] * len(group_sizes)
    for group in sorted(range(len(group_sizes)), key=lambda i: -group_sizes[i]):
        size = group_sizes[group]
        if size >= 2:
            run = next((i for i in sorted(range(len(sockets)), key=lambda i: sockets[i])
                        if not taken[i] and capacity[i] == sockets[i] and sockets[i] >= size), None)
            if run is not None:
                taken[run] = True
                capacity[run] = 0
                assignment[group] = run
        else:
            run = next((i for i in sorted(range(len(sockets)), key=lambda i: (-capacity[i], i))
                        if not taken[i] and capacity[i] >= 1), None)
            if run is not None:
                capacity[run] -= 1
                assignment[group] = run
    return assignment


def summarize_loadout(xml: str | ET.Element, catalogue: dict | None = None) -> dict:
    """Summarize the active skills/items/jewels of a PoB document.

    ``catalogue`` optionally maps gem names to metadata (``support`` flag) when
    gem IDs alone are not enough to classify a gem.
    """
    root = ET.fromstring(xml) if isinstance(xml, str) else xml
    skill_set = active_skill_set(root)
    item_set = active_item_set(root)
    items_by_id = {item.get("id"): item for item in root.findall("./Items/Item")}
    slot_items: dict[str, dict] = {}
    if item_set is not None:
        for slot in item_set.findall("Slot"):
            item = items_by_id.get(slot.get("itemId"))
            if item is not None:
                parsed = parse_item(item.text)
                parsed["itemId"] = item.get("id")
                slot_items[slot.get("name")] = parsed
    build = root.find("Build")
    main_index = int(build.get("mainSocketGroup", "1")) - 1 if build is not None else 0
    raw_groups = skill_set.findall("Skill") if skill_set is not None else []
    groups = []
    for index, skill in enumerate(raw_groups):
        gems = []
        for gem in skill.findall("Gem"):
            kind = _gem_kind(gem, catalogue)
            level = gem.get("level")
            gems.append({"name": gem.get("nameSpec", ""), "kind": kind or "granted",
                         "level": int(level) if level and level.isdigit() else None,
                         "quality": int(gem.get("quality", "0") or 0) if (gem.get("quality") or "0").lstrip("-").isdigit() else 0,
                         "enabled": gem.get("enabled", "true").lower() != "false",
                         "socketed": bool(gem.get("gemId")) and not skill.get("source"),
                         "instance": f"g{index + 1}:{len(gems) + 1}"})
        granted = bool(skill.get("source")) or not any(gem["socketed"] for gem in gems)
        active_gems = [gem for gem in gems if gem["kind"] == "active" and gem["enabled"]]
        selected = skill.get("mainActiveSkill", "1")
        main_active = (active_gems[int(selected) - 1]["name"]
                       if selected.isdigit() and 0 < int(selected) <= len(active_gems) else None)
        groups.append({"index": index + 1, "slot": skill.get("slot") or None,
                       "label": skill.get("label") or "", "source": skill.get("source") or None,
                       "enabled": skill.get("enabled", "true").lower() != "false",
                       "includeInFullDPS": skill.get("includeInFullDPS", "false").lower() == "true",
                       "isMain": index == main_index, "mainActive": main_active,
                       "itemGranted": granted, "gems": gems,
                       "socketedGemCount": sum(gem["socketed"] for gem in gems),
                       "activeCount": sum(gem["socketed"] and gem["kind"] == "active" for gem in gems),
                       "supportCount": sum(gem["socketed"] and gem["kind"] == "support" for gem in gems)})
    by_slot: dict[str, dict] = {}
    for name, item in slot_items.items():
        if name.startswith("Flask"):
            continue
        entry = {"slot": name, "item": {key: item[key] for key in ("name", "base", "rarity", "variant", "corrupted")},
                 "isUnique": item["rarity"] == "unique",
                 "linkedRuns": [len(run) for run in item["sockets"]],
                 "socketTotal": item.get("socketCount", 0), "groups": []}
        by_slot[name] = entry
    unplaced = []
    slot_groups: dict[str, list[dict]] = {}
    for group in groups:
        if not group["itemGranted"] and group["socketedGemCount"]:
            slot_groups.setdefault(group["slot"] or "", []).append(group)
    for slot, listed in slot_groups.items():
        entry = by_slot.get(slot)
        if entry is None:
            unplaced.extend(group["index"] for group in listed)
            continue
        assignment = _pack([group["socketedGemCount"] for group in listed], entry["linkedRuns"])
        for group, run in zip(listed, assignment):
            group["linkRun"] = run
            if run is None:
                unplaced.append(group["index"])
            entry["groups"].append(group["index"])
    for entry in by_slot.values():
        used = sum(groups[i - 1]["socketedGemCount"] for i in entry["groups"])
        entry["socketsUsed"] = used
        entry["socketsSpare"] = max(0, entry["socketTotal"] - used)
    spec = active_spec(root)
    jewels = []
    if spec is not None:
        for socket in spec.findall("./Sockets/Socket"):
            item = items_by_id.get(socket.get("itemId"))
            if item is None:
                continue
            parsed = parse_item(item.text)
            jewels.append({"node": socket.get("nodeId"), "name": parsed["name"], "base": parsed["base"],
                           "rarity": parsed["rarity"], "isUnique": parsed["rarity"] == "unique",
                           "variant": parsed["variant"], "corrupted": parsed["corrupted"],
                           "lines": [line for line in parsed["text"].splitlines()[1:]
                                     if line.strip() and ":" not in line][:8]})
    gear = []
    for name, item in slot_items.items():
        gear.append({"slot": name, "name": item["name"], "base": item["base"], "rarity": item["rarity"],
                     "isUnique": item["rarity"] == "unique", "variant": item["variant"],
                     "corrupted": item["corrupted"], "league": item["league"],
                     "links": item["linked"] or None, "sockets": item.get("socketCount", 0)})
    gear.sort(key=lambda row: (SLOT_ORDER.index(row["slot"]) if row["slot"] in SLOT_ORDER else 99, row["slot"]))
    socketed = [gem for group in groups if not group["itemGranted"] for gem in group["gems"] if gem["socketed"]]
    main = groups[main_index] if 0 <= main_index < len(groups) else None
    return {
        "groups": groups, "bySlot": by_slot, "gear": gear, "jewels": jewels,
        "mainGroup": main["index"] if main else None,
        "mainSkill": main["mainActive"] if main else None,
        "mainLinkGems": [gem["name"] for gem in main["gems"] if gem["socketed"]] if main else [],
        "socketedGemCount": len(socketed),
        "activeGemCount": sum(gem["kind"] == "active" for gem in socketed),
        "supportGemCount": sum(gem["kind"] == "support" for gem in socketed),
        "socketedGroupCount": sum(1 for group in groups if not group["itemGranted"] and group["socketedGemCount"]),
        "supportedGroupCount": sum(1 for group in groups if not group["itemGranted"]
                                   and group["activeCount"] and group["supportCount"]),
        "itemGranted": [{"index": group["index"], "source": group["source"], "skills": [g["name"] for g in group["gems"]]}
                        for group in groups if group["itemGranted"]],
        "unplacedGroups": unplaced,
        "uniqueCount": sum(row["isUnique"] for row in gear) + sum(row["isUnique"] for row in jewels),
        "jewelCount": len(jewels),
    }


def item_level_requirement(text: str | None) -> int:
    """Character level an item text demands (``LevelReq:`` / ``Requires Level``)."""
    values = [int(value) for value in re.findall(r"(?mi)^(?:LevelReq:|Requires Level)\s*(\d+)", text or "")]
    return max(values, default=0)


LOADOUT_VIEW_SCHEMA = 2


def loadout_view(xml: str | ET.Element, catalogue: dict | None = None) -> dict:
    """Compact, JSON-safe view of the active loadout for the app/result payload.

    Derived only from the final XML.  ``slots`` lists every socketed item with
    its unique/base name, linked runs and used/spare sockets; ``groups`` are the
    socketed skill groups (item-granted skills are listed separately and never
    counted as gems).
    """
    summary = summarize_loadout(xml, catalogue)
    groups = [{"index": group["index"], "slot": group["slot"], "label": group["label"],
               "isMain": group["isMain"], "mainActive": group["mainActive"], "enabled": group["enabled"],
               "linkRun": group.get("linkRun"),
               "gems": [{key: gem[key] for key in ("name", "kind", "level", "quality", "enabled")}
                        for gem in group["gems"] if gem["socketed"]]}
              for group in summary["groups"] if not group["itemGranted"] and group["socketedGemCount"]]
    slots = {}
    for name, entry in summary["bySlot"].items():
        if entry["socketTotal"] or entry["groups"]:
            slots[name] = {"item": entry["item"], "isUnique": entry["isUnique"], "sockets": entry["socketTotal"],
                           "linkedRuns": entry["linkedRuns"], "used": entry["socketsUsed"],
                           "spare": entry["socketsSpare"], "groups": entry["groups"]}
    return {"schema": LOADOUT_VIEW_SCHEMA, "groups": groups, "slots": slots,
            "gear": [{key: row[key] for key in ("slot", "name", "base", "rarity", "isUnique", "variant", "links")}
                     for row in summary["gear"]],
            "jewels": [{key: jewel[key] for key in ("node", "name", "base", "rarity", "isUnique", "variant", "lines")}
                       for jewel in summary["jewels"]],
            "itemGranted": summary["itemGranted"],
            "counts": {"socketedGems": summary["socketedGemCount"], "activeGems": summary["activeGemCount"],
                       "supportGems": summary["supportGemCount"], "socketedGroups": summary["socketedGroupCount"],
                       "supportedGroups": summary["supportedGroupCount"],
                       "itemGrantedSkills": len(summary["itemGranted"]), "jewels": summary["jewelCount"],
                       "uniques": summary["uniqueCount"], "unplacedGroups": len(summary["unplacedGroups"])},
            "mainSkill": summary["mainSkill"], "mainLinkGems": summary["mainLinkGems"]}
