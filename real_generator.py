"""Prompt -> validated intent -> deterministic, PoB-scored generated build."""
from __future__ import annotations

import copy
import json
import re
import secrets
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from build_assembly import assemble
from build_progression import add_progression, flasks_complete
from build_generator import (_item_parts, mechanics_fingerprint, offense_value, quote,
                             validate_calculation, validate_structure)
from generation_data import (GameData, RareItem, base_required_level, rare_templates,
                             roll_line, solve_suffixes)
from ollama_service import DEFAULT_MODEL, ask_json
from passive_search import (SearchBudget, graph, paths_from, initial_nodes, mastery_choices,
                            candidate_score, recounted_stats, score, search_tree, heuristic,
                            tree_defenses_met, candidate_minion_count,
                            temporary_minion_population, sustained_resource_use)
from pob_engine import close_worker, export_with_pob, get_worker
from services import game_context, market_data

FINAL_REFINEMENT_RESERVE = 820


def named_skill(prompt: str, data: GameData) -> str | None:
    aliases = {r"\bzombies?\b": "Raise Zombie", r"\bEK\b": "Ethereal Knives",
               r"\bSRS\b": "Summon Raging Spirit"}
    mentioned = [name for name in data.main_names if re.search(
        r"(?<!\w)" + re.escape(name) + r"(?!\w)(?!\s+charges?\b)", prompt, re.I)]
    if mentioned:
        return max(mentioned, key=len)
    return next((name for pattern, name in aliases.items() if re.search(pattern, prompt, re.I)), None)


def budget_from_prompt(prompt: str, divine_chaos: float) -> float | None:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(divines?|divs?|d|chaos(?:\s+orbs?)?|c)\b", prompt, re.I)
    if match is None:
        return None
    value = float(match[1]) * (divine_chaos if match[2].lower().startswith("d") else 1)
    if not 1 <= value <= 10_000_000:
        raise ValueError("The budget must be between 1 and 10,000,000 chaos orbs")
    return value


def normalize_intent(prompt: str, reply: dict, data: GameData, market: dict) -> dict:
    explicit = named_skill(prompt, data)
    name = explicit or str(reply.get("skill", ""))
    try:
        gem = data.gem(name)
        if gem["name"] not in data.main_names:
            raise ValueError("Utility gem cannot be a main skill")
    except ValueError:
        if explicit:
            raise
        # Deterministic fallback for malformed IDs, utility skills and partial
        # model replies. An explicit named skill is never replaced.
        name = "Raise Zombie" if "minion" in prompt.lower() else "Winter Orb"
        gem = data.gem(name)
    tags = gem["tags"]
    explicit_asc = next((name for name in ("Elementalist", "Necromancer", "Occultist")
                         if re.search(r"\b" + name + r"\b", prompt, re.I)), None)
    asc = explicit_asc or str(reply.get("ascendancy", ""))
    if asc not in {"Elementalist", "Necromancer", "Occultist"}:
        asc = "Necromancer" if tags.get("minion") else "Occultist" if tags.get("chaos") else "Elementalist"
    focus = ("defense" if re.search(
                 r"\b(tanky|defen[cs]e(?:[-\s]+focus)?|defensive(?:[-\s]+focus)?|"
                 r"focus(?:ed)?\s+(?:on\s+)?defen[cs]e|survivability)\b", prompt, re.I) else
             "damage" if re.search(
                 r"\b(dps|more damage|damage[-\s]+focus|focus(?:ed)?\s+(?:on\s+)?damage)\b",
                 prompt, re.I) else str(reply.get("focus", "balanced")))
    if focus not in {"damage", "defense", "balanced"}:
        focus = "balanced"
    level_match = re.search(r"\b(?:level|lvl)\s*(\d+)\b", prompt, re.I)
    level = int(level_match[1]) if level_match else 90
    if not 80 <= level <= 100:
        raise ValueError("Requested level must be between 80 and 100")
    damage = next((tag for tag in ("chaos", "cold", "lightning", "fire", "physical") if tags.get(tag)), "physical")
    ignite = bool(re.search(r"\b(ignite|burning)\b", prompt, re.I))
    if asc == "Elementalist" and gem["name"] in {"Ethereal Knives", "Penance Brand", "Wave of Conviction"}:
        ignite = ignite or not re.search(r"\b(hit[- ]based|non[- ]ignite|no ignite)\b", prompt, re.I)
    archetype = ("minion" if tags.get("minion") else "attack" if tags.get("attack") else
                 "ignite" if ignite else "dot" if tags.get("dot") or gem["name"] in {
                     "Vortex", "Cold Snap", "Bane", "Bane of Condemnation", "Essence Drain", "Contagion", "Soulrend"} else "spell")
    if ignite and asc != "Elementalist" and not tags.get("fire"):
        raise ValueError("This ignite recipe requires a fire skill or Elementalist's Shaper of Flames")
    weapon_types = gem.get("weaponTypes") or {}
    allowed_weapon_types = ([key for key, enabled in weapon_types.items() if enabled]
                            if isinstance(weapon_types, dict) else
                            list(weapon_types) if isinstance(weapon_types, (list, tuple, set)) else [])
    weapon = next((kind for kind in ("Wand", "Bow", "Staff", "Claw", "Dagger", "One Handed Sword",
                                   "One Handed Axe", "One Handed Mace", "Sceptre")
                  if (weapon_types.get(kind) if isinstance(weapon_types, dict) else kind in allowed_weapon_types)),
                 "Wand")
    utility = {"Flame Dash": "Boots", "Steelskin": "Gloves"}
    if archetype == "minion":
        curse = {"fire": "Flammability", "cold": "Frostbite", "lightning": "Conductivity", "chaos": "Despair"}.get(damage, "Vulnerability")
        utility.update({"Determination": "Helmet", "Convocation": "Boots", curse: "Gloves"})
    elif archetype == "ignite":
        utility.update({"Malevolence": "Helmet", "Flammability": "Gloves"})
    elif damage == "cold":
        utility.update({"Hatred": "Helmet", "Frostbite": "Gloves"})
    elif damage == "chaos":
        utility.update({"Malevolence": "Helmet", "Despair": "Gloves"})
    elif damage == "lightning":
        utility.update({"Wrath": "Helmet", "Conductivity": "Gloves"})
    elif damage == "fire":
        utility.update({"Anger": "Helmet", "Flammability": "Gloves"})
    else:
        utility.update({"Determination": "Helmet", "Vulnerability": "Gloves"})
    # Utility requests are independent of main-skill recognition.
    requested_utilities = [name for name, entry in data.gems.items() if not entry["support"]
                           and name not in data.main_names and re.search(
                               r"(?<!\w)" + re.escape(name) + r"(?!\w)", prompt, re.I)]
    aura_requests = [name for name in requested_utilities if data.gem(name)["tags"].get("aura")]
    if aura_requests:
        for name in list(utility):
            if data.gem(name)["tags"].get("aura"):
                del utility[name]
    for name in requested_utilities:
        utility[name] = "Helmet" if data.gem(name)["tags"].get("aura") else "Boots"
    spec = {"skill": gem["name"], "ascendancy": asc, "level": level, "focus": focus,
            "damageType": "fire" if ignite else damage, "baseDamageType": damage, "archetype": archetype,
            "budgetChaos": budget_from_prompt(prompt, market["divineChaos"]), "weaponType": weapon,
            "weaponTypes": allowed_weapon_types,
            "utility": utility, "noUniques": bool(re.search(
                r"\bno[\s-]+uniques?\b|\brares?(?:[\s-]+(?:items?|gear|equipment))?[\s-]+only\b",
                prompt, re.I)),
            "requestedUtilities": requested_utilities,
            "intent": str(reply.get("intent", ""))[:350]}
    # These mechanics require dedicated gear/tree recipes. Fail specifically
    # instead of producing a different build which happens to pass numeric gates.
    for pattern, label in ((r"\bchaos inoculation\b|\bCI\b", "Chaos Inoculation"),
                           (r"\blow[- ]life\b", "low life"), (r"\bward loop\b", "ward loop")):
        if re.search(pattern, prompt, re.I):
            raise ValueError(f"The clean generator does not yet have a {label} mechanic recipe")
    return spec


def parse_intent(prompt, model, data, market):
    named = named_skill(prompt, data)
    try:
        reply = ask_json(model,
            "Turn the request into a PoE 1 Witch build intent. Choose only installed main skills. "
            "Return JSON: {skill, ascendancy, focus, intent}. Ascendancy is Elementalist, Necromancer, "
            "or Occultist; focus is balanced, damage or defense. Respect the exact named skill. "
            "Do not write game IDs, items, passive nodes, or calculated stats.",
            json.dumps({"request": prompt, "namedSkill": named,
                        "mainSkills": [named] if named else data.main_names}), tokens=350)
    except RuntimeError as exc:
        # Invalid JSON from an installed small model is recoverable. Connection
        # and setup errors retain the existing actionable failure behavior.
        if "unusable plan" not in str(exc):
            raise
        reply = {}
    return normalize_intent(prompt, reply, data, market)


def target_dps(output: dict, spec: dict) -> float:
    if spec["archetype"] == "ignite":
        return float(output.get("IgniteDPS", 0))
    if spec["archetype"] == "dot":
        return max(output.get("FullDotDPS", 0), output.get("IgniteDPS", 0), output.get("TotalDotDPS", 0))
    return offense_value(output)


def calculate_finalist(worker, xml: str, node_count: int, trace: list | None = None) -> dict:
    """Recalculate a finalist and restart PoB if it silently drops its tree.

    Repeated candidate imports can leave PoB returning equipment-only stats
    while ignoring a populated passive tree. A clean Lua state forces this
    exact XML through a fresh import before export and legality decisions.
    """
    calculation = worker.request("calculate", xml=xml)
    used = int(calculation.get("passives", {}).get("used", 0) or 0)
    tolerance = max(12, int(node_count * 0.15))
    if node_count > 16 and used + tolerance < node_count:
        reset = getattr(worker, "close", None)
        if callable(reset):
            reset()
            calculation = worker.request("calculate", xml=xml)
            if trace is not None:
                trace.append({"kind": "pob_worker_reset",
                              "reason": "finalist import returned too few allocated passive nodes",
                              "candidateNodes": node_count, "firstImportUsed": used,
                              "retryUsed": calculation.get("passives", {}).get("used", 0)})
    return calculation


def release_repairable_suffixes(items: list[RareItem]) -> None:
    """Free resistance/attribute suffixes so gear packages can be repaired.

    Unique swaps often displace several resistance-bearing rares at once.
    Merely adding affixes to the remaining rares fails when their suffixes
    are already full, even if those old rolls are now redundant. The solver
    recalculates the actual deficits and rebuilds these specific suffixes.
    """
    repairable = re.compile(r"(?:Fire|Cold|Lightning) Resistance|to (?:Strength|Dexterity|Intelligence)", re.I)
    for item in items:
        item.mods = [mod for mod in item.mods
                     if not any(repairable.search(line) for line in mod.get("lines", []))]


def mechanic_profile(spec: dict) -> dict:
    key = (spec["ascendancy"], spec["skill"])
    profiles = {
        ("Elementalist", "Winter Orb"): {
            "name": "winter_orb_elementalist", "damageSource": "player_spell",
            "hitOrAilment": "cold_hit", "conversion": "none modeled",
            "resourceUse": "mana per cast", "summonModel": "none",
            "compatibleAscendancies": ["Elementalist"],
            "compatibleUtilityChoices": ["Flame Dash", "Steelskin", "Hatred", "Frostbite", "Clarity"],
            "requiredInteractions": ["cold spell damage", "sustained channeling"],
        },
        ("Elementalist", "Ethereal Knives"): {
            "name": "ethereal_knives_ignite_elementalist", "damageSource": "player_spell",
            "hitOrAilment": "ignite", "conversion": "none modeled",
            "resourceUse": "mana per cast", "summonModel": "none",
            "compatibleAscendancies": ["Elementalist"],
            "compatibleUtilityChoices": ["Flame Dash", "Steelskin", "Malevolence", "Flammability", "Clarity"],
            "requiredInteractions": ["Shaper of Flames", "ignite damage calculation"],
        },
        ("Necromancer", "Raise Zombie"): {
            "name": "raise_zombie_necromancer", "damageSource": "permanent_minion",
            "hitOrAilment": "minion hit", "conversion": "none modeled",
            "resourceUse": "mana per summon", "summonModel": "permanent minion count",
            "compatibleAscendancies": ["Necromancer"],
            "compatibleUtilityChoices": ["Flame Dash", "Steelskin", "Determination", "Convocation",
                                         "Flammability", "Frostbite", "Conductivity", "Despair",
                                         "Vulnerability", "Clarity"],
            "requiredInteractions": ["recalculate active zombie count for each item candidate"],
        },
        ("Necromancer", "Summon Raging Spirit"): {
            "name": "srs_necromancer", "damageSource": "temporary_minion",
            "hitOrAilment": "minion hit", "conversion": "none modeled",
            "resourceUse": "mana-limited summoning rate", "summonModel": "duration and sustainable cast rate, capped by PoB limit",
            "compatibleAscendancies": ["Necromancer"],
            "compatibleUtilityChoices": ["Flame Dash", "Steelskin", "Determination", "Convocation",
                                         "Flammability", "Frostbite", "Conductivity", "Despair",
                                         "Vulnerability", "Clarity"],
            "requiredInteractions": ["summon rate", "spirit duration", "resource-limited population"],
        },
    }
    profile = profiles.get(key)
    if key == ("Elementalist", "Ethereal Knives") and spec["archetype"] != "ignite":
        profile = None
    if profile is None:
        temporary_weapon_minion = spec.get("skill") == "Animate Weapon"
        return {"name": "generic_experimental", "damageSource": spec["archetype"],
                "hitOrAilment": "unclassified", "conversion": "not modeled explicitly",
                "compatibleAscendancies": [], "compatibleUtilityChoices": [],
                "resourceUse": "mana-limited summoning rate" if temporary_weapon_minion else "single-use mana check",
                "summonModel": ("duration and sustainable cast rate, capped by PoB limit"
                                if temporary_weapon_minion else "not modeled"),
                "requiredInteractions": (["weapon summon rate", "weapon duration", "resource-limited population"]
                                         if temporary_weapon_minion else [])}
    return profile


