"""Generated, level-legal campaign and mapping loadouts for a finished design."""
from __future__ import annotations

import copy
import math
from itertools import combinations
import re
import xml.etree.ElementTree as ET

from build_assembly import SocketConflict, assemble, disable_conditional_item_skill_groups
from build_generator import _item_parts, _main_group, offense_value
from generation_data import base_required_level, rare_templates, solve_suffixes
from loadout_summary import active_spec, item_level_requirement, summarize_loadout
from mechanics import retarget
from passive_search import candidate_minion_count, graph, heuristic, paths_from
from skill_packages import (apply_stage_caps, build_facts, capacity_from_equipment, fill_packages,
                            groups_from_xml, make_group, pack_groups, package_group, trigger_level_legal)
from unique_pricing import price_unique_equipment
import unique_policy
from unique_policy import worth_price


def _gem_level(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return -1


def mapping_unique_packages(options: list[tuple], spec: dict, data, level: int,
                            limit: int = 24) -> list[list[tuple]]:
    """Return level-legal, budget-valid single and two-item Mapping targets."""
    eligible = []
    for option in options:
        name, slot, text, price = option[:4]
        _, _, base = _item_parts(text)
        definition = data.bases.get(base, {})
        if not definition or base_required_level(definition) > level:
            continue
        eligible.append(option[:4])
    gains = spec.get("uniqueScreenGain") or {}
    terms = ("minion", "reservation", "spell", "life", "resistance", spec.get("damageType", ""))
    def rank(option):
        low = option[2].casefold()
        return (float(gains.get(option[0], 0.0)),
                sum(term.casefold() in low for term in terms if term)), option[0]
    eligible.sort(key=lambda option: (tuple(-value for value in rank(option)[0]), option[0], option[1]))
    required = set(spec.get("requestedUniques", ()))
    cap = unique_policy.mapping_cap(spec)
    stated = unique_policy.is_stated(spec)
    packages = []
    seen = set()
    def keep(package):
        names = {entry[0] for entry in package}
        if required and not required <= names:
            return
        if cap is not None:
            # A level-75 character owns a proportional share of the budget: the cheaper subset of what
            # the final build equips.  Requested items always stay.
            prices = [entry[3] for entry in package if entry[0] not in required]
            if stated and (any(price is None for price in prices) or sum(prices) > cap + 1e-9):
                return
            if not stated and (unique_policy.package_total(spec, prices) > cap + 1e-9 or
                               sum(price is None for price in prices) > unique_policy.MAX_UNPRICED_UNIQUES):
                return
        if len({entry[1] for entry in package}) != len(package):
            return
        key = tuple(sorted((entry[0], entry[1]) for entry in package))
        if key not in seen:
            seen.add(key)
            packages.append(list(package))
    def conflict(package):
        for member in package:
            if member[1] != "Weapon 1":
                continue
            base = data.bases.get(_item_parts(member[2])[2], {})
            if base.get("tags", {}).get("two_hand_weapon") and any(other[1] == "Weapon 2" for other in package):
                return True
        return False
    single_limit = max(1, limit // 2)
    single_options = [option for option in eligible if option[0] in required]
    single_options.extend(option for option in eligible if option[0] not in required)
    for option in single_options[:single_limit]:
        keep([option])
    candidates = []
    for size in (2, 3):
        for package in combinations(eligible[:12], size):
            names = {entry[0] for entry in package}
            if required and not required <= names:
                continue
            if conflict(package):
                continue
            relevance = tuple(sum(rank(entry)[0][index] for entry in package) for index in (0, 1))
            candidates.append((tuple(-value for value in relevance), tuple(sorted(names)), package))
    for _, _, package in sorted(candidates):
        keep(package)
        if len(packages) >= limit:
            break
    return packages[:limit]


# Checkpoints are after each act's passive quests, with kill-all bandits.
# The final total agrees with PoB's 23 quest points + one bandit point.
ACTS = [(12, 2), (22, 4), (32, 7), (40, 8), (45, 10),
        (50, 13), (55, 16), (60, 19), (65, 21), (68, 24)]


def milestones(end_level: int) -> list[dict]:
    result = [{"title": "Act 1 - Start (level 1)", "level": 1, "act": 1, "questPoints": 0,
               "ascendancyPoints": 0, "links": 1, "resistancePenalty": 0, "resistanceTarget": 0}]
    for act, (level, points) in enumerate(ACTS, 1):
        result.append({"title": f"Act {act} - Level {level}", "level": level, "act": act,
                       "questPoints": points, "ascendancyPoints": 0 if act < 4 else 2 if act < 7 else 4 if act < 10 else 6,
                       "links": 3 if act <= 2 else 4,
                       "resistancePenalty": 0 if act < 5 else -30 if act < 10 else -60,
                       "resistanceTarget": 45 if act == 1 else 60 if act == 2 else 75})
    result.extend([
        {"title": "Mapping - Level 75", "level": 75, "act": 11, "questPoints": 24,
         "ascendancyPoints": 6, "links": 6, "resistancePenalty": -60, "resistanceTarget": 75},
        {"title": f"Endgame - Level {end_level}", "level": end_level, "act": 12, "questPoints": 24,
         "ascendancyPoints": 8, "links": 6, "resistancePenalty": -60, "resistanceTarget": 75}])
    return result


def gem_level(gem: dict, character_level: int, cap: int = 20) -> int:
    if not gem.get("levels"):
        raise ValueError("Installed gem progression data is missing; refresh PoB metadata")
    return max((entry["level"] for entry in gem["levels"] if entry["requiredLevel"] <= character_level
                and entry["level"] <= cap), default=0)


def connected_order(official: dict, allocation: set[str], start: str, spec: dict,
                    masteries: dict[str, int]) -> list[str]:
    """Order a finished tree without introducing respecs or disconnected nodes."""
    adjacency = graph(official, lambda node: True)
    result, selected = [start], {start}
    while selected != allocation:
        notable_groups = {official[key].get("group") for key in selected if official[key].get("isNotable")}
        candidates = [key for key in allocation - selected if
                      (official[key].get("isMastery") and key in masteries and
                       official[key].get("group") in notable_groups) or
                      (not official[key].get("isMastery") and adjacency[key] & selected)]
        if not candidates:
            raise ValueError("Cannot create connected leveling trees from the endgame allocation")
        key = max(candidates, key=lambda key: (heuristic(official[key], spec), -int(key)))
        selected.add(key)
        result.append(key)
    return result


def stage_skill(spec: dict, data, phase: dict) -> str:
    if phase["level"] == 1:
        return "Fireball"
    if phase["act"] == 12:
        return spec["skill"]
    requested = data.gem(spec["skill"])
    normal = requested.get("baseName", spec["skill"]).removeprefix("Vaal ")
    if normal not in data.gems:
        normal = spec["skill"].removeprefix("Vaal ").split(" of ")[0]
    # Do not require Vaal/transfigured drops in the campaign. Siosa makes normal
    # off-class gems available after the Act 3 Library quest.
    early_witch = {"Fireball", "Freezing Pulse", "Spark", "Blight", "Raise Zombie",
                   "Summon Raging Spirit", "Summon Skeletons"}
    if normal in data.gems and (phase["act"] >= 3 or normal in early_witch) and gem_level(data.gem(normal), phase["level"]):
        if spec["archetype"] != "ignite" or normal == "Fireball" or phase["ascendancyPoints"] >= 2:
            return normal
    fallback = ("Summon Raging Spirit" if spec["archetype"] == "minion" and phase["level"] >= 4 else
                {"cold": "Freezing Pulse", "lightning": "Spark", "chaos": "Blight"}.get(spec["damageType"], "Fireball"))
    if fallback not in data.gems or not gem_level(data.gem(fallback), phase["level"]):
        raise ValueError("No installed leveling skill is available for this stage")
    return fallback


def ascendancy_order(official: dict, allocation: set[str], start: str, spec: dict) -> list[str]:
    # Allocate complete notable paths for each Lab, rather than spending the
    # first two points on small nodes in two different branches.
    adjacency = graph(official, lambda node: bool(node.get("ascendancyName")))
    adjacency = {key: neighbors & allocation for key, neighbors in adjacency.items() if key in allocation}
    result, selected = [start], {start}
    while selected != allocation:
        paths = paths_from(selected, adjacency)
        choices = [(key, path) for key, path in paths.items() if path and official[key].get("isNotable")]
        if not choices:
            raise ValueError("Ascendancy progression contains an incomplete notable path")
        key, path = max(choices, key=lambda row: (heuristic(official[row[0]], spec) / len(row[1]), -len(row[1]), -int(row[0])))
        result.extend(path)
        selected.update(path)
    return result


def add_flasks(root, data, level: int):
    equipment = root.find("Items")
    active = equipment.get("activeItemSet")
    item_set = next(entry for entry in equipment.findall("ItemSet") if entry.get("id") == active)
    next_id = max((int(item.get("id")) for item in equipment.findall("Item")), default=0)
    items = {item.get("id"): item for item in equipment.findall("Item")}
    for index, kind in enumerate(("Life", "Mana", "Quicksilver", "Granite", "Quartz"), 1):
        slot_name = f"Flask {index}"
        slot = next((entry for entry in item_set.findall("Slot") if entry.get("name") == slot_name), None)
        equipped = items.get(slot.get("itemId")) if slot is not None else None
        if equipped is not None and _item_parts(equipped.text)[2] in data.bases and data.bases[_item_parts(equipped.text)[2]]["type"] == "Flask":
            continue
        choices = [(name, base) for name, base in data.bases.items() if base["type"] == "Flask"
                   and (name.endswith(" Life Flask") if kind == "Life" else
                        name.endswith(" Mana Flask") if kind == "Mana" else name == kind + " Flask")
                   and base_required_level(base) <= level]
        if not choices:
            choices = [(name, base) for name, base in data.bases.items() if base["type"] == "Flask"
                       and name.endswith(" Life Flask") and base_required_level(base) <= level]
        if not choices:
            raise ValueError("Installed flask definitions are missing")
        name, base = max(choices, key=lambda row: (base_required_level(row[1]), row[0]))
        next_id += 1
        flask = ET.SubElement(equipment, "Item", id=str(next_id))
        flask.text = f"Rarity: NORMAL\n{name}\nQuality: 0"
        items[str(next_id)] = flask
        if slot is None:
            ET.SubElement(item_set, "Slot", name=slot_name, itemId=str(next_id), active="false")
        else:
            slot.set("itemId", str(next_id))


def flasks_complete(xml: str, data, level: int) -> bool:
    root = ET.fromstring(xml)
    equipment = root.find("Items")
    if equipment is None:
        return False
    active = equipment.get("activeItemSet")
    item_set = next((entry for entry in equipment.findall("ItemSet") if entry.get("id") == active), None)
    if item_set is None:
        return False
    items = {item.get("id"): item for item in equipment.findall("Item")}
    slots = {slot.get("name"): slot for slot in item_set.findall("Slot")}
    for index in range(1, 6):
        item = items.get(slots.get(f"Flask {index}").get("itemId")) if slots.get(f"Flask {index}") is not None else None
        if item is None:
            return False
        base = _item_parts(item.text)[2]
        if base not in data.bases or data.bases[base]["type"] != "Flask" or base_required_level(data.bases[base]) > level:
            return False
    return True


def combine_loadouts(documents: list[str], phases: list[dict], notes: str) -> str:
    """Merge independently validated stages, remapping every equipment ID."""
    result = ET.fromstring(documents[-1])
    for section in ("Tree", "Skills", "Items", "Config", "Notes"):
        for child in result.findall(section):
            result.remove(child)
    count = str(len(phases))
    tree = ET.SubElement(result, "Tree", activeSpec=count)
    skills = ET.SubElement(result, "Skills", activeSkillSet=count, defaultGemLevel="characterLevel", defaultGemQuality="0")
    items = ET.SubElement(result, "Items", activeItemSet=count, useSecondWeaponSet="false")
    config = ET.SubElement(result, "Config", activeConfigSet=count)
    next_item_id = 0
    for index, (xml, phase) in enumerate(zip(documents, phases), 1):
        source = ET.fromstring(xml)
        spec = copy.deepcopy(source.find("./Tree/Spec"))
        spec.set("title", phase["title"])
        tree.append(spec)
        source_item_set = source.find("./Items/ItemSet")
        item_set_mapping = {source_item_set.get("id"): str(index)}
        for section, tag, dest in (("Skills", "SkillSet", skills), ("Config", "ConfigSet", config)):
            entry = copy.deepcopy(source.find(f"./{section}/{tag}"))
            entry.set("id", str(index))
            entry.set("title", phase["title"])
            if section == "Skills":
                # Animate Weapon/Guardian select their equipment by ItemSet ID.
                # PoB serializes these references even when the default set was
                # selected, so leaving them at 1 uses the starting stage's gear.
                for gem in entry.findall("./Skill/Gem"):
                    for attr in ("skillMinionItemSet", "skillMinionItemSetCalcs"):
                        if gem.get(attr) is not None:
                            gem.set(attr, item_set_mapping[gem.get(attr)])
            dest.append(entry)
        mapping = {}
        for item in source.findall("./Items/Item"):
            next_item_id += 1
            mapping[item.get("id")] = str(next_item_id)
            entry = copy.deepcopy(item)
            entry.set("id", str(next_item_id))
            items.append(entry)
        entry = copy.deepcopy(source_item_set)
        entry.set("id", str(index))
        entry.set("title", phase["title"])
        for slot in entry.findall("Slot"):
            if slot.get("itemId") not in {None, "0"}:
                slot.set("itemId", mapping[slot.get("itemId")])
        items.append(entry)
        for socket in spec.findall("./Sockets/Socket"):
            if socket.get("itemId") not in {None, "0"}:
                socket.set("itemId", mapping[socket.get("itemId")])
    ET.SubElement(result, "Notes").text = notes
    return ET.tostring(result, encoding="unicode")


def select_stage(xml: str, index: int, level: int) -> str:
    root = ET.fromstring(xml)
    root.find("Build").set("level", str(level))
    for section, attr in (("Tree", "activeSpec"), ("Skills", "activeSkillSet"),
                          ("Items", "activeItemSet"), ("Config", "activeConfigSet")):
        root.find(section).set(attr, str(index))
    return ET.tostring(root, encoding="unicode")


def stage_checks(xml: str, phase: dict, calc: dict, data, context) -> list[dict]:
    root = ET.fromstring(xml)
    output = calc["stats"]
    tree = root.find("Tree")
    specs = tree.findall("Spec") if tree is not None else []
    active_spec = tree.get("activeSpec", "1") if tree is not None else "1"
    spec = specs[int(active_spec) - 1] if active_spec.isdigit() and 0 < int(active_spec) <= len(specs) else (specs[0] if specs else None)
    nodes = {node for node in spec.get("nodes", "").split(",") if node} if spec is not None else set()
    budget = phase["level"] - 1 + phase["questPoints"] + max(0, output.get("ExtraPoints", 1) - 1)
    skill_container = root.find("Skills")
    active_skill_set = skill_container.get("activeSkillSet") if skill_container is not None else None
    active_skills = next((entry for entry in root.findall("./Skills/SkillSet")
                          if entry.get("id") == active_skill_set), None)
    valid_gems = all(any(entry["level"] == _gem_level(gem.get("level")) and entry["requiredLevel"] <= phase["level"]
                         for entry in data.gem(gem.get("nameSpec"))["levels"])
                     for gem in (active_skills.findall("./Skill/Gem") if active_skills is not None else [])
                     if gem.get("gemId"))
    ids = {item.get("id"): item for item in root.findall("./Items/Item")}
    item_container = root.find("Items")
    active_item_set = item_container.get("activeItemSet") if item_container is not None else None
    equipment = next((entry for entry in root.findall("./Items/ItemSet")
                      if entry.get("id") == active_item_set), None)
    valid_items = True
    for slot in equipment.findall("Slot"):
        item = ids.get(slot.get("itemId"))
        if item is None:
            if slot.get("itemId") not in {None, "0"}:
                valid_items = False
            continue
        _, _, base = _item_parts(item.text)
        valid_items = (valid_items and base in data.bases and base_required_level(data.bases[base]) <= phase["level"]
                       and item_level_requirement(item.text) <= phase["level"])
    complete_flasks = flasks_complete(xml, data, phase["level"])
    _, main, _ = _main_group(root)
    links = sum(1 for gem in main.findall("Gem") if gem.get("gemId"))
    loadout = summarize_loadout(root, data.gems)
    # Mapping and Endgame must carry the complete six-link; leveling stages may be shorter.
    exact = phase.get("act", 0) >= 11
    link_ok = links == phase["links"] if exact else links <= phase["links"]
    placed = not loadout["unplacedGroups"]
    socket_ok = link_ok and placed
    spare = {slot: entry["socketsSpare"] for slot, entry in loadout["bySlot"].items() if entry["socketTotal"]}
    connected = True
    official = context["tree"]["nodes"]
    for ascendancy in (False, True):
        subset = {key for key in nodes if bool(official[key].get("ascendancyName")) == ascendancy
                  and not official[key].get("isMastery")}
        if not subset:
            continue
        start = next((key for key in subset if official[key].get("isAscendancyStart")), None) if ascendancy else next(
            (key for key in subset if official[key].get("classStartIndex") == 3), None)
        adjacency = graph(official, lambda node: True)
        reached, frontier = {start}, [start]
        while frontier:
            for neighbor in adjacency.get(frontier.pop(), set()) & subset - reached:
                reached.add(neighbor)
                frontier.append(neighbor)
        connected = connected and subset <= reached
    facts = [
        ("PoB calculation", calc.get("calculated") and offense_value(output) > 0),
        ("Stage passive budget", calc["passives"]["used"] <= budget and calc["passives"]["ascendancy"] <= phase["ascendancyPoints"]),
        ("Connected stage tree", connected),
        ("Stage gem levels", valid_gems),
        ("Stage equipment levels", valid_items),
        ("Five equipped flasks", complete_flasks, "Each active stage must resolve five level-legal flask items"),
        ("Stage sockets", socket_ok,
         f"{phase['title']}; main link {links}/{phase['links']}"
         f"{' (exactly required)' if exact else ''}; {loadout['socketedGemCount']} socketed gems in "
         f"{loadout['socketedGroupCount']} groups; unplaced groups {loadout['unplacedGroups']}; "
         f"spare sockets {spare}"),
        ("Stage attributes", all(output.get(attr, 0) >= output.get("Req" + attr, 0) for attr in ("Str", "Dex", "Int"))),
        ("Stage resistances", all(output.get(element + "Resist", -60) >= phase["resistanceTarget"] for element in ("Fire", "Cold", "Lightning"))),
        ("Stage mana", output.get("ManaUnreserved", 0) >= output.get("ManaCost", 0)),
        ("Stage health pool", output.get("Life", 0) + output.get("EnergyShield", 0) >=
         (3000 if phase["act"] == 12 else 30 * phase["level"] if phase["act"] == 11 else
          20 * phase["level"])),
    ]
    return [{"name": fact[0], "passed": bool(fact[1]),
             "reason": fact[2] if len(fact) > 2 else phase["title"]} for fact in facts]


def _waive_mana(checks: list[dict], warning: str | None) -> list[dict]:
    """Turn the Stage mana failure of a degraded Mapping stage into a visible warning."""
    if not warning:
        return checks
    return [{**check, "passed": True, "warning": True, "reason": warning} if check["name"] == "Stage mana" else check
            for check in checks]


def build_notes(spec: dict, stages: list[dict]) -> str:
    lines = [spec["skill"] + " " + spec["ascendancy"] + " - campaign to endgame",
             "HOW TO USE", "Choose the same named stage for Tree, Skills, Items and Configuration, or use PoB's Loadouts dropdown.",
             "Act sets are end-of-act checkpoints. Levels are targets: gain levels if you are below the checkpoint.",
             "PoB uses one global character level; set it to the level in the stage title when comparing campaign stats.",
             "Gems are leveled only as far as the character can equip them. Level them naturally; the listed levels are ceilings, not instant upgrades.",
             "BANDITS: kill all three. Complete passive-point side quests; use /passives to check missing rewards.",
             "LABYRINTH: Normal by Act 4 (level 33+), Cruel by Act 7 (55+), Merciless before Act 10 Kitava (68+), Eternal before Endgame (75+ and trial offering).",
             "Keep the passive path shown: successive trees add nodes without requiring refunds.",
             "GEM SOURCES: use Witch quest rewards and town vendors first. Complete A Fixture of Fate in the Act 3 Library for Siosa's normal gems; complete Fallen from Grace in Act 6 for Lilly's normal gems.",
             "Vaal and transfigured gems are optional endgame acquisitions from drops/trade/Labyrinth; campaign stages use their normal version or a named leveling skill.",
             "GEAR: rare items are modifier targets, not guaranteed drops or priced shopping lists. Prioritize the main linked sockets, movement speed, life, resistances, then required attributes.",
             "Flasks are recovery/utility bases to obtain and upgrade. They are disabled in calculations, so no permanent flask uptime is assumed.",
             "Upgrade flask suffixes for bleed/corrupted blood, freeze and curse protection as available. Avoid duplicate suffixes.",
             "After Act 5 Kitava replace lost resistances (-30%); after Act 10 Kitava replace them again (-60% total).",
             "The act checkpoints show targets after those penalties, not pre-Kitava values.",
             "RESOURCE PLAN: Main-skill sustain uses at most 85% of calculated recovery, leaving a reserve for movement, curses and other utility casts.",
             "CURSE: The selected curse matches the build's damage mechanism. Cast it when needed; PoB damage does not assume continuous curse uptime.", ""]
    for entry in spec.get("disabledConditionalItemSkills", []):
        lines.append(f"CONDITIONAL ITEM SKILL: {entry['skill']} from {entry['item']} is disabled in calculations because trigger timing, corpse availability and uptime are not modeled.")
    previous = None
    for phase in stages:
        lines += [phase["title"].upper(), f"Main link: {' -> '.join(phase['gems'])}",
                  f"Passive budget: {phase['passiveBudget']} paid points; allocated {phase['passives']}; ascendancy {phase['ascendancyPoints']} points."]
        if phase["mainSkill"] != previous:
            lines.append(f"Use {phase['mainSkill']} as your main skill for this stage. " +
                         (f"Replace {previous} when you have the new gem and linked sockets." if previous else
                          "Take Fireball from the Witch's starting quest reward; kill enemies for corpses before using Raise Zombie."))
        previous = phase["mainSkill"]
        lines.extend(phase["instructions"])
        lines.append("Gems: " + "; ".join(f"{name} level {level}" for name, level in phase["gemLevels"].items()))
        lines.append("Equipment: " + "; ".join(
            (f"{item['slot']}: {item['name']} ({item['base']})" if item.get("isUnique") else
             f"{item['slot']}: {item['base']}") for item in phase["gear"]))
        for group in phase.get("skillGroups", []):
            lines.append(f"Skill group {group['index']} ({group['slot']}): " + " + ".join(
                f"{gem['name']} {gem['level']}" for gem in group["gems"]))
        if phase.get("jewels"):
            lines.append("Jewels: " + "; ".join(
                f"node {jewel['node']}: {jewel['name']} ({jewel['base']})" for jewel in phase["jewels"]))
        if phase.get("act") == 11:
            if phase.get("uniquePackageCostChaos") is None:
                lines.append("Mapping package price is unknown or no priced unique package was selected; rare equipment remains unpriced.")
            elif phase.get("uniquePackageCostChaos", 0):
                lines.append(f"Mapping unique package has a {phase['uniquePackageCostChaos']:g} chaos priced subtotal; this is not a complete gear cost because rare slots remain unpriced.")
        lines.append("")
    lines += ["MAPPING AND ENDGAME", "Mapping has an independent level-legal equipment target. The generator compares bounded unique packages against all-rare gear in PoB and keeps rares when no tested package scores better within budget.",
              "Endgame is a separate requested-level design and equipment target. The validation checks establish minimum viability, not boss-kill capability.",
              "Rare items and gems remain unpriced. Improve sustained mana recovery, chaos resistance and ailment protection before harder maps."]
    mapping = spec.get("mappingSupportPlan", {})
    if mapping.get("integratedCoverageSupports"):
        lines.append("The selected link includes clear coverage support: " +
                     ", ".join(mapping["integratedCoverageSupports"]) + ".")
    for option in mapping.get("mappingAlternatives", [])[:2]:
        lines.append(f"CLEAR OPTION: {option['support']} provides {option['role']} at support-socket sweep {option['screeningSocket']}; it retained about {option['singleTargetRetention']:.0%} of the compared single-target support ({option['comparedWith']}). This is a coverage alternative, not a simulated pack-DPS claim; recalculate after swapping it into the finished link.")
    return "\n".join(lines)


EARLY_UTILITY = {"Flame Dash", "Steelskin"}
RESERVING_ROLES = {"aura", "defense", "herald"}


def stage_utility_cap(level: int) -> int:
    return 1 if level == 1 else 3 if level < 25 else 4


def _ci_nodes(official: dict, nodes: set[str]) -> set[str]:
    return {key for key in nodes if official.get(key, {}).get("name") == "Chaos Inoculation"}


def _fill_stage_supports(worker, render, groups, data, level, need, compatible, spec, cap):
    """Complete a main link from level-legal compatible supports (best measured first)."""
    main = groups[0]
    chosen = {gem["name"] for gem in main["gems"]}
    for _ in range(need):
        names = [name for name, gem in data.gems.items()
                 if gem.get("support") and gem["id"] in compatible and name not in chosen
                 and not name.startswith("Awakened ") and not gem.get("tags", {}).get("exceptional")
                 and gem.get("maxLevel", 20) >= 20 and gem_level(gem, level)
                 and name not in {"Sacrifice", "Vaal Sacrifice", "Cast on Death"}]
        names = sorted(names)[:40]
        if not names:
            break
        xml = render(groups)
        base = offense_value(worker.request("calculate", xml=xml)["stats"])
        scored = []
        for offset in range(0, len(names), 20):
            batch = names[offset:offset + 20]
            scored.extend(worker.request("supportScores", xml=xml,
                                         candidates=[data.gem(name)["id"] for name in batch])["candidates"])
        if not scored:
            break
        best = max(scored, key=lambda row: (offense_value(row["stats"]), row["id"]))
        if offense_value(best["stats"]) <= base * 1.001:
            break
        name = data.by_id[best["id"]]["name"]
        chosen.add(name)
        main["gems"].append({"instance": f"main:{len(main['gems']) + 1}", "name": name, "kind": "support",
                             "level": gem_level(data.gem(name), level, cap), "quality": 0,
                             "enabled": True, "count": 1})
    return groups


def _stage_main_group(spec, data, phase, main_skill, pool, compatible, cap):
    early = ["Minion Damage", "Added Lightning Damage", "Added Cold Damage", "Arcane Surge", "Combustion",
             "Lesser Multiple Projectiles"]
    names = [name for name in dict.fromkeys([*pool, *early]) if name in data.gems
             and data.gem(name)["id"] in compatible and gem_level(data.gem(name), phase["level"])
             and (phase["act"] >= 3 or name in early)][:phase["links"] - 1]
    gems = [(main_skill, "active"), *[(name, "support") for name in names]]
    return make_group("main", "main", gems, slot="Body Armour", main_active=main_skill,
                      include_in_full_dps=True,
                      level_for=lambda name: gem_level(data.gem(name), phase["level"], cap),
                      justification="Stage main link")


def _stage_utility_groups(spec, data, phase, final_groups, cap, capacity, groups, notes):
    """Add the endgame utility packages that are level-legal and fit this stage's sockets."""
    level = phase["level"]
    for source in final_groups:
        if source["role"] == "main":
            continue
        gems = []
        for gem in source["gems"]:
            name = gem["name"]
            if name not in data.gems:
                continue
            if phase["act"] < 3 and (gem["kind"] == "support" or name not in EARLY_UTILITY):
                continue
            if phase["act"] < 11 and data.gem(name).get("tags", {}).get("exceptional"):
                continue   # exceptional gems are endgame acquisitions, not campaign gems
            usable = gem_level(data.gem(name), level, min(cap, 10) if name == "Steelskin" else cap)
            if usable:
                gems.append({**gem, "level": usable, "instance": gem["instance"]})
        label = ", ".join(gem["name"] for gem in source["gems"])
        if not any(gem["kind"] == "active" for gem in gems):
            if phase["act"] >= 3:
                notes.append(f"Add the {source['role']} package ({label}) once its gems unlock.")
            continue
        group = {**source, "gems": gems, "slot": None,
                 "mainActive": next(gem["name"] for gem in gems if gem["kind"] == "active")}
        trigger = next((gem for gem in gems if gem["name"] == "Cast when Damage Taken"), None)
        if trigger is not None:
            triggered = next(gem for gem in gems if gem["kind"] == "active")
            ok, why = trigger_level_legal(data, trigger["name"], trigger["level"], triggered["name"],
                                          triggered["level"])
            if not ok:
                group["gems"] = [gem for gem in gems if gem is not trigger]
                group["delivery"] = "manual"
                notes.append(f"Cast the {triggered['name']} manually at this stage: {why}.")
        trial = [*groups, group]
        if pack_groups(trial, capacity)["unplaced"]:
            notes.append(f"Add the {source['role']} package ({label}) when you have enough linked sockets.")
            continue
        groups.append(group)
    return groups


def _stage_fill_spare(spec, phase_spec, data, phase, items, capacity, groups, notes, stats=None):
    """Give every spare socket of this stage's gear a level-legal, role-justified gem group.

    Same catalogue and ordering as the endgame fill (no PoB-measured packages, no reservations), so a
    stage never lists an unexplained empty socket; one movement skill at most.
    """
    if phase["level"] < 12:
        return groups
    facts = build_facts(phase_spec, items, {}, data)
    facts["level"] = phase["level"]
    present = {gem["name"] for group in groups for gem in group["gems"] if gem["kind"] == "active"}
    roles = {group["role"] for group in groups}
    for pkg in fill_packages(phase_spec, data, facts, present, roles=roles):
        if str(pkg["evidence"]).startswith("stat"):
            continue
        if pkg["role"] == "movement" and "movement" in roles:
            continue
        if any(name in present for name, _ in pkg["gems"][:1]):
            continue
        packing = pack_groups(groups, capacity)
        used = {slot: 0 for slot in capacity}
        for group in groups:
            slot = packing["placements"].get(group["id"])
            if slot in used:
                used[slot] += len(group["gems"])
        largest = max((info["total"] - used[slot] for slot, info in capacity.items()), default=0)
        if largest <= 0:
            break
        trimmed = {**pkg, "gems": pkg["gems"][:largest]}
        group = package_group(trimmed, data, phase["level"])
        if any(not gem["level"] for gem in group["gems"]):
            continue
        if stats:
            # Gems whose attribute requirements the stage's character cannot meet are not placed.
            from skill_planner import fit_levels_to_attributes
            known = {gem["instance"] for g in groups for gem in g["gems"]}
            if fit_levels_to_attributes([*groups, group], known, stats, data):
                continue
        if pack_groups([*groups, group], capacity)["unplaced"]:
            continue
        groups = [*groups, group]
        present.add(pkg["gems"][0][0])
        roles.add(pkg["role"])
    packing = pack_groups(groups, capacity)
    used = {slot: 0 for slot in capacity}
    for group in groups:
        slot = packing["placements"].get(group["id"])
        if slot in used:
            used[slot] += len(group["gems"])
    open_sockets = {slot: info["total"] - used[slot] for slot, info in capacity.items()
                    if info["total"] - used[slot] > 0 and slot != "Body Armour"}
    if open_sockets:
        notes.append("Sockets left open at this stage (no further level-legal gem with a stated role is "
                     "available yet): " + ", ".join(f"{slot} x{count}" for slot, count in sorted(open_sockets.items()))
                     + ".")
    return groups


def _drop_last_reserver(groups: list[dict]) -> str | None:
    for group in reversed(groups):
        if group["role"] not in RESERVING_ROLES:
            continue
        actives = [gem for gem in group["gems"] if gem["kind"] == "active"]
        if len(actives) > 1:
            removed = actives[-1]
            group["gems"].remove(removed)
            if group.get("mainActive") == removed["name"]:
                group["mainActive"] = actives[0]["name"]
            return removed["name"]
        groups.remove(group)
        return actives[0]["name"] if actives else group["id"]
    return None


def _stage_jewels(root, selected: set[str], level: int, data) -> dict[str, str]:
    """Endgame jewels whose sockets are allocated by this stage and whose requirements are met."""
    spec_node = active_spec(root)
    if spec_node is None:
        return {}
    items = {item.get("id"): item.text for item in root.findall("./Items/Item")}
    result = {}
    for socket in spec_node.findall("./Sockets/Socket"):
        text = items.get(socket.get("itemId"))
        if not text or socket.get("nodeId") not in selected:
            continue
        _, _, base = _item_parts(text)
        definition = data.bases.get(base, {})
        required = max(base_required_level(definition) if definition else 1, item_level_requirement(text))
        if required <= level:
            result[socket.get("nodeId")] = text.strip("\n\t ")
    return result


def _gear_rows(summary: dict) -> list[dict]:
    return [{"slot": row["slot"], "name": row["name"], "rarity": row["rarity"], "base": row["base"],
             "isUnique": row["isUnique"], "variant": row["variant"], "links": row["links"],
             "sockets": row["sockets"], "corrupted": row["corrupted"]} for row in summary["gear"]]


def _stage_loadout_fields(summary: dict) -> dict:
    return {"skillGroups": [{"index": group["index"], "slot": group["slot"], "label": group["label"],
                             "isMain": group["isMain"], "mainActive": group["mainActive"],
                             "gems": [{key: gem[key] for key in ("name", "kind", "level", "quality", "enabled")}
                                      for gem in group["gems"] if gem["socketed"]]}
                            for group in summary["groups"] if not group["itemGranted"] and group["socketedGemCount"]],
            "socketedGemCount": summary["socketedGemCount"],
            "supportedGroupCount": summary["supportedGroupCount"],
            "itemGranted": summary["itemGranted"],
            "jewels": [{key: jewel[key] for key in ("node", "name", "base", "rarity", "isUnique")}
                       for jewel in summary["jewels"]],
            "slotLinks": {slot: {"linkedRuns": entry["linkedRuns"], "used": entry["socketsUsed"],
                                 "spare": entry["socketsSpare"]}
                          for slot, entry in summary["bySlot"].items() if entry["socketTotal"]}}


def add_progression(endgame_xml: str, spec: dict, context: dict, data, worker, stage,
                    unique_candidates: list[tuple] | None = None, market: dict | None = None):
    root = ET.fromstring(endgame_xml)
    final_spec = active_spec(root)
    official = context["tree"]["nodes"]
    final_nodes = set(final_spec.get("nodes").split(","))
    masteries = {key: int(effect) for key, effect in re.findall(r"\{(\d+),(\d+)\}", final_spec.get("masteryEffects", ""))}
    regular = {key for key in final_nodes if not official[key].get("ascendancyName")}
    asc = final_nodes - regular
    start = next(key for key in regular if official[key].get("classStartIndex") == 3)
    asc_start = next(key for key in asc if official[key].get("isAscendancyStart"))
    # Endgame focus should not starve campaign checkpoints of life and
    # resistance nodes. Keep the exact final allocation, but order its legal
    # prefix defensively so the level-75 and act loadouts remain survivable.
    progression_spec = {**spec, "focus": "defense"}
    # Chaos Inoculation must not appear in a stage prefix merely because the
    # keystone is on the final tree; stages stay life-based until the Endgame
    # respec (gear, recovery and the keystone are all required together).
    ci = _ci_nodes(official, regular) if spec.get("defenseModel") == "ci" else set()
    try:
        order = connected_order(official, regular - ci, start, progression_spec, masteries)
        ci_deferred = bool(ci)
    except ValueError:
        order, ci_deferred = connected_order(official, regular, start, progression_spec, masteries), False
    asc_order = ascendancy_order(official, asc, asc_start, spec)
    final_summary = summarize_loadout(root, data.gems)
    final_groups = [group for group in groups_from_xml(root, data)]
    final_main = next(group for group in final_groups if group["role"] == "main")
    final_supports = [gem["name"] for gem in final_main["gems"] if gem["kind"] == "support"]
    mapping_pool = list((spec.get("mappingSupportPlan") or {}).get("mappingLink") or final_supports)
    phases, documents, summaries = milestones(spec["level"]), [], []
    previous_skill, previous_ascendancy = None, 0
    unique_candidates = unique_candidates or []
    budget_cap = spec.get("budgetChaos")
    for phase in phases:
        stage("Generating and validating " + phase["title"])
        instructions = []
        notes_for_stage: list[str] = []
        mana_warning = None
        if phase["act"] == 12:
            document = copy.deepcopy(root)
            add_flasks(document, data, phase["level"])
            xml = ET.tostring(document, encoding="unicode")
            calc = worker.request("calculate", xml=xml)
            main_skill = spec["skill"]
            stage_uniques = {}
            mapping_package_price = 0
            if ci_deferred:
                instructions.append(
                    "RESPEC TO CHAOS INOCULATION: earlier stages stay life-based. Before taking Chaos "
                    "Inoculation, have the energy-shield gear, recovery and defenses of this stage; allocate the "
                    "keystone with the remaining points and refund the life nodes it replaces.")
        else:
            main_skill = stage_skill(spec, data, phase)
            cap = 19 if phase["act"] == 11 else 20
            phase_spec = {**retarget(spec, main_skill, data.gem(main_skill)["tags"]),
                          "level": phase["level"], "minionCount": 1,
                          "ascendancy": spec["ascendancy"] if phase["ascendancyPoints"] else "None",
                          "mainLinks": phase["links"], "utilitySockets": stage_utility_cap(phase["level"]),
                          "enemyLevel": phase["level"], "resistancePenalty": phase["resistancePenalty"],
                          "defenseModel": "hybrid", "skillGroups": None}
            selected = set(order[:phase["level"] + phase["questPoints"]])
            if phase["ascendancyPoints"]:
                selected.update(asc_order[:phase["ascendancyPoints"] + 1])
            effects = {key: value for key, value in masteries.items() if key in selected}
            # A temporary attack skill may need a different weapon than the
            # requested final attack. Spell/minion starters can use the same type.
            items = rare_templates(data, spec["archetype"], spec["weaponType"], character_level=phase["level"],
                                   defense_model="hybrid")
            stage_uniques = {}
            mapping_package_price = 0
            stage_jewels = _stage_jewels(root, selected, phase["level"], data) if phase["act"] == 11 else {}
            groups: list[dict] = []
            stage_fill_notes: list[str] = []

            def render(skill_groups=None, jewels=None):
                return assemble(phase_spec, context, data, selected, [], items, effects, stage_uniques,
                                stage_jewels if jewels is None else jewels,
                                skill_groups=groups if skill_groups is None else skill_groups)

            # Chaos resistance is repaired from the Merciless Kitava penalty onward (never under CI).
            stage_chaos_target = 0 if phase["act"] >= 10 and spec.get("defenseModel") != "ci" else None
            capacity = apply_stage_caps(capacity_from_equipment(items, {}, data), phase_spec["utilitySockets"],
                                        phase["links"])
            pool = mapping_pool if phase["act"] == 11 else final_supports
            probe_main = make_group("main", "main", [(main_skill, "active")], slot="Body Armour",
                                    main_active=main_skill, include_in_full_dps=True,
                                    level_for=lambda name: gem_level(data.gem(name), phase["level"], cap))
            compatible = set(worker.request("supports", xml=render([probe_main], {}))["supports"])
            groups = [_stage_main_group(spec, data, phase, main_skill, pool, compatible, cap)]
            if phase["act"] >= 11 and len(groups[0]["gems"]) < phase["links"]:
                groups = _fill_stage_supports(worker, render, groups, data, phase["level"],
                                              phase["links"] - len(groups[0]["gems"]), compatible, spec, cap)
            groups = _stage_utility_groups(spec, data, phase, final_groups, cap, capacity, groups, notes_for_stage)
            if phase["level"] == 1:
                groups = groups[:1]
            else:
                stage_fill_notes.clear()
                groups = _stage_fill_spare(spec, phase_spec, data, phase, items, capacity, groups, stage_fill_notes,
                                           stats=worker.request("calculate", xml=render())["stats"])
            for attempt in range(12):    # one step per dropped reserver / fixed deficit
                calc = worker.request("calculate", xml=render())
                estimated_population = candidate_minion_count(calc["stats"], phase_spec)
                if (estimated_population is not None and
                        estimated_population != phase_spec.get("minionCount")):
                    phase_spec["minionCount"] = estimated_population
                    continue
                if solve_suffixes(items, data, calc["stats"], resistance_target=phase["resistanceTarget"],
                                  chaos_target=stage_chaos_target):
                    continue
                if calc["stats"].get("ManaUnreserved", 0) < calc["stats"].get("ManaCost", 0):
                    removed = _drop_last_reserver(groups)
                    if removed:
                        instructions.append(f"Add {removed} later, once reservation leaves enough mana for your main skill.")
                        continue
                    if phase["act"] >= 11:
                        # Degraded stage, not a failed generation: keep the complete link and
                        # report the mana shortfall so the player knows what to repair.
                        mana_warning = (
                            f"WARNING: {phase['title']} cannot fully sustain its {phase['links']}-link: "
                            f"mana cost {calc['stats'].get('ManaCost', 0):.0f} exceeds unreserved mana "
                            f"{calc['stats'].get('ManaUnreserved', 0):.0f} even with no optional reservations. "
                            "Use mana flasks, leech or more mana until the gear is repaired.")
                        instructions.append(mana_warning)
                        break
                    # Campaign stages may take links gradually.
                    main_group = groups[0]
                    supports_here = [gem for gem in main_group["gems"] if gem["kind"] == "support"]
                    if supports_here:
                        main_group["gems"].remove(supports_here[-1])
                        continue
                break
            if phase["act"] == 11 and unique_candidates:
                best_score = offense_value(calc["stats"])
                best_base = best_score
                mapping_target = None
                for package in mapping_unique_packages(unique_candidates, spec, data, phase["level"]):
                    package_items = copy.deepcopy(items)
                    package_uniques = {entry[1]: entry[2] for entry in package}
                    for item in package_items:
                        if item.slot in package_uniques:
                            item.mods.clear()
                    def package_xml():
                        return assemble(phase_spec, context, data, selected, [], package_items,
                                        effects, package_uniques, stage_jewels, skill_groups=groups)
                    try:
                        candidate_xml = ET.fromstring(package_xml())
                    except SocketConflict:
                        continue
                    add_flasks(candidate_xml, data, phase["level"])
                    trial_xml = ET.tostring(candidate_xml, encoding="unicode")
                    trial_calc = worker.request("calculate", xml=trial_xml)
                    for _ in range(3):
                        if not solve_suffixes(package_items, data, trial_calc["stats"],
                                              resistance_target=phase["resistanceTarget"],
                                              chaos_target=stage_chaos_target):
                            break
                        candidate_xml = ET.fromstring(package_xml())
                        add_flasks(candidate_xml, data, phase["level"])
                        trial_xml = ET.tostring(candidate_xml, encoding="unicode")
                        trial_calc = worker.request("calculate", xml=trial_xml)
                    trial_checks = stage_checks(trial_xml, phase, trial_calc, data, context)
                    trial_score = offense_value(trial_calc["stats"])
                    package_cost = [entry[3] for entry in package if entry[0] not in spec.get("requestedUniques", ())]
                    gain = math.log(trial_score / best_base) if best_base > 0 and trial_score > 0 else 0.0
                    if (all(check["passed"] for check in trial_checks) and trial_score > best_score + 0.001
                            and worth_price(spec, gain, package_cost)):
                        best_score = trial_score
                        package_price = (None if any(entry[3] is None for entry in package) else
                                         sum(entry[3] for entry in package))
                        mapping_target = (package_items, package_uniques, trial_calc, package_price)
                if mapping_target:
                    pre_package = (items, stage_uniques, calc, mapping_package_price, copy.deepcopy(groups),
                                   list(stage_fill_notes))
                    items, stage_uniques, calc, mapping_package_price = mapping_target
                    # The package may bring sockets of its own (fixed-socket uniques): fill them too.
                    capacity = apply_stage_caps(capacity_from_equipment(items, stage_uniques, data),
                                                phase_spec["utilitySockets"], phase["links"])
                    stage_fill_notes.clear()
                    groups = _stage_fill_spare(spec, phase_spec, data, phase, items, capacity, groups,
                                               stage_fill_notes, stats=calc["stats"])
                    names = [_item_parts(text)[1] for text in stage_uniques.values()]
                    instructions.append("Mapping equipment target: " + ", ".join(names) +
                                        ("; package selected from level-legal PoB comparisons within the stated budget."
                                         if spec.get("budgetChaos") is not None else
                                         f"; level-legal subset of the final build's uniques, limited to "
                                         f"{unique_policy.MAPPING_BUDGET_SHARE * 100:.0f}% of the standard unique budget "
                                         f"({unique_policy.mapping_cap(spec) or 0:g} chaos)."))
                    try:
                        render()
                    except SocketConflict:
                        # The package's sockets cannot hold this stage's gem groups: keep the all-rare target.
                        items, stage_uniques, calc, mapping_package_price, groups, saved_notes = pre_package
                        stage_fill_notes[:] = saved_notes
                        instructions.pop()
                        instructions.append("Mapping kept its all-rare equipment target; the best unique package "
                                            "did not leave enough sockets for the stage's gem groups.")
                elif unique_candidates:
                    instructions.append("Mapping kept its all-rare equipment target; no tested level-legal unique package improved the PoB result within the Mapping budget.")
            document = ET.fromstring(render())
            add_flasks(document, data, phase["level"])
            xml = ET.tostring(document, encoding="unicode")
            calc = worker.request("calculate", xml=xml)
            # Verify eligible tiers/groups as well as PoB's resulting requirements.
            for item in items:
                assert base_required_level(item.definition) <= phase["level"]
                probe = type(item)(item.slot, item.base, item.definition, item_level=item.item_level)
                for mod in item.mods:
                    if not probe.can_add(mod):
                        raise ValueError("Illegal leveling affix on " + item.slot)
                    probe.mods.append(mod)
            if phase["act"] == 11:
                instructions.append("Begin with white maps, then yellow maps as damage and survival allow. "
                                    "Mapping assumes a six-linked body armour; socket and link crafting is not priced.")
                if stage_jewels:
                    instructions.append("Socket jewels: " + "; ".join(
                        f"node {node}: {_item_parts(text)[1]}" for node, text in sorted(stage_jewels.items(),
                                                                                      key=lambda row: int(row[0]))) + ".")
            instructions.extend(notes_for_stage)
            instructions.extend(stage_fill_notes)
        checks = _waive_mana(stage_checks(xml, phase, calc, data, context), mana_warning)
        failures = [check["name"] for check in checks if not check["passed"]]
        if failures:
            raise ValueError(phase["title"] + " failed validation: " + ", ".join(
                f"{check['name']}: {check['reason']}" for check in checks if not check["passed"]))
        if main_skill != previous_skill:
            unlock = min(entry["requiredLevel"] for entry in data.gem(main_skill)["levels"])
            instructions.append(f"{main_skill} unlocks at character level {unlock}. Obtain the gem before switching; normal off-class gems can be bought from Siosa after the Act 3 Library quest.")
        if phase["act"] == 1 and spec["archetype"] == "minion":
            instructions.append("Start with Fireball to create corpses. Raise Zombie is an early helper; Summon Raging Spirit unlocks at level 4 and normal Summon Skeletons at level 10. Use the stage's main skill once unlocked.")
        if phase["ascendancyPoints"] > previous_ascendancy:
            names = [official[key]["name"] for key in asc_order[previous_ascendancy + 1:phase["ascendancyPoints"] + 1]
                     if official[key].get("isNotable")]
            instructions.append("Complete the next Labyrinth and allocate: " + ", ".join(names) + ".")
        if phase["act"] in {5, 10}:
            instructions.append(f"This checkpoint includes Kitava's {phase['resistancePenalty']}% total resistance penalty; repair gear to reach 75% fire, cold and lightning resistance.")
        previous_skill, previous_ascendancy = main_skill, phase["ascendancyPoints"]
        export = worker.request("export", xml=xml)
        saved_xml, disabled_grants = disable_conditional_item_skill_groups(export["xml"])
        if disabled_grants:
            spec["disabledConditionalItemSkills"] = disabled_grants
            export = worker.request("export", xml=saved_xml)
            if disable_conditional_item_skill_groups(export["xml"])[1]:
                raise ValueError(phase["title"] + " re-enabled unsupported conditional item-granted skills")
        documents.append(export["xml"])
        # PoB's first save materializes default combat placeholders. Score and
        # summarize the saved stage that will actually be merged/shared.
        calc = worker.request("calculate", xml=export["xml"])
        checks = _waive_mana(stage_checks(export["xml"], phase, calc, data, context), mana_warning)
        failures = [check["name"] for check in checks if not check["passed"]]
        if failures:
            raise ValueError(phase["title"] + " failed saved-export validation: " + ", ".join(failures))
        summaries.append(_stage_summary(export["xml"], phase, main_skill, instructions, calc, checks, data,
                                        stage_uniques, mapping_package_price, market, budget_cap))
    xml = combine_loadouts(documents, phases, build_notes(spec, summaries))
    loadouts = worker.request("loadouts", xml=xml)["loadouts"]
    if any(phase["title"] not in loadouts for phase in phases):
        raise ValueError("PoB could not link all named progression loadouts")
    # Exercise the merged file as well; item IDs and active set associations
    # must preserve every previously calculated stage, not only Endgame.
    for index, summary in enumerate(summaries, 1):
        selected_xml = select_stage(xml, index, summary["level"])
        calc = worker.request("calculate", xml=selected_xml)
        merged_checks = _waive_mana(stage_checks(selected_xml, summary, calc, data, context),
                                    (summary.get("warnings") or [None])[0])
        failures = [entry["name"] for entry in merged_checks if not entry["passed"]]
        if failures:
            raise ValueError("Merged loadout made " + summary["title"] +
                             " invalid: " + ", ".join(
                                 entry["name"] + ": " + entry["reason"]
                                 for entry in merged_checks if not entry["passed"]))
        merged = _stage_summary(selected_xml, summary, summary["mainSkill"], summary["instructions"], calc,
                                merged_checks, data, None, summary.get("uniquePackageCostChaos"), market,
                                budget_cap)
        # The merged, selected loadout is authoritative for what a user imports.
        drift = [name for name in ("gems", "socketedGemCount") if merged[name] != summary[name]]
        if drift or [row["name"] for row in merged["gear"]] != [row["name"] for row in summary["gear"]]:
            raise ValueError(f"Merged loadout changed the equipment or skills of {summary['title']}")
        if calc["stats"] != summary["stats"] or calc["passives"]["used"] != summary["passives"]:
            changed = {key: {"stage": summary["stats"].get(key), "merged": calc["stats"].get(key)}
                       for key in set(summary["stats"]) | set(calc["stats"])
                       if summary["stats"].get(key) != calc["stats"].get(key)}
            # Use the actual merged loadout's PoB calculation as authoritative;
            # configurations can materialize default combat placeholders only
            # when the named loadouts are combined.
            summary["mergeStatChanges"] = changed
            summary["stats"] = calc["stats"]
            summary["passives"] = calc["passives"]["used"]
            summary["passiveBudget"] = (summary["level"] - 1 + summary["questPoints"] +
                                         max(0, calc["stats"].get("ExtraPoints", 1) - 1))
            summary["validation"] = merged_checks
            summary["_calculation"] = calc
    return xml, summaries


def _stage_summary(final_xml: str, phase: dict, main_skill: str, instructions: list[str], calc: dict,
                   checks: list[dict], data, stage_uniques, package_price, market, budget) -> dict:
    """Everything a stage reports, derived from its final exported XML."""
    loadout = summarize_loadout(final_xml, data.gems)
    gear = _gear_rows(loadout)
    levels = {}
    for group in loadout["groups"]:
        for gem in group["gems"]:
            if gem["socketed"] and gem["level"] is not None:
                levels.setdefault(gem["name"], gem["level"])
    price = None
    if market is not None:
        price = price_unique_equipment(
            [{**row, "slot": row["slot"]} for row in gear] +
            [{"slot": "Jewel " + jewel["node"], "name": jewel["name"], "base": jewel["base"],
              "rarity": jewel["rarity"], "variant": jewel["variant"], "corrupted": jewel["corrupted"],
              "links": None} for jewel in loadout["jewels"]],
            market, budget, scope="stage_equipped_uniques", label=phase["title"])
    is_mapping = phase.get("act") == 11
    unique_names = [row["name"] for row in gear if row["isUnique"] and not row["slot"].startswith("Flask")]
    cost = (price["uniqueSubtotalChaos"] if price is not None and not price["unknown"] else
            None if price is not None else package_price if is_mapping else None)
    summary = {**phase, "mainSkill": main_skill, "gems": loadout["mainLinkGems"], "gemLevels": levels,
               "gear": gear, "stats": calc["stats"], "validation": checks,
               "uniquePackageCostChaos": cost if is_mapping else None,
               "uniquePackage": unique_names if is_mapping else [],
               "stageUniques": unique_names,
               "priceCoverage": price,
               "passives": calc["passives"]["used"],
               "passiveBudget": phase["level"] - 1 + phase["questPoints"] + max(0, calc["stats"].get("ExtraPoints", 1) - 1),
               "_calculation": calc, "instructions": list(instructions),
               "warnings": [text for text in instructions if text.startswith("WARNING")]}
    summary.update(_stage_loadout_fields(loadout))
    if not any(text.startswith("Use the shown") for text in summary["instructions"]):
        summary["instructions"].append(
            f"Use the shown {len(loadout['mainLinkGems'])}-gem main link; keep enough free mana to use "
            f"{main_skill}. Upgrade your mana flask as needed.")
    return summary
