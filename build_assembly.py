"""Assemble clean PoB XML from a generated design, without reference exports."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from generation_data import GameData, RareItem


CONDITIONAL_ITEM_SKILLS = {
    "The Queen's Hunger": {"Bone Offering", "Flesh Offering", "Spirit Offering"},
}


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


def assemble(spec: dict, context: dict, data: GameData, nodes: set[str],
             supports: list[str], items: list[RareItem], masteries: dict[str, int] | None = None,
             uniques: dict[str, str] | None = None,
             jewels: dict[str, RareItem | str] | None = None) -> str:
    root = ET.Element("PathOfBuilding")
    ET.SubElement(root, "Build", {"level": str(spec["level"]), "characterLevelAutoMode": "false",
        "bandit": "None", "className": "Witch", "ascendClassName": spec["ascendancy"],
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
    skills = ET.SubElement(root, "Skills", activeSkillSet="1", defaultGemLevel="20", defaultGemQuality="0")
    skill_set = ET.SubElement(skills, "SkillSet", id="1", title="Generated skills")

    def group(names, slot, main=False):
        skill = ET.SubElement(skill_set, "Skill", {"slot": slot, "enabled": "true", "label": "",
            "mainActiveSkill": "1", "mainActiveSkillCalcs": "1", "includeInFullDPS": str(main).lower()})
        for name in names:
            gem = data.gem(name)
            level = spec.get("gemLevels", {}).get(name, min(gem.get("maxLevel", 20), 20))
            # Keep guard/utility strength requirements modest.
            if name == "Steelskin":
                level = min(level, 10)
            ET.SubElement(skill, "Gem", {"gemId": gem["gameId"], "variantId": gem["variantId"],
                "skillId": gem["skillId"], "nameSpec": gem["name"], "level": str(level), "quality": "0",
                "enabled": "true", "enableGlobal1": "true", "enableGlobal2": "false",
                "count": str(spec.get("minionCount", 1) if main and not gem["support"] else 1)})
    group([spec["skill"], *supports], "Body Armour", True)
    utility = spec.get("utility", {})
    # Single active gem per group keeps PoB's active-skill indices unambiguous.
    for name, slot in utility.items():
        if name in data.gems:
            group([name], slot)
    equipment = ET.SubElement(root, "Items", activeItemSet="1", useSecondWeaponSet="false")
    item_set = ET.SubElement(equipment, "ItemSet", id="1", title="Generated equipment", useSecondWeaponSet="false")
    unique_mainhand = (uniques or {}).get("Weapon 1")
    two_handed_mainhand = False
    if unique_mainhand:
        lines = [line.strip() for line in unique_mainhand.splitlines() if line.strip()]
        rarity_index = next((index for index, line in enumerate(lines)
                             if line.upper().startswith("RARITY:")), None)
        if rarity_index is not None and rarity_index + 2 < len(lines):
            base = data.bases.get(lines[rarity_index + 2], {})
            tags = base.get("tags", {})
            two_handed_mainhand = bool(tags.get("two_hand_weapon") or tags.get("twohanded"))
    if two_handed_mainhand and (uniques or {}).get("Weapon 2"):
        raise ValueError("A two-handed Weapon 1 unique cannot be equipped with a requested Weapon 2 item")
    active_items = [item for item in items
                    if not item.slot.startswith("Flask ") and
                    not (two_handed_mainhand and item.slot == "Weapon 2")]
    for index, item in enumerate(active_items, 1):
        count = (spec.get("mainLinks", 6) if item.slot == "Body Armour" else
                 min(spec.get("utilitySockets", 4), item.definition.get("socketLimit", 0),
                     4 if item.slot in {"Helmet", "Gloves", "Boots"} else 3))
        text = (uniques or {}).get(item.slot) or item.text(count)
        ET.SubElement(equipment, "Item", id=str(index)).text = text
        ET.SubElement(item_set, "Slot", name=item.slot, itemId=str(index))
    unique_flasks = sorted(((slot, text) for slot, text in (uniques or {}).items()
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
    return ET.tostring(root, encoding="unicode")
