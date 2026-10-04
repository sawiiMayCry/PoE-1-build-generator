"""Assemble clean PoB XML from a generated design, without reference exports.

Skills are serialized from *structured skill groups* (see ``skill_packages``):
each group is a real PoB socket group with its own equipment slot and linked
sockets, any number of active gems and supports, and per-instance gem
level/quality/enabled state.  Assembly is pure: it never mutates the spec, the
items or the groups it receives, and it fails with ``SocketConflict`` when the
groups cannot physically fit the equipped items.

Skills granted by items or the passive tree (for example ``EnemyExplode``)
are *not* socketed gems.  They are never emitted here -- PoB derives them from
the item/tree modifiers -- and ``loadout_summary.summarize_loadout`` reports
them separately from socketed gems.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from generation_data import GameData, RareItem
from skill_packages import (
    ROLE_ORDER, as_group_dicts, capacity_from_equipment, gem_socket_color, legacy_groups,
    pack_groups, parse_socket_runs, socket_line, validate_groups, apply_stage_caps)


CONDITIONAL_ITEM_SKILLS = {
    "The Queen's Hunger": {"Bone Offering", "Flesh Offering", "Spirit Offering"},
}
ROLE_LABELS = {"main": "Main", "movement": "Movement", "guard": "Guard", "aura": "Reservation",
               "defense": "Defense", "herald": "Herald", "curse": "Curse", "offering": "Offerings",
               "minion_helper": "Minion helper", "trigger": "Trigger", "utility": "Utility"}


class SocketConflict(ValueError):
    """Skill groups cannot be socketed into the equipped items as requested."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(problems))


def disable_conditional_item_skill_groups(xml: str) -> tuple[str, list[dict]]:
    """Disable item-granted skills whose trigger/dependency uptime is not modeled."""
    root = ET.fromstring(xml)
    disabled = []
    for skill in root.findall("./Skills/SkillSet/Skill"):
        source = skill.get("source", "")
        item_name = next((name for name in CONDITIONAL_ITEM_SKILLS if name in source), None)
        if item_name is None:
            continue
        group_enabled = skill.get("enabled", "true").lower() != "false"
        for gem in skill.findall("Gem"):
            name = gem.get("nameSpec", "")
            if name not in CONDITIONAL_ITEM_SKILLS[item_name]:
                continue
            was_enabled = group_enabled and gem.get("enabled", "true").lower() != "false"
            skill.set("enabled", "false")
            gem.set("enabled", "false")
            if was_enabled:
                disabled.append({"item": item_name, "skill": name, "wasEnabled": True})
    if not disabled:
        return xml, []
    return ET.tostring(root, encoding="unicode"), disabled


def _set_sockets(text: str, line: str | None) -> str:
    """Return item text with its gem ``Sockets:`` line replaced (abyssal sockets kept)."""
    lines = text.split("\n")
    kept_abyss = ""
    for index, existing in enumerate(lines):
        if existing.strip().startswith("Sockets:"):
            groups = existing.split(":", 1)[1].split()
            abyss = [group for group in groups if set(group.replace("-", "")) <= {"A", "D"}]
            kept_abyss = " ".join(abyss)
            del lines[index]
            break
    value = " ".join(part for part in (line or "", kept_abyss) if part)
    if not value:
        return "\n".join(lines)
    position = next((i for i, entry in enumerate(lines) if entry.strip().startswith("Implicits:")), len(lines))
    lines.insert(position, "Sockets: " + value)
    return "\n".join(lines)


def _default_level(spec: dict, data: GameData, name: str) -> int:
    gem = data.gem(name)
    level = spec.get("gemLevels", {}).get(name, min(gem.get("maxLevel", 20), 20))
    # Keep guard/utility strength requirements modest.
    return min(level, 10) if name == "Steelskin" else level