def assess_quality(spec: dict, calculation: dict, profile: dict,
                   mechanic_checks: list[dict] | None = None,
                   search_limited: bool = False) -> tuple[str, list[str]]:
    output = calculation.get("stats", {})
    warnings = []
    cost, rate, regen = (float(output.get(key, 0) or 0)
                         for key in ("ManaCost", "Speed", "ManaRegen"))
    # For temporary minions, PoB's full tooltip cast rate is not the
    # sustainable use rate. sync_permanent_minion_count derives an achievable
    # population from mana-limited casts times duration; reporting the raw
    # tooltip rate as a failed sustain check would contradict that model.
    temporary_population_sustained = (
        profile.get("name") == "srs_necromancer" and bool(spec.get("_srsPopulationSustainable")) or
        spec.get("skill") == "Animate Weapon" and bool(spec.get("_temporaryPopulationSustainable")))
    zombie = profile.get("name") == "raise_zombie_necromancer"
    if cost > 0 and rate > 0 and regen < cost * rate and not temporary_population_sustained and not zombie:
        warnings.append(f"Mana regeneration ({regen:.1f}/s) is below estimated skill use ({cost * rate:.1f}/s).")
    if profile["name"] == "generic_experimental":
        warnings.append("No tested mechanic profile matches this skill and ascendancy combination.")
    for name in spec.get("unmodeledFlaskEffects", []):
        warnings.append(f"Unique flask {name} is equipped, but its effect is disabled because flask uptime is not modeled.")
    if profile["name"] == "srs_necromancer" and not spec.get("_srsPopulationSustainable"):
        warnings.append("Temporary SRS population is not derived from summon rate, duration, and mana sustain.")
    if spec.get("skill") == "Animate Weapon" and not spec.get("_temporaryPopulationSustainable"):
        warnings.append("Animate Weapon population is not derived from summon rate, duration, and mana sustain.")
    pool = float(output.get("Life", 0) or 0) + float(output.get("EnergyShield", 0) or 0)
    if pool < 6000:
        warnings.append(f"Endgame life plus energy shield is {pool:.0f}; quality target is at least 6,000.")
    ehp = float(output.get("TotalEHP", 0) or 0)
    if ehp < 15000:
        warnings.append(f"Calculated effective hit pool is {ehp:.0f}; quality target is at least 15,000.")
    for element in ("Fire", "Cold", "Lightning"):
        resistance = float(output.get(element + "Resist", -60) or 0)
        if resistance < 75:
            warnings.append(f"{element} resistance is {resistance:.0f}%; quality target is 75%.")
    if not mechanic_checks:
        warnings.append("Mechanic activation checks were not supplied to quality assessment.")
    else:
        for check in mechanic_checks:
            if not check.get("passed"):
                warnings.append(f"Mechanic check failed: {check.get('name', 'unknown check')}.")
    passives = calculation.get("passives", {})
    maximum = int(passives.get("maximum", 0) or 0)
    used = int(passives.get("used", 0) or 0)
    unspent = max(0, maximum - used)
    if maximum and unspent > max(8, int(maximum * 0.15)):
        warnings.append(f"{unspent} passive points remain unspent; quality limit is {max(8, int(maximum * 0.15))}.")
    if search_limited:
        warnings.append("The design search reached its 2,000-evaluation limit; passive and complete-build quality is unverified.")
    if profile.get("name") == "generic_experimental":
        return "experimental", warnings
    return ("validated" if not warnings else "experimental"), warnings


def assess_mechanics(spec: dict, calculation: dict, profile: dict, xml: str,
                     context: dict) -> list[dict]:
    """Report modeled mechanic activation separately from legality and quality."""
    stats = calculation.get("stats", {})
    known = profile.get("name") != "generic_experimental"
    checks = [{"name": "Tested mechanic profile", "passed": known,
               "reason": profile.get("name") if known else "No tested profile matches this skill and ascendancy"},
              {"name": "Main-skill damage calculated", "passed": target_dps(stats, spec) > 0,
               "reason": "PoB must calculate positive damage for the requested mechanism"}]
    if known:
        ascendancies = set(profile.get("compatibleAscendancies", []))
        checks.append({"name": "Compatible ascendancy",
                       "passed": spec.get("ascendancy") in ascendancies,
                       "reason": (f"{spec.get('ascendancy')} is in the tested compatible ascendancy set"
                                  if spec.get("ascendancy") in ascendancies else
                                  f"Expected one of {sorted(ascendancies)}")})
        allowed_utility = set(profile.get("compatibleUtilityChoices", []))
        explicit_utility = set(spec.get("requestedUtilities", []))
        selected_utility = set(spec.get("utility", {}))
        unsupported_utility = selected_utility - allowed_utility - explicit_utility
        checks.append({"name": "Compatible utility choices", "passed": not unsupported_utility,
                       "reason": ("Selected utility gems match the tested recipe or explicit user requests"
                                  if not unsupported_utility else
                                  "Unmodeled utility choices: " + ", ".join(sorted(unsupported_utility)))})
    nodes = context.get("tree", {}).get("nodes", {})
    root = ET.fromstring(xml)
    tree = root.find("./Tree")
    specs = tree.findall("Spec") if tree is not None else []
    active_spec = tree.get("activeSpec", "1") if tree is not None else "1"
    tree_spec = next((entry for entry in specs if entry.get("id") == active_spec), None)
    if tree_spec is None and specs and active_spec.isdigit():
        index = int(active_spec) - 1
        tree_spec = specs[index] if 0 <= index < len(specs) else specs[0]
    if tree_spec is None:
        tree_spec = specs[0] if specs else None
    allocated = set(tree_spec.get("nodes", "").split(",")) if tree_spec is not None else set()
    if profile.get("name") == "ethereal_knives_ignite_elementalist":
        shaper = next((key for key, node in nodes.items() if node.get("name") == "Shaper of Flames"), None)
        ignite = max(stats.get("IgniteDPS", 0), stats.get("WithIgniteDPS", 0)) > 0
        checks.extend([
            {"name": "Shaper of Flames allocated", "passed": shaper is not None and shaper in allocated,
             "reason": "EK ignite requires the Elementalist Shaper of Flames notable"},
            {"name": "Ignite damage active", "passed": ignite,
             "reason": "PoB must report nonzero ignite damage for the ignite recipe"},
        ])
    elif profile.get("name") == "raise_zombie_necromancer":
        count = int(stats.get("ActiveMinionLimit", 0) or 0)
        checks.append({"name": "Permanent zombie limit calculated", "passed": count > 0,
                       "reason": f"PoB calculated {count} active zombies"})
    elif profile.get("name") == "srs_necromancer":
        count = int(spec.get("minionCount", 0) or 0)
        checks.append({"name": "Temporary summon population modeled", "passed": count > 0 and
                       bool(spec.get("_srsPopulationSustainable")),
                       "reason": f"Sustainable estimated active spirits: {count}" if count > 0 else
                       "No sustainable SRS population could be established from cast rate, duration, mana or life sustain"})
    if spec.get("skill") == "Animate Weapon":
        count = int(spec.get("minionCount", 0) or 0)
        sustained = bool(spec.get("_temporaryPopulationSustainable"))
        checks.append({"name": "Temporary weapon population modeled", "passed": count > 0 and sustained,
                       "reason": f"Sustainable estimated animated weapons: {count}" if sustained else
                       "No sustainable Animate Weapon population could be established from cast rate, duration, mana or life sustain"})
    return checks


def sync_permanent_minion_count(spec: dict, calculation: dict) -> bool:
    if spec["skill"] in {"Summon Raging Spirit", "Animate Weapon"}:
        stats = calculation.get("stats", {})
        population = temporary_minion_population(stats, spec)
        estimate, sustainable = population if population is not None else (0, False)
        sustainable_key = ("_srsPopulationSustainable" if spec["skill"] == "Summon Raging Spirit"
                           else "_temporaryPopulationSustainable")
        spec[sustainable_key] = sustainable
        if estimate == spec.get("minionCount"):
            return False
        spec["minionCount"] = estimate
        return True
    if spec["skill"] != "Raise Zombie":
        return False
    count = int(calculation.get("stats", {}).get("ActiveMinionLimit", 0) or 0)
    if count <= 0 or count == spec.get("minionCount"):
        return False
    spec["minionCount"] = count
    return True


def ordinary_jewel_templates(data: GameData, spec: dict, level: int) -> list[RareItem]:
    """Build legal rare jewels from PoB's explicit ordinary-jewel modifier pool."""
    candidates = []
    for base_name, definition in sorted(data.bases.items()):
        tags = definition.get("tags", {})
        if (base_name == "Timeless Jewel" or definition.get("type") != "Jewel"
                or not tags.get("jewel") or not tags.get("default")
                or any(tags.get(key) for key in ("abyss_jewel", "expansion_jewel_large",
                                                  "expansion_jewel_medium", "expansion_jewel_small",
                                                  "timeless_jewel", "animal_charm"))):
            continue
        item = RareItem("Jewel", base_name, definition, item_level=level, quality=0)
        for kind in ("Prefix", "Suffix"):
            ranked = []
            for mod in data.jewel_mods:
                if mod.get("kind") != kind or len(mod.get("lines", [])) != 1 or not item.can_add(mod):
                    continue
                value = heuristic({"stats": mod["lines"]}, spec)
                if value > 0:
                    ranked.append((value, mod["id"], mod))
            for _, _, mod in sorted(ranked, key=lambda row: (-row[0], row[1])):
                if item.can_add(mod):
                    item.mods.append(mod)
                if sum(mod.get("kind") == kind for mod in item.mods) >= 2:
                    break
        if item.mods:
            candidates.append((sum(heuristic({"stats": mod["lines"]}, spec) for mod in item.mods),
                               base_name, item))
    # Jewel colors rarely change the modeled stats; keep the best ordinary
    # base of each color at most so every socket comparison stays bounded.
    selected = []
    seen_signatures = set()
    for _, base_name, item in sorted(candidates, key=lambda row: (-row[0], row[1])):
        signature = tuple(mod["id"] for mod in item.mods)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        selected.append(item)
        if len(selected) == 6:
            break
    return selected


def search_ordinary_jewels(context: dict, spec: dict, allocated: set[str], jewels: dict[str, RareItem],
                           templates: list[RareItem], render, worker, calc: dict,
                           budget: SearchBudget, trace: list | None) -> tuple[set[str], dict[str, RareItem], dict]:
    """Test legal socket-plus-jewel packages against PoB while preserving link budget."""
    if not templates:
        return allocated, jewels, calc
    nodes = context["tree"]["nodes"]
    adjacency = graph(nodes, lambda node: not node.get("ascendancyName") and not node.get("isMastery")
                      and not node.get("isProxy")
                      and ("classStartIndex" not in node or node["classStartIndex"] == 3))
    score_now = score(calc["stats"], spec)
    # Add at most two sockets in a bounded pass. A socket and its travel path
    # are evaluated together with each ordinary jewel candidate.
    for _ in range(2):
        remaining_points = calc["passives"]["maximum"] - calc["passives"]["used"]
        reachable = paths_from(allocated, adjacency)
        sockets = [(len(path), int(key), key, path) for key, path in reachable.items()
                   if nodes.get(key, {}).get("isJewelSocket") and key not in jewels and path
                   and len(path) <= remaining_points]
        sockets.sort()
        sockets = sockets[:8]
        best = None
        for _, _, key, path in sockets:
            for template in templates[:4]:
                if not budget.claim(reserve=FINAL_REFINEMENT_RESERVE):
                    break
                candidate_nodes = allocated | set(path)
                candidate_jewels = {**jewels, key: template}
                candidate = worker.request("calculate", xml=render(nodes=candidate_nodes,
                                               jewelset=candidate_jewels))
                checks = validate_calculation(candidate)
                delta = candidate_score(candidate["stats"], spec) - score_now
                accepted = all(check["passed"] for check in checks) and delta > 0.001
                if trace is not None:
                    trace.append({"kind": "jewel_candidate", "node": key, "base": template.base,
                                  "mods": [mod["id"] for mod in template.mods], "eligible": accepted,
                                  "score_delta": round(delta, 6),
                                  "reason": "improved PoB score and passed legality checks; compared with other sockets" if accepted else
                                            "failed legality checks" if not all(check["passed"] for check in checks) else
                                            "did not improve objective"})
                if accepted and (best is None or delta > best[0] or
                                 (delta == best[0] and (int(key), template.base) <
                                  (int(best[1]), best[2].base))):
                    best = (delta, key, template, candidate, candidate_nodes)
            if budget.limit - budget.used <= FINAL_REFINEMENT_RESERVE:
                break
        if best is None:
            break
        _, key, template, calc, allocated = best
        jewels[key] = template
        score_now = score(calc["stats"], spec)
        if trace is not None:
            trace.append({"kind": "jewel_selection", "node": key, "base": template.base,
                          "mods": [mod["id"] for mod in template.mods], "selected": True,
                          "reason": "highest scoring legal jewel package for a reachable ordinary socket"})
    return allocated, jewels, calc


def support_candidate_shortlist(identifiers, data: GameData, spec: dict, limit: int = 50) -> list[str]:
    """Keep the most relevant PoB-compatible supports for bounded scoring.

    PoB's compatibility API intentionally returns every legal support, including
    candidates that calculate identically to the baseline. Use installed gem
    tags plus skill tags to rank the broad candidate list before spending full
    design evaluations; all retained candidates still receive exact PoB scores.
    """
    skill = data.gem(spec["skill"])
    skill_tags = skill.get("tags", {})
    archetype = spec["archetype"]
    relevant_weights = {spec.get("damageType", ""): 7,
                        spec.get("baseDamageType", ""): 3,
                        "minion": 12 if archetype == "minion" else 0,
                        "attack": 5 if archetype in {"attack", "minion"} else 0,
                        "melee": 4 if archetype in {"attack", "minion"} else 0,
                        "spell": 5 if archetype not in {"attack", "minion"} else 0,
                        "elemental": 2,
                        "critical": 2,
                        "ailment": 3 if archetype in {"ignite", "dot"} else 0,
                        "duration": 2 if archetype in {"ignite", "dot", "minion"} else 0,
                        "projectile": 2 if skill_tags.get("projectile") else 0,
                        "area": 2 if skill_tags.get("area") else 0,
                        "channeling": 2 if skill_tags.get("channeling") else 0}
    relevant_weights = {tag: weight for tag, weight in relevant_weights.items() if tag and weight}

    def relevance(identifier: str) -> tuple[int, str]:
        gem = data.by_id.get(identifier, {})
        tags = gem.get("tags", {})
        value = sum(weight for tag, weight in relevant_weights.items() if tags.get(tag))
        name = gem.get("name", "").casefold()
        if "damage" in name or "penetration" in name:
            value += 2
        if any(term in name for term in ("poison", "bleed", "ignite", "burning", "ailment", "sadism")):
            value += 5
        if "chaos" in tags and archetype in {"minion", "attack", "ignite", "dot"}:
            value += 2
        if any(term in name for term in ("speed", "echo", "multistrike", "unleash", "intensity")):
            value += 1
        if any(term in name for term in ("mana", "inspiration", "lifetap", "efficiency")):
            value += 1
        return value, gem.get("name", identifier).casefold()

    return sorted(set(identifiers), key=lambda identifier: (-relevance(identifier)[0],
                                                            relevance(identifier)[1], identifier))[:limit]


