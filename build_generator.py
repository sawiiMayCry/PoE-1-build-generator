"""Construct and validate complete Witch PoBs from current compatible patterns.

Curated recipes combine current tree, gem and equipment choices. Reference
exports are ingredients, never results.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import re
import secrets
import time
import xml.etree.ElementTree as ET
import zlib

from pob_engine import calculate_with_pob
from services import game_context, market_data, reference_xml

WINTER_REFS = ("h8klSvqllefw", "W4yCI3RRdniV")
SRS_REFS = ("cT7EOnsriDz5", "7_6IS6EVRZfT")
VORTEX_REFS = ("XIootCmOcoyV", "Q-Kayt3t1dYy")
PENANCE_REFS = ("l1ETl58DQShc", "SuiNbOyFLToF")
SUPPORTED_RECIPES = (
    {"ascendancy": "Elementalist", "skill": "Winter Orb", "description": "Cold spell"},
    {"ascendancy": "Elementalist", "skill": "Penance Brand", "description": "Fire brand"},
    {"ascendancy": "Necromancer", "skill": "Summon Raging Spirit", "description": "Fire minions"},
    {"ascendancy": "Occultist", "skill": "Vortex", "description": "Cold spell"},
)
REFERENCE_IDS = {
    ("Elementalist", "Winter Orb"): WINTER_REFS,
    ("Elementalist", "Penance Brand"): PENANCE_REFS,
    ("Necromancer", "Summon Raging Spirit"): SRS_REFS,
    ("Occultist", "Vortex"): VORTEX_REFS,
}
SKILL = "Winter Orb"
ASCENDANCY = "Elementalist"
SLOTS_FROM_DONOR = ("Flask 1", "Flask 2", "Flask 3")
REQUIRED_SLOTS = ("Weapon 1", "Helmet", "Body Armour", "Gloves", "Boots", "Belt", "Amulet", "Ring 1", "Ring 2")


def decode_pob(raw: str) -> str:
    raw = raw.strip()
    if len(raw) > 2_000_000:
        raise ValueError("PoB share code is too large")
    payload = base64.b64decode(raw.replace("-", "+").replace("_", "/") + "=" * (-len(raw) % 4), validate=True)
    inflater = zlib.decompressobj()
    xml = inflater.decompress(payload, 8_000_001)
    if len(xml) > 8_000_000 or not inflater.eof:
        raise ValueError("PoB export is incomplete or too large")
    return xml.decode("utf-8")


def encode_pob(xml: str) -> str:
    return base64.urlsafe_b64encode(zlib.compress(xml.encode("utf-8"), 9)).decode("ascii").rstrip("=")


def mechanics_fingerprint(xml: str) -> str:
    root = ET.fromstring(xml)
    build = root.find("Build")
    if build is not None:
        for key in ("name", "characterName", "accountName"):
            build.attrib.pop(key, None)
    for tag in ("Notes", "BuildNotes"):
        for node in root.findall(tag):
            root.remove(node)
    return hashlib.sha256(ET.tostring(root, encoding="utf-8")).hexdigest()


def _active_set(root, tag, child, active_attribute):
    container = root.find(tag)
    if container is None:
        raise ValueError(f"PoB has no {tag} section")
    sets = container.findall(child)
    if not sets:
        raise ValueError(f"PoB has no {child}")
    active = container.get(active_attribute, "1")
    if child == "Spec" and active.isdigit():
        index = int(active) - 1
        if 0 <= index < len(sets):
            return sets[index]
    return next((entry for entry in sets if entry.get("id") == active), sets[0])


def _main_group(root):
    skill_set = _active_set(root, "Skills", "SkillSet", "activeSkillSet")
    groups = skill_set.findall("Skill")
    index = int(root.find("Build").get("mainSocketGroup", "1")) - 1
    if index < 0 or index >= len(groups):
        raise ValueError("PoB main socket group is missing")
    return skill_set, groups[index], index


def _items_by_slot(root):
    items = root.find("Items")
    item_set = _active_set(root, "Items", "ItemSet", "activeItemSet")
    by_id = {item.get("id"): item for item in items.findall("Item")}
    return items, {slot.get("name"): (slot, by_id.get(slot.get("itemId"))) for slot in item_set.findall("Slot")}


def _item_parts(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    line = next((i for i, value in enumerate(lines) if value.upper().startswith("RARITY:")), None)
    if line is None or line + 1 >= len(lines):
        raise ValueError("Equipped item has no rarity or name")
    rarity = lines[line].split(":", 1)[1].strip().lower()
    name = lines[line + 1]
    base = lines[line + 2] if rarity in {"rare", "unique", "magic", "relic"} and line + 2 < len(lines) else name
    return rarity, name, base


def _gem_color(gem_data: str, gem) -> str | None:
    identifier = gem.get("gemId", "")
    marker = f'gameId = "{identifier}"'
    index = gem_data.find(marker)
    if index < 0:
        return None
    segment = gem_data[index:index + 800]
    requirements = [re.search(rf"req{attr}\s*=\s*(\d+)", segment) for attr in ("Str", "Dex", "Int")]
    if any(value is None for value in requirements):
        return None
    values = [int(value.group(1)) for value in requirements]
    return "RGB"[values.index(max(values))] if max(values) > 0 else None


def _socket_colors(item_text: str) -> list[str]:
    """Colours of the item's largest linked group of gem sockets.

    PoB writes linked sockets joined by "-" and separate groups split by
    spaces ("B-R-W-W-W W"). Abyssal (A) and delve (D) sockets cannot hold gems.
    """
    match = re.search(r"(?im)^Sockets:[ \t]*([RGBWAD]+(?:[ \t]*[- ][ \t]*[RGBWAD]+)*)[ \t]*$", item_text or "")
    if not match:
        return []
    groups = [[color for color in re.split(r"\s*-\s*", group) if color in {"R", "G", "B", "W"}]
              for group in match.group(1).split()]
    return max(groups, key=len, default=[])


def _colors_fit(gems, sockets, gem_data, tree_version: str) -> bool:
    if len(gems) > len(sockets) or len(gems) > 6 or not gems:
        return False
    major, minor = map(int, tree_version.split("_")[:2])
    # Since 3.29 colours grant bonus quality; they do not restrict placement.
    if (major, minor) >= (3, 29):
        return True
    colors = [_gem_color(gem_data, gem) for gem in gems]
    if any(color is None for color in colors):
        return False
    # Each gem needs a socket of its colour; white sockets cover any shortfall.
    shortfall = sum(max(0, colors.count(color) - sockets.count(color)) for color in "RGB")
    return shortfall <= sockets.count("W")


def _clean(root, label):
    build = root.find("Build")
    build.set("name", label)
    private = {"author", "account", "accountname", "charactername", "playername", "createdby", "updatedby"}
    for parent in root.iter():
        for child in list(parent):
            if child.tag.lower() in {"notes", "buildnotes", "private_notes"}:
                parent.remove(child)
        for attr in list(parent.attrib):
            if attr.lower() in private:
                del parent.attrib[attr]
    for item in root.findall("./Items/Item"):
        text = item.text or ""
        lines = [line for line in text.splitlines() if not re.match(r"\s*Note\s*:", line, re.I)]
        for index, line in enumerate(lines):
            if re.match(r"\s*Rarity:\s*Rare\s*$", line, re.I) and index + 1 < len(lines):
                lines[index + 1] = "Witchcraft crafted gear"
                break
        item.text = "\n".join(lines)
    for index, entry in enumerate(root.findall("./Tree/Spec"), 1):
        entry.set("title", f"Generated tree {index}")
    for index, entry in enumerate(root.findall("./Items/ItemSet"), 1):
        entry.set("title", f"Generated equipment {index}")
    for index, entry in enumerate(root.findall("./Skills/SkillSet"), 1):
        entry.set("title", f"Generated skills {index}")
        for group_index, group in enumerate(entry.findall("Skill"), 1):
            if "label" in group.attrib:
                group.set("label", f"Skill group {group_index}")
    for index, entry in enumerate(root.findall("./Config/ConfigSet"), 1):
        if "title" in entry.attrib:
            entry.set("title", f"Generated configuration {index}")


def _diversify_tree(root, official_nodes: dict, preference: str, skill: str) -> str:
    """Trade one connected leaf for a nearby legal node on the same branch."""
    spec = _active_set(root, "Tree", "Spec", "activeSpec")
    allocation = spec.get("nodes", "").split(",")
    selected = set(allocation)
    mastered = {match.group(1) for match in re.finditer(r"\{(\d+),", spec.get("masteryEffects", ""))}
    def neighbors(node_id):
        node = official_nodes.get(node_id, {})
        return set(node.get("out", [])) | set(node.get("in", []))
    if skill == "Winter Orb":
        targets = (("Annihilation", "Spell Critical Strike Chance"),
                   ("Light of Divinity", "Discipline and Training")) if preference == "damage" else (
                   ("Light of Divinity", "Discipline and Training"),
                   ("Annihilation", "Spell Critical Strike Chance"))
    elif skill == "Vortex":
        targets = (("Blast Radius", "Area of Effect"),)
    elif skill == "Penance Brand":
        targets = (("Light of Divinity", "Area of Effect"),)
    else:
        raise ValueError(f"No tree strategy for {skill}")
    for old_name, new_name in targets:
        for old in allocation:
            old_node = official_nodes.get(old, {})
            if old_node.get("name") != old_name or old in mastered:
                continue
            parent = neighbors(old) & selected
            if len(parent) != 1:
                continue
            for candidate in neighbors(next(iter(parent))) - selected:
                new_node = official_nodes.get(candidate, {})
                if new_node.get("name") == new_name and not new_node.get("ascendancyName"):
                    allocation[allocation.index(old)] = candidate
                    spec.set("nodes", ",".join(allocation))
                    return f"{old_name} → {new_name}"
    raise ValueError("The current tree has no legal alternative for this recipe")


def construct(core_xml: str, donor_xml: str, context: dict, preference: str,
              ascendancy: str = ASCENDANCY, skill: str = SKILL) -> tuple[str, dict]:
    root = ET.fromstring(core_xml)
    donor = ET.fromstring(donor_xml)
    for source in (root, donor):
        build = source.find("Build")
        spec = _active_set(source, "Tree", "Spec", "activeSpec")
        if build is None or build.get("className") != "Witch" or build.get("ascendClassName") != ascendancy:
            raise ValueError("Reference PoB is not the supported Witch ascendancy")
        if spec.get("treeVersion") != context["treeVersion"]:
            raise ValueError("Reference PoB tree is stale for the current game version")
    skill_set, old_main, index = _main_group(root)
    _, donor_main, _ = _main_group(donor)
    if not all(any(g.get("nameSpec") == skill for g in group.findall("Gem")) for group in (old_main, donor_main)):
        raise ValueError("Reference six-link does not match the requested skill")
    gem_data = (context["pobHome"] / "Data" / "Gems.lua").read_text(encoding="utf-8")
    items, core_slots = _items_by_slot(root)
    main_slot = old_main.get("slot", "Body Armour")
    equipped = core_slots.get(main_slot, (None, None))[1]
    if equipped is None:
        raise ValueError(f"Reference core has no equipped {main_slot} for the main link")
    sockets = _socket_colors(equipped.text or "")
    replacement = copy.deepcopy(donor_main)
    replacement.set("slot", main_slot)
    if skill == "Vortex":
        high_dex = next((gem for gem in replacement.findall("Gem") if gem.get("nameSpec") == "Hypothermia"), None)
        elemental_focus = next((gem for gem in old_main.findall("Gem") if gem.get("nameSpec") == "Elemental Focus"), None)
        if high_dex is None or elemental_focus is None:
            raise ValueError("Vortex references lack the curated attribute-safe main link")
        position = list(replacement).index(high_dex)
        replacement.remove(high_dex)
        replacement.insert(position, copy.deepcopy(elemental_focus))
    if not _colors_fit(replacement.findall("Gem"), sockets, gem_data, context["treeVersion"]):
        raise ValueError("Main gems do not fit the equipped socket colours")
    old_supports = {g.get("gemId") for g in old_main.findall("Gem") if g.get("nameSpec") != skill}
    new_supports = {g.get("gemId") for g in replacement.findall("Gem") if g.get("nameSpec") != skill}
    if len(old_supports.symmetric_difference(new_supports)) < 2:
        raise ValueError("References do not provide meaningfully different main links")
    skill_set.remove(old_main)
    skill_set.insert(index, replacement)
    _, donor_slots = _items_by_slot(donor)
    next_id = max((int(item.get("id", "0")) for item in items.findall("Item") if item.get("id", "").isdigit()), default=0)
    changes = []
    swap_slots = (*SLOTS_FROM_DONOR, "Belt") if skill == "Penance Brand" else SLOTS_FROM_DONOR
    for name in swap_slots:
        if name not in core_slots or name not in donor_slots or donor_slots[name][1] is None:
            raise ValueError(f"Reference lacks required {name} equipment")
        target_slot, old_item = core_slots[name]
        donor_item = donor_slots[name][1]
        if old_item is None or (old_item.text or "").strip() == (donor_item.text or "").strip():
            continue
        next_id += 1
        item_copy = copy.deepcopy(donor_item)
        item_copy.set("id", str(next_id))
        first_set = next((i for i, child in enumerate(items) if child.tag == "ItemSet"), len(items))
        items.insert(first_set, item_copy)
        target_slot.set("itemId", str(next_id))
        changes.append(name)
    if len(changes) < 3:
        raise ValueError("References did not produce a distinct equipment plan")
    tree_change = _diversify_tree(root, context["tree"]["nodes"], preference, skill)
    label = f"[GENERATED] Witchcraft {ascendancy} {skill} {secrets.token_hex(3)}"
    _clean(root, label)
    xml = ET.tostring(root, encoding="unicode")
    if mechanics_fingerprint(xml) in {mechanics_fingerprint(core_xml), mechanics_fingerprint(donor_xml)}:
        raise ValueError("Generated build matches a reference export")
    return xml, {"changedSlots": changes, "changedMainLinks": sorted(new_supports ^ old_supports),
                 "treeChange": tree_change,
                 "method": "Curated current-version tree, main links, and gear recombination"}


def construct_srs(core_xml: str, donor_xml: str, context: dict, preference: str) -> tuple[str, dict]:
    """Build a fire SRS Necromancer from the current low-budget progression."""
    root = ET.fromstring(core_xml)
    donor = ET.fromstring(donor_xml)
    if root.find("Build").get("ascendClassName") != "Necromancer" or donor.find("Build").get("ascendClassName") != "Necromancer":
        raise ValueError("SRS references must both be Necromancers")
    if any(_active_set(source, "Tree", "Spec", "activeSpec").get("treeVersion") != context["treeVersion"]
           for source in (root, donor)):
        raise ValueError("SRS reference is stale for the current tree")
    root.find("Tree").set("activeSpec", "8")
    root.find("Skills").set("activeSkillSet", "2")
    root.find("Items").set("activeItemSet", "3")
    root.find("Build").set("level", "90")
    root.find("Build").set("mainSocketGroup", "1")
    skill_set, main, _ = _main_group(root)
    if not any(gem.get("nameSpec") == "Summon Raging Spirit" for gem in main.findall("Gem")):
        raise ValueError("SRS progression has no main skill")
    high_set = next((entry for entry in root.findall("./Skills/SkillSet") if entry.get("id") == "1"), None)
    greater = next((gem for group in high_set.findall("Skill") for gem in group.findall("Gem")
                    if gem.get("nameSpec") == "Greater Multistrike"), None) if high_set is not None else None
    old = next((gem for gem in main.findall("Gem") if gem.get("nameSpec") == "Multistrike"), None)
    if greater is None or old is None:
        raise ValueError("SRS reference lacks the curated main-link alternative")
    position = list(main).index(old)
    main.remove(old)
    main.insert(position, copy.deepcopy(greater))
    items, core_slots = _items_by_slot(root)
    _, donor_slots = _items_by_slot(donor)
    changes = []
    next_id = max((int(item.get("id", "0")) for item in items.findall("Item") if item.get("id", "").isdigit()), default=0)
    for name in SLOTS_FROM_DONOR:
        target_slot, old_item = core_slots.get(name, (None, None))
        donor_item = donor_slots.get(name, (None, None))[1]
        if target_slot is None or old_item is None or donor_item is None:
            raise ValueError(f"SRS reference lacks {name}")
        if (old_item.text or "").strip() == (donor_item.text or "").strip():
            continue
        next_id += 1
        copied = copy.deepcopy(donor_item)
        copied.set("id", str(next_id))
        first_set = next((i for i, child in enumerate(items) if child.tag == "ItemSet"), len(items))
        items.insert(first_set, copied)
        target_slot.set("itemId", str(next_id))
        changes.append(name)
    if len(changes) < 3:
        raise ValueError("SRS references did not produce distinct equipment")
    spec = _active_set(root, "Tree", "Spec", "activeSpec")
    allocated = spec.get("nodes", "").split(",")
    official = context["tree"]["nodes"]
    target_name = "Armour and Life Regeneration" if preference == "defense" else "Physical and Minion Damage"
    target = next((node_id for node_id, node in official.items()
                   if node.get("name") == target_name
                   and node_id not in allocated
                   and set(node.get("out", []) + node.get("in", [])) & set(allocated)), None)
    if not target:
        raise ValueError(f"Current tree has no connected {target_name} node")
    allocated.append(target)
    spec.set("nodes", ",".join(allocated))
    tree_change = f"Allocated {target_name}"
    gem_data = (context["pobHome"] / "Data" / "Gems.lua").read_text(encoding="utf-8")
    body = core_slots.get("Body Armour", (None, None))[1]
    if body is None or not _colors_fit(main.findall("Gem"), _socket_colors(body.text or ""), gem_data, context["treeVersion"]):
        raise ValueError("SRS main link is incompatible with the equipped body armour")
    _clean(root, f"[GENERATED] Witchcraft Necromancer Fire SRS {secrets.token_hex(3)}")
    xml = ET.tostring(root, encoding="unicode")
    if mechanics_fingerprint(xml) in {mechanics_fingerprint(core_xml), mechanics_fingerprint(donor_xml)}:
        raise ValueError("Generated SRS matches a source export")
    return xml, {"changedSlots": changes, "changedMainLinks": ["Multistrike → Greater Multistrike"],
                 "treeChange": tree_change,
                 "method": "Curated current-version Fire SRS tree, main links, and gear recombination"}


def validate_structure(xml: str, context: dict, ascendancy: str = ASCENDANCY, skill: str = SKILL) -> tuple[list[dict], dict]:
    root = ET.fromstring(xml)
    build = root.find("Build")
    if build is None:
        raise ValueError("PoB has no Build element")
    checks = []
    def check(name, okay, reason):
        checks.append({"name": name, "passed": bool(okay), "reason": reason})
    level = int(build.get("level", "0"))
    check("Witch and ascendancy", build.get("className") == "Witch" and build.get("ascendClassName") == ascendancy,
          f"Expected a Witch {ascendancy}")
    check("Character level", 80 <= level <= 100, f"Level {level} must be between 80 and 100 for this endgame recipe")
    spec = _active_set(root, "Tree", "Spec", "activeSpec")
    nodes = [value for value in spec.get("nodes", "").split(",") if value]
    official = context["tree"]["nodes"]
    missing = [value for value in nodes if value not in official]
    item_ids = {item.get("id"): item for item in root.findall("./Items/Item")}
    cluster_socketed = any("Cluster Jewel" in (item_ids.get(socket.get("itemId")).text or "")
                           for socket in spec.findall("./Sockets/Socket") if item_ids.get(socket.get("itemId")) is not None)
    reference_cluster_nodes = context.get("referenceClusterNodes", set())
    invalid = [value for value in missing if not (value in reference_cluster_nodes and cluster_socketed)]
    check("Current passive tree", spec.get("treeVersion") == context["treeVersion"] and not invalid and len(nodes) == len(set(nodes)),
          f"Tree must use {context['treeVersion']} with official nodes or preserved reference cluster-jewel nodes; invalid: {invalid[:5]}")
    asc_nodes = [official[n] for n in nodes if n in official and official[n].get("ascendancyName")]
    alternate = context["tree"].get("alternate_ascendancies", [])
    secondary_raw = spec.get("secondaryAscendClassId", "0")
    secondary_id = int(secondary_raw) if secondary_raw.isdigit() else 0
    secondary_name = alternate[secondary_id - 1]["id"] if 1 <= secondary_id <= len(alternate) else None
    allowed_ascendancies = {ascendancy, secondary_name} if secondary_name else {ascendancy}
    wrong_asc = [node.get("ascendancyName") for node in asc_nodes if node.get("ascendancyName") not in allowed_ascendancies]
    paid_asc = sum(not node.get("isAscendancyStart") for node in asc_nodes)
    # Paid points are counted by PoB after import. Raw XML includes start,
    # granted and cluster nodes and is not a reliable paid-point total.
    check("Ascendancy classes", not wrong_asc, f"Unexpected ascendancies: {wrong_asc}")
    _, main, _ = _main_group(root)
    gems = main.findall("Gem")
    gem_data = (context["pobHome"] / "Data" / "Gems.lua").read_text(encoding="utf-8")
    bad_gems = [g.get("nameSpec") for g in gems if not g.get("gemId") or f'"{g.get("gemId")}"' not in gem_data]
    check("Main skill and gem IDs", len(gems) >= 5 and any(g.get("nameSpec") == skill for g in gems) and not bad_gems,
          f"Main link needs {skill} and at least five known gems; unknown: {bad_gems}")
    items, slots = _items_by_slot(root)
    missing_slots = [name for name in REQUIRED_SLOTS if name not in slots or slots[name][1] is None]
    check("Complete equipment", not missing_slots, f"Missing equipped slots: {missing_slots}")
    main_slot = main.get("slot", "Body Armour")
    equipped = slots.get(main_slot, (None, None))[1]
    sockets = _socket_colors(equipped.text or "") if equipped is not None else []
    check("Main link sockets and colours", _colors_fit(gems, sockets, gem_data, context["treeVersion"]),
          f"{main_slot} has {len(sockets)} linked sockets for {len(gems)} gems; current colour rules applied")
    base_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore")
                          for path in (context["pobHome"] / "Data" / "Bases").rglob("*.lua"))
    unique_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore")
                            for path in (context["pobHome"] / "Data" / "Uniques").rglob("*.lua"))
    foulborn_map = context["pobHome"] / "Data" / "ModFoulbornMap.lua"
    foulborn_text = foulborn_map.read_text(encoding="utf-8") if foulborn_map.is_file() else ""
    invalid_items = []
    gear = []
    for name, (_, item) in slots.items():
        if item is None or name.endswith("Swap") or name.startswith("Graft") or name.startswith("Belt Abyssal"):
            continue
        try:
            rarity, item_name, base = _item_parts(item.text)
        except ValueError:
            invalid_items.append(name)
            continue
        if f'itemBases["{base}"]' not in base_text and name in REQUIRED_SLOTS:
            invalid_items.append(f"{name} base {base}")
        if rarity == "unique":
            known_unique = bool(re.search(rf"(?m)^{re.escape(item_name)}$", unique_text))
            if item_name.startswith("Foulborn "):
                base_unique = item_name.removeprefix("Foulborn ")
                known_unique = (bool(re.search(rf"(?m)^{re.escape(base_unique)}$", unique_text))
                                and f'["{base_unique}"]' in foulborn_text)
            if not known_unique:
                invalid_items.append(f"{name} unique {item_name}")
        gear.append({"slot": name, "rarity": rarity, "name": item_name, "base": base})
    check("Current item definitions", not invalid_items, f"Unknown PoB item definitions: {invalid_items[:6]}")
    check("Configuration", root.find("./Config/ConfigSet") is not None, "PoB configuration set is required")
    return checks, {"level": level, "gear": gear, "gems": [g.get("nameSpec") for g in gems],
                    "treeNodes": len(nodes), "ascendancyPoints": paid_asc}


def validate_calculation(stats: dict) -> list[dict]:
    output = stats.get("stats", {})
    life = float(output.get("Life", 0)) + float(output.get("EnergyShield", 0))
    offense = offense_value(output)
    checks = [
        {"name": "PoB calculation", "passed": bool(stats.get("calculated") and output), "reason": "PoB must return calculated outputs"},
        {"name": "Endgame health pool", "passed": life >= 3000, "reason": f"Life plus energy shield: {life:,.0f}; minimum for this recipe: 3,000"},
        {"name": "Main skill offense", "passed": offense > 0, "reason": f"PoB calculated {offense:,.0f} DPS"},
    ]
    points = stats.get("passives", {})
    used, maximum = points.get("used"), points.get("maximum")
    asc, secondary = points.get("ascendancy"), points.get("secondaryAscendancy")
    legal_points = (all(isinstance(value, (int, float)) for value in (used, maximum, asc, secondary))
                    and used <= maximum and 0 <= asc <= 8 and 0 <= secondary <= 8)
    checks.append({"name": "Legal passive points", "passed": legal_points,
                   "reason": f"PoB counts {used} paid passives (limit {maximum}); "
                             f"{asc} primary and {secondary} secondary ascendancy points (limit 8 each)"})
    for element in ("Fire", "Cold", "Lightning"):
        value = output.get(element + "Resist")
        checks.append({"name": element + " resistance", "passed": value is not None and float(value) >= 75,
                       "reason": f"PoB reports {value}% (target: 75%)"})
    # PoB only emits Req<attr> when a requirement is above zero; attribute
    # requirement immunity and Omniscience (which moves them to ReqOmni) omit it.
    attributes = ("Str", "Dex", "Int") + (("Omni",) if output.get("ReqOmni") is not None else ())
    for attr in attributes:
        required, actual = output.get("Req" + attr, 0), output.get(attr)
        checks.append({"name": attr + " requirements", "passed": actual is not None and actual >= required,
                       "reason": f"{actual} available; {required} required"})
    return checks


def offense_value(output: dict) -> float:
    return max(float(output.get(key, 0)) for key in
               ("FullDPS", "FullDotDPS", "CombinedDPS", "TotalDPS", "TotalDotDPS"))


def quote(gear: list[dict], market: dict, cap: float) -> dict:
    subtotal = 0.0
    unknown = []
    priced = []
    for item in gear:
        if item["rarity"] == "unique" and market["prices"].get(item["name"]):
            value = max(market["prices"][item["name"]])
            subtotal += value
            priced.append({"slot": item["slot"], "name": item["name"], "chaos": round(value, 1), "kind": "estimated"})
        else:
            display = item["base"] if item["rarity"] == "rare" else item["name"]
            unknown.append({"slot": item["slot"], "name": display, "reason": "Rare/magic item needs a modifier-aware trade search" if item["rarity"] != "unique" else "Unique has no current-league quote"})
    return {"pricedSubtotalChaos": round(subtotal, 1), "pricedSubtotalDivine": round(subtotal / market["divineChaos"], 2),
            "unknown": unknown, "priced": priced, "complete": not unknown and not market["errors"],
            "budgetChaos": cap, "budgetStatus": "priced subtotal exceeds budget" if subtotal > cap else
            ("within budget" if not unknown and not market["errors"] else "unverified: unpriced slots remain"),
            "source": market["source"], "updated": market["updated"], "league": market["league"],
            "divineChaos": market["divineChaos"], "sourceErrors": market["errors"]}


def generate(request: dict, app_root, data_root, stage) -> dict:
    asc, skill = request.get("ascendancy"), request.get("skill")
    recipe_key = (asc, skill)
    if recipe_key not in REFERENCE_IDS:
        raise ValueError("Choose a supported Witch ascendancy and skill combination.")
    try:
        cap = float(request.get("budgetChaos"))
    except (TypeError, ValueError):
        raise ValueError("Enter a budget in chaos orbs")
    if not 1 <= cap <= 10_000_000:
        raise ValueError("Budget must be between 1 and 10,000,000 chaos")
    preference = str(request.get("preference", "balanced"))
    if preference not in {"balanced", "damage", "defense"}:
        raise ValueError("Choose balanced, damage, or defense")
    stage("Checking current game and market data")
    context = game_context()
    market = market_data(context["league"])
    stage("Loading current-version build patterns")
    reference_ids = REFERENCE_IDS[recipe_key]
    refs = [reference_xml(ref, decode_pob) for ref in reference_ids]
    candidates = []
    failures = []
    for index in ((0,) if recipe_key == ("Necromancer", "Summon Raging Spirit") else (0, 1)):
        try:
            stage("Constructing a new tree, gem and gear plan")
            xml, recipe = (construct_srs(refs[index], refs[1-index], context, preference)
                           if recipe_key == ("Necromancer", "Summon Raging Spirit") else
                           construct(refs[index], refs[1-index], context, preference, asc, skill))
            source_spec = _active_set(ET.fromstring(refs[index]), "Tree", "Spec", "activeSpec")
            source_clusters = {node for node in source_spec.get("nodes", "").split(",")
                               if node not in context["tree"]["nodes"] and node.isdigit() and int(node) >= 65536}
            check_context = {**context, "referenceClusterNodes": source_clusters}
            checks, details = validate_structure(xml, check_context, asc, skill)
            if not all(c["passed"] for c in checks):
                raise ValueError("; ".join(c["name"] + ": " + c["reason"] for c in checks if not c["passed"]))
            stage("Calculating the candidate with Path of Building")
            calculation = calculate_with_pob(xml, app_root, data_root)
            checks += validate_calculation(calculation)
            if not all(c["passed"] for c in checks):
                raise ValueError("; ".join(c["name"] + ": " + c["reason"] for c in checks if not c["passed"]))
            stage(f"Pricing the candidate against {context['league']}")
            price = quote(details["gear"], market, cap)
            if price["pricedSubtotalChaos"] > cap:
                raise ValueError("Priced subtotal exceeds the requested budget")
            candidates.append({"xml": xml, "recipe": recipe, "checks": checks,
                               "details": details, "calculation": calculation, "price": price})
        except Exception as exc:
            failures.append(f"Variant {index + 1}: {exc}")
    if not candidates:
        raise ValueError("No valid build satisfies these constraints. " + " ".join(failures)[:1200])
    def score(candidate):
        stats = candidate["calculation"]["stats"]
        health = float(stats.get("Life", 0)) + float(stats.get("EnergyShield", 0))
        dps = offense_value(stats)
        if preference == "damage":
            return dps
        if preference == "defense":
            return health
        return (dps ** 0.35) * (health ** 0.65)
    chosen = max(candidates, key=score)
    build_id = "g" + secrets.token_hex(10)
    xml = chosen["xml"]
    return {"id": build_id, "name": ET.fromstring(xml).find("Build").get("name"),
            "class": "Witch", "ascendancy": asc, "mainSkill": skill,
            "level": chosen["details"]["level"], "gems": chosen["details"]["gems"],
            "treeNodes": chosen["details"]["treeNodes"], "ascendancyPoints": chosen["details"]["ascendancyPoints"],
            "gear": chosen["details"]["gear"], "validation": chosen["checks"],
            "stats": chosen["calculation"]["stats"], "pobVersion": chosen["calculation"].get("version"),
            "quote": chosen["price"], "recipe": chosen["recipe"], "league": context["league"],
            "treeVersion": context["treeVersion"], "officialTreeRelease": context["officialRelease"],
            "createdAt": int(time.time()), "shareStatus": "pending", "shareUrl": None,
            "_xml": xml, "_fingerprint": mechanics_fingerprint(xml)}
