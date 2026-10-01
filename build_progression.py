"""Generated, level-legal campaign and mapping loadouts for a finished design."""
from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET

from build_assembly import assemble
from build_generator import _item_parts, _main_group, offense_value
from generation_data import base_required_level, rare_templates, solve_suffixes
from passive_search import candidate_minion_count, graph, heuristic, paths_from


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
         "ascendancyPoints": 6, "links": 5, "resistancePenalty": -60, "resistanceTarget": 75},
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
    valid_gems = all(any(entry["level"] == int(gem.get("level")) and entry["requiredLevel"] <= phase["level"]
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
        valid_items = valid_items and base in data.bases and base_required_level(data.bases[base]) <= phase["level"]
    complete_flasks = flasks_complete(xml, data, phase["level"])
    _, main, _ = _main_group(root)
    links = len(main.findall("Gem"))
    counts = {}
    for group in active_skills.findall("Skill") if active_skills is not None else []:
        socketed = sum(bool(gem.get("gemId")) for gem in group.findall("Gem"))
        if socketed:
            counts[group.get("slot")] = counts.get(group.get("slot"), 0) + socketed
    def socket_count(slot):
        entry = next((entry for entry in equipment.findall("Slot") if entry.get("name") == slot), None)
        if entry is None or entry.get("itemId") not in ids:
            return 0
        text = ids[entry.get("itemId")].text
        match = re.search(r"(?m)^Sockets: ([RGBW -]+)$", text)
        return sum(color in "RGBW" for color in match[1]) if match else 0
    available_sockets = {slot: socket_count(slot) for slot in counts}
    socket_ok = links <= phase["links"] and all(count <= available_sockets[slot] for slot, count in counts.items())
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
         f"{phase['title']}; main link {links}/{phase['links']}; groups {counts}; "
         f"available sockets {available_sockets}"),
        ("Stage attributes", all(output.get(attr, 0) >= output.get("Req" + attr, 0) for attr in ("Str", "Dex", "Int"))),
        ("Stage resistances", all(output.get(element + "Resist", -60) >= phase["resistanceTarget"] for element in ("Fire", "Cold", "Lightning"))),
        ("Stage mana", output.get("ManaUnreserved", 0) >= output.get("ManaCost", 0)),
        ("Stage health pool", output.get("Life", 0) + output.get("EnergyShield", 0) >=
         (3000 if phase["act"] == 12 else 30 * phase["level"] if phase["act"] == 11 else
          20 * phase["level"])),
    ]
    return [{"name": fact[0], "passed": bool(fact[1]),
             "reason": fact[2] if len(fact) > 2 else phase["title"]} for fact in facts]


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
        lines.append("Equipment: " + "; ".join(f"{item['slot']}: {item['base']}" for item in phase["gear"]))
        lines.append("")
    lines += ["MAPPING AND ENDGAME", "Mapping uses meaningful measured supports up to a five-link and campaign-accessible rares; acquire the six-link and final equipment before selecting Endgame.",
              "Endgame is the requested level and generated equipment target. The validation checks establish minimum viability, not boss-kill capability.",
              "Rare items and gems remain unpriced. Improve sustained mana recovery, chaos resistance and ailment protection before harder maps."]
    mapping = spec.get("mappingSupportPlan", {})
    if mapping.get("integratedCoverageSupports"):
        lines.append("The selected link includes clear coverage support: " +
                     ", ".join(mapping["integratedCoverageSupports"]) + ".")
    for option in mapping.get("mappingAlternatives", [])[:2]:
        lines.append(f"CLEAR OPTION: {option['support']} provides {option['role']} at support-socket sweep {option['screeningSocket']}; it retained about {option['singleTargetRetention']:.0%} of the compared single-target support ({option['comparedWith']}). This is a coverage alternative, not a simulated pack-DPS claim; recalculate after swapping it into the finished link.")
    return "\n".join(lines)