def support_refinement_order(identifiers, data: GameData, initial_candidates=(), limit: int = 24) -> list[str]:
    """Reserve late PoB support slots for resource supports before DPS ranking.

    The initial support shortlist is calculated before final gear/tree sustain
    is known. A support such as Lifetap can therefore look unsustainable there
    but become viable after life regeneration gear and passives are selected.
    Keep those resource options in the late complete-build calculation batch.
    """
    identifiers = list(dict.fromkeys(identifiers))
    by_name = {data.by_id[identifier]["name"]: identifier for identifier in identifiers
               if identifier in data.by_id}
    resource_terms = ("inspiration", "lifetap", "life tap", "blood magic", "mana cost",
                      "mana efficiency", "unleash")
    resource_ids = [identifier for name, identifier in sorted(by_name.items(), key=lambda row: row[0].casefold())
                    if any(term in name.casefold() for term in resource_terms)]
    candidate_by_name = {entry.get("name"): entry for entry in initial_candidates}

    def long_run_damage(identifier):
        name = data.by_id.get(identifier, {}).get("name")
        entry = candidate_by_name.get(name, {})
        damage = max(0.0, float(entry.get("damage", 0) or 0))
        sustainability = entry.get("sustainability") or {}
        if sustainability.get("sustainable"):
            return damage
        checks = sustainability.get("checks", [])
        if not checks:
            return damage
        coverage = min(max(0.0, float(check.get("availablePerSecond", 0) or 0)) /
                       max(0.1, float(check.get("usePerSecond", 0) or 0)) for check in checks)
        return damage * min(1.0, coverage)

    damage_ids = sorted(identifiers, key=lambda identifier: (-long_run_damage(identifier),
                                                              data.by_id.get(identifier, {}).get("name", "").casefold(),
                                                              identifier))
    return list(dict.fromkeys([*resource_ids, *damage_ids]))[:limit]


def clarity_socket_slot(spec: dict, data: GameData, items: list[RareItem]) -> str | None:
    """Find an equipment slot with room for one additional utility gem."""
    if "Clarity" in spec.get("utility", {}):
        return spec["utility"]["Clarity"]
    for slot in ("Helmet", "Gloves", "Boots", "Weapon 1", "Weapon 2", "Shield",
                 "Amulet", "Ring 1", "Ring 2", "Belt"):
        item = next((entry for entry in items if entry.slot == slot), None)
        if item is None:
            continue
        current = sum(assigned == slot for assigned in spec.get("utility", {}).values())
        slot_limit = 4 if slot in {"Helmet", "Gloves", "Boots"} else 3
        capacity = min(int(spec.get("utilitySockets", 4)), slot_limit,
                       int(item.definition.get("socketLimit", 0) or 0))
        if current < capacity:
            return slot
    return None


def mana_utility_levels(spec: dict, data: GameData, calc: dict, items: list[RareItem],
                        *, require_deficit: bool = True) -> list[int]:
    """List legal Clarity levels when the current complete design cannot sustain mana use."""
    if ("Clarity" not in data.gems or
            ("Clarity" in spec.get("utility", {}) and not spec.get("_automaticClarity"))):
        return []
    if clarity_socket_slot(spec, data, items) is None:
        return []
    resource = sustained_resource_use(calc.get("stats", {}), spec)
    if not resource or not any(check.get("resource") == "mana" for check in resource.get("checks", ())):
        return []
    if (require_deficit and not spec.get("_automaticClarity") and
            not any(check.get("resource") == "mana" and not check.get("sustainable")
                    for check in resource.get("checks", ()))):
        return []
    clarity = data.gem("Clarity")
    levels = clarity.get("levels") or ()
    stats = calc.get("stats", {})
    return [int(entry["level"]) for entry in levels
            if int(entry.get("requiredLevel", 0) or 0) <= int(spec.get("level", 1))
            and int(entry.get("int", 0) or 0) <= int(stats.get("Int", 0) or 0)
            and int(entry.get("dex", 0) or 0) <= int(stats.get("Dex", 0) or 0)
            and int(entry.get("str", 0) or 0) <= int(stats.get("Str", 0) or 0)]


def search_mana_utility(spec: dict, data: GameData, items: list[RareItem], render,
                        worker, calc: dict, budget: SearchBudget, trace: list | None = None,
                        *, reserve: int = 0, require_full_legality: bool = True):
    """Try legal Clarity levels after gear, links and passives have stabilized."""
    levels = mana_utility_levels(spec, data, calc, items)
    if not levels:
        return calc
    slot = clarity_socket_slot(spec, data, items)
    original_utility = dict(spec.get("utility", {}))
    original_levels = dict(spec.get("gemLevels", {}))
    baseline_score = candidate_score(calc["stats"], spec)
    original_clarity_level = original_levels.get("Clarity")
    candidates = []
    for level in levels:
        if not budget.claim(reserve=reserve):
            break
        spec.setdefault("utility", {})["Clarity"] = slot
        spec.setdefault("gemLevels", {})["Clarity"] = level
        candidate = worker.request("calculate", xml=render())
        checks = validate_calculation(candidate)
        resource = sustained_resource_use(candidate.get("stats", {}), spec)
        value = candidate_score(candidate["stats"], spec)
        stats = candidate.get("stats", {})
        has_one_use = stats.get("ManaUnreserved", 0) >= stats.get("ManaCost", 0)
        calculated = bool(candidate.get("calculated") and stats)
        offense = offense_value(stats) > 0
        feasible = ((all(check["passed"] for check in checks) and has_one_use)
                    if require_full_legality else calculated and offense and has_one_use)
        failed_checks = [check["name"] for check in checks if not check["passed"]]
        if not has_one_use:
            failed_checks.append("Mana for one use")
        candidates.append({"level": level, "calc": candidate, "score": value,
                           "feasible": feasible,
                           "sustainable": bool(resource and resource.get("sustainable")),
                           "failedChecks": (failed_checks if require_full_legality else
                                            (["PoB calculation"] if not calculated else []) +
                                            (["Main skill offense"] if not offense else []) +
                                            (["Mana for one use"] if not has_one_use else [])),
                           "stats": candidate.get("stats", {})})
    spec["utility"] = original_utility
    if original_levels:
        spec["gemLevels"] = original_levels
    else:
        spec.pop("gemLevels", None)
    feasible = [entry for entry in candidates if entry["feasible"]]
    if not feasible:
        if trace is not None:
            trace.append({"kind": "resource_utility_search", "gem": "Clarity", "selected": False,
                          "evaluatedLevels": [entry["level"] for entry in candidates],
                          "failures": [{"level": entry["level"], "checks": entry["failedChecks"]}
                                       for entry in candidates],
                          "reason": "No legal Clarity level improved the resource utility candidate"})
        return calc
    # Before passives are optimized, keep the lowest reservation so the tree
    # search can spend points on sustain. Once the full design is stable, prefer
    # a level that actually supports repeated casts, then compare its score.
    best = (min(feasible, key=lambda entry: entry["level"])
            if not require_full_legality else
            max(feasible, key=lambda entry: (entry["sustainable"], entry["score"], -entry["level"])))
    improves_score = best["score"] > baseline_score + 0.001
    safely_lowers_auto_level = (original_clarity_level is not None and best["sustainable"] and
                                best["level"] < int(original_clarity_level) and
                                best["score"] >= baseline_score - 0.001)
    baseline_resource = sustained_resource_use(calc.get("stats", {}), spec)
    repairs_sustain = bool(best["sustainable"] and
                           not (baseline_resource and baseline_resource.get("sustainable", False)))
    baseline_can_cast = (calc.get("stats", {}).get("ManaUnreserved", 0) >=
                         calc.get("stats", {}).get("ManaCost", 0))
    repairs_one_cast = not baseline_can_cast
    if not improves_score and not safely_lowers_auto_level and not repairs_one_cast and not repairs_sustain:
        if trace is not None:
            trace.append({"kind": "resource_utility_search", "gem": "Clarity", "selected": False,
                          "evaluatedLevels": [entry["level"] for entry in candidates],
                          "reason": "No legal Clarity level improved the complete-build objective"})
        return calc
    spec.setdefault("utility", {})["Clarity"] = slot
    spec.setdefault("gemLevels", {})["Clarity"] = best["level"]
    spec["_automaticClarity"] = True
    spec["resourceUtilityReason"] = (
        f"Clarity level {best['level']} used in {slot}; mana recovery "
        f"{best['stats'].get('ManaRegen', 0):g}/s against estimated use "
        f"{best['stats'].get('ManaCost', 0) * best['stats'].get('Speed', 0):g}/s" +
        ("; restored repeated-cast mana sustain" if repairs_sustain else
         "; restored enough unreserved mana for one cast" if repairs_one_cast else ""))
    if trace is not None:
        trace.append({"kind": "resource_utility_search", "gem": "Clarity", "slot": slot,
                      "selectedLevel": best["level"], "selected": True,
                      "evaluatedLevels": [entry["level"] for entry in candidates],
                      "manaRegen": best["stats"].get("ManaRegen", 0),
                      "manaUse": best["stats"].get("ManaCost", 0) * best["stats"].get("Speed", 0),
                      "sustainable": best["sustainable"],
                      "reason": ("restored repeated-cast mana sustain" if repairs_sustain else
                                 "restored a payable main-skill cast" if repairs_one_cast else
                                 "best legal complete-build score; ties use the lowest mana reservation")})
    return best["calc"]


def choose_support_candidate(feasible: list[dict], spec: dict) -> tuple[dict, dict, bool]:
    """Choose a support while valuing sustained use across the finished link.

    Damage focus still favors offense, but a support that keeps a high fraction
    of the best damage while making repeated casts sustainable is a stronger
    complete-build choice. This selection is repeated after each link is added.
    """
    sustainability = {entry["id"]: sustained_resource_use(entry["stats"], spec) for entry in feasible}
    sustainable = [entry for entry in feasible
                   if sustainability[entry["id"]] is not None and
                   sustainability[entry["id"]]["sustainable"]]
    damage_best = max(target_dps(recounted_stats(entry["stats"], spec), spec) for entry in feasible)
    sustain_best = max((target_dps(recounted_stats(entry["stats"], spec), spec) for entry in sustainable),
                       default=0)
    sustain_damage_floor = {"damage": 0.70, "balanced": 0.65, "defense": 0.4}[spec["focus"]]
    sustainability_acceptable = bool(sustainable) and sustain_best >= damage_best * sustain_damage_floor
    selection_pool = sustainable if sustainability_acceptable else feasible
    def sustained_damage(entry):
        damage = target_dps(recounted_stats(entry["stats"], spec), spec)
        resource = sustainability[entry["id"]]
        if not resource or resource.get("sustainable"):
            return damage
        # If no support keeps repeated casting fully sustainable, estimate
        # long-run damage from the fraction of resource use covered by recovery.
        # Temporary summons already have their population-scaled damage.
        checks = resource.get("checks", [])
        if not checks:
            return damage
        coverage = min(max(0.0, check["availablePerSecond"]) /
                       max(0.1, check["usePerSecond"]) for check in checks)
        return damage * min(1.0, coverage)

    best = max(selection_pool, key=lambda entry: (
        sustained_damage(entry), target_dps(recounted_stats(entry["stats"], spec), spec), entry["id"]))
    return best, sustainability, sustainability_acceptable


def search_links(spec, data, render, worker, stage, trace=None, budget=None):
    supports = []
    for index in range(5):
        xml = render(supports)
        candidates = worker.request("supports", xml=xml)["supports"]
        names = []
        for identifier in candidates:
            gem = data.by_id.get(identifier)
            if (gem and gem["name"] not in supports and not gem["name"].startswith("Awakened ")
                    and gem["name"] not in {"Sacrifice", "Vaal Sacrifice", "Cast on Death"}
                    and (gem["name"] != "Decay" or spec["archetype"] == "dot")
                    and not gem["tags"].get("exceptional") and gem["maxLevel"] >= 20):
                names.append(identifier)
        if not names:
            break
        compatible_count = len(names)
        names = support_candidate_shortlist(names, data, spec)
        if budget is not None:
            # Preserve room for optional unique packages and final tree
            # refinement after the core links have been selected.
            available = budget.limit - budget.used - 200
            if available <= 1:
                budget.reserve_blocked = True
                break
            names = names[:available - 1]
        baseline = worker.request("calculate", xml=xml)["stats"]
        if budget is not None:
            budget.used += 1
        scored = []
        for offset in range(0, len(names), 20):
            stage(f"Scoring support {index + 1}/5: candidates {offset + 1}-{min(offset + 20, len(names))}/{len(names)}")
            batch = sorted(names)[offset:offset + 20]
            if budget is not None:
                budget.used += len(batch)
            scored.extend(worker.request("supportScores", xml=xml, candidates=batch)["candidates"])
        # Resource availability is an actual constraint, not an assumed buff.
        feasible = [entry for entry in scored if entry["stats"].get("ManaUnreserved", 0) >=
                    entry["stats"].get("ManaCost", 0) and
                    entry["stats"].get("LifeUnreserved", entry["stats"].get("Life", 0)) > 0]
        if not feasible:
            if trace is not None:
                trace.append({"kind": "support_selection", "link_index": index + 1,
                              "candidates": len(scored), "feasible": 0,
                              "reason": "no candidate passed one-use resource checks"})
            break
        best, sustainability, sustainability_acceptable = choose_support_candidate(feasible, spec)
        if trace is not None:
            trace.append({"kind": "support_selection", "link_index": index + 1,
                          "compatibleCandidates": compatible_count,
                          "shortlistedCandidates": len(scored),
                          "candidates": [{"name": data.by_id.get(entry["id"], {}).get("name", entry["id"]),
                                          "damage": target_dps(recounted_stats(entry["stats"], spec), spec),
                                          "feasible": entry in feasible,
                                          "sustainability": sustainability.get(entry["id"])} for entry in scored],
                          "sustainableCandidates": sum(bool(result and result["sustainable"])
                                                       for result in sustainability.values()),
                          "selectionRule": ("highest damage among sustained candidates within focus damage tolerance"
                                            if sustainability_acceptable else
                                            "highest resource-adjusted long-run damage among one-use feasible candidates"),
                          "selected": data.by_id[best["id"]]["name"]})
        # Fill a legal five-link even if the fourth support is chiefly utility.
        if len(supports) >= 4 and target_dps(recounted_stats(best["stats"], spec), spec) <= target_dps(
                recounted_stats(baseline, spec), spec):
            break
        supports.append(data.by_id[best["id"]]["name"])
    if len(supports) < 4:
        raise ValueError(f"PoB could not construct a compatible five-link for {spec['skill']}")
    return supports