def equipment_capacity(spec: dict, data, items, uniques: dict[str, str] | None):
    """``(active_items, capacity, two_handed_mainhand)`` exactly as assembly will equip them.

    Shared by the assembler and the skill planner so both agree on which slots exist and how many
    sockets they offer (a two-handed Weapon 1 unique removes Weapon 2 and may gain sockets).
    """
    uniques = uniques or {}
    unique_mainhand = uniques.get("Weapon 1")
    two_handed_mainhand = False
    if unique_mainhand:
        lines = [line.strip() for line in unique_mainhand.splitlines() if line.strip()]
        rarity_index = next((index for index, line in enumerate(lines)
                             if line.upper().startswith("RARITY:")), None)
        if rarity_index is not None and rarity_index + 2 < len(lines):
            base = data.bases.get(lines[rarity_index + 2], {})
            tags = base.get("tags", {})
            two_handed_mainhand = bool(tags.get("two_hand_weapon") or tags.get("twohanded"))
    if two_handed_mainhand and uniques.get("Weapon 2"):
        raise ValueError("A two-handed Weapon 1 unique cannot be equipped with a requested Weapon 2 item")
    active_items = [item for item in items
                    if not item.slot.startswith("Flask ") and
                    not (two_handed_mainhand and item.slot == "Weapon 2")]
    capacity = apply_stage_caps(capacity_from_equipment(active_items, uniques, data),
                                spec.get("utilitySockets"), spec.get("mainLinks"))
    if two_handed_mainhand and "Weapon 1" in capacity:
        from skill_packages import slot_socket_cap
        base_name = capacity["Weapon 1"].get("item")
        if not capacity["Weapon 1"]["fixedRuns"]:
            capacity["Weapon 1"]["total"] = slot_socket_cap(
                "Weapon 1", data.bases.get(base_name, {}), two_handed=True)
    return active_items, capacity, two_handed_mainhand