def add_progression(endgame_xml: str, spec: dict, context: dict, data, worker, stage):
    root = ET.fromstring(endgame_xml)
    final_spec = root.find("./Tree/Spec")
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
    order = connected_order(official, regular, start, progression_spec, masteries)
    asc_order = ascendancy_order(official, asc, asc_start, spec)
    final_supports = [gem.get("nameSpec") for gem in _main_group(root)[1].findall("Gem")][1:]
    phases, documents, summaries = milestones(spec["level"]), [], []
    previous_skill, previous_ascendancy = None, 0
    for phase in phases:
        stage("Generating and validating " + phase["title"])
        instructions = []
        if phase["act"] == 12:
            document = copy.deepcopy(root)
            add_flasks(document, data, phase["level"])
            xml = ET.tostring(document, encoding="unicode")
            calc = worker.request("calculate", xml=xml)
            main_skill = spec["skill"]
            gem_levels = {gem.get("nameSpec"): int(gem.get("level")) for gem in document.findall("./Skills/SkillSet/Skill/Gem")
                          if gem.get("gemId")}
            supports = final_supports
        else:
            main_skill = stage_skill(spec, data, phase)
            phase_spec = {**spec, "skill": main_skill, "level": phase["level"], "minionCount": 1,
                          "ascendancy": spec["ascendancy"] if phase["ascendancyPoints"] else "None",
                          "mainLinks": phase["links"], "utilitySockets": 1 if phase["level"] == 1 else 3 if phase["level"] < 25 else 4,
                          "enemyLevel": phase["level"], "resistancePenalty": phase["resistancePenalty"]}
            phase_spec["utility"] = {name: slot for name, slot in spec["utility"].items()
                                     if gem_level(data.gem(name), phase["level"])
                                     and (phase["act"] >= 3 or name in {"Flame Dash", "Steelskin"})}
            if phase["level"] == 1:
                phase_spec["utility"] = {}
            gem_levels = {name: gem_level(data.gem(name), phase["level"], 19 if phase["act"] == 11 else 20)
                          for name in [main_skill, *phase_spec["utility"]]}
            phase_spec["gemLevels"] = gem_levels
            selected = set(order[:phase["level"] + phase["questPoints"]])
            if phase["ascendancyPoints"]:
                selected.update(asc_order[:phase["ascendancyPoints"] + 1])
            effects = {key: value for key, value in masteries.items() if key in selected}
            # A temporary attack skill may need a different weapon than the
            # requested final attack. Spell/minion starters can use the same type.
            items = rare_templates(data, spec["archetype"], spec["weaponType"], character_level=phase["level"])
            supports = []
            def render():
                return assemble(phase_spec, context, data, selected, supports, items, effects)
            xml = render()
            compatible = set(worker.request("supports", xml=xml)["supports"])
            early = ["Minion Damage", "Added Lightning Damage", "Added Cold Damage", "Arcane Surge", "Combustion", "Lesser Multiple Projectiles"]
            available = [name for name in dict.fromkeys([*final_supports, *early]) if name in data.gems
                         and data.gem(name)["id"] in compatible and gem_level(data.gem(name), phase["level"])
                         and (phase["act"] >= 3 or name in early)]
            for name in available[:phase["links"] - 1]:
                supports.append(name)
                gem_levels[name] = gem_level(data.gem(name), phase["level"], 19 if phase["act"] == 11 else 20)
            for attempt in range(5):
                calc = worker.request("calculate", xml=render())
                estimated_population = candidate_minion_count(calc["stats"], phase_spec)
                if (estimated_population is not None and
                        estimated_population != phase_spec.get("minionCount")):
                    phase_spec["minionCount"] = estimated_population
                    continue
                if solve_suffixes(items, data, calc["stats"], resistance_target=phase["resistanceTarget"]):
                    continue
                if calc["stats"].get("ManaUnreserved", 0) < calc["stats"].get("ManaCost", 0):
                    auras = [name for name in phase_spec["utility"] if data.gem(name)["tags"].get("aura")]
                    if auras:
                        removed = auras[-1]
                        del phase_spec["utility"][removed]
                        gem_levels.pop(removed, None)
                        instructions.append(f"Add {removed} later, once reservation leaves enough mana for your main skill.")
                        continue
                    if supports:
                        gem_levels.pop(supports.pop(), None)
                        continue
                break
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
                instructions.append("Begin with white maps, then yellow maps as damage and survival allow; upgrade to a six-link for Endgame.")
        checks = stage_checks(xml, phase, calc, data, context)
        failures = [check["name"] for check in checks if not check["passed"]]
        if failures:
            raise ValueError(phase["title"] + " failed validation: " + ", ".join(failures))
        if main_skill != previous_skill:
            unlock = min(entry["requiredLevel"] for entry in data.gem(main_skill)["levels"])
            instructions.append(f"{main_skill} unlocks at character level {unlock}. Obtain the gem before switching; normal off-class gems can be bought from Siosa after the Act 3 Library quest.")
        if phase["act"] == 1 and spec["archetype"] == "minion":
            instructions.append("Start with Fireball to create corpses. Raise Zombie is an early helper; Summon Raging Spirit unlocks at level 4 and normal Summon Skeletons at level 10. Use the stage's main skill once unlocked.")
        if phase["ascendancyPoints"] > previous_ascendancy:
            names = [official[key]["name"] for key in asc_order[previous_ascendancy + 1:phase["ascendancyPoints"] + 1]
                     if official[key].get("isNotable")]
            instructions.append("Complete the next Labyrinth and allocate: " + ", ".join(names) + ".")
        instructions.append(f"Use the shown {len(supports) + 1}-gem main link; keep enough free mana to use {main_skill}. Upgrade your mana flask as needed.")
        if phase["act"] in {5, 10}:
            instructions.append(f"This checkpoint includes Kitava's {phase['resistancePenalty']}% total resistance penalty; repair gear to reach 75% fire, cold and lightning resistance.")
        previous_skill, previous_ascendancy = main_skill, phase["ascendancyPoints"]
        export = worker.request("export", xml=xml)
        documents.append(export["xml"])
        # PoB's first save materializes default combat placeholders. Score and
        # summarize the saved stage that will actually be merged/shared.
        calc = worker.request("calculate", xml=export["xml"])
        checks = stage_checks(export["xml"], phase, calc, data, context)
        failures = [check["name"] for check in checks if not check["passed"]]
        if failures:
            raise ValueError(phase["title"] + " failed saved-export validation: " + ", ".join(failures))
        gear = []
        for item in document.findall("./Items/Item"):
            _, _, base = _item_parts(item.text)
            item_slots = [slot.get("name") for slot in document.findall("./Items/ItemSet/Slot") if slot.get("itemId") == item.get("id")]
            for slot in item_slots:
                gear.append({"slot": slot, "base": base})
        actual_levels = {gem.get("nameSpec"): int(gem.get("level")) for gem in document.findall("./Skills/SkillSet/Skill/Gem")
                         if gem.get("gemId")}
        summaries.append({**phase, "mainSkill": main_skill, "gems": [main_skill, *supports],
                          "gemLevels": actual_levels, "gear": gear, "stats": calc["stats"], "validation": checks,
                          "passives": calc["passives"]["used"], "passiveBudget": phase["level"] - 1 + phase["questPoints"] +
                          max(0, calc["stats"].get("ExtraPoints", 1) - 1),
                          "_calculation": calc,
                          "instructions": instructions})
    xml = combine_loadouts(documents, phases, build_notes(spec, summaries))
    loadouts = worker.request("loadouts", xml=xml)["loadouts"]
    if any(phase["title"] not in loadouts for phase in phases):
        raise ValueError("PoB could not link all named progression loadouts")
    # Exercise the merged file as well; item IDs and active set associations
    # must preserve every previously calculated stage, not only Endgame.
    for index, summary in enumerate(summaries, 1):
        selected_xml = select_stage(xml, index, summary["level"])
        calc = worker.request("calculate", xml=selected_xml)
        merged_checks = stage_checks(selected_xml, summary, calc, data, context)
        failures = [entry["name"] for entry in merged_checks if not entry["passed"]]
        if failures:
            raise ValueError("Merged loadout made " + summary["title"] +
                             " invalid: " + ", ".join(
                                 entry["name"] + ": " + entry["reason"]
                                 for entry in merged_checks if not entry["passed"]))
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