def current_unique(text: str) -> str:
    """Resolve PoB's variant annotations to the last (current) variant."""
    variants = re.findall(r"(?m)^Variant:.*$", text)
    current = len(variants)
    result = []
    implicit_count = None
    implicit_lines = []
    for line in text.strip().splitlines():
        if line.startswith("Variant:") or line.startswith(("League:", "Source:", "Requires ")):
            continue
        match = re.match(r"\{variant:([\d,]+)\}(.*)", line)
        if match:
            if current not in {int(number) for number in match[1].split(",")}:
                # Variant-tagged implicit alternatives still count as one of
                # the database's implicit lines before we filter them.
                if implicit_count and implicit_count > 0:
                    implicit_count -= 1
                continue
            line = match[2]
        if line.startswith("Implicits:"):
            implicit_count = int(line.split(":")[1])
            result.append("__IMPLICITS__")
            continue
        line = re.sub(r"\{[^}]*\}", "", line)
        if implicit_count and implicit_count > 0:
            implicit_lines.append(roll_line(line))
            implicit_count -= 1
        else:
            result.append(roll_line(line))
    if "__IMPLICITS__" in result:
        pos = result.index("__IMPLICITS__")
        result[pos:pos + 1] = [f"Implicits: {len(implicit_lines)}", *implicit_lines]
    return "Rarity: UNIQUE\n" + "\n".join(result)


def unique_market_price(definition: dict, market: dict, links: int | None = None) -> float | None:
    variant = definition.get("selectedVariantLabel")
    if variant in {None, "Current"}:
        variant = None
    listings = market.get("listings", {}).get(definition.get("name"), [])
    matches = [entry for entry in listings
               if ((entry.get("variant") in {None, "Current"} if variant is None
                    else entry.get("variant") == variant))
               and (links is None or entry.get("links") == links)]
    return min((float(entry["chaos"]) for entry in matches if entry.get("chaos")), default=None)


def unique_package_within_budget(current_prices, addition_prices, budget: float | None) -> bool:
    """Require known variant/link quotes and enforce the cumulative package cap."""
    if budget is None:
        return True
    prices = [*current_prices, *addition_prices]
    return all(price is not None for price in prices) and sum(prices) <= budget


def unique_jewel_shortlist(unique_defs: list[dict], no_uniques: bool,
                           requested: set[str]) -> list[dict]:
    if no_uniques:
        return []
    jewels = [entry for entry in unique_defs if entry.get("type") == "Jewel" and entry.get("name")]
    jewels.sort(key=lambda entry: (entry.get("name") not in requested, entry.get("name", "")))
    if requested:
        return [entry for entry in jewels if entry.get("name") in requested]
    return jewels[:16]


def missing_unique_reason(name: str, unique_defs: list[dict], market: dict, budget: float | None) -> str:
    definition = next((entry for entry in unique_defs if entry.get("name") == name), {})
    if budget is not None:
        price = unique_market_price(definition, market)
        if price is None:
            return f"Requested unique '{name}' has no usable variant-specific market quote for the stated budget"
        if price > budget:
            return f"Requested unique '{name}' exceeds the stated budget ({price:g} chaos quoted)"
    return (f"Requested unique '{name}' is incompatible with the generated equipment slots or item "
            "requirements, or failed PoB legality checks")


def unique_options(context, spec, market, items, data, unique_defs=None):
    if spec["noUniques"]:
        return []
    options = []
    cap = spec["budgetChaos"]
    if unique_defs is None:
        unique_defs = []
        for path in (context["pobHome"] / "Data" / "Uniques").glob("*.lua"):
            for block in re.findall(r"\[\[(.*?)\]\]", path.read_text(encoding="utf-8"), re.S):
                try:
                    text = current_unique(block)
                    _, name, base_name = _item_parts(text)
                except ValueError:
                    continue
                base = data.bases.get(base_name)
                if base:
                    unique_defs.append({"name": name, "base": base_name, "type": base.get("type"),
                                        "subType": base.get("subType"), "raw": text})
    for definition in unique_defs:
        name, base_name = definition.get("name"), definition.get("base")
        if not name or not base_name:
            continue
        base = data.bases.get(base_name, {})
        for item in items:
            unique_type = definition.get("type", base.get("type"))
            unique_subtype = definition.get("subType", base.get("subType"))
            same_item_class = unique_type == item.definition.get("type")
            # Armor bases within a slot often have different subtypes (for
            # example, Close Helmet and Hubris Circlet). Weapon subtypes,
            # however, determine whether the requested weapon can be equipped.
            if item.slot == "Weapon 1":
                weapon_classes = {"Wand", "Bow", "Staff", "Claw", "Dagger", "One Handed Sword",
                                  "One Handed Axe", "One Handed Mace", "Sceptre"}
                if unique_type not in weapon_classes:
                    continue
                allowed = set(spec.get("weaponTypes") or ())
                if allowed and unique_type not in allowed and unique_subtype not in allowed:
                    continue
            elif not same_item_class:
                continue
            elif item.slot.startswith("Weapon") and unique_subtype != item.definition.get("subType"):
                continue
            max_sockets = min(6, base.get("socketLimit", item.definition.get("socketLimit", 0)))
            if item.slot == "Body Armour":
                sockets = min(spec.get("mainLinks", 6), max_sockets)
            elif item.slot in spec.get("utility", {}).values():
                sockets = min(spec.get("utilitySockets", 4), max_sockets,
                              4 if item.slot in {"Helmet", "Gloves", "Boots"} else 3)
            else:
                sockets = 0
            if item.slot == "Body Armour" and sockets < spec.get("mainLinks", 6):
                continue
            price = unique_market_price(definition, market, sockets if sockets > 0 else None)
            # A stated budget needs a variant/link-specific quote. Without a
            # budget, missing quotes leave the candidate visible as unknown.
            if cap is not None and (price is None or price > cap):
                continue
            raw = definition.get("raw") or current_unique(definition.get("source", ""))
            if sockets and not re.search(r"(?m)^Sockets:", raw):
                raw += "\nSockets: " + "-".join("B" for _ in range(sockets))
            options.append((name, item.slot, raw, price))
    return options


def flask_templates(data: GameData, level: int) -> list[RareItem]:
    """Provide flask slots to the unique search without scoring flask effects."""
    kinds = ("Life", "Mana", "Quicksilver", "Granite", "Quartz")
    templates = []
    for index, kind in enumerate(kinds, 1):
        choices = [(name, base) for name, base in data.bases.items()
                   if base.get("type") == "Flask" and
                   (name.endswith(" Life Flask") if kind == "Life" else
                    name.endswith(" Mana Flask") if kind == "Mana" else name == kind + " Flask") and
                   base_required_level(base) <= level]
        if not choices:
            choices = [(name, base) for name, base in data.bases.items()
                       if base.get("type") == "Flask" and name.endswith(" Life Flask") and
                       base_required_level(base) <= level]
        if not choices:
            raise ValueError("Installed flask definitions are missing")
        base_name, definition = max(choices, key=lambda row: (base_required_level(row[1]), row[0]))
        templates.append(RareItem(f"Flask {index}", base_name, definition,
                                  item_level=level, quality=0))
    return templates


def unique_is_two_handed(text: str, data: GameData) -> bool:
    try:
        _, _, base_name = _item_parts(text)
    except ValueError:
        return False
    tags = data.bases.get(base_name, {}).get("tags", {})
    return bool(tags.get("two_hand_weapon") or tags.get("twohanded"))


def unique_pair_has_weapon_conflict(first: tuple, second: tuple, data: GameData) -> bool:
    mainhand = first if first[1] == "Weapon 1" else second if second[1] == "Weapon 1" else None
    offhand = first if first[1] == "Weapon 2" else second if second[1] == "Weapon 2" else None
    return bool(mainhand and offhand and unique_is_two_handed(mainhand[2], data))


def mentioned_uniques(prompt: str, context: dict, unique_defs=None) -> list[str]:
    found = []
    def is_mentioned(name: str) -> bool:
        variants = [name]
        without_article = re.sub(r"^The\s+", "", name, flags=re.I)
        if without_article != name:
            variants.append(without_article)
        return any(re.search(r"(?<!\w)" + re.escape(variant) + r"(?!\w)", prompt, re.I)
                   for variant in variants)

    if unique_defs is not None:
        for definition in unique_defs:
            name = definition.get("name")
            if name and is_mentioned(name):
                found.append(name)
        return list(dict.fromkeys(found))
    home = context.get("pobHome")
    if home is None:
        return found
    for path in (home / "Data" / "Uniques").glob("*.lua"):
        for block in re.findall(r"\[\[(.*?)\]\]", path.read_text(encoding="utf-8"), re.S):
            try:
                _, name, _ = _item_parts(current_unique(block))
            except ValueError:
                continue
            if is_mentioned(name):
                found.append(name)
    return list(dict.fromkeys(found))


def validate_design(context, spec, allocated, masteries, items, uniques, data, jewels=None):
    nodes = context["tree"]["nodes"]
    connected = True
    for asc in (None, spec["ascendancy"]):
        subset = {key for key in allocated if not nodes[key].get("isMastery")
                  and nodes[key].get("ascendancyName") == asc}
        start = next((key for key in subset if nodes[key].get("isAscendancyStart")), None) if asc else next(
            (key for key in subset if nodes[key].get("classStartIndex") == 3), None)
        adjacency = graph(nodes, lambda node: node.get("ascendancyName") == asc and not node.get("isMastery"))
        # Restrict reachability to allocated nodes; unallocated travel nodes do
        # not make disconnected allocations legal.
        adjacency = {key: neighbors & subset for key, neighbors in adjacency.items() if key in subset}
        connected = connected and start is not None and subset <= paths_from({start}, adjacency).keys()
    groups = {nodes[key].get("group") for key in allocated if nodes[key].get("isNotable")}
    valid_masteries = (len(set(masteries.values())) == len(masteries) and all(
        key in allocated and nodes[key].get("isMastery") and nodes[key].get("group") in groups and
        any(effect.get("effect") == value for effect in nodes[key].get("masteryEffects", []))
        for key, value in masteries.items()))
    counts = {}
    for name, slot in spec["utility"].items():
        if name in data.gems:
            counts[slot] = counts.get(slot, 0) + 1
    socket_capacity = all(count <= next((min(4, item.definition.get("socketLimit", 0)) for item in items
                                         if item.slot == slot), 0) for slot, count in counts.items())
    legal_affixes = True
    for item in items:
        if item.slot in uniques:
            continue
        copy = type(item)(item.slot, item.base, item.definition)
        for mod in item.mods:
            legal_affixes = legal_affixes and copy.can_add(mod)
            copy.mods.append(mod)
    jewels = jewels or {}
    legal_jewels = True
    for node_id, jewel in jewels.items():
        if isinstance(jewel, str):
            try:
                _, _, base_name = _item_parts(jewel)
            except ValueError:
                legal_jewels = False
                continue
            definition = data.bases.get(base_name, {})
            legal_jewels = legal_jewels and node_id in allocated and bool(nodes.get(node_id, {}).get("isJewelSocket"))
            legal_jewels = legal_jewels and definition.get("type") == "Jewel" and bool(definition.get("tags", {}).get("jewel"))
            continue
        definition = data.bases.get(jewel.base, {})
        tags = definition.get("tags", {})
        legal_jewels = legal_jewels and node_id in allocated and bool(nodes.get(node_id, {}).get("isJewelSocket"))
        legal_jewels = legal_jewels and definition.get("type") == "Jewel" and bool(tags.get("jewel"))
        probe = RareItem("Jewel", jewel.base, definition, item_level=jewel.item_level, quality=jewel.quality)
        for mod in jewel.mods:
            legal_jewels = legal_jewels and probe.can_add(mod)
            probe.mods.append(mod)
    return [{"name": "Connected passive tree", "passed": bool(connected),
             "reason": "All regular and ascendancy paths connect through allocated nodes to their own start"},
            {"name": "Legal mastery effects", "passed": bool(valid_masteries),
             "reason": "Masteries require an allocated group notable and a distinct installed effect"},
            {"name": "Utility socket capacity", "passed": socket_capacity,
             "reason": "Utility groups must fit their equipped items' sockets"},
            {"name": "Legal rare affixes", "passed": legal_affixes,
             "reason": "Generated rares use eligible installed tiers, distinct groups and at most three prefixes/suffixes"},
            {"name": "Ordinary jewel sockets and affixes", "passed": legal_jewels,
             "reason": "Each rare jewel must use one allocated ordinary socket and eligible PoB jewel affixes"}]


def unique_pair_shortlist(unique_candidates, uniques, archetype, damage_type, limit=6):
    """Return deterministic different-slot unique packages, including synergies.

    Selected single-item uniques may anchor a package with a second item.
    Pairs of two new candidates are retained too, so complementary items are
    considered even when neither improved the all-rare design alone.
    """
    members = []
    for option in unique_candidates:
        name, slot, text, candidate_price = option
        selected_text = uniques.get(slot)
        if selected_text is not None and _item_parts(selected_text)[1] != name:
            continue
        members.append((*option, selected_text is not None))
    terms = (("minion", "reservation", "aura", "spell", "cast speed", "maximum life", "resistance")
             if archetype == "minion" else
             ("reservation", "aura", "spell", "cast speed", damage_type, "maximum life", "resistance"))
    pairs = []
    supported_interactions = {frozenset(("The Baron", "Shaper's Touch"))}
    for index, first in enumerate(members):
        for second in members[index + 1:]:
            if first[1] == second[1] or first[0] == second[0] or (first[4] and second[4]):
                continue
            relevance = sum(term in (first[0] + " " + first[2]).lower() for term in terms)
            relevance += sum(term in (second[0] + " " + second[2]).lower() for term in terms)
            interaction = frozenset((first[0], second[0])) in supported_interactions
            pairs.append((not interaction, -relevance, first[0], second[0], first, second))
    return [(first, second) for _, _, _, _, first, second in sorted(pairs)[:limit]]


