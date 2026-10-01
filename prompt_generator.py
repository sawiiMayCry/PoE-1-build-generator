"""Public clean-generation entry point and legacy reference benchmark helpers.

generate() delegates to real_generator; reference helpers are retained for
regressions and comparisons and are never used as generation fallbacks.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import time
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

from build_generator import (
    REQUIRED_SLOTS, _active_set, _clean, _colors_fit, _item_parts,
    _items_by_slot, _socket_colors, decode_pob, mechanics_fingerprint,
    offense_value, quote, validate_calculation, validate_structure,
)
from ollama_service import DEFAULT_MODEL, ask_json
from pob_engine import calculate_with_pob
from services import game_context, market_data, reference_xml

REFERENCE_IDS = (
    "h8klSvqllefw", "W4yCI3RRdniV", "kXluNlsNQnKn", "eOtcfO47VWsG",
    "N3ej9xo-tYyk", "7_6IS6EVRZfT", "X_AhFC3h1Plp", "bQpIA48sZe7f",
    "eTPr2T4HC75_", "1MzEsvTwtFvx", "wypEsswtjq2y", "Q_423jgW_7Xr",
    "Vesy4e5HV3gt", "8cQ3YmPIEgrg", "cT7EOnsriDz5", "Q-Kayt3t1dYy",
    "XIootCmOcoyV", "l1ETl58DQShc", "SuiNbOyFLToF",
)
SWAP_SLOTS = ("Flask 1", "Flask 2", "Flask 3", "Flask 4", "Flask 5",
              "Belt", "Amulet", "Ring 1", "Ring 2", "Gloves", "Boots", "Helmet")


class PatternUnusable(ValueError):
    """The chosen source pattern cannot yield a valid build; try another."""


# Gems with these tags are auras, curses, movement and similar utility skills,
# so naming one in a prompt does not request it as the main skill.
UTILITY_TAGS = {"aura", "herald", "guard", "warcry", "blessing", "curse", "hex", "mark",
                "movement", "travel", "blink", "link", "banner", "stance"}


@lru_cache(maxsize=4)
def _read_gem_data(path: str, modified: float) -> str:
    return Path(path).read_text(encoding="utf-8")


def _gem_data(context: dict) -> str:
    path = context["pobHome"] / "Data" / "Gems.lua"
    return _read_gem_data(str(path), path.stat().st_mtime)


@lru_cache(maxsize=4)
def _main_skill_names(gem_data: str) -> frozenset[str]:
    names = set()
    for match in re.finditer(r'\["Metadata/Items/Gems/SkillGem[^"]+"\]\s*=\s*\{\s*name\s*=\s*"([^"]+)"(.*?)\n\t\},',
                             gem_data, re.S):
        tag_block = re.search(r"tags\s*=\s*\{(.*?)\}", match.group(2), re.S)
        tags = set(re.findall(r"(\w+)\s*=\s*true", tag_block.group(1))) if tag_block else set()
        if not tags & UTILITY_TAGS:
            names.add(match.group(1))
    return frozenset(names)


def _support(gem) -> bool:
    return "SupportGem" in (gem.get("gemId") or "")


def _active_skill(group) -> str | None:
    active = [gem.get("nameSpec") for gem in group.findall("Gem")
              if gem.get("nameSpec") and "SkillGem" in (gem.get("gemId") or "")]
    return active[0] if active else None


def _source_ids(data_root: Path) -> list[str]:
    ids = list(REFERENCE_IDS)
    library = data_root / "build_library.json"
    if library.is_file():
        try:
            ids += [entry["id"] for entry in json.loads(library.read_text(encoding="utf-8"))
                    if isinstance(entry, dict) and isinstance(entry.get("id"), str)]
        except (OSError, ValueError, KeyError):
            pass
    return list(dict.fromkeys(ids))


def catalog(context: dict, data_root: Path) -> tuple[list[dict], dict[str, ET.Element], list[str]]:
    patterns, roots, errors = [], {}, []
    for source_id in _source_ids(data_root):
        try:
            root = ET.fromstring(reference_xml(source_id, decode_pob))
            build = root.find("Build")
            spec = _active_set(root, "Tree", "Spec", "activeSpec")
            if build is None or build.get("className") != "Witch" or spec.get("treeVersion") != context["treeVersion"]:
                continue
            ascendancy = build.get("ascendClassName")
            if ascendancy not in {"Elementalist", "Necromancer", "Occultist"}:
                continue
            _, slots = _items_by_slot(root)
            if any(slots.get(slot, (None, None))[1] is None for slot in REQUIRED_SLOTS):
                continue
            skill_set = _active_set(root, "Skills", "SkillSet", "activeSkillSet")
            roots[source_id] = root
            for index, group in enumerate(skill_set.findall("Skill"), 1):
                gems = group.findall("Gem")
                skill = _active_skill(group)
                slot = group.get("slot")
                item = slots.get(slot, (None, None))[1]
                if (not skill or len(gems) not in {5, 6} or sum(_support(gem) for gem in gems) < 2
                        or item is None or len(_socket_colors(item.text or "")) < len(gems)):
                    continue
                patterns.append({"id": f"{source_id}:{index}", "source": source_id,
                                 "group": index, "ascendancy": ascendancy, "skill": skill,
                                 "level": int(build.get("level", "0")), "links": len(gems),
                                 "mainSlot": slot})
                if ascendancy == "Necromancer" and skill == "Summon Raging Spirit":
                    patterns.append({"id": f"{source_id}:{index}:zombie", "source": source_id,
                                     "group": index, "ascendancy": ascendancy, "skill": "Raise Zombie",
                                     "level": int(build.get("level", "0")), "links": len(gems),
                                     "mainSlot": slot, "replaceSkill": True})
        except Exception as exc:
            errors.append(f"{source_id}: {str(exc)[:100]}")
    return patterns, roots, errors


def _budget_from_prompt(prompt: str, divine_chaos: float) -> float | None:
    match = re.search(r"(?i)\b(\d+(?:\.\d+)?)\s*(divines?|divs?|d|chaos(?:\s+orbs?)?|c)\b", prompt)
    if not match:
        return None
    value = float(match.group(1))
    return value * (divine_chaos if match.group(2).lower().startswith(("d", "div")) else 1)


def _choose_pattern(prompt: str, model: str, patterns: list[dict], context: dict,
                    market: dict, feedback: str = "") -> tuple[dict, float, dict]:
    exact_skills = {entry["skill"] for entry in patterns
                    if re.search(r"(?i)(?<!\w)" + re.escape(entry["skill"]) + r"(?!\w)", prompt)}
    if re.search(r"(?i)\bzombies?\b", prompt):
        exact_skills.add("Raise Zombie")
    # "frenzy charges" describes a resource, not the Frenzy skill.
    mentioned = sorted((name for name in _main_skill_names(_gem_data(context)) if len(name) >= 5 and
                        re.search(r"(?i)(?<!\w)" + re.escape(name) + r"(?!\w)(?!\s+charges?\b)", prompt)),
                       key=len, reverse=True)
    if mentioned and not exact_skills:
        raise ValueError(f"No current validated Witch pattern for {mentioned[0]}. Add a compatible current PoB source build.")
    explicit_asc = next((name for name in ("Elementalist", "Necromancer", "Occultist")
                         if re.search(r"(?i)\b" + name + r"\b", prompt)), None)
    allowed = [entry for entry in patterns
               if (not exact_skills or entry["skill"] in exact_skills)
               and (not explicit_asc or entry["ascendancy"] == explicit_asc)]
    if not allowed:
        raise ValueError("No current Witch PoB pattern matches the named skill and ascendancy. Try another prompt or add a current reference to the catalogue.")
    supplied_budget = _budget_from_prompt(prompt, market["divineChaos"])
    prompt_data = {"request": prompt, "league": context["league"], "treeVersion": context["treeVersion"],
                   "divineChaos": market["divineChaos"], "exactBudgetChaos": supplied_budget,
                   "patterns": [{key: entry[key] for key in ("id", "ascendancy", "skill", "level", "links")}
                                for entry in allowed], "previousFailure": feedback}
    response = ask_json(model,
        "You are planning a NEW Path of Exile 1 Witch build. The provided pattern IDs are ingredients, not finished answers. "
        "Choose one exact pattern ID whose skill and ascendancy fit the user's request. Do not substitute a different "
        "skill if the user named one. If their exact requested skill has no pattern, return pattern_id as an empty string. "
        "Return JSON only: {pattern_id, requested_skill, budget_chaos, focus, intent}. requested_skill is the exact "
        "skill named by the user or an empty string for broad prompts. focus is damage, defense or balanced. "
        "If exactBudgetChaos is given, copy that value. Otherwise choose a sensible chaos cap, default 500. "
        "Never invent game IDs, item prices or calculated stats.",
        json.dumps(prompt_data, ensure_ascii=False), tokens=350)
    pattern_id = str(response.get("pattern_id", "")).strip()
    requested_skill = str(response.get("requested_skill", "")).strip().casefold()
    if requested_skill in {"zombie", "zombies"}:
        requested_skill = "raise zombie"
    # Skill substitution is prevented above: when the prompt names a catalogue
    # skill, `allowed` holds only that skill. The model's reply only picks
    # among `allowed`, so tolerate small models echoing a partial ID or the
    # skill name instead of the exact pattern ID.
    pattern = next((entry for entry in allowed if entry["id"] == pattern_id), None)
    if pattern is None and pattern_id:
        same_source = [entry for entry in allowed if entry["id"].startswith(pattern_id + ":")]
        pattern = next((entry for entry in same_source if entry["skill"].casefold() == requested_skill),
                       same_source[0] if same_source else None)
    if pattern is None and requested_skill:
        pattern = next((entry for entry in allowed if entry["skill"].casefold() == requested_skill), None)
    if pattern is None and len({entry["skill"] for entry in allowed}) == 1 and exact_skills:
        pattern = allowed[0]
    if pattern is None:
        raise ValueError("The local model could not match that prompt to a current Witch skill pattern.")
    cap = supplied_budget if supplied_budget is not None else 10_000_000.0
    if not 1 <= cap <= 10_000_000:
        raise ValueError("The requested budget must be between 1 and 10,000,000 chaos orbs.")
    focus = response.get("focus")
    if focus not in {"balanced", "damage", "defense"}:
        response["focus"] = "balanced"
    return pattern, cap, response


def _neighbors(node_id: str, nodes: dict) -> set[str]:
    node = nodes.get(node_id, {})
    return set(node.get("in", [])) | set(node.get("out", []))


def _tree_options(root: ET.Element, context: dict) -> list[dict]:
    spec = _active_set(root, "Tree", "Spec", "activeSpec")
    allocated = [value for value in spec.get("nodes", "").split(",") if value]
    selected = set(allocated)
    nodes = context["tree"]["nodes"]
    mastered = {match.group(1) for match in re.finditer(r"\{(\d+),", spec.get("masteryEffects", ""))}
    options = []
    for old in allocated:
        node = nodes.get(old, {})
        if (not node or not node.get("stats") or old in mastered or node.get("ascendancyName") or node.get("isMastery")
                or node.get("isKeystone") or "Jewel Socket" in node.get("name", "")):
            continue
        # These leaves often exist to meet a hard equipment or resistance
        # requirement. Do not offer to remove them as a generic damage swap.
        if re.search(r"(?i)resistan|dexterity|strength|intelligence|all attributes|reservation",
                     " ".join(node.get("stats", []))):
            continue
        parent = _neighbors(old, nodes) & selected
        if len(parent) != 1:
            continue
        for new in sorted(_neighbors(next(iter(parent)), nodes) - selected):
            replacement = nodes.get(new, {})
            if (not replacement or not replacement.get("stats") or replacement.get("ascendancyName") or replacement.get("isMastery")
                    or replacement.get("isKeystone") or "Jewel Socket" in replacement.get("name", "")):
                continue
            options.append({"id": f"T{len(options)+1}", "kind": "swap", "from": old, "to": new,
                            "label": f"{node.get('name')} → {replacement.get('name')}",
                            "stats": replacement.get("stats", [])})
    points = context.get("passiveCounts", {})
    if points and points["used"] < points["maximum"]:
        available = set()
        for old in allocated:
            available.update(_neighbors(old, nodes) - selected)
        for new in sorted(available):
            node = nodes.get(new, {})
            if (not node or not node.get("stats") or node.get("ascendancyName") or node.get("isMastery")
                    or node.get("isKeystone") or "Jewel Socket" in node.get("name", "")):
                continue
            options.append({"id": f"T{len(options)+1}", "kind": "add", "to": new,
                            "label": f"Allocate {node.get('name')}", "stats": node.get("stats", [])})
    # Return a bounded, diverse set of actual current-tree alternatives.
    return sorted(options, key=lambda entry: entry["kind"] != "add")[:24]


def _target_level(prompt: str, source_level: int, required_level: int) -> int:
    explicit = re.search(r"(?i)\b(?:level|lvl)\s*(\d{1,3})\b", prompt)
    # With no requested level, allow one new point for the model's tree plan.
    target = int(explicit.group(1)) if explicit else max(source_level, min(100, required_level + 1))
    if not 1 <= target <= 100:
        raise PatternUnusable("The source passive tree requires a character above level 100.")
    if target < required_level:
        raise PatternUnusable(f"This passive tree requires level {required_level}, above your requested level {target}.")
    return target


def _prepare_pattern(pattern: dict, roots: dict, prompt: str, context: dict,
                     app_root: Path, data_root: Path, stage) -> tuple[dict, dict]:
    stage("Checking the source's paid passive points with Path of Building")
    source = copy.deepcopy(roots[pattern["source"]])
    build = source.find("Build")
    build.set("mainSocketGroup", str(pattern["group"]))
    build.set("characterLevelAutoMode", "false")
    baseline = calculate_with_pob(ET.tostring(source, encoding="unicode"), app_root, data_root)
    points = baseline.get("passives")
    if not points:
        raise RuntimeError("PoB did not return the source's passive-point count.")
    source_level = int(build.get("level", "0"))
    target = _target_level(prompt, source_level, int(points["requiredLevel"]))
    build.set("level", str(target))
    build.set("characterLevelAutoMode", "false")
    roots[pattern["source"]] = source
    counts = {**points, "maximum": points["maximum"] + target - source_level}
    return {**pattern, "level": target, "sourceLevel": source_level}, {**context, "passiveCounts": counts}


def _donor_options(pattern: dict, roots: dict[str, ET.Element]) -> list[dict]:
    donors = []
    _, core_slots = _items_by_slot(roots[pattern["source"]])
    for source_id, root in roots.items():
        if source_id == pattern["source"] or root.find("Build").get("ascendClassName") != pattern["ascendancy"]:
            continue
        _, slots = _items_by_slot(root)
        gear = []
        for name in SWAP_SLOTS:
            item = slots.get(name, (None, None))[1]
            core_item = core_slots.get(name, (None, None))[1]
            if item is None or core_item is None or (item.text or "").strip() == (core_item.text or "").strip():
                continue
            gear.append(name)
        if len(gear) >= 3:
            donors.append({"id": source_id, "slots": gear})
    return sorted(donors, key=lambda entry: -len(entry["slots"]))[:6]


def _support_options(pattern: dict, roots: dict[str, ET.Element]) -> tuple[list[dict], dict[str, ET.Element]]:
    core = roots[pattern["source"]]
    active = _active_set(core, "Skills", "SkillSet", "activeSkillSet")
    group = active.findall("Skill")[pattern["group"] - 1]
    used = {gem.get("gemId") for gem in group.findall("Gem")}
    main_names = {gem.get("nameSpec") for gem in group.findall("Gem")}
    ignite_core = "Burning Damage" in main_names and "Combustion" in main_names
    ignite_supports = {"Unbound Ailments", "Deadly Ailments", "Burning Damage", "Swift Affliction",
                       "Combustion", "Cruelty", "Ignite Proliferation", "Efficacy", "Empower", "Lifetap"}
    zombie_supports = {"Minion Life", "Feeding Frenzy", "Multistrike", "Meat Shield",
                       "Melee Physical Damage", "Ruthless", "Empower"}
    choices, gems = [], {}
    # Same-skill alternatives first, then supports in the same source and
    # ascendancy. Every candidate is copied from a real, current PoB.
    sources = sorted(roots.items(), key=lambda pair: pair[0] != pattern["source"])
    for same_skill_only in (True, False):
        for source_id, root in sources:
            if root.find("Build").get("ascendClassName") != pattern["ascendancy"]:
                continue
            skill_set = _active_set(root, "Skills", "SkillSet", "activeSkillSet")
            for other in skill_set.findall("Skill"):
                if same_skill_only and _active_skill(other) != pattern["skill"]:
                    continue
                for gem in other.findall("Gem"):
                    gem_id = gem.get("gemId")
                    if not _support(gem) or not gem_id or gem_id in used or gem_id in gems:
                        continue
                    if pattern.get("replaceSkill") and gem.get("nameSpec") not in zombie_supports:
                        continue
                    if ignite_core and gem.get("nameSpec") not in ignite_supports:
                        continue
                    gems[gem_id] = gem
                    choices.append({"id": gem_id, "name": gem.get("nameSpec"), "source": source_id})
                    if len(choices) >= 20:
                        return choices, gems
    return choices, gems


def _choose_actions(prompt: str, model: str, pattern: dict, root: ET.Element,
                    roots: dict[str, ET.Element], context: dict, feedback: str = "") -> tuple[dict, list[dict], dict[str, ET.Element]]:
    tree = _tree_options(root, context)
    donors = _donor_options(pattern, roots)
    supports, support_gems = _support_options(pattern, roots)
    skill_set = _active_set(root, "Skills", "SkillSet", "activeSkillSet")
    main = skill_set.findall("Skill")[pattern["group"] - 1]
    removable = [{"id": gem.get("gemId"), "name": gem.get("nameSpec")}
                 for gem in main.findall("Gem") if _support(gem)]
    if pattern.get("replaceSkill"):
        removable = [entry for entry in removable if entry["name"] == "Unleash"]
    if not tree or not donors or not supports or not removable:
        raise PatternUnusable("This current skill pattern lacks enough legal tree, support or equipment alternatives.")
    request_data = {"request": prompt, "pattern": pattern, "treeOptions": tree,
                    "removableSupports": removable, "availableSupports": supports,
                    "donors": donors, "previousFailure": feedback}
    response = ask_json(model,
        "Design a NEW coherent PoE1 Witch build from these real component choices. Return JSON only with: "
        "tree_option (one listed T ID), donor_id (one listed source ID), gear_slots (array of one to three "
        "listed slots whose items differ from the core), remove_support (one listed gem ID), add_support "
        "(one listed different gem ID), reason (short explanation). Respect the user's playstyle. Prefer "
        "flasks and gear that preserve resistance and attribute requirements. Choose a support that benefits "
        "the named main skill. Use only listed IDs; you cannot invent item modifiers, passives or stats.",
        json.dumps(request_data, ensure_ascii=False, separators=(",", ":")), tokens=450)
    # Small local models sometimes echo a support's display name or familiar
    # alias instead of its PoB metadata ID. Resolve against the supplied list.
    def resolve(value, choices):
        value = str(value or "").casefold().strip()
        for entry in choices:
            if value in {str(entry["id"]).casefold(), str(entry["name"]).casefold()}:
                return entry["id"]
        return None
    response["remove_support"] = resolve(response.get("remove_support"), removable) or removable[0]["id"]
    response["add_support"] = resolve(response.get("add_support"), supports) or supports[0]["id"]
    tree_ids = {entry["id"] for entry in tree}
    tree_choice = str(response.get("tree_option", "")).strip().upper()
    response["tree_option"] = tree_choice if tree_choice in tree_ids else tree[0]["id"]
    donor_ids = {entry["id"] for entry in donors}
    if response.get("donor_id") not in donor_ids:
        response["donor_id"] = donors[0]["id"]
    permitted_slots = next(entry["slots"] for entry in donors if entry["id"] == response["donor_id"])
    requested = response.get("gear_slots") if isinstance(response.get("gear_slots"), list) else []
    selected = list(dict.fromkeys(slot for slot in requested if slot in permitted_slots))[:3]
    # Flask swaps rarely break resistance or attribute checks, so they are the
    # default when the model's own choice is empty or invalid.
    safe_slots = [slot for slot in permitted_slots if slot.startswith("Flask ")]
    response["gear_slots"] = selected or safe_slots[:3] or permitted_slots[:1]
    if pattern.get("replaceSkill"):
        strength = next((entry for entry in tree if entry["kind"] == "add" and entry["label"] == "Allocate Strength"), None)
        if strength:
            response["tree_option"] = strength["id"]
    return response, tree, support_gems


def _construct(pattern: dict, action: dict, tree_options: list[dict],
               support_gems: dict[str, ET.Element], roots: dict[str, ET.Element],
               context: dict) -> tuple[str, dict]:
    core_xml = ET.tostring(roots[pattern["source"]], encoding="unicode")
    root = copy.deepcopy(roots[pattern["source"]])
    build = root.find("Build")
    build.set("mainSocketGroup", str(pattern["group"]))
    skill_set = _active_set(root, "Skills", "SkillSet", "activeSkillSet")
    main = skill_set.findall("Skill")[pattern["group"] - 1]
    if pattern.get("replaceSkill"):
        original = next(gem for gem in main.findall("Gem") if "SkillGem" in (gem.get("gemId") or ""))
        for key in ("skillMinionCalcs", "skillMinionSkillCalcs", "skillMinionSkill", "skillMinion"):
            original.attrib.pop(key, None)
        original.set("gemId", "Metadata/Items/Gems/SkillGemRaiseZombie")
        original.set("nameSpec", "Raise Zombie")
        original.set("skillId", "RaiseZombie")
        original.set("variantId", "RaiseZombie")
        original.set("count", "1")
        main.set("label", "Raise Zombie")
    old_id = str(action.get("remove_support", ""))
    new_id = str(action.get("add_support", ""))
    old_gem = next((gem for gem in main.findall("Gem") if gem.get("gemId") == old_id and _support(gem)), None)
    new_gem = support_gems.get(new_id)
    if old_gem is None or new_gem is None or old_id == new_id:
        raise ValueError("Model chose a support outside the supplied legal components")
    position = list(main).index(old_gem)
    main.remove(old_gem)
    main.insert(position, copy.deepcopy(new_gem))
    tree_choice = next((entry for entry in tree_options if entry["id"] == action.get("tree_option")), None)
    if tree_choice is None:
        raise ValueError("Model chose a passive outside the current official tree alternatives")
    spec = _active_set(root, "Tree", "Spec", "activeSpec")
    allocated = [value for value in spec.get("nodes", "").split(",") if value]
    if tree_choice["kind"] == "swap":
        allocated[allocated.index(tree_choice["from"])] = tree_choice["to"]
    else:
        allocated.append(tree_choice["to"])
    spec.set("nodes", ",".join(allocated))
    donor_id = str(action.get("donor_id", ""))
    if donor_id not in roots or donor_id == pattern["source"] or roots[donor_id].find("Build").get("ascendClassName") != pattern["ascendancy"]:
        raise ValueError("Model chose an incompatible equipment donor")
    donor = roots[donor_id]
    items, core_slots = _items_by_slot(root)
    _, donor_slots = _items_by_slot(donor)
    slots = action.get("gear_slots")
    if not isinstance(slots, list):
        raise ValueError("Model must provide equipped slots")
    slots = list(dict.fromkeys(slots))[:6]
    next_id = max((int(item.get("id", "0")) for item in items.findall("Item") if item.get("id", "").isdigit()), default=0)
    changed = []
    for name in slots:
        if name not in SWAP_SLOTS or name not in core_slots or name not in donor_slots:
            raise ValueError(f"Model chose unavailable equipment slot {name!r}")
        target_slot, old_item = core_slots[name]
        donor_item = donor_slots[name][1]
        if old_item is None or donor_item is None or (old_item.text or "").strip() == (donor_item.text or "").strip():
            raise ValueError(f"Chosen {name} item does not create a new equipped choice")
        next_id += 1
        copied = copy.deepcopy(donor_item)
        copied.set("id", str(next_id))
        first_set = next((index for index, child in enumerate(items) if child.tag == "ItemSet"), len(items))
        items.insert(first_set, copied)
        target_slot.set("itemId", str(next_id))
        changed.append(name)
    _, updated_slots = _items_by_slot(root)
    main_item = updated_slots.get(main.get("slot"), (None, None))[1]
    if main_item is None or not _colors_fit(main.findall("Gem"), _socket_colors(main_item.text or ""),
                                            _gem_data(context), context["treeVersion"]):
        raise ValueError("Generated main link no longer fits its equipped sockets")
    _clean(root, f"[GENERATED] Witchcraft {pattern['ascendancy']} {pattern['skill']} {secrets.token_hex(3)}")
    xml = ET.tostring(root, encoding="unicode")
    if mechanics_fingerprint(xml) == mechanics_fingerprint(core_xml):
        raise ValueError("Model plan did not produce a distinct build")
    return xml, {"changedSlots": changed, "changedMainLinks": [f"{old_gem.get('nameSpec')} → {new_gem.get('nameSpec')}"],
                 "treeChange": tree_choice["label"], "method": "Local-model component plan from current PoB and official tree data",
                 "source": pattern["source"], "donor": donor_id,
                 "levelChange": (f"Target level {pattern['level']} fits the source's planned tree "
                                  f"(exported character level {pattern['sourceLevel']})."
                                  if pattern.get("sourceLevel", pattern["level"]) != pattern["level"] else ""),
                 "modelReason": (str(action.get("reason", ""))[:350] if changed else
                                 "The local model chose the skill, passive, and support. Gear swaps were omitted "
                                 "after validation found a failed requirement.")}


def _validate_candidate(pattern: dict, action: dict, tree_options: list[dict], support_gems: dict,
                        roots: dict, context: dict, market: dict, cap: float, prompt: str,
                        app_root: Path, data_root: Path, stage):
    stage("Constructing the model's build plan")
    xml, recipe = _construct(pattern, action, tree_options, support_gems, roots, context)
    source_spec = _active_set(roots[pattern["source"]], "Tree", "Spec", "activeSpec")
    clusters = {value for value in source_spec.get("nodes", "").split(",")
                if value not in context["tree"]["nodes"] and value.isdigit() and int(value) >= 65536}
    checks, details = validate_structure(xml, {**context, "referenceClusterNodes": clusters},
                                         pattern["ascendancy"], pattern["skill"])
    if not all(check["passed"] for check in checks):
        raise ValueError("; ".join(check["name"] + ": " + check["reason"] for check in checks if not check["passed"]))
    stage("Calculating and checking the exact build in Path of Building")
    calculation = calculate_with_pob(xml, app_root, data_root)
    checks += validate_calculation(calculation)
    if not all(check["passed"] for check in checks):
        raise ValueError("; ".join(check["name"] + ": " + check["reason"] for check in checks if not check["passed"]))
    points = calculation["passives"]
    details["ascendancyPoints"] = points["ascendancy"] + points["secondaryAscendancy"]
    stage(f"Pricing equipped items in {context['league']}")
    price = quote(details["gear"], market, cap)
    explicit_budget = _budget_from_prompt(prompt, market["divineChaos"]) is not None
    if not explicit_budget:
        price["budgetChaos"] = None
        price["budgetStatus"] = "not specified"
    if explicit_budget and price["pricedSubtotalChaos"] > cap:
        raise ValueError("Priced equipment alone exceeds the requested budget")
    return xml, recipe, checks, details, calculation, price


def _legacy_reference_generate(request: dict, app_root: Path, data_root: Path, stage) -> dict:
    prompt = str(request.get("prompt", "")).strip()
    if not 12 <= len(prompt) <= 2000:
        raise ValueError("Describe the Witch build you want in 12 to 2,000 characters.")
    model = str(request.get("model") or DEFAULT_MODEL)
    stage("Checking current league, tree and prices")
    context = game_context()
    market = market_data(context["league"])
    stage("Loading current Witch build components")
    patterns, roots, source_errors = catalog(context, data_root)
    if not patterns:
        raise RuntimeError("No current Witch PoB components are available. " + " ".join(source_errors[:3]))
    feedback = ""
    failures = []
    excluded_patterns = set()
    for attempt in range(3):
        stage(f"Asking {model} to plan the build" + (f" (attempt {attempt+1})" if attempt else ""))
        pattern = None
        try:
            available_patterns = [entry for entry in patterns if entry["id"] not in excluded_patterns]
            pattern, cap, high_plan = _choose_pattern(prompt, model, available_patterns, context, market, feedback)
            pattern, plan_context = _prepare_pattern(pattern, roots, prompt, context, app_root, data_root, stage)
            stage("Asking the local model to choose passives, gems and equipment")
            action, tree_options, support_gems = _choose_actions(prompt, model, pattern, roots[pattern["source"]],
                                                                  roots, plan_context, feedback)
            # Fall back from the model's gear choice to flask swaps only, then
            # to the source gear, before giving up on this pattern.
            flasks = [slot for slot in action["gear_slots"] if slot.startswith("Flask ")]
            candidates = [action]
            if flasks and flasks != action["gear_slots"]:
                candidates.append({**action, "gear_slots": flasks})
            if action["gear_slots"]:
                candidates.append({**action, "gear_slots": []})
            candidate_failures = []
            for candidate in candidates:
                try:
                    if candidate is not action:
                        stage("Retrying with fewer equipment swaps after a failed check")
                    xml, recipe, checks, details, calculation, price = _validate_candidate(
                        pattern, candidate, tree_options, support_gems, roots, plan_context, market,
                        cap, prompt, app_root, data_root, stage)
                    break
                except Exception as candidate_error:
                    candidate_failures.append(str(candidate_error)[:500])
            else:
                raise PatternUnusable(" | ".join(candidate_failures))
            build_id = "g" + secrets.token_hex(10)
            return {"id": build_id, "name": ET.fromstring(xml).find("Build").get("name"),
                    "class": "Witch", "ascendancy": pattern["ascendancy"], "mainSkill": pattern["skill"],
                    "level": details["level"], "gems": details["gems"], "treeNodes": details["treeNodes"],
                    "ascendancyPoints": details["ascendancyPoints"], "gear": details["gear"],
                    "validation": checks, "stats": calculation["stats"], "pobVersion": calculation.get("version"),
                    "quote": price, "recipe": recipe, "modelUsed": model, "prompt": prompt,
                    "modelIntent": str(high_plan.get("intent", ""))[:350],
                    "league": context["league"], "treeVersion": context["treeVersion"],
                    "officialTreeRelease": context["officialRelease"], "createdAt": int(time.time()),
                    "shareStatus": "pending", "shareUrl": None, "_xml": xml,
                    "_fingerprint": mechanics_fingerprint(xml)}
        except Exception as exc:
            if isinstance(exc, PatternUnusable) and pattern is not None:
                excluded_patterns.add(pattern["id"])
            feedback = str(exc)[:500]
            no_pattern = "No current validated Witch pattern" in feedback or "No current Witch PoB pattern" in feedback
            if no_pattern and failures:
                # The named skill's patterns were all excluded after failing
                # validation; report those failures, not the emptied catalogue.
                break
            failures.append(feedback)
            if no_pattern:
                break
    if failures and all("Priced equipment alone exceeds the requested budget" in failure for failure in failures):
        raise ValueError("No validated build fits that budget: priced equipment alone exceeds the cap. Raise the budget or omit it.")
    if failures and ("No current validated Witch pattern" in failures[-1] or "No current Witch PoB pattern" in failures[-1]):
        raise ValueError(failures[-1])
    raise ValueError("The local model could not make a valid build for this prompt. " + " | ".join(failures[-3:])[:1200])


def generate(request: dict, app_root: Path, data_root: Path, stage) -> dict:
    """Public entry point: generate mechanics from installed data, never a donor."""
    from real_generator import generate as generate_from_data
    return generate_from_data(request, app_root, data_root, stage)