def assemble_with_report(spec: dict, context: dict, data: GameData, nodes: set[str],
                         supports: list[str], items: list[RareItem], masteries: dict[str, int] | None = None,
                         uniques: dict[str, str] | None = None,
                         jewels: dict[str, RareItem | str] | None = None, *,
                         skill_groups=None, main_group_id: str | None = None) -> tuple[str, dict]:
    """Serialize a loadout and report how its groups were socketed.

    ``skill_groups`` (planner dicts or ``build_contracts.SkillGroup``) take
    precedence over ``spec["skillGroups"]``; with neither, the legacy
    ``supports`` + ``spec["utility"]`` shape is converted.  ``main_group_id``
    makes another group the PoB main socket group (used to probe a utility
    group's support legality).  The report lists placements, linked runs,
    per-slot occupancy and the socketed gem count.
    """
    uniques = uniques or {}
    raw_groups = skill_groups if skill_groups is not None else spec.get("skillGroups")
    if raw_groups is not None:
        groups = [dict(group) for group in as_group_dicts(raw_groups)]
        for group in groups:
            group["gems"] = [dict(gem) for gem in group["gems"]]
    else:
        groups = legacy_groups(spec, supports, data, level_for=lambda name: _default_level(spec, data, name))
    for group in groups:
        for gem in group["gems"]:
            if gem.get("level") is None:
                gem["level"] = _default_level(spec, data, gem["name"])

    root = ET.Element("PathOfBuilding")
    pantheon = spec.get("pantheon") or {}
    ET.SubElement(root, "Build", {"level": str(spec["level"]), "characterLevelAutoMode": "false",
        "bandit": "None", "className": "Witch", "ascendClassName": spec["ascendancy"],
        "pantheonMajorGod": str(pantheon.get("major") or "None"),
        "pantheonMinorGod": str(pantheon.get("minor") or "None"),
        "mainSocketGroup": "1", "targetVersion": "3_0", "viewMode": "TREE",
        "name": spec["skill"] + " " + spec["ascendancy"]})
    classes = context["tree"]["classes"]
    class_id = next(index for index, entry in enumerate(classes) if entry["name"] == "Witch")
    ascend_id = (0 if spec["ascendancy"] == "None" else
                next(index for index, entry in enumerate(classes[class_id]["ascendancies"], 1)
                     if entry["name"] == spec["ascendancy"]))
    tree = ET.SubElement(root, "Tree", activeSpec="1")
    tree_spec = ET.SubElement(tree, "Spec", {"classId": str(class_id), "ascendClassId": str(ascend_id),
        "secondaryAscendClassId": "0", "treeVersion": context["treeVersion"],
        "nodes": ",".join(sorted(nodes, key=int)), "masteryEffects": ",".join(
            "{" + node + "," + str(effect) + "}" for node, effect in sorted((masteries or {}).items()))})
    socket_xml = ET.SubElement(tree_spec, "Sockets")

    active_items, capacity, two_handed_mainhand = equipment_capacity(spec, data, items, uniques)
    if raw_groups is None:
        # Legacy callers named slots that may hold no generated item (stage
        # fixtures); keep their historical leniency for unequipped slots only.
        for group in groups:
            slot = group.get("slot")
            if slot and slot not in capacity:
                size = sum(len(g["gems"]) for g in groups if g.get("slot") == slot)
                capacity[slot] = {"total": size, "fixedRuns": None, "unique": False, "item": None,
                                  "unequipped": True}
    problems = validate_groups(groups, capacity, data)
    if problems:
        raise SocketConflict(problems)
    packing = pack_groups(groups, capacity)
    placements = packing["placements"]
    by_slot_groups: dict[str, list[dict]] = {}
    for group in groups:
        by_slot_groups.setdefault(placements[group["id"]], []).append(group)

    # Socket layouts: one linked run per multi-gem group, one socket per lone gem.
    layouts: dict[str, str] = {}
    run_report: dict[str, list[int]] = {}
    for slot, listed in by_slot_groups.items():
        info = capacity[slot]
        main_here = next((g for g in listed if g["role"] == "main"), None)
        sizes = [len(g["gems"]) for g in listed]
        order = sorted(range(len(listed)), key=lambda i: (-sizes[i], listed[i]["id"]))
        if info["fixedRuns"] is not None:
            layouts[slot] = None  # keep the item's own fixed sockets
            run_report[slot] = list(info["fixedRuns"])
            continue
        run_sizes = [sizes[i] for i in order]
        colors: list[str] = []
        for i in order:
            colors.extend(gem_socket_color(data, gem["name"]) for gem in listed[i]["gems"])
        if slot == "Body Armour" and main_here is not None:
            target = min(info["total"], int(spec.get("mainLinks", len(main_here["gems"]))))
            extra = target - sum(run_sizes)
            if extra > 0:
                position = next(j for j, i in enumerate(order) if listed[i] is main_here)
                run_sizes[position] += extra
                insert_at = sum(run_sizes[:position]) + sizes[order[position]]
                colors[insert_at:insert_at] = ["B"] * extra
        spare = max(0, min(int(spec.get("spareSockets", 0)), info["total"] - sum(run_sizes)))
        layouts[slot] = socket_line(run_sizes, colors, spare=spare)
        run_report[slot] = run_sizes + [1] * spare

    skills = ET.SubElement(root, "Skills", activeSkillSet="1", defaultGemLevel="20", defaultGemQuality="0")
    skill_set = ET.SubElement(skills, "SkillSet", id="1", title="Generated skills")
    ordered = sorted(groups, key=lambda g: (g["role"] != "main", ROLE_ORDER.index(g["role"])
                                            if g["role"] in ROLE_ORDER else 99, g["id"]))
    chosen_main = main_group_id or next((g["id"] for g in ordered if g["role"] == "main"), ordered[0]["id"])
    main_index = next(index for index, g in enumerate(ordered, 1) if g["id"] == chosen_main)
    root.find("Build").set("mainSocketGroup", str(main_index))
    for group in ordered:
        gems = group["gems"]
        enabled_actives = [gem for gem in gems if gem["kind"] == "active" and gem.get("enabled", True)]
        selected = 1
        if group.get("mainActive"):
            selected = 1 + next((i for i, gem in enumerate(enabled_actives)
                                 if gem["name"] == group["mainActive"]), 0)
        skill = ET.SubElement(skill_set, "Skill", {
            "slot": placements[group["id"]], "enabled": str(group.get("enabled", True)).lower(),
            "label": ROLE_LABELS.get(group["role"], group["role"].title()),
            "mainActiveSkill": str(selected), "mainActiveSkillCalcs": str(selected),
            "includeInFullDPS": str(bool(group.get("includeInFullDPS"))).lower()})
        for gem in gems:
            record = data.gem(gem["name"])
            minion_count = (spec.get("minionCount") or 1) if (
                group["role"] == "main" and gem["name"] == group.get("mainActive")
                and gem["kind"] == "active") else (gem.get("count") or 1)
            ET.SubElement(skill, "Gem", {"gemId": record["gameId"], "variantId": record["variantId"],
                "skillId": record["skillId"], "nameSpec": record["name"], "level": str(gem["level"]),
                "quality": str(gem.get("quality", 0)),
                "enabled": str(gem.get("enabled", True)).lower(), "enableGlobal1": "true",
                "enableGlobal2": "false", "count": str(minion_count)})

    equipment = ET.SubElement(root, "Items", activeItemSet="1", useSecondWeaponSet="false")
    item_set = ET.SubElement(equipment, "ItemSet", id="1", title="Generated equipment", useSecondWeaponSet="false")
    for index, item in enumerate(active_items, 1):
        text = uniques.get(item.slot) or item.text(0)
        if layouts.get(item.slot):
            text = _set_sockets(text, layouts[item.slot])
        ET.SubElement(equipment, "Item", id=str(index)).text = text
        ET.SubElement(item_set, "Slot", name=item.slot, itemId=str(index))
    unique_flasks = sorted(((slot, text) for slot, text in uniques.items()
                            if slot.startswith("Flask ")), key=lambda row: int(row[0].split()[1]))
    for index, (slot, text) in enumerate(unique_flasks, start=len(active_items) + 1):
        ET.SubElement(equipment, "Item", id=str(index)).text = text
        ET.SubElement(item_set, "Slot", name=slot, itemId=str(index), active="false")
    for index, (node_id, jewel) in enumerate(sorted((jewels or {}).items(), key=lambda row: int(row[0])),
                                              start=len(active_items) + len(unique_flasks) + 1):
        text = jewel.text(0) if isinstance(jewel, RareItem) else jewel
        ET.SubElement(equipment, "Item", id=str(index)).text = text
        ET.SubElement(socket_xml, "Socket", nodeId=str(node_id), itemId=str(index))
    config = ET.SubElement(root, "Config", activeConfigSet="1")
    config_set = ET.SubElement(config, "ConfigSet", id="1", title="Conservative combat settings")
    # Kitava penalties are supplied by PoB for this level. No synthetic buffs,
    # charges, flasks, custom modifiers, or borrowed boss conditions are added.
    ET.SubElement(config_set, "Input", name="enemyLevel", number=str(spec.get("enemyLevel", 83)))
    if "resistancePenalty" in spec:
        ET.SubElement(config_set, "Input", name="resistancePenalty", number=str(spec["resistancePenalty"]))
    # Current PoB keeps Pantheons in the ConfigSet (the Build attributes are only a legacy loader), so
    # the selection is written to both places.
    if pantheon.get("major"):
        ET.SubElement(config_set, "Input", name="pantheonMajorGod", string=str(pantheon["major"]))
    if pantheon.get("minor"):
        ET.SubElement(config_set, "Input", name="pantheonMinorGod", string=str(pantheon["minor"]))
    slots_report = {}
    for slot, info in capacity.items():
        used = sum(len(g["gems"]) for g in by_slot_groups.get(slot, []))
        total = sum(run_report[slot]) if slot in run_report else info["total"]
        slots_report[slot] = {"used": used, "sockets": total if used else 0, "limit": info["total"],
                              "groups": [g["id"] for g in by_slot_groups.get(slot, [])],
                              "linkedRuns": run_report.get(slot, []), "unique": info.get("unique", False)}
    report = {"placements": placements, "slots": slots_report,
              "socketedGemCount": sum(len(g["gems"]) for g in groups),
              "groups": [{"id": g["id"], "role": g["role"], "slot": placements[g["id"]],
                          "gems": [gem["name"] for gem in g["gems"]]} for g in ordered],
              "mainSocketGroup": main_index, "itemGrantedSkillsEmitted": 0}
    return ET.tostring(root, encoding="unicode"), report


def assemble(spec: dict, context: dict, data: GameData, nodes: set[str],
             supports: list[str], items: list[RareItem], masteries: dict[str, int] | None = None,
             uniques: dict[str, str] | None = None,
             jewels: dict[str, RareItem | str] | None = None, *,
             skill_groups=None, main_group_id: str | None = None) -> str:
    """Compatibility wrapper returning only the XML (see ``assemble_with_report``)."""
    return assemble_with_report(spec, context, data, nodes, supports, items, masteries, uniques, jewels,
                                skill_groups=skill_groups, main_group_id=main_group_id)[0]