def reconcile_unique_package_trace(trace: list | None, selected_names: set[str]) -> None:
    """Distinguish the best tested package seed from the final retained gear."""
    if trace is None:
        return
    for package in (entry for entry in trace if entry.get("kind") == "unique_package"
                    and entry.get("evaluated")):
        package["selected"] = all(name in selected_names for name in package["items"])
        if package["selected"]:
            package["reason"] = "package retained in the highest-scoring verified complete design"
        elif package.get("bestPackage"):
            package["reason"] = "best legal pair seed was tested but not retained in final complete-design refinement"


def tree_reroute_removals(nodes: dict, allocated: set[str], jewels: dict | None = None,
                          limit: int = 32) -> list[set[str]]:
    """Return connected branch removals that free points for a reroute.

    A removal includes every allocated regular-tree node disconnected by
    removing its branch point. Ascendancy allocations stay on their separate
    graph, and a branch containing an equipped jewel socket is preserved.
    """
    adjacency = graph(nodes, lambda node: not node.get("ascendancyName") and not node.get("isMastery")
                      and not node.get("isProxy")
                      and ("classStartIndex" not in node or node["classStartIndex"] == 3))
    regular = set(allocated) & adjacency.keys()
    start = next((key for key in regular if nodes[key].get("classStartIndex") == 3), None)
    if start is None:
        return []
    protected = set(jewels or {})
    unique = {}
    for key in regular - {start}:
        if nodes[key].get("isKeystone") or nodes[key].get("isJewelSocket"):
            continue
        remaining = regular - {key}
        restricted = {node: neighbors & remaining for node, neighbors in adjacency.items() if node in remaining}
        connected = set(paths_from({start}, restricted))
        removed = regular - connected
        if not removed or removed & protected:
            continue
        unique[frozenset(removed)] = removed
    return sorted(unique.values(), key=lambda removed: (len(removed), tuple(sorted(removed, key=int))))[:limit]


def complete_design_search(context, spec, data, worker, stage, trace, budget, state,
                           unique_options=(), gear_seeds=()):
    """Refine complete feasible builds in three bounded, deterministic sweeps.

    Every retained state includes its gear, links, tree, masteries, jewels and
    unique package. Neighbours are measured by PoB on that complete state;
    the shared SearchBudget counts each candidate evaluation.
    """
    width = 6
    nodes = context["tree"]["nodes"]
    adjacency = graph(nodes, lambda node: not node.get("ascendancyName") and not node.get("isMastery")
                      and not node.get("isProxy") and not node.get("isJewelSocket")
                      and ("classStartIndex" not in node or node["classStartIndex"] == 3))

    def clone(value):
        return copy.deepcopy(value)

    def render_state(candidate):
        old_count = spec.get("minionCount")
        if candidate.get("minion_count") is not None:
            spec["minionCount"] = candidate["minion_count"]
        try:
            return assemble(spec, context, data, candidate["nodes"], candidate["supports"],
                            candidate["items"], candidate["masteries"], candidate["uniques"],
                            candidate["jewels"])
        finally:
            if old_count is None:
                spec.pop("minionCount", None)
            else:
                spec["minionCount"] = old_count

    def signature(candidate):
        unique_gear = tuple((slot, text) for slot, text in sorted(candidate["uniques"].items()))
        rare_gear = tuple((item.slot, item.base,
                           tuple((mod.get("id"), tuple(mod.get("lines", ())), mod.get("kind"),
                                  mod.get("group")) for mod in item.mods))
                          for item in sorted(candidate["items"], key=lambda value: value.slot))
        links = tuple(candidate["supports"])
        tree = tuple(sorted(candidate["nodes"], key=int))
        masteries = tuple(sorted(candidate["masteries"].items()))
        jewels = tuple((slot, value if isinstance(value, str) else
                        (value.base, tuple((mod.get("id"), tuple(mod.get("lines", ())))
                                           for mod in value.mods)))
                       for slot, value in sorted(candidate["jewels"].items()))
        return unique_gear, rare_gear, links, tree, masteries, jewels

    unchanged = object()

    def state_score(output, candidate, baseline_count=unchanged):
        old_count = spec.get("minionCount")
        count = candidate.get("minion_count") if baseline_count is unchanged else baseline_count
        if count is None:
            spec.pop("minionCount", None)
        else:
            spec["minionCount"] = count
        try:
            return candidate_score(output, spec)
        finally:
            if old_count is None:
                spec.pop("minionCount", None)
            else:
                spec["minionCount"] = old_count

    def state_recount(output, baseline_count):
        old_count = spec.get("minionCount")
        if baseline_count is None:
            spec.pop("minionCount", None)
        else:
            spec["minionCount"] = baseline_count
        try:
            return recounted_stats(output, spec)
        finally:
            if old_count is None:
                spec.pop("minionCount", None)
            else:
                spec["minionCount"] = old_count

    def retain(candidates):
        distinct = {}
        for candidate in candidates:
            key = signature(candidate)
            if key not in distinct or candidate["score"] > distinct[key]["score"]:
                distinct[key] = candidate
        return sorted(distinct.values(), key=lambda value: (-value["score"], signature(value)))[:width]

    def update_population(candidate, calculation):
        old_count = spec.get("minionCount")
        if candidate.get("minion_count") is not None:
            spec["minionCount"] = candidate["minion_count"]
        changed = sync_permanent_minion_count(spec, calculation)
        candidate["minion_count"] = spec.get("minionCount")
        if changed:
            if not budget.claim():
                budget.exhausted = True
                spec["minionCount"] = old_count
                return None
            calculation = worker.request("calculate", xml=render_state(candidate))
        spec["minionCount"] = old_count
        return calculation

    def evaluate(candidate, reserve=0):
        if not budget.claim(reserve=reserve):
            return None
        finalist_xml = render_state(candidate)
        calculation = worker.request("calculate", xml=finalist_xml)
        calculation = update_population(candidate, calculation)
        if calculation is None:
            return None
        if not all(check["passed"] for check in validate_calculation(calculation)):
            return None
        candidate["calc"] = calculation
        candidate["score"] = state_score(calculation["stats"], candidate)
        return candidate

    def evaluate_gear(candidate, reserve=0):
        """Repair rare suffixes around a unique before judging legality."""
        if not budget.claim(reserve=reserve):
            return None
        calculation = worker.request("calculate", xml=render_state(candidate))
        calculation = update_population(candidate, calculation)
        if calculation is None:
            return None
        for _ in range(3):
            before = {item.slot: list(item.mods) for item in candidate["items"]}
            if not solve_suffixes(candidate["items"], data, calculation["stats"]):
                break
            if not budget.claim(reserve=reserve):
                for item in candidate["items"]:
                    item.mods = before[item.slot]
                break
            calculation = worker.request("calculate", xml=render_state(candidate))
            calculation = update_population(candidate, calculation)
            if calculation is None:
                return None
        if not all(check["passed"] for check in validate_calculation(calculation)):
            return None
        candidate["calc"] = calculation
        candidate["score"] = state_score(calculation["stats"], candidate)
        return candidate

    beam = retain([clone(state), *(clone(seed) for seed in gear_seeds)])
    # Gear candidates can each take several PoB imports while rare affixes are
    # repaired. Reserve fixed shares of the budget available at entry so that
    # those repairs cannot consume the link and passive-tree searches. The
    # tree gets half of the remaining budget because it is the final quality
    # pass and often needs multiple scored batches to use available points.
    resource_utility_reserve = len(mana_utility_levels(
        spec, data, state["calc"], state["items"], require_deficit=False))
    refinement_budget = max(0, budget.limit - budget.used - resource_utility_reserve)
    stage_reserves = {
        "gear": resource_utility_reserve + refinement_budget * 85 // 100,
        "links": resource_utility_reserve + refinement_budget * 60 // 100,
        "tree": resource_utility_reserve,
    }
    gear_names_tested: set[str] = set()
    gear_name_failures: dict[str, str] = {}
    for sweep, kind in enumerate(("gear", "links", "tree"), 1):
        future_reserve = stage_reserves[kind]
        budget_at_start = budget.used
        children = []
        stage(f"Complete-build refinement {sweep}/3: {kind} alternatives across up to {width} designs")
        if kind == "gear":
            # Filter per parent before applying its eight-candidate cap. The
            # shortlist can begin with uniques already equipped by the seed;
            # truncating globally would leave that beam design with no gear
            # neighbours even when other slots have eligible alternatives.
            options = list(unique_options)
            for parent in beam:
                tested = 0
                equipped_names = {_item_parts(text)[1] for text in parent["uniques"].values()}
                for name, slot, text, price in options:
                    if budget.limit - budget.used <= future_reserve:
                        break
                    if (tested >= 8 or name in spec.get("requestedUniques", ()) or name in equipped_names
                            or (slot in parent["uniques"] and
                                _item_parts(parent["uniques"][slot])[1] in spec.get("requestedUniques", ()))):
                        continue
                    primary = parent["uniques"].get("Weapon 1")
                    offhand = parent["uniques"].get("Weapon 2")
                    if ((slot == "Weapon 2" and primary and unique_is_two_handed(primary, data)) or
                            (slot == "Weapon 1" and offhand and unique_is_two_handed(text, data))):
                        gear_name_failures[name] = "incompatible with an equipped two-handed weapon and off-hand slot"
                        continue
                    if not unique_package_within_budget(parent.get("unique_prices", {}).values(),
                                                        [price], spec.get("budgetChaos")):
                        continue
                    candidate = clone(parent)
                    gear_names_tested.add(name)
                    candidate["uniques"][slot] = text
                    candidate.setdefault("unique_prices", {})[slot] = price
                    displaced = next((item for item in candidate["items"] if item.slot == slot), None)
                    if displaced is not None:
                        displaced.mods.clear()
                    release_repairable_suffixes(candidate["items"])
                    scored = evaluate_gear(candidate, future_reserve)
                    if scored is None:
                        gear_name_failures[name] = ("search budget exhausted before a valid complete calculation"
                                                    if budget.exhausted else
                                                    "candidate failed PoB legality or equipment repair")
                        if budget.exhausted or budget.limit - budget.used <= future_reserve:
                            break
                        continue
                    children.append(scored)
                    tested += 1
                if budget.exhausted:
                    break
        elif kind == "links":
            for parent in beam:
                tested = 0
                for index, old_support in enumerate(parent["supports"]):
                    if tested >= 4 or budget.limit - budget.used <= future_reserve:
                        break
                    base_links = parent["supports"][:index] + parent["supports"][index + 1:]
                    base_xml = render_state({**parent, "supports": base_links})
                    compatible = worker.request("supports", xml=base_xml)["supports"]
                    current_names = set(base_links)
                    ids = []
                    for identifier in compatible:
                        gem = data.by_id.get(identifier)
                        if (gem and gem["name"] not in current_names and
                                not gem["name"].startswith("Awakened ") and
                                gem["name"] not in {"Sacrifice", "Vaal Sacrifice", "Cast on Death"} and
                                not gem["tags"].get("exceptional")):
                            ids.append(identifier)
                    ids = sorted(set(ids))
                    # Explore supports that scored well during the initial
                    # link pass before falling back to alphabetical IDs. The
                    # old six-ID cap often excluded a strong support solely
                    # because its name sorted later (for example Minion
                    # Damage and Multistrike on SRS).
                    initial_links = [entry for entry in (trace or ())
                                     if entry.get("kind") == "support_selection"]
                    initial_candidates = (initial_links[index].get("candidates", ())
                                          if index < len(initial_links) else ())
                    ordered_ids = support_refinement_order(ids, data, initial_candidates, limit=24)
                    allowed = budget.claim(min(24, len(ordered_ids)), reserve=future_reserve)
                    if not allowed:
                        break
                    measured = worker.request("supportScores", xml=base_xml,
                                              candidates=ordered_ids[:allowed])["candidates"]
                    if not measured:
                        continue
                    if trace is not None:
                        names_by_id = [data.by_id[identifier]["name"] for identifier in ordered_ids[:allowed]]
                        trace.append({"kind": "complete_design_support_candidates",
                                      "parentSupport": old_support, "evaluated": names_by_id,
                                      "prioritizedResourceSupports": [name for name in names_by_id
                                                                       if any(term in name.casefold() for term in
                                                                              ("inspiration", "lifetap", "life tap",
                                                                               "blood magic", "mana cost",
                                                                               "mana efficiency", "unleash"))]})
                    preferred, _, _ = choose_support_candidate(measured, spec)
                    options_for_link = [preferred, *sorted(
                        (entry for entry in measured if entry is not preferred),
                        key=lambda entry: (
                            -state_score(entry["stats"], parent, parent.get("minion_count")), entry["id"]))]
                    for entry in options_for_link[:2]:
                        gem_name = data.by_id[entry["id"]]["name"]
                        trial = clone(parent)
                        trial["supports"] = [*base_links, gem_name]
                        trial["calc"] = {**parent["calc"],
                                          "stats": state_recount(entry["stats"], parent.get("minion_count"))}
                        trial["minion_count"] = candidate_minion_count(entry["stats"], spec) or parent.get("minion_count")
                        trial["score"] = state_score(entry["stats"], trial, parent.get("minion_count"))
                        if (entry["stats"].get("ManaUnreserved", 0) >= entry["stats"].get("ManaCost", 0)
                                and all(check["passed"] for check in validate_calculation(trial["calc"]))):
                            children.append(trial)
                            tested += 1
                            if tested >= 4:
                                break
                if budget.exhausted:
                    break
        else:
            for parent in beam:
                current = clone(parent)
                accepted_paths = 0
                accepted_reroutes = 0
                for _ in range(2):
                    if budget.limit - budget.used <= future_reserve:
                        break
                    hints = {**spec, "_defensesMet": tree_defenses_met(current["calc"]["stats"], spec)}
                    removals = tree_reroute_removals(nodes, current["nodes"], current["jewels"])
                    removals.sort(key=lambda removed: (
                        sum(heuristic(nodes[key], hints) for key in removed),
                        len(removed), tuple(sorted(removed, key=int))))
                    rerouted = False
                    for removed in removals[:3]:
                        reduced_nodes = current["nodes"] - removed
                        base_paths = paths_from(reduced_nodes, adjacency)
                        mastery_nodes_removed = {key for key in current["masteries"]
                                                 if key not in reduced_nodes or
                                                 nodes.get(key, {}).get("group") not in {
                                                     nodes[value].get("group") for value in reduced_nodes
                                                     if nodes.get(value, {}).get("isNotable")}}
                        available_points = (current["calc"]["passives"]["maximum"] -
                                            current["calc"]["passives"]["used"] + len(removed) +
                                            len(mastery_nodes_removed))
                        candidates = []
                        for key, path in base_paths.items():
                            node = nodes[key]
                            if (not path or len(path) > available_points or set(path) & removed or
                                    node.get("isKeystone") or any(nodes[value].get("isKeystone") for value in path)):
                                continue
                            hint = sum(heuristic(nodes[value], hints) for value in path) / len(path)
                            if hint > 0 and (node.get("isNotable") or len(path) == 1 or
                                             heuristic(node, hints) >= 3):
                                candidates.append((hint, key, path))
                        shortlist = sorted(candidates, key=lambda row: (-row[0], int(row[1])))[:8]
                        allowed = budget.claim(min(len(shortlist), 8), reserve=future_reserve)
                        if not allowed:
                            break
                        shortlist = shortlist[:allowed]
                        measured = worker.request("nodes", xml=render_state({
                            **current, "nodes": reduced_nodes}), candidates=[
                                {"id": key, "nodes": path} for _, key, path in shortlist])["candidates"]
                        path_by_key = {key: path for _, key, path in shortlist}
                        best = max(measured, key=lambda row: (
                            state_score(row["stats"], current, current.get("minion_count")), -int(row["id"])),
                                   default=None)
                        if (best is None or state_score(best["stats"], current, current.get("minion_count"))
                                <= current["score"] + 0.001):
                            continue
                        trial = clone(current)
                        trial["nodes"] = reduced_nodes | set(path_by_key[best["id"]])
                        trial["nodes"] -= mastery_nodes_removed
                        trial["masteries"] = {key: effect for key, effect in trial["masteries"].items()
                                               if key not in mastery_nodes_removed}
                        trial["minion_count"] = candidate_minion_count(best["stats"], spec) or current.get(
                            "minion_count")
                        evaluated = evaluate(trial, future_reserve)
                        if evaluated is None or evaluated["score"] <= current["score"] + 0.001:
                            continue
                        current = evaluated
                        accepted_reroutes += 1
                        rerouted = True
                        if trace is not None:
                            trace.append({"kind": "complete_design_tree_reroute",
                                          "removedNodes": sorted(removed, key=int),
                                          "addedPath": path_by_key[best["id"]],
                                          "passives": current["calc"]["passives"]["used"],
                                          "score": round(current["score"], 6),
                                          "reason": "replaced a low-value connected branch with a higher-scoring legal path"})
                        break
                    if not rerouted:
                        break
                # Eight path additions often leave a high level character
                # with many justified points unspent. Keep searching the
                # candidate tree until its score stops improving or the
                # shared per-sweep budget is exhausted.
                while accepted_paths < 24:
                    used = current["calc"]["passives"]["used"]
                    maximum = current["calc"]["passives"]["maximum"]
                    if used >= maximum:
                        break
                    paths = paths_from(current["nodes"], adjacency)
                    hints = {**spec, "_defensesMet": tree_defenses_met(current["calc"]["stats"], spec)}
                    candidates = []
                    for key, path in paths.items():
                        node = nodes[key]
                        if (not path or used + len(path) > maximum or node.get("isKeystone") or
                                any(nodes[value].get("isKeystone") for value in path)):
                            continue
                        hint = sum(heuristic(nodes[value], hints) for value in path) / len(path)
                        if hint > 0 and (node.get("isNotable") or len(path) == 1 or
                                         heuristic(node, hints) >= 3):
                            candidates.append((hint, key, path))
                    shortlist = sorted(candidates, key=lambda row: (-row[0], int(row[1])))[:72]
                    allowed = budget.claim(min(12, len(shortlist)), reserve=future_reserve)
                    if not allowed:
                        break
                    shortlist = shortlist[:allowed]
                    measured = worker.request("nodes", xml=render_state(current), candidates=[
                        {"id": key, "nodes": path} for _, key, path in shortlist])["candidates"]
                    paths_by_key = {key: path for _, key, path in shortlist}
                    best = max(measured, key=lambda row: (
                        state_score(row["stats"], current, current.get("minion_count")), -int(row["id"])),
                               default=None)
                    if best is None or state_score(best["stats"], current, current.get("minion_count")) <= current["score"] + 0.001:
                        break
                    path = paths_by_key[best["id"]]
                    current["nodes"].update(path)
                    current["calc"] = {**current["calc"],
                                      "stats": state_recount(best["stats"], current.get("minion_count")),
                                      "passives": {**current["calc"]["passives"], "used": used + len(path)}}
                    current["minion_count"] = candidate_minion_count(best["stats"], spec) or current.get("minion_count")
                    current["score"] = state_score(best["stats"], current, current.get("minion_count"))
                    accepted_paths += 1
                if accepted_paths or accepted_reroutes:
                    children.append(current)
                    if trace is not None:
                        trace.append({"kind": "complete_design_tree_refinement",
                                      "reroutes": accepted_reroutes,
                                      "passivePathsAdded": accepted_paths,
                                      "passives": current["calc"]["passives"]["used"],
                                      "score": round(current["score"], 6)})
                if budget.exhausted:
                    break
        beam = retain([*beam, *children])
        if trace is not None:
            trace.append({"kind": "complete_design_sweep", "sweep": sweep, "dimension": kind,
                          "candidates": len(children), "retained": len(beam),
                          "bestScore": round(beam[0]["score"], 6),
                          "budgetAtStart": budget_at_start,
                          "evaluations": budget.used,
                          "reservedForLaterSweeps": future_reserve,
                          "reason": "retained the six highest-scoring distinct complete build designs"})
    # Recalculate all retained finalists so passive/support overrides and
    # population rescaling cannot outrank a better complete PoB import.
    # Preserve the current and pre-optional-gear seed for final verification.
    # Batched overrides can be internally legal while a subsequent full PoB
    # import exposes interactions they did not model; refinements must never
    # erase the pre-search equipment design from verification.
    finalists = retain(beam)
    for fallback in [state, *gear_seeds[:1]]:
        if signature(fallback) not in {signature(candidate) for candidate in finalists}:
            finalists.append(clone(fallback))
    verified = []
    old_count = spec.get("minionCount")
    for candidate in finalists:
        if candidate.get("minion_count") is None:
            spec.pop("minionCount", None)
        else:
            spec["minionCount"] = candidate["minion_count"]
        finalist_xml = render_state(candidate)
        calculation = calculate_finalist(worker, finalist_xml, len(candidate["nodes"]), trace)
        if sync_permanent_minion_count(spec, calculation):
            candidate["minion_count"] = spec.get("minionCount")
            finalist_xml = render_state(candidate)
            calculation = calculate_finalist(worker, finalist_xml, len(candidate["nodes"]), trace)
        final_checks = validate_calculation(calculation)
        if (not all(check["passed"] for check in final_checks)
                and any(check["name"] in {"Dex requirements", "Str requirements", "Int requirements",
                                          "Elemental resistances"} and not check["passed"]
                        for check in final_checks)
                and solve_suffixes(candidate["items"], data, calculation["stats"])):
            finalist_xml = render_state(candidate)
            calculation = calculate_finalist(worker, finalist_xml, len(candidate["nodes"]), trace)
            if sync_permanent_minion_count(spec, calculation):
                candidate["minion_count"] = spec.get("minionCount")
                finalist_xml = render_state(candidate)
                calculation = calculate_finalist(worker, finalist_xml, len(candidate["nodes"]), trace)
            final_checks = validate_calculation(calculation)
        if not all(check["passed"] for check in final_checks):
            if trace is not None:
                failed_root = ET.fromstring(finalist_xml)
                tree_spec = failed_root.find("./Tree/Spec")
                trace.append({"kind": "complete_design_finalist_rejected",
                              "supports": list(candidate["supports"]),
                              "uniques": {slot: _item_parts(text)[1]
                                          for slot, text in candidate["uniques"].items()},
                              "nodeCount": len(candidate["nodes"]),
                              "nodeIds": sorted(candidate["nodes"], key=int),
                              "rareAffixes": {item.slot: [mod.get("id") for mod in item.mods]
                                              for item in candidate["items"]},
                              "passives": calculation.get("passives", {}).get("used"),
                              "stats": {key: calculation.get("stats", {}).get(key) for key in
                                        ("Life", "EnergyShield", "Str", "Dex", "Int", "FireResist",
                                         "ColdResist", "LightningResist")},
                              "xmlTreeVersion": tree_spec.get("treeVersion") if tree_spec is not None else None,
                              "xmlClassId": tree_spec.get("classId") if tree_spec is not None else None,
                              "xmlAscendancyId": tree_spec.get("ascendClassId") if tree_spec is not None else None,
                              "xmlNodes": tree_spec.get("nodes") if tree_spec is not None else None,
                              "xml": finalist_xml,
                              "failedChecks": [check for check in final_checks if not check["passed"]]})
            continue
        candidate["calc"] = calculation
        candidate["minion_count"] = spec.get("minionCount")
        candidate["score"] = candidate_score(calculation["stats"], spec)
        verified.append(candidate)
        if trace is not None:
            trace.append({"kind": "complete_design_finalist", "score": round(candidate["score"], 6),
                          "supports": list(candidate["supports"]),
                          "uniques": {slot: _item_parts(text)[1] for slot, text in candidate["uniques"].items()},
                          "passives": calculation["passives"]["used"], "verified": True})
    if old_count is None:
        spec.pop("minionCount", None)
    else:
        spec["minionCount"] = old_count
    if not verified:
        raise ValueError("Complete-design refinement produced no valid final candidate")
    best = sorted(verified, key=lambda candidate: (-candidate["score"], signature(candidate)))[0]
    if trace is not None:
        selected_names = {_item_parts(text)[1] for text in best["uniques"].values()}
        for name, slot, _text, price in unique_options:
            selected = name in selected_names
            tested = name in gear_names_tested
            trace.append({"kind": "complete_design_unique_result", "name": name, "slot": slot,
                          "selected": selected, "evaluated": tested, "quotedPrice": price,
                          "reason": ("retained in the highest-scoring verified complete design" if selected else
                                     gear_name_failures.get(name) if tested else
                                     "not evaluated because the bounded search budget or per-design shortlist was exhausted")})
    return best


def build_design(spec, context, market, app_root, data_root, stage, data=None, trace=None):
    worker = get_worker(app_root, data_root)
    initial_calls = worker.calls
    initial_export_recoveries = getattr(worker, "export_recoveries", 0)
    budget = SearchBudget(2000)
    data = data or GameData(worker.request("metadata"))
    allocated = initial_nodes(context, spec)
    items = rare_templates(data, spec["archetype"], spec["weaponType"],
                           damage_type=spec["damageType"],
                           base_damage_type=spec.get("baseDamageType", spec["damageType"]),
                           focus=spec["focus"])
    items.extend(flask_templates(data, spec["level"]))
    supports, masteries, uniques = [], {}, {}
    jewels: dict[str, RareItem] = {}
    requested_uniques = set(spec.get("requestedUniques", []))
    if requested_uniques and spec["noUniques"]:
        raise ValueError("The request asks for a unique item and rares-only equipment at the same time")

    def render(nodes=None, links=None, mastery=None, unique=None, jewelset=None):
        return assemble(spec, context, data, allocated if nodes is None else nodes,
                        supports if links is None else links, items,
                        masteries if mastery is None else mastery, uniques if unique is None else unique,
                        jewels if jewelset is None else jewelset)

    # Required equipment is installed as a package before any tree, support,
    # or optional-unique optimization. This lets later PoB searches build
    # around the requested gear instead of evaluating it as a late swap.
    unique_defs = data.unique_items
    if unique_defs is None:
        unique_defs = worker.request("uniques").get("items", [])
    unique_options_all = unique_options(context, spec, market, items, data, unique_defs)
    required_equipment = [name for name in requested_uniques if not any(
        entry.get("name") == name and entry.get("type") == "Jewel" for entry in unique_defs)]
    unique_prices = {}
    if required_equipment:
        package_slots = set()
        for name in sorted(required_equipment):
            candidates = [option for option in unique_options_all
                          if option[0] == name and option[1] not in package_slots]
            if not candidates:
                reason = missing_unique_reason(name, unique_defs, market, spec["budgetChaos"])
                raise ValueError(f"Could not equip requested unique item '{name}': {reason}")
            candidate = candidates[0]
            candidate_name, slot, text, candidate_price = candidate
            if spec["budgetChaos"] is not None:
                if not unique_package_within_budget(unique_prices.values(), [candidate_price],
                                                    spec["budgetChaos"]):
                    if candidate_price is None or any(price is None for price in unique_prices.values()):
                        raise ValueError(f"Requested unique '{name}' has no usable variant-specific market quote")
                    package_subtotal = sum(price for price in unique_prices.values() if price is not None)
                    if package_subtotal + candidate_price > spec["budgetChaos"]:
                        raise ValueError(
                            f"Requested unique package exceeds the stated budget: {package_subtotal:g} + "
                            f"{candidate_price:g} chaos for '{name}' is over {spec['budgetChaos']:g}")
            uniques[slot] = text
            unique_prices[slot] = candidate_price
            package_slots.add(slot)
            displaced = next((item for item in items if item.slot == slot), None)
            if displaced is not None:
                displaced.mods.clear()
            if trace is not None:
                trace.append({"kind": "required_unique_package", "name": candidate_name,
                              "slot": slot, "quoted_price": candidate_price,
                              "selected": True,
                              "reason": "installed before passive, link and optional-equipment search"})
        if ("Weapon 1" in uniques and "Weapon 2" in uniques and
                unique_is_two_handed(uniques["Weapon 1"], data)):
            raise ValueError("Requested unique package combines a two-handed Weapon 1 with an incompatible Weapon 2 item")
        release_repairable_suffixes(items)

    stage("Solving equipment resistances and attributes with installed modifier tiers")
    budget.claim()
    calc = worker.request("calculate", xml=render())
    if sync_permanent_minion_count(spec, calc):
        budget.claim()
        calc = worker.request("calculate", xml=render())
    for _ in range(3):
        if not solve_suffixes(items, data, calc["stats"]):
            break
        if not budget.claim():
            break
        calc = worker.request("calculate", xml=render())
    # Lock a legal, scored main link before passive search. The tree objective
    # must see the same damage mechanism and support costs as the final design.
    supports = search_links(spec, data, lambda links: render(links=links), worker, stage,
                            trace=trace, budget=budget)
    if budget.claim():
        calc = worker.request("calculate", xml=render())
    # Support requirements can add attributes; repair them before evaluating
    # passive overrides so every tree candidate uses legal final links.
    for _ in range(3):
        if not solve_suffixes(items, data, calc["stats"]):
            break
        if not budget.claim():
            break
        calc = worker.request("calculate", xml=render())
    if sync_permanent_minion_count(spec, calc):
        if budget.claim():
            calc = worker.request("calculate", xml=render())
        else:
            budget.exhausted = True
    if mana_utility_levels(spec, data, calc, items):
        stage("Adding a legal mana-sustain utility before passive optimization")
        calc = search_mana_utility(spec, data, items, render, worker, calc, budget, trace,
                                   reserve=FINAL_REFINEMENT_RESERVE, require_full_legality=False)
    stage("Building a connected passive tree and scoring clusters in PoB")
    # Preserve room for equipment and tree refinement after this linked tree pass.
    allocated, calc = search_tree(context, spec, allocated, lambda nodes: render(nodes=nodes), worker, stage,
                                  trace=trace, budget=budget, baseline_calc=calc,
                                  budget_reserve=FINAL_REFINEMENT_RESERVE)
    if sync_permanent_minion_count(spec, calc):
        if not budget.claim():
            budget.exhausted = True
        else:
            calc = worker.request("calculate", xml=render())
    jewel_templates = ordinary_jewel_templates(data, spec, spec["level"])
    if jewel_templates:
        stage("Searching reachable ordinary jewel sockets with legal rare affixes")
    allocated, jewels, calc = search_ordinary_jewels(
        context, spec, allocated, jewels, jewel_templates,
        lambda nodes=None, jewelset=None: render(nodes=nodes, jewelset=jewelset),
        worker, calc, budget, trace)
    if sync_permanent_minion_count(spec, calc):
        if budget.claim():
            calc = worker.request("calculate", xml=render())
        else:
            budget.exhausted = True
    stage("Scoring available mastery effects")
    used_effects = set()
    candidates = mastery_choices(context, allocated)
    candidates.sort(key=lambda entry: -heuristic(context["tree"]["nodes"][entry[0]], spec))
    for node, effects in candidates:
        if budget.limit - budget.used <= FINAL_REFINEMENT_RESERVE:
            # Keep enough shared budget for the complete gear/link/tree pass.
            break
        if calc["passives"]["used"] >= calc["passives"]["maximum"]:
            break
        best = None
        # Score up to eight likely effects; the final XML calculation verifies
        # mastery legality and point consumption, including duplicate effects.
        ranked = sorted(effects, key=lambda effect: -heuristic({"stats": effect.get("stats", [])}, spec))[:8]
        for effect in ranked:
            if not budget.claim():
                break
            identifier = effect.get("effect")
            if identifier in used_effects:
                continue
            candidate = worker.request("calculate", xml=render(nodes=allocated | {node},
                                        mastery={**masteries, node: identifier}))
            if candidate["passives"]["used"] > candidate["passives"]["maximum"]:
                continue
            if candidate_score(candidate["stats"], spec) > score(calc["stats"], spec) + 0.001:
                if best is None or candidate_score(candidate["stats"], spec) > score(best[1]["stats"], spec):
                    best = (identifier, candidate)
        if best:
            masteries[node], calc = best
            used_effects.add(best[0])
            allocated.add(node)
            if trace is not None:
                trace.append({"kind": "mastery_selection", "node": node,
                              "effect": best[0], "reason": "best compatible PoB score"})
    stage("Comparing affordable uniques with generated equipment")
    unique_candidates = unique_options(context, spec, market, items, data, unique_defs)
    required_candidates = [option for option in unique_candidates if option[0] in requested_uniques]
    optional_by_slot = {}
    for option in unique_candidates:
        if option[0] not in requested_uniques:
            optional_by_slot.setdefault(option[1], []).append(option)
    # Keep optional equipment search bounded and representative across slots;
    # explicit requests always remain in the shortlist.
    optional_candidates = []
    for slot, entries in sorted(optional_by_slot.items()):
        ranked = sorted(entries, key=lambda option: (
            -sum(term in option[2].lower() for term in
                 (("minion", "spell", "cast speed", spec["damageType"], "maximum life", "resistance")
                  if spec["archetype"] == "minion" else
                  ("spell", "cast speed", "critical strike", spec["damageType"], "damage", "maximum life", "resistance"))),
            option[0]))
        optional_candidates.extend(ranked[:2])
    supported_interactions = []
    if spec["archetype"] == "minion" and spec["skill"] == "Raise Zombie":
        for names in (("The Baron", "Shaper's Touch"),):
            interaction = []
            for name in names:
                if name in requested_uniques:
                    continue
                option = next((entry for entry in unique_candidates if entry[0] == name), None)
                if option is not None:
                    if not any(entry[0] == name for entry in optional_candidates):
                        optional_candidates.append(option)
                    interaction.append(name)
            if len(interaction) == len(names):
                supported_interactions.append(list(names))
    unique_candidates = sorted(required_candidates, key=lambda option: (option[0], option[1])) + optional_candidates
    # Keep the best feasible design before optional unique swaps as a full
    # gear-beam seed. Required user uniques, if any, stay installed.
    all_rare_seed = {"nodes": set(allocated), "supports": list(supports),
                     "items": copy.deepcopy(items), "masteries": dict(masteries),
                     "uniques": dict(uniques), "unique_prices": dict(unique_prices),
                     "jewels": copy.deepcopy(jewels), "minion_count": spec.get("minionCount"),
                     "calc": calc, "score": candidate_score(calc["stats"], spec)}
    if trace is not None:
        trace.append({"kind": "unique_shortlist", "requested": [entry[0] for entry in required_candidates],
                      "optionalTested": len(optional_candidates),
                      "supportedInteractions": supported_interactions,
                      "reason": "all requested compatible slot candidates plus up to two optional candidates per equipment slot"})
    for name, slot, text, candidate_price in unique_candidates:
        if name in {_item_parts(value)[1] for value in uniques.values()}:
            continue
        # Keep enough of the shared limit for complete-build refinement after
        # optional singles, pairs and jewels. Explicitly requested items are
        # mandatory and may consume that reserve when needed.
        # Keep a larger slice for complete two-item packages. Without this,
        # standalone unique repairs can consume the exact reserve that pair
        # evaluation subsequently requires.
        package_reserve = 0 if name in requested_uniques else FINAL_REFINEMENT_RESERVE
        if not budget.claim(reserve=package_reserve):
            if trace is not None:
                trace.append({"kind": "search_limit", "phase": "unique_search", "used": budget.used})
            break
        if slot in uniques:
            continue
        if not unique_package_within_budget(unique_prices.values(), [candidate_price], spec["budgetChaos"]):
            continue
        snapshots = {item.slot: list(item.mods) for item in items}
        previous_minion_count = spec.get("minionCount")
        displaced = next((item for item in items if item.slot == slot), None)
        if displaced is not None:
            displaced.mods.clear()
        trial_uniques = {**uniques, slot: text}
        candidate = worker.request("calculate", xml=render(unique=trial_uniques))
        if sync_permanent_minion_count(spec, candidate):
            if budget.claim(reserve=package_reserve):
                candidate = worker.request("calculate", xml=render(unique=trial_uniques))
            else:
                budget.exhausted = True
        for _ in range(3):
            if not solve_suffixes(items, data, candidate["stats"]):
                break
            if not budget.claim(reserve=package_reserve):
                budget.exhausted = True
                break
            candidate = worker.request("calculate", xml=render(unique=trial_uniques))
        checks = validate_calculation(candidate)
        improves = score(candidate["stats"], spec) > score(calc["stats"], spec)
        accepted = all(check["passed"] for check in checks) and (name in requested_uniques or improves)
        if trace is not None:
            trace.append({"kind": "unique_candidate", "name": name, "slot": slot,
                          "requested": name in requested_uniques, "evaluated": True,
                          "selected": accepted,
                          "quoted_price": candidate_price,
                          "score_delta": round(score(candidate["stats"], spec) - score(calc["stats"], spec), 6),
                          "failed_checks": [check["name"] for check in checks if not check["passed"]],
                          "reason": ("requested item passed legality checks" if accepted and name in requested_uniques else
                                     "improved objective and passed legality checks" if accepted else
                                     "failed legality checks" if not all(check["passed"] for check in checks) else
                                     "did not improve objective")})
        if accepted:
            uniques = trial_uniques
            unique_prices[slot] = candidate_price
            calc = candidate
        else:
            unique_prices.pop(slot, None)
            if previous_minion_count is not None:
                spec["minionCount"] = previous_minion_count
            for item in items:
                item.mods = snapshots[item.slot]
    # Some uniques are useful only as a package: a reservation-enabling item
    # can make a high-cost aura setup viable, or two items can compensate for
    # each other's displaced rare affixes. Test a bounded, deterministic set
    # of full PoB equipment pairs, including pairs anchored by a selected
    # requested/single-item unique.
    pair_candidates = unique_pair_shortlist(unique_candidates, uniques, spec["archetype"],
                                            spec["damageType"], limit=14)
    pair_candidates = [(first, second) for first, second in pair_candidates
                       if not unique_pair_has_weapon_conflict(first, second, data)]
    package_best = None
    pair_design_seeds = []
    for pair_index, (first, second) in enumerate(pair_candidates):
        additions = [member for member in (first, second) if not member[4]]
        if not unique_package_within_budget(unique_prices.values(),
                                            [member[3] for member in additions], spec["budgetChaos"]):
            if trace is not None:
                trace.append({"kind": "unique_package", "items": [first[0], second[0]],
                              "slots": [first[1], second[1]], "evaluated": False,
                              "eligible": False, "selected": False,
                              "quoted_prices": [first[3], second[3]],
                              "reason": "package has an unknown item quote or exceeds the cumulative budget"})
            continue
        if not budget.claim(5, reserve=250):
            if trace is not None:
                for pending_first, pending_second in pair_candidates[pair_index:]:
                    trace.append({"kind": "unique_package", "items": [pending_first[0], pending_second[0]],
                                  "slots": [pending_first[1], pending_second[1]], "evaluated": False,
                                  "eligible": False, "selected": False,
                                  "quoted_prices": [pending_first[3], pending_second[3]],
                                  "reason": "not evaluated because the bounded search budget was reserved for final complete-build refinement"})
            break
        snapshots = {item.slot: list(item.mods) for item in items}
        previous_minion_count = spec.get("minionCount")
        trial_uniques = dict(uniques)
        trial_prices = dict(unique_prices)
        for member in additions:
            name, slot, text, candidate_price, _ = member
            trial_uniques[slot] = text
            trial_prices[slot] = candidate_price
            displaced = next((item for item in items if item.slot == slot), None)
            if displaced is not None:
                displaced.mods.clear()
        release_repairable_suffixes(items)
        candidate = worker.request("calculate", xml=render(unique=trial_uniques))
        if sync_permanent_minion_count(spec, candidate):
            candidate = worker.request("calculate", xml=render(unique=trial_uniques))
        for _ in range(3):
            if not solve_suffixes(items, data, candidate["stats"]):
                break
            candidate = worker.request("calculate", xml=render(unique=trial_uniques))
        checks = validate_calculation(candidate)
        delta = score(candidate["stats"], spec) - score(calc["stats"], spec)
        accepted = all(check["passed"] for check in checks) and delta > 0.001
        package_trace = {"kind": "unique_package", "items": [first[0], second[0]],
                          "slots": [first[1], second[1]], "evaluated": True,
                          "eligible": accepted, "selected": False, "bestPackage": False,
                          "quoted_prices": [first[3], second[3]], "score_delta": round(delta, 6),
                          "failed_checks": [check["name"] for check in checks if not check["passed"]],
                          "reason": "complete two-item package improved the PoB objective and passed legality checks"
                          if accepted else "package was not a legal objective improvement"}
        if trace is not None:
            trace.append(package_trace)
        if accepted and (package_best is None or delta > package_best[0]):
            package_best = (delta, candidate, trial_uniques, trial_prices,
                            {item.slot: list(item.mods) for item in items}, spec.get("minionCount"), package_trace)
        if accepted:
            pair_design_seeds.append({
                "nodes": set(allocated), "supports": list(supports),
                "items": copy.deepcopy(items), "masteries": dict(masteries),
                "uniques": dict(trial_uniques), "unique_prices": dict(trial_prices),
                "jewels": copy.deepcopy(jewels), "minion_count": spec.get("minionCount"),
                "calc": candidate, "score": candidate_score(candidate["stats"], spec),
            })
        for item in items:
            item.mods = snapshots[item.slot]
        if previous_minion_count is not None:
            spec["minionCount"] = previous_minion_count
    if package_best:
        _, calc, uniques, unique_prices, accepted_mods, accepted_minion_count, chosen_trace = package_best
        chosen_trace["bestPackage"] = True
        for item in items:
            item.mods = accepted_mods[item.slot]
        if accepted_minion_count is not None:
            spec["minionCount"] = accepted_minion_count
    # Unique jewels with direct item modifiers can occupy ordinary allocated sockets.
    # Radius/tree transformations need a separately modeled tree and are never guessed.
    # Explicit requirements are tested first and alone. Optional catalog search
    # is bounded to keep the full design budget available to meaningful choices.
    jewel_unique_defs = unique_jewel_shortlist(unique_defs, spec["noUniques"], requested_uniques)
    for definition in jewel_unique_defs:
        name = definition["name"]
        raw = definition.get("raw") or ""
        special = re.search(r"(?i)radius|allocat(?:e|es|ed|ing)|transforms?|keystone|passive skills? in", raw)
        if special:
            if name in requested_uniques:
                raise ValueError(f"Requested unique jewel '{name}' needs unsupported passive-tree transformation mechanics")
            continue
        socket = next((key for key in sorted(allocated, key=int)
                       if context["tree"]["nodes"].get(key, {}).get("isJewelSocket")), None)
        if socket is None:
            if name in requested_uniques:
                raise ValueError(f"Requested unique jewel '{name}' needs an available allocated ordinary jewel socket")
            continue
        jewel_quote = unique_market_price(definition, market)
        if not unique_package_within_budget(unique_prices.values(), [jewel_quote], spec["budgetChaos"]):
            if name in requested_uniques:
                raise ValueError(f"Requested unique jewel '{name}' has no usable quote or exceeds the cumulative budget")
            continue
        try:
            text = raw if raw.startswith("Rarity: UNIQUE") else current_unique(raw)
            _, _, jewel_base = _item_parts(text)
        except ValueError:
            if name in requested_uniques:
                raise ValueError(f"Requested unique jewel '{name}' has no supported current PoB variant")
            continue
        if jewel_base not in data.bases or data.bases[jewel_base].get("type") != "Jewel":
            if name in requested_uniques:
                raise ValueError(f"Requested unique jewel '{name}' does not fit an ordinary jewel socket")
            continue
        previous = jewels.get(socket)
        jewels[socket] = text
        if not budget.claim(reserve=0 if name in requested_uniques else 350):
            if previous is None:
                jewels.pop(socket, None)
            else:
                jewels[socket] = previous
            break
        candidate = worker.request("calculate", xml=render())
        previous_minion_count = spec.get("minionCount")
        if sync_permanent_minion_count(spec, candidate):
            if budget.claim():
                candidate = worker.request("calculate", xml=render())
            else:
                spec["minionCount"] = previous_minion_count
                if previous is None:
                    jewels.pop(socket, None)
                else:
                    jewels[socket] = previous
                budget.exhausted = True
                break
        checks = validate_calculation(candidate)
        delta = score(candidate["stats"], spec) - score(calc["stats"], spec)
        accepted = all(check["passed"] for check in checks) and (name in requested_uniques or delta > 0.001)
        if trace is not None:
            trace.append({"kind": "unique_jewel_candidate", "name": name, "node": socket,
                          "requested": name in requested_uniques, "quoted_price": jewel_quote,
                          "score_delta": round(delta, 6), "selected": accepted,
                          "reason": "requested or improved objective and passed legality checks" if accepted else
                                   "failed legality checks" if not all(check["passed"] for check in checks) else
                                   "did not improve objective"})
        if accepted:
            jewels[socket], calc = text, candidate
            unique_prices[f"Jewel {socket}"] = jewel_quote
        else:
            if previous_minion_count is not None:
                spec["minionCount"] = previous_minion_count
            if previous is None:
                jewels.pop(socket, None)
            else:
                jewels[socket] = previous
    selected_unique_names = {_item_parts(value)[1] for value in uniques.values()}
    selected_unique_names.update(_item_parts(value)[1] for value in jewels.values() if isinstance(value, str))
    missing_requested = requested_uniques - selected_unique_names
    if missing_requested:
        reasons = [missing_unique_reason(name, unique_defs, market, spec["budgetChaos"])
                   for name in sorted(missing_requested)]
        raise ValueError("Could not equip requested unique item(s): " + " | ".join(reasons))
    # Refine distinct complete gear/link/tree designs together. Two-item
    # packages scored above remain alternative beam seeds instead of being
    # discarded as soon as the locally best pair is chosen.
    base_state = {"nodes": set(allocated), "supports": list(supports),
                  "items": copy.deepcopy(items), "masteries": dict(masteries),
                  "uniques": dict(uniques), "unique_prices": dict(unique_prices),
                  "jewels": copy.deepcopy(jewels), "minion_count": spec.get("minionCount"),
                  "calc": calc, "score": candidate_score(calc["stats"], spec)}
    for seed in pair_design_seeds:
        seed["nodes"] = set(allocated)
        seed["supports"] = list(supports)
        seed["masteries"] = dict(masteries)
        seed["jewels"] = copy.deepcopy(jewels)
    if any(_item_parts(value)[1] in requested_uniques for value in jewels.values() if isinstance(value, str)):
        all_rare_seed["jewels"] = copy.deepcopy(jewels)
    all_rare_seed["nodes"] = set(allocated)
    all_rare_seed["supports"] = list(supports)
    all_rare_seed["masteries"] = dict(masteries)
    best_design = complete_design_search(context, spec, data, worker, stage, trace, budget,
                                         base_state, optional_candidates,
                                         [all_rare_seed, *pair_design_seeds])
    allocated = best_design["nodes"]
    supports = best_design["supports"]
    items = best_design["items"]
    masteries = best_design["masteries"]
    uniques = best_design["uniques"]
    unique_prices = best_design.get("unique_prices", unique_prices)
    spec["unmodeledFlaskEffects"] = sorted({_item_parts(text)[1] for slot, text in uniques.items()
                                               if slot.startswith("Flask ")})
    jewels = best_design["jewels"]
    calc = best_design["calc"]
    if mana_utility_levels(spec, data, calc, items):
        stage("Testing Clarity levels to sustain repeated mana use")
        calc = search_mana_utility(spec, data, items, render, worker, calc, budget, trace)
    reconcile_unique_package_trace(trace, {_item_parts(text)[1] for text in uniques.values()})
    if best_design.get("minion_count") is not None:
        spec["minionCount"] = best_design["minion_count"]
    xml = render()
    stage("Validating the exact generated XML in Path of Building")
    # Share a real PoB save, including computed stats required by pobb.in.
    # Reimport checks the exact exported XML before validation/fingerprinting.
    try:
        calc = export_with_pob(xml, app_root, data_root)
    except Exception as exc:
        if trace is not None:
            trace.append({"kind": "exact_export_failure", "error": f"{type(exc).__name__}: {exc}",
                          "xml": xml})
        raise
    xml = calc.pop("xml")
    checks, details = validate_structure(xml, context, spec["ascendancy"], spec["skill"])
    checks.extend(validate_calculation(calc))
    checks.extend(validate_design(context, spec, allocated, masteries, items, uniques, data, jewels))
    checks.append({"name": "Usable main skill resources", "passed": calc["stats"].get("ManaUnreserved", 0) >=
                   calc["stats"].get("ManaCost", 0), "reason": "Unreserved mana must cover the main skill's cost"})
    compatibility = worker.request("supports", xml=xml)["supports"]
    checks.append({"name": "Support compatibility", "passed": all(data.gem(name)["id"] in compatibility
                   for name in supports), "reason": "PoB's skill-type expressions must accept every selected support"})
    failures = [check["name"] + ": " + check["reason"] for check in checks if not check["passed"]]
    if failures:
        raise ValueError("Generated build failed validation: " + " | ".join(failures))
    if trace is not None:
        trace.append({"kind": "progression_input", "xml": xml})
    # Start progression from a fresh engine state. The preceding support and
    # unique candidate batches can leave PoB's derived enemy placeholders in
    # its persistent calc cache, changing only defensive outputs on reimport.
    prior_calls = worker.calls
    close_worker()
    worker = get_worker(app_root, data_root)
    worker.calls = prior_calls
    xml, progression = add_progression(xml, spec, context, data, worker, stage)
    stage("Exporting and verifying the complete campaign-to-endgame PoB")
    expected_endgame = progression[-1].pop("_calculation")
    final = export_with_pob(xml, app_root, data_root)
    xml = final.pop("xml")
    final_changes = {key: {"progression": expected_endgame.get(key), "export": final.get(key)}
                     for key in set(expected_endgame) | set(final)
                     if expected_endgame.get(key) != final.get(key)}
    if final_changes:
        # The final PoB save can materialize its combat placeholders after the
        # merged-loadout calculation. Keep the exact-export calculation as the
        # reported result and validate that authoritative saved build below.
        progression[-1]["finalExportStatChanges"] = final_changes
        progression[-1]["stats"] = final.get("stats", {})
        progression[-1]["passives"] = final.get("passives", {}).get("used", 0)
    calc = final
    structural, details = validate_structure(xml, context, spec["ascendancy"], spec["skill"])
    structural.append({"name": "Five equipped flasks", "passed": flasks_complete(xml, data, spec["level"]),
                       "reason": "The active exported endgame item set must resolve five level-legal flask items"})
    if any(not check["passed"] for check in structural):
        raise ValueError("Complete build failed final structural validation")
    final_calculation_checks = validate_calculation(calc)
    checks = [check for check in checks if check["name"] not in
              ({entry["name"] for entry in structural} | {entry["name"] for entry in final_calculation_checks})]
    checks[:0] = structural
    checks.extend(final_calculation_checks)
    checks.append({"name": "Campaign to endgame progression", "passed": True,
                   "reason": f"{len(progression)} matched loadouts; every stage calculated and checked for levels, points, sockets, attributes, resistances and mana"})
    price = quote(details["gear"], market, spec["budgetChaos"] if spec["budgetChaos"] is not None else 10_000_000)
    if spec["budgetChaos"] is None:
        price["budgetChaos"] = None
        price["budgetStatus"] = "not specified; unpriced slots remain" if price["unknown"] else "not specified"
    if spec["budgetChaos"] is not None and price["pricedSubtotalChaos"] > spec["budgetChaos"]:
        raise ValueError("Priced equipment alone exceeds the requested budget")
    unspent_points = max(0, calc["passives"]["maximum"] - calc["passives"]["used"])
    passive_reason = (f"{unspent_points} points remain; the 2,000-evaluation limit stopped further refinements."
                      if budget.exhausted else
                      f"{unspent_points} points remain because no measured positive candidate survived pruning.")
    profile = mechanic_profile(spec)
    selected_ascendancy = sorted({context["tree"]["nodes"][node].get("name", "")
                                  for node in allocated
                                  if context["tree"]["nodes"].get(node, {}).get("ascendancyName") == spec["ascendancy"]
                                  and context["tree"]["nodes"].get(node, {}).get("isNotable")
                                  and context["tree"]["nodes"][node].get("name")})
    important_passives = []
    for node_id in sorted(allocated, key=lambda value: int(value)):
        node = context["tree"]["nodes"].get(node_id, {})
        if (node.get("ascendancyName") or not (node.get("isNotable") or node.get("isKeystone"))
                or not node.get("name")):
            continue
        stats = "; ".join(node.get("stats", [])[:2])
        important_passives.append(f"{node['name']}: {stats}" if stats else node["name"])
    unique_selection_reasons = [
        (f"Required unique flask {item['name']} equipped in {item['slot']}; effect disabled in conservative PoB calculations because flask uptime is not modeled"
         if item["rarity"] == "unique" and item["slot"].startswith("Flask ") and
         item["name"] in requested_uniques else
         f"Required unique {item['name']} equipped in {item['slot']} to honor the request"
         if item["name"] in requested_uniques else
         f"Optional unique {item['name']} selected in {item['slot']} after PoB scoring")
        for item in details["gear"] if item["rarity"] == "unique"]
    recipe = {"generation": "from-game-data", "treeChange": f"Generated {calc['passives']['used']} paid passives from the Witch start",
              "unspentPoints": unspent_points,
              "changedMainLinks": supports,
              "changedSlots": ([item.slot for item in items if not item.slot.startswith("Flask ")] +
                               [slot for slot in uniques if slot.startswith("Flask ")]),
              "masteries": masteries, "modelReason": "Generated and scored in real PoB; rare templates and gems remain unpriced.",
              "mechanics": {**profile, "damageType": spec["damageType"]},
              "selectionReasons": {"ascendancy": ("Selected from the requested skill tags and explicit prompt choices" +
                                                     (": " + ", ".join(selected_ascendancy) if selected_ascendancy else ".")),
                                   "passives": "PoB-calculated candidate gains; neutral leaves pruned; " + passive_reason,
                                   "importantPassives": important_passives[:12],
                                   "utility": ([spec["resourceUtilityReason"]]
                                               if spec.get("resourceUtilityReason") else []),
                                   "uniques": unique_selection_reasons,
                                   "jewels": [f"Rare jewel selected in passive socket {node} after PoB scoring"
                                              for node in sorted(jewels, key=int)]},
              "assumptions": ([f"All {spec['minionCount']} permanent zombies active"] if spec["skill"] == "Raise Zombie" and spec.get("minionCount") else
                              [f"Estimated {spec['minionCount']} sustainable temporary spirits from PoB duration, cast rate, mana cost and regeneration"]
                              if spec["skill"] == "Summon Raging Spirit" and spec.get("minionCount") else []),
              "constraints": spec, "evaluations": worker.calls - initial_calls,
              "exportRecoveries": getattr(worker, "export_recoveries", 0) - initial_export_recoveries,
              "unmodeledFlaskEffects": list(spec.get("unmodeledFlaskEffects", [])),
              "designEvaluations": budget.used,
              "searchLimitWarning": (f"Search stopped at {budget.used}/{budget.limit} design evaluations after preserving mandatory search budget; best feasible candidate retained"
                                     if budget.exhausted else None)}
    recipe["progression"] = progression
    return xml, recipe, checks, details, calc, price


def generate(request: dict, app_root: Path, data_root: Path, stage) -> dict:
    prompt = str(request.get("prompt", "")).strip()
    if not 12 <= len(prompt) <= 2000:
        raise ValueError("Describe the build in 12-2000 characters")
    model = str(request.get("model") or DEFAULT_MODEL)
    stage("Checking current league, tree and installed PoB data")
    context = game_context()
    market = market_data(context["league"])
    data = GameData(get_worker(app_root, data_root).request("metadata"))
    data.unique_items = get_worker(app_root, data_root).request("uniques").get("items", [])
    stage(f"Parsing build intent with {model}")
    spec = parse_intent(prompt, model, data, market)
    spec["requestedUniques"] = mentioned_uniques(prompt, context, data.unique_items)
    xml, recipe, checks, details, calculation, price = build_design(
        spec, context, market, app_root, data_root, stage, data)
    mechanics = recipe.get("mechanics") or mechanic_profile(spec)
    recipe["mechanics"] = mechanics
    mechanic_checks = assess_mechanics(spec, calculation, mechanics, xml, context)
    quality_status, quality_warnings = assess_quality(
        spec, calculation, mechanics, mechanic_checks, bool(recipe.get("searchLimitWarning")))
    return {"id": "g" + secrets.token_hex(10), "name": spec["skill"] + " " + spec["ascendancy"],
            "class": "Witch", "ascendancy": spec["ascendancy"], "mainSkill": spec["skill"],
            "level": details["level"], "gems": details["gems"], "treeNodes": details["treeNodes"],
            "ascendancyPoints": details["ascendancyPoints"], "gear": details["gear"],
            "validation": checks, "mechanicChecks": mechanic_checks,
            "qualityStatus": quality_status, "qualityWarnings": quality_warnings,
            "stats": calculation["stats"], "pobVersion": calculation.get("version"),
            "quote": price, "recipe": recipe, "modelUsed": model, "prompt": prompt,
            "modelIntent": f"{spec['focus'].capitalize()} focus with {spec['skill']}; links and passive clusters scored in PoB.",
            "progression": recipe.get("progression", []),
            "league": context["league"], "treeVersion": context["treeVersion"],
            "officialTreeRelease": context["officialRelease"], "createdAt": int(time.time()),
            "shareStatus": "pending", "shareUrl": None, "_xml": xml, "_fingerprint": mechanics_fingerprint(xml)}
