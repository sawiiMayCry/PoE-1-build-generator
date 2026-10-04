"""Prompt -> validated intent -> deterministic, PoB-scored generated build."""
from __future__ import annotations

import copy
import os
import json
import re
import secrets
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from build_contracts import Check, EvaluationResult
from loadout_summary import loadout_view
from unique_pricing import entry_from_text, resolve_unique
from build_evaluation import (active_item_set, active_skill_set, active_tree_spec as resolve_tree_spec,
                              final_quality_report, package_report, equipped_items, tier_targets)
from build_assembly import assemble, disable_conditional_item_skill_groups, SocketConflict
from build_progression import add_progression, flasks_complete
from build_generator import (_item_parts, mechanics_fingerprint, offense_value, quote,
                             validate_calculation, validate_structure)
from generation_data import (GameData, RareItem, base_required_level, rare_templates,
                             roll_line, solve_suffixes, support_is_noncombat)
import tree_sanity
import unique_policy
from unique_policy import (apply_default_budget, budget_report, gain_per_cost, policy_summary,
                           worth_price)
from ollama_service import DEFAULT_MODEL, ask_json
from passive_search import (SearchBudget, jewel_socket_paths, tree_pool_target, paid_use_rate, CHANNEL_CYCLE_SECONDS, graph, paths_from, initial_nodes, mastery_choices,
                            candidate_score, recounted_stats, score, search_tree, heuristic,
                            tree_defenses_met, candidate_minion_count,
                            temporary_minion_population, sustained_resource_use)
from mechanics import (delivery as mechanic_delivery, is_dot_skill, mechanic_profile, minion_model, skill_tags)
from pob_engine import close_worker, export_with_pob, get_worker
from services import game_context, market_data

FINAL_REFINEMENT_RESERVE = 820
MASTERY_REFINEMENT_RESERVE = 120
KEYSTONE_REFINEMENT_RESERVE = 80
DESIGN_EVALUATION_LIMIT = 2700   # was 2300; the wider unique search (about +400 evaluations) must not starve the tree passes
JEWEL_SEARCH_SHARE = 260   # evaluations protected from the tree pass for jewel packages


def mastery_reserve_for(budget: SearchBudget) -> int:
    """Keep a mastery share only when the configured budget can support it."""
    return min(MASTERY_REFINEMENT_RESERVE, max(0, budget.limit - FINAL_REFINEMENT_RESERVE))


def keystone_reserve_for(budget: SearchBudget) -> int:
    """Keep a keystone share after reserving mastery and final-refinement work."""
    remaining = budget.limit - FINAL_REFINEMENT_RESERVE - mastery_reserve_for(budget)
    return min(KEYSTONE_REFINEMENT_RESERVE, max(0, remaining))


def keystone_package_candidates(context: dict, spec: dict, calc: dict, data: GameData,
                                items: list[RareItem], allocated: set[str], masteries: dict | None = None,
                                jewels: dict | None = None) -> list[tuple[str, str, list[str], str, list[str], dict]]:
    """Build reachable keystone packages, freeing a connected branch when needed."""
    nodes = context["tree"]["nodes"]
    stats = calc.get("stats", {})
    adjacency = graph(nodes, lambda node: not node.get("ascendancyName") and not node.get("isMastery")
                      and not node.get("isProxy") and not node.get("isJewelSocket")
                      and ("classStartIndex" not in node or node["classStartIndex"] == 3))
    reachable = paths_from(allocated, adjacency)
    tags = data.gem(spec["skill"])["tags"]
    remaining_points = (calc.get("passives", {}).get("maximum", 0) -
                        calc.get("passives", {}).get("used", 0))
    masteries = dict(masteries or {})
    shield = any(item.slot == "Weapon 2" and item.definition.get("type") == "Shield" for item in items)
    compatible = {
        "Elemental Overload": (spec.get("archetype") != "minion" and
                                spec.get("damageType") in {"fire", "cold", "lightning"},
                                "elemental hit/ailment mechanism; exact PoB result checked"),
        "Resolute Technique": (spec.get("archetype") == "attack",
                               "attack mechanism; exact PoB hit and damage result checked"),
        "Point Blank": (bool(tags.get("projectile") and tags.get("attack")),
                        "projectile attack mechanism; exact PoB range configuration retained"),
        "Mind Over Matter": (spec.get("focus") == "defense" and
                             float(stats.get("ManaUnreserved", 0) or 0) >= 500,
                             "defensive intent with a substantial unreserved mana pool"),
        "Eldritch Battery": (spec.get("focus") == "defense" and
                             float(stats.get("EnergyShield", 0) or 0) >= 500,
                             "defensive intent with energy shield available to fund skills"),
        "Necromantic Aegis": (spec.get("archetype") == "minion" and shield,
                              "minion build with an equipped shield whose stats transfer to minions"),
        "Zealot's Oath": (spec.get("focus") == "defense" and
                          float(stats.get("EnergyShield", 0) or 0) > 0 and
                          float(stats.get("LifeRegen", 0) or 0) > 0,
                          "defensive energy-shield build with life regeneration to redirect"),
    }
    result = []
    for key, path in reachable.items():
        node = nodes.get(key, {})
        if not node.get("isKeystone") or not path:
            continue
        if any(nodes.get(value, {}).get("isKeystone") for value in path if value != key):
            continue
        name = node.get("name", "")
        rule = compatible.get(name)
        if rule and rule[0]:
            needed = max(0, len(path) - remaining_points)
            if not needed:
                result.append((name, key, path, rule[1], [], masteries))
                continue
            packages_added = 0
            for removed in tree_reroute_removals(nodes, allocated, jewels, limit=48):
                if any(nodes.get(value, {}).get("isKeystone") for value in removed):
                    continue
                remaining_tree = allocated - set(removed)
                rerouted_path = paths_from(remaining_tree, adjacency).get(key)
                if not rerouted_path or any(nodes.get(value, {}).get("isKeystone") for value in rerouted_path[:-1]):
                    continue
                remaining_groups = {nodes[value].get("group") for value in remaining_tree
                                    if nodes[value].get("isNotable")}
                lost_masteries = {mastery_node for mastery_node in masteries
                                  if mastery_node not in remaining_tree or
                                  nodes.get(mastery_node, {}).get("group") not in remaining_groups}
                reroute_needed = max(0, len(rerouted_path) - remaining_points)
                if len(removed) + len(lost_masteries) < reroute_needed:
                    continue
                updated_masteries = {mastery_node: effect for mastery_node, effect in masteries.items()
                                     if mastery_node not in lost_masteries}
                result.append((name, key, rerouted_path, rule[1],
                               sorted(removed, key=int), updated_masteries))
                packages_added += 1
                if packages_added >= 2:
                    break
    return sorted(result, key=lambda entry: (len(entry[2]), len(entry[4]), entry[0], int(entry[1])))


# Skill data, not logic: non-fire skills that the Elementalist's Shaper of Flames turns into an ignite
# build unless the prompt asks for hits. Other skills default to their own hit mechanism.
ELEMENTALIST_IGNITE_DEFAULT_SKILLS = {"Ethereal Knives", "Penance Brand", "Wave of Conviction"}

# Item data, not logic: unique pairs whose combined effect is specific to one skill's own mods
# (The Baron adds zombies per Strength; Shaper's Touch supplies the Strength). The generator
# still keeps them only if PoB scores the pair.
UNIQUE_INTERACTIONS_BY_SKILL = {"Raise Zombie": (("The Baron", "Shaper's Touch"),)}


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
    if explicit is None:
        # A prompt that names only a utility/buff skill (an offering, aura, curse) as the build's
        # skill must fail clearly instead of silently substituting a different skill.
        for rejected in sorted((n for n, entry in data.gems.items() if not entry.get("support")
                                and n not in data.main_names), key=len, reverse=True):
            match = re.search(r"(?<!\w)" + re.escape(rejected) + r"(?!\w)", prompt, re.I)
            if match and not re.search(r"(?i)\b(?:with|using|and|plus|use)\s+(?:the\s+)?$",
                                       prompt[:match.start()]):
                raise ValueError(
                    f"{rejected} is a buff or utility skill with no damage of its own, so it cannot be the "
                    "build's main skill. Name a damage skill; utilities can be requested alongside it, "
                    f"for example 'Raise Spectre Necromancer with {rejected}'.")
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
    if asc == "Elementalist" and gem["name"] in ELEMENTALIST_IGNITE_DEFAULT_SKILLS:
        ignite = ignite or not re.search(r"\b(hit[- ]based|non[- ]ignite|no ignite)\b", prompt, re.I)
    archetype = ("minion" if tags.get("minion") else "attack" if tags.get("attack") else
                 "ignite" if ignite else "dot" if is_dot_skill(gem["name"], tags) else "spell")
    explicit_poison = bool(re.search(r"\b(poison|poisoning)\b", prompt, re.I))
    base_damage = damage
    damage_mechanism = ("ignite" if archetype == "ignite" else "damage_over_time" if archetype == "dot" else
                        "poison" if explicit_poison else "minion_hit" if archetype == "minion" else
                        "attack_hit" if archetype == "attack" else "spell_hit")
    if explicit_poison:
        damage = "chaos"
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
    default_curse = next((name for name in utility if data.gems.get(name, {}).get("tags", {}).get("curse")), None)
    requested_curses = [name for name in requested_utilities
                        if data.gems.get(name, {}).get("tags", {}).get("curse")]
    if requested_curses and default_curse not in requested_curses:
        curse_slot = utility.pop(default_curse, "Gloves") if default_curse else "Gloves"
    else:
        curse_slot = utility.get(default_curse, "Gloves") if default_curse else "Gloves"
    for name in requested_utilities:
        utility[name] = ("Helmet" if data.gem(name)["tags"].get("aura") else
                         curse_slot if name in requested_curses else "Boots")
    expected_curse = next((name for name in utility if data.gems.get(name, {}).get("tags", {}).get("curse")), None)
    spec = {"skill": gem["name"], "skillTags": sorted(tag for tag, on in tags.items() if on),
            "channelled": bool(tags.get("channelling") or tags.get("channeling")),
            "ascendancy": asc, "level": level, "focus": focus,
            "damageType": "fire" if ignite else damage, "baseDamageType": base_damage,
            "damageMechanism": damage_mechanism, "resourceReserveFraction": 0.15,
            "expectedCurse": expected_curse,
            "curseSelectionReason": "explicitly requested" if requested_curses else "damage-type-compatible default",
            "archetype": archetype,
            "budgetChaos": budget_from_prompt(prompt, market.get("divineChaos") or unique_policy.FALLBACK_DIVINE_CHAOS),
            "weaponType": weapon,
            "weaponTypes": allowed_weapon_types,
            "utility": utility, "noUniques": bool(re.search(
                r"\bno[\s-]+uniques?\b|\brares?(?:[\s-]+(?:items?|gear|equipment))?[\s-]+only\b",
                prompt, re.I)),
            "requestedUtilities": requested_utilities,
            "intent": str(reply.get("intent", ""))[:350]}
    apply_default_budget(spec, market)
    # These mechanics require dedicated gear/tree recipes. Fail specifically
    # instead of producing a different build which happens to pass numeric gates.
    spec["minionModel"] = minion_model(spec)
    spec["mechanicDelivery"] = mechanic_delivery(spec)
    spec["defenseModel"] = ("ci" if re.search(r"\bchaos inoculation\b|\bCI\b", prompt, re.I) else "hybrid")
    for pattern, label in ((r"\blow[- ]life\b", "low life"), (r"\bward loop\b", "ward loop")):
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


CHAOS_RESISTANCE_FLOOR = 0
CHAOS_REPAIR_SHARE = 10      # evaluations funded for the final chaos-resistance gear repair


def chaos_target(spec: dict) -> int | None:
    """Chaos resistance is repaired like elemental resistance, except under Chaos Inoculation
    (the character is immune, so affixes would be wasted)."""
    return None if spec.get("defenseModel") == "ci" else CHAOS_RESISTANCE_FLOOR


def repair_chaos_resistance(spec, data, items, render, worker, budget, calc, trace):
    """Final hard repair of chaos resistance on the finished gear, with its own funded share.

    The generic solver already tries to meet the floor at every gear change, but later tree, unique
    and jewel decisions can move it. This pass measures the deficit on the final design and fills it
    with legal chaos-resistance affixes on rare items; if the free affix slots cannot reach the floor
    it records exactly how far short the build is and why.
    """
    target = chaos_target(spec)
    if target is None:
        return calc
    budget.extend("chaos_repair", CHAOS_REPAIR_SHARE)
    share = budget.share("chaos_repair")
    before = float(calc["stats"].get("ChaosResist", 0) or 0)
    start = budget.used
    summary = {"floor": target, "before": before, "iterations": 0, "added": 0}
    for _ in range(4):
        if float(calc["stats"].get("ChaosResist", 0) or 0) >= target:
            break
        added = solve_suffixes(items, data, calc["stats"], chaos_target=target)
        if not added or not budget.claim():
            break
        summary["added"] += added
        summary["iterations"] += 1
        calc = worker.request("calculate", xml=render())
    summary["after"] = float(calc["stats"].get("ChaosResist", 0) or 0)
    if summary["after"] < target:
        free = {item.slot: 3 - sum(m["kind"] == "Suffix" for m in item.mods)
                for item in items if not item.slot.startswith("Flask ")}
        summary["reason"] = ("no rare item has a free suffix with an eligible chaos-resistance tier; free suffix slots: "
                             + ", ".join(f"{slot} {n}" for slot, n in free.items() if n > 0))
    share["used"] += budget.used - start
    spec["chaosRepair"] = summary
    if trace is not None:
        trace.append({"kind": "chaos_resistance_repair", **summary})
    return calc


PANTHEON_MAJORS = ("TheBrineKing", "Lunaris", "Solaris", "Arakaali")
PANTHEON_MINORS = ("Gruthkul", "Yugul", "Abberath", "Tukohama", "Garukhan", "Ralakesh", "Ryslatha", "Shakari")
PANTHEON_LABEL = {"TheBrineKing": "Soul of the Brine King", "Lunaris": "Soul of Lunaris", "Solaris": "Soul of Solaris",
                  "Arakaali": "Soul of Arakaali", "Gruthkul": "Soul of Gruthkul", "Yugul": "Soul of Yugul",
                  "Abberath": "Soul of Abberath", "Tukohama": "Soul of Tukohama", "Garukhan": "Soul of Garukhan",
                  "Ralakesh": "Soul of Ralakesh", "Ryslatha": "Soul of Ryslatha", "Shakari": "Soul of Shakari"}
PANTHEON_SHARE = 14          # evaluations funded for the Pantheon comparison (4 majors + 8 minors + final)
MIN_PANTHEON_EHP_GAIN = 0.005


def pantheon_rule(spec: dict, stats: dict) -> tuple[str, str, str]:
    """Documented fallback when PoB's calculation cannot separate the gods (their best effects are
    conditional on being hit, on enemy counts, or on ailments the configuration does not apply).

    Major: Soul of the Brine King. Its stun-recovery, stun/freeze avoidance and chill reduction cover the
    crowd-control gap of an ES/life caster or minion owner, who has no stun avoidance of their own.
    Minor: Soul of Shakari when chaos resistance is the weakest layer (5% reduced chaos damage taken and
    poison protection), otherwise Soul of Gruthkul (physical damage reduction after being hit).
    """
    chaos = float(stats.get("ChaosResist", 0) or 0)
    if spec.get("defenseModel") != "ci" and chaos < 40:
        minor = ("Shakari", f"chaos resistance is only {chaos:.0f}%: Shakari reduces chaos damage taken")
    else:
        minor = ("Gruthkul", "physical damage reduction after being hit")
    return "TheBrineKing", minor[0], ("Brine King: stun/freeze avoidance for a character without its own; "
                                      f"Minor {PANTHEON_LABEL[minor[0]]}: {minor[1]}")


def settle_reservations(spec, render, worker, budget, calc, trace):
    """Final consistency check: the exported plan must leave mana for the main skill.

    Later tree passes (refill, keystone packages) can undo the mana a repair bought. When the finished
    build no longer pays for its reservations, the last reserving package is dropped, one at a time, and
    the omission is recorded, instead of exporting a build that cannot cast its main skill.
    """
    groups = spec.get("skillGroups")
    if not groups:
        return calc
    dropped = []
    while (float(calc["stats"].get("ManaUnreserved", 0) or 0) < float(calc["stats"].get("ManaCost", 0) or 0)
           and budget.claim()):
        victim = next((group for group in reversed(groups) if group["role"] in {"aura", "defense", "herald"}
                       and not group["id"].startswith("fill_")), None)
        if victim is None:
            break
        actives = [gem for gem in victim["gems"] if gem["kind"] == "active"]
        if len(actives) > 1:
            gem = actives[-1]
            victim["gems"].remove(gem)
            victim["mainActive"] = actives[0]["name"] if victim.get("mainActive") == gem["name"] else victim.get("mainActive")
            dropped.append(gem["name"])
        else:
            groups.remove(victim)
            dropped.append(actives[0]["name"] if actives else victim["id"])
        spec["skillGroups"] = groups
        calc = worker.request("calculate", xml=render())
    if dropped:
        spec["reservationsDropped"] = dropped
        summary = spec.get("skillPlanSummary")
        if summary is not None:
            summary.setdefault("omissions", []).extend(
                {"package": name, "role": "aura",
                 "reason": "dropped after the final tree: unreserved mana no longer covered the main skill"}
                for name in dropped)
        if trace is not None:
            trace.append({"kind": "reservations_settled", "dropped": dropped,
                          "manaUnreserved": calc["stats"].get("ManaUnreserved"), "manaCost": calc["stats"].get("ManaCost")})
    return calc


def select_pantheon(spec, render, worker, budget, calc, trace):
    """Choose the major and minor Pantheon by measuring them in PoB, with a documented fallback.

    Each god is calculated on the finished build; a god is preferred only when it raises effective hit
    pool by at least MIN_PANTHEON_EHP_GAIN. Otherwise the documented rule in `pantheon_rule` decides.
    The selection is written to the PoB config (Build pantheonMajorGod/pantheonMinorGod).
    """
    budget.extend("pantheon", PANTHEON_SHARE)
    share = budget.share("pantheon")
    start = budget.used

    def ehp(stats):
        return float(stats.get("TotalEHP", 0) or 0)

    spec["pantheon"] = {"major": None, "minor": None}
    base = ehp(calc["stats"])
    measured: dict[str, float] = {}
    chosen = {"major": None, "minor": None}
    reference = base
    for kind, options in (("major", PANTHEON_MAJORS), ("minor", PANTHEON_MINORS)):
        best, pick = reference * (1 + MIN_PANTHEON_EHP_GAIN), None
        for god in options:
            if not budget.claim():
                break
            spec["pantheon"] = {"major": chosen["major"], "minor": None}
            spec["pantheon"][kind] = god
            value = ehp(worker.request("calculate", xml=render())["stats"])
            measured[god] = value
            if value > best:
                best, pick = value, god
        chosen[kind] = pick
        if pick is not None:
            reference = best
    rule_major, rule_minor, rule_reason = pantheon_rule(spec, calc["stats"])
    selection = {"measuredEhp": measured, "baselineEhp": base,
                 "major": chosen["major"] or rule_major, "minor": chosen["minor"] or rule_minor,
                 "majorBy": "PoB effective hit pool" if chosen["major"] else "rule",
                 "minorBy": "PoB effective hit pool" if chosen["minor"] else "rule", "rule": rule_reason}
    spec["pantheon"] = {"major": selection["major"], "minor": selection["minor"]}
    spec["pantheonSelection"] = selection
    result = worker.request("calculate", xml=render()) if budget.claim() else calc
    share["used"] += budget.used - start
    if trace is not None:
        trace.append({"kind": "pantheon_selection", **selection})
    return result


def release_repairable_suffixes(items: list[RareItem]) -> None:
    """Free resistance/attribute suffixes so gear packages can be repaired.

    Unique swaps often displace several resistance-bearing rares at once.
    Merely adding affixes to the remaining rares fails when their suffixes
    are already full, even if those old rolls are now redundant. The solver
    recalculates the actual deficits and rebuilds these specific suffixes.
    """
    repairable = re.compile(r"(?:Fire|Cold|Lightning|Chaos) Resistance|to (?:Strength|Dexterity|Intelligence)", re.I)
    for item in items:
        item.mods = [mod for mod in item.mods
                     if not any(repairable.search(line) for line in mod.get("lines", []))]


def assess_quality(spec: dict, calculation: dict, profile: dict,
                   mechanic_checks: list[dict] | None = None,
                   search_limited: bool = False,
                   diagnostics: dict | None = None) -> tuple[str, list[str]]:
    """Return (status, warnings). `diagnostics` is recipe["qualityDiagnostics"]: when supplied,
    completeness gaps and the damage screening target gate the badge (no silent upgrade)."""
    output = calculation.get("stats", {})
    warnings = []
    if diagnostics:
        for gap in diagnostics.get("completeness", {}).get("gaps", []):
            warnings.append("Incomplete build: " + gap + ".")
        for gap in diagnostics.get("encounterReadiness", {}).get("gaps", []):
            if gap.startswith("damage "):
                warnings.append("Encounter readiness: " + gap + ".")
    cost, regen = (float(output.get(key, 0) or 0) for key in ("ManaCost", "ManaRegen"))
    rate = paid_use_rate(output, spec)
    # For temporary minions, PoB's full tooltip cast rate is not the sustainable use rate.
    # sync_permanent_minion_count derives an achievable population from mana-limited casts times
    # duration; reporting the raw tooltip rate as a failed sustain check would contradict that model.
    model = minion_model(spec)
    if (cost > 0 and rate > 0 and regen < cost * rate and model != "permanent"
            and not (model == "temporary" and spec.get("_populationSustainable"))):
        warnings.append(f"Mana regeneration ({regen:.1f}/s) is below estimated skill use ({cost * rate:.1f}/s).")
    if spec.get("linkShortfall"):
        warnings.append("Main link is incomplete: " + str(spec["linkShortfall"]) + ".")
    if spec.get("linkResourceDeficit"):
        deficit = spec["linkResourceDeficit"]
        warnings.append(f"Six-link mana cost {deficit.get('ManaCost')} exceeds unreserved mana "
                        f"{deficit.get('ManaUnreserved')} after whole-link repair.")
    for name in spec.get("unmodeledFlaskEffects", []):
        warnings.append(f"Unique flask {name} is equipped, but its effect is disabled because flask uptime is not modeled.")
    if model == "temporary" and not spec.get("_populationSustainable"):
        warnings.append("Temporary minion population is not sustained by PoB cast rate, duration, minions per cast, "
                        "mana/life recovery and the minion limit.")
    tier = tier_targets(spec)
    pool = (0.0 if spec.get("defenseModel") == "ci" else float(output.get("Life", 0) or 0)) + float(output.get("EnergyShield", 0) or 0)
    if pool < tier["lifePoolMin"]:
        warnings.append(f"Endgame life plus energy shield is {pool:.0f}; quality target is at least {tier['lifePoolMin']:,}.")
    ehp = float(output.get("TotalEHP", 0) or 0)
    if ehp < tier["ehpMin"]:
        warnings.append(f"Calculated effective hit pool is {ehp:.0f}; quality target is at least {tier['ehpMin']:,}.")
    for element in ("Fire", "Cold", "Lightning"):
        resistance = float(output.get(element + "Resist", -60) or 0)
        if resistance < tier["resistTarget"]:
            warnings.append(f"{element} resistance is {resistance:.0f}%; quality target is {tier['resistTarget']}%.")
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
        warnings.append("The design search reached its evaluation limit; passive and complete-build quality is unverified.")
    return ("validated" if not warnings else "experimental"), warnings


def defense_model_checks(spec: dict, stats: dict, allocated: set, nodes: dict) -> list[dict]:
    """Mechanic-specific defense/sustain checks for life, hybrid and CI recipes.

    CI at exactly 1 life passes when the keystone is allocated and ES plus its
    recovery work. A 1-life character without CI, or with no functioning ES
    protection, fails with a specific reason. Life/chaos resistance is never
    demanded of CI (chaos damage is not taken).
    """
    life = float(stats.get("Life", 0) or 0)
    es = float(stats.get("EnergyShield", 0) or 0)
    ci_node = next((key for key, node in nodes.items()
                    if node.get("name") == "Chaos Inoculation" and node.get("isKeystone")), None)
    ci_allocated = ci_node is not None and ci_node in allocated
    model = spec.get("defenseModel", "hybrid")
    checks = []
    if model == "ci":
        checks.append({"name": "Chaos Inoculation allocated", "passed": ci_allocated,
                       "reason": "CI recipe requires the Chaos Inoculation keystone"})
        checks.append({"name": "CI maximum life is 1", "passed": life <= 1,
                       "reason": f"PoB reports maximum life {life:g}"})
        target = tree_pool_target(spec)
        checks.append({"name": "CI energy shield pool", "passed": es >= target, "severity": "warning",
                       "reason": f"{es:.0f} ES against a {target} target (life is 1 by design)"})
        recovery = {key: float(stats.get(key, 0) or 0) for key in
                    ("EnergyShieldRecharge", "EnergyShieldRegenRecovery", "EnergyShieldLeechRate")}
        unknown = not any(key in stats for key in recovery)
        checks.append({"name": "CI energy shield recovery", "passed": unknown or any(v > 0 for v in recovery.values()),
                       "reason": ("recovery outputs unavailable; treated as unknown" if unknown else
                                  "ES recharge/regeneration/leech: " + ", ".join(f"{k}={v:g}" for k, v in recovery.items()))})
    elif "Life" in stats:
        checks.append({"name": "No unprotected 1-life character", "passed": life > 1 or ci_allocated,
                       "reason": f"Maximum life {life:g}" + ("" if life > 1 or ci_allocated else
                                                          " without Chaos Inoculation protection")})
    return checks


def assess_mechanics(spec: dict, calculation: dict, profile: dict, xml: str,
                     context: dict) -> list[dict]:
    """Report modeled mechanic activation separately from legality and quality."""
    stats = calculation.get("stats", {})
    override = bool(profile.get("override"))
    checks = [{"name": "Mechanic profile", "passed": True,
               "reason": (f"tested override {profile.get('name')}" if override else
                          f"derived from gem tags and PoB outputs: {profile.get('delivery')} "
                          f"({profile.get('damageSource')}); no tested profile is required")},
              {"name": "Main-skill damage calculated", "passed": target_dps(stats, spec) > 0,
               "reason": "PoB must calculate positive damage for the requested mechanism"}]
    if override:
        # Tested overrides additionally pin the ascendancy and utility set they were verified with.
        ascendancies = set(profile.get("compatibleAscendancies", []))
        if ascendancies:
            checks.append({"name": "Compatible ascendancy",
                           "passed": spec.get("ascendancy") in ascendancies,
                           "reason": (f"{spec.get('ascendancy')} is in the tested compatible ascendancy set"
                                      if spec.get("ascendancy") in ascendancies else
                                      f"Expected one of {sorted(ascendancies)}")})
        allowed_utility = set(profile.get("compatibleUtilityChoices", []))
        if allowed_utility:
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
    required_nodes = list(profile.get("requiredNodes", []))
    for node_name in required_nodes:
        node_key = next((key for key, node in nodes.items() if node.get("name") == node_name), None)
        checks.append({"name": f"{node_name} allocated", "passed": node_key is not None and node_key in allocated,
                       "reason": f"The tested recipe requires the {node_name} passive"})
    if spec.get("archetype") == "ignite":
        ignite = max(stats.get("IgniteDPS", 0), stats.get("WithIgniteDPS", 0)) > 0
        checks.append({"name": "Ignite damage active", "passed": ignite,
                       "reason": "PoB must report nonzero ignite damage for the ignite recipe"})
    model = minion_model(spec)
    if model is not None:
        limit = int(stats.get("ActiveMinionLimit", 0) or 0)
        count = int(spec.get("minionCount", 0) or 0)
        checks.append({"name": "Minion limit calculated", "passed": limit > 0,
                       "reason": f"PoB calculated a limit of {limit} active minions"})
        if model == "temporary":
            sustained = bool(spec.get("_populationSustainable"))
            per_cast = stats.get("SummonedMinionsPerCast")
            checks.append({"name": "Temporary minion population modeled", "passed": count > 0 and sustained,
                           "reason": (f"{count} minions sustained: {per_cast or 1} per cast, "
                                      f"{float(stats.get('Duration', 0) or 0):.1f}s lifetime, limit {limit}"
                                      if sustained else
                                      "The minion population cannot be maintained from PoB cast rate, duration, "
                                      "minions per cast and mana/life sustain")})
    checks.extend(defense_model_checks(spec, stats, allocated, nodes))
    checks.extend(unique_drawback_checks(
        {slot: text for slot, text in equipped_items(root).items() if "Rarity: UNIQUE" in text.upper().replace("RARITY: ", "Rarity: ")},
        stats, spec))
    expected_curse = spec.get("expectedCurse")
    if expected_curse:
        checks.append({"name": "Mechanism-matched curse equipped",
                       "passed": expected_curse in spec.get("utility", {}),
                       "reason": (f"{expected_curse} is socketed for the {spec.get('damageMechanism', 'selected')} damage plan; curse uptime is not assumed"
                                  if expected_curse in spec.get("utility", {}) else
                                  f"The selected damage plan requires {expected_curse} in a utility socket")})
    resource_plan = sustained_resource_use(stats, spec)
    if resource_plan is not None and (resource_plan.get("checks") or "population" in resource_plan):
        checks.append({"name": "Main-skill sustain leaves utility reserve",
                       "passed": bool(resource_plan.get("sustainable")),
                       "reason": (f"Main-skill use is covered after reserving {spec.get('resourceReserveFraction', 0.15):.0%} of regeneration for utility casts"
                                  if resource_plan.get("sustainable") else
                                  "Main-skill use exceeds recovery after the utility-cast reserve")})
    return checks


def population_assumptions(spec: dict) -> list[str]:
    count, model = spec.get("minionCount"), minion_model(spec)
    if not count or model is None:
        return []
    if model == "permanent":
        return [f"All {count} permanent minions active (PoB-reported limit)"]
    return [f"{count} temporary minions alive: min(PoB minion limit, cast rate x minions per cast x lifetime); "
            "mana/life pays only the refresh casts"]


def sync_permanent_minion_count(spec: dict, calculation: dict) -> bool:
    """Synchronise ``spec["minionCount"]`` with PoB outputs for any minion skill.

    Permanent minions use PoB's reported limit; temporary ones the rate x lifetime population capped by
    that limit (``mechanics.minion_population``).  ``_populationSustainable`` records whether the model
    holds, so quality and mechanic checks never charge a temporary summon at its tooltip cast rate."""
    model = minion_model(spec)
    if model is None:
        return False
    stats = calculation.get("stats", {})
    if model == "permanent":
        count = int(stats.get("ActiveMinionLimit", 0) or 0)
        spec["_populationSustainable"] = True
        if count <= 0 or count == spec.get("minionCount"):
            return False
        spec["minionCount"] = count
        return True
    population = temporary_minion_population(stats, spec)
    estimate, sustainable = population if population is not None else (0, False)
    spec["_populationSustainable"] = sustainable
    if estimate == spec.get("minionCount"):
        return False
    spec["minionCount"] = estimate
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


def sustain_coverage(stats: dict, spec: dict) -> float | None:
    """Worst available/use ratio over mana and life (None when the skill has no sustained cost)."""
    model = sustained_resource_use(stats, spec)
    checks = model.get("checks", []) if model else []
    if not checks:
        return None
    return min(max(0.0, float(c["availablePerSecond"])) / max(0.1, float(c["usePerSecond"])) for c in checks)


def evaluate_calculation(calc: dict, spec: dict, context: dict, allocated: set, uniques: dict,
                         data: GameData | None = None, cost: int = 1) -> EvaluationResult:
    """Whole-build evaluation result for one PoB calculation of a complete candidate.

    Legality (PoB-calculated limits), mechanic validity (defense model, unique drawbacks) and
    resource deficits are separate; `feasible` is false when any error-level check fails.
    """
    stats = calc.get("stats", {})
    result = EvaluationResult(calculation=calc, cost=cost)
    result.legality = [Check(c["name"], bool(c["passed"]), message=c.get("reason", ""))
                       for c in validate_calculation(calc)]
    nodes = context.get("tree", {}).get("nodes", {})
    result.mechanics = [Check(c["name"], bool(c["passed"]), severity=c.get("severity", "error"),
                              message=c.get("reason", ""))
                        for c in defense_model_checks(spec, stats, set(allocated), nodes)]
    result.mechanics += [Check(c["name"], bool(c["passed"]), message=c.get("reason", ""))
                         for c in unique_drawback_checks(uniques, stats, spec, data)]
    mana_gap = float(stats.get("ManaCost", 0) or 0) - float(stats.get("ManaUnreserved", 0) or 0)
    if mana_gap > 0:
        result.resource_deficits["mana"] = mana_gap
    if float(stats.get("LifeUnreserved", stats.get("Life", 1)) or 0) <= 0:
        result.resource_deficits["life"] = 1.0
    result.score = candidate_score(stats, spec) if stats else 0.0
    result.metrics = {key: float(stats[key]) for key in ("FullDPS", "Life", "EnergyShield", "TotalEHP")
                      if key in stats}
    result.reasons = [f"{check.id}: {check.message}" for check in result.failures()]
    return result


PACKAGE_SEARCH_SHARE = 260   # evaluations funded for supporting skill packages (beyond the design limit)
PACKAGE_MAX_EVALUATIONS = 90


def _plan_supporting_skills_once(spec, data, render, worker, budget, items, uniques, supports, calc, trace,
                                 context=None, allocated=()):
    """Plan movement/reservation/curse/herald/defense groups with Agent 2's planner.

    Every candidate is a whole-build PoB evaluation of the complete group list on the
    final gear, tree and jewels, so packages are scored with their resource and
    reservation effects. On planner failure the legacy single-gem utility groups stay.
    """
    from skill_planner import plan_skill_loadout
    from skill_packages import capacity_from_equipment
    budget.extend("packages", PACKAGE_SEARCH_SHARE)
    share = budget.share("packages")
    used_before = budget.used

    sustain_state = {"baseline": None}   # coverage of the main-group-only baseline (first evaluation)

    def evaluate(groups):
        if not budget.claim():
            share["skipped"] = "budget exhausted"
            return {"stats": {}, "ok": False, "reasons": ["search budget exhausted"]}
        result = worker.request("calculate", xml=render(groups=groups))
        evaluation = evaluate_calculation(result, spec, context, allocated, uniques, data)
        reasons = [check.id for check in evaluation.failures()]
        # A package may not break (or further erode) the main skill's sustain.
        after = sustain_coverage(result.get("stats", {}), spec)
        if sustain_state["baseline"] is None:
            sustain_state["baseline"] = after if after is not None else -1.0
        elif after is not None and sustain_state["baseline"] >= 0:
            baseline = sustain_state["baseline"]
            floor = min(1.0, baseline) * 0.98
            if after + 1e-9 < floor:
                reasons.append(f"main-skill sustain coverage {after:.2f} below {floor:.2f}")
        return {"stats": result.get("stats", {}), "ok": evaluation.feasible and not any(
            r.startswith("main-skill sustain") for r in reasons), "reasons": reasons}

    def attribute_repair(candidate_groups):
        """Let the rare gear carry the attributes a package's gems need (undone if the package is dropped)."""
        snapshot = {item.slot: list(item.mods) for item in items}

        def rollback():
            for item in items:
                item.mods = list(snapshot[item.slot])
        try:
            if not budget.claim():
                return None
            result = worker.request("calculate", xml=render(groups=candidate_groups))
            for _ in range(3):
                if not solve_suffixes(items, data, result["stats"], chaos_target=chaos_target(spec)):
                    break
                if not budget.claim():
                    break
                result = worker.request("calculate", xml=render(groups=candidate_groups))
        except SocketConflict:
            rollback()
            return None
        changed = [items_item.slot for items_item in items if items_item.mods != snapshot[items_item.slot]]
        if trace is not None:
            trace.append({"kind": "attribute_repair", "gear_changed": changed,
                          "attributes": {key: result["stats"].get(key) for key in
                                         ("Str", "Dex", "Int", "ReqStr", "ReqDex", "ReqInt")}})
        if not changed:
            return None         # nothing could be added: the attributes are genuinely unavailable
        return {"stats": result["stats"], "rollback": rollback}

    try:
        from build_assembly import equipment_capacity
        items, capacity, _ = equipment_capacity(spec, data, items, uniques)
        from skill_planner import PobGroupEvaluator
        group_checker = PobGroupEvaluator(worker, data, lambda groups, **kw: render(groups=groups, **kw))
        plan = plan_skill_loadout(spec, data, supports, evaluate, capacity=capacity, items=items,
                                  uniques=uniques, legality=group_checker.legality,
                                  max_evaluations=PACKAGE_MAX_EVALUATIONS,
                                  requested=spec.get("requestedUtilities", []),
                                  attribute_repair=attribute_repair)
        budget.used += group_checker.calls
    except Exception as exc:   # planner bugs must not discard the generated build
        if trace is not None:
            import traceback
            trace.append({"kind": "skill_package_plan", "error": f"{type(exc).__name__}: {exc}",
                          "traceback": traceback.format_exc()[-1800:],
                          "reason": "planner failed; legacy utility groups retained"})
        return calc
    share["used"] += budget.used - used_before
    if plan.get("error") or plan.get("problems"):
        if trace is not None:
            trace.append({"kind": "skill_package_plan", "error": plan.get("error"),
                          "problems": plan.get("problems"),
                          "reason": "plan rejected; legacy utility groups retained"})
        return calc
    groups = plan["groups"]
    # Explicitly requested utility gems that no package covers keep a standalone group.
    present = {gem["name"] for group in groups for gem in group["gems"]}
    from skill_packages import make_group, pack_groups
    for index, name in enumerate(spec.get("requestedUtilities", []), 1):
        if name in present or name not in data.gems:
            continue
        extra = make_group(f"requested-{index}", "other", [(name, "active")],
                           slot=spec.get("utility", {}).get(name), justification="explicitly requested")
        if not pack_groups([*groups, extra], capacity)["unplaced"]:
            groups = [*groups, extra]
        else:
            plan.setdefault("omissions", []).append({"package": name, "role": "other",
                                                      "reason": "explicitly requested utility has no free socket"})
    try:
        result = worker.request("calculate", xml=render(groups=groups)) if budget.claim() else None
    except SocketConflict as exc:   # the plan must never crash the build: keep the legacy groups
        if trace is not None:
            trace.append({"kind": "skill_package_plan", "error": f"SocketConflict: {exc}",
                          "reason": "final plan does not fit the equipped sockets; legacy groups retained"})
        return calc
    checks_ok = result is not None and evaluate_calculation(
        result, spec, context, allocated, uniques, data).feasible
    if not checks_ok:
        if trace is not None:
            trace.append({"kind": "skill_package_plan", "reason": "final plan failed legality; legacy groups retained"})
        return calc
    spec["skillGroups"] = groups
    spec["_repairGroups"] = plan.get("repairGroups", {})
    spec["skillPlanSummary"] = {"socketedGems": plan.get("socketedGemCount"), "roles": plan.get("roles"),
                                "occupancy": plan.get("occupancy"), "omissions": plan.get("omissions", []),
                                "accepted": plan.get("accepted", []), "evaluations": plan.get("evaluations"),
                                "fill": plan.get("fill", []), "spareSockets": plan.get("spareSockets", {}),
                                "fillExhausted": plan.get("fillExhausted", False),
                                "droppedSupports": plan.get("droppedSupports", [])}
    if trace is not None:
        trace.append({"kind": "skill_package_plan", "selected": True, "socketedGems": plan.get("socketedGemCount"),
                      "groups": [{"id": g["id"], "role": g["role"], "slot": g.get("slot"),
                                  "gems": [x["name"] for x in g["gems"]]} for g in groups],
                      "omissions": plan.get("omissions", []), "evaluations": budget.used - used_before})
    return result


MANA_REPAIR_SHARE = 160      # evaluations funded for the whole-build mana repair
MANA_REPAIR_MAX_NODES = 6
MANA_NODE_PATTERN = re.compile(r"maximum Mana|Mana Reservation Efficiency|Reservation Efficiency|"
                               r"Mana Regeneration|reduced Mana Cost|Mana Cost of", re.I)


PURE_MANA_REASON = re.compile(r"^(?:reservation does not fit even with Enlighten: )?resource shortfall: mana ([\d,]+(?:\.\d+)?)$")


def mana_shortfall(summary: dict | None) -> float:
    """Mana a tree repair must find to unlock the *cheapest* worthwhile omitted package (0 when none).

    Only omissions whose sole problem is a mana shortfall count: a package that also lacks a
    measurable benefit (or a gate such as Determination's armour floor) is not worth repairing for.
    Unlocking the cheapest package first is what a bounded repair can actually deliver; the repair
    keeps going while later packages stay reachable.
    """
    pure = []
    for omission in (summary or {}).get("omissions", []):
        if not omission.get("repairable") or omission.get("fill"):
            continue
        match = PURE_MANA_REASON.match(str(omission.get("reason", "")))
        if match:
            pure.append(float(match.group(1).replace(",", "")))
    return min(pure) if pure else 0.0


MANA_REPAIR_OPTIONS = 12


def efficiency_rank(text: str, reserving: set[str]) -> int:
    """0: raises reservation efficiency of skills that are actually reserved; 1: other mana nodes.

    Reservations are a percentage of the pool, so extra maximum Mana barely helps (and can hurt the
    margin when most of the pool is reserved); efficiency that applies to the reserving skills does.
    """
    if "Reservation Efficiency" not in text:
        return 1
    for name in reserving:
        if name in text:
            return 0
    if "Herald" in text:
        return 0 if any(name.startswith("Herald of") for name in reserving) else 2
    if re.search(r"Mines|Stance|Curse Aura|Totem|Banner", text):
        return 2
    return 0


def repair_target(summary: dict | None, groups_by_package: dict) -> tuple[str | None, list | None]:
    """The omitted package a mana repair aims at: the cheapest one whose only problem is mana."""
    best = None
    for omission in (summary or {}).get("omissions", []):
        if not omission.get("repairable") or omission.get("fill"):
            continue
        match = PURE_MANA_REASON.match(str(omission.get("reason", "")))
        if not match:
            continue
        package = omission["package"]
        groups = groups_by_package.get(package + "+enlighten") or groups_by_package.get(package)
        value = float(match.group(1).replace(",", ""))
        if groups and (best is None or value < best[0]):
            best = (value, package, groups)
    return (best[1], best[2]) if best else (None, None)


def repair_mana_tree(spec, data, render, worker, budget, context, allocated, jewels, uniques, groups,
                     shortfall, trace, target_groups=None, target=None):
    """Bounded whole-build mana repair: add or swap in mana/reservation notables.

    Every candidate is a whole-build PoB calculation of the current group plan. It must
    raise the (unreserved mana minus main-skill cost) margin, stay legal and keep at least
    97% of the offense score. At most MANA_REPAIR_MAX_NODES notables are accepted.
    Returns (allocated, summary).
    """
    nodes = context["tree"]["nodes"]
    adjacency = graph(nodes, lambda node: not node.get("ascendancyName") and not node.get("isMastery")
                      and not node.get("isProxy")
                      and ("classStartIndex" not in node or node["classStartIndex"] == 3))
    budget.extend("mana_repair", MANA_REPAIR_SHARE)
    share = budget.share("mana_repair")
    start_used = budget.used

    def margin(stats):
        return float(stats.get("ManaUnreserved", 0) or 0) - float(stats.get("ManaCost", 0) or 0)

    # The objective is the margin of the *whole candidate that failed* (current plan + the omitted package):
    # reservations are percentages of the pool, so extra mana alone barely helps; only the tree nodes that
    # raise reservation efficiency or recovery can make the extra package fit.
    measured_groups = target_groups or groups
    reserving_names = {gem["name"] for group in measured_groups for gem in group["gems"]
                       if gem["kind"] == "active"}

    def measure(node_set):
        budget.used += 1
        return worker.request("calculate", xml=render(nodes=node_set, groups=measured_groups))

    current = set(allocated)
    protected_nodes: set[str] = set()
    base = measure(current)
    floor = candidate_score(base["stats"], spec) * 0.97
    current_calc, accepted, tried = base, [], 0
    for _ in range(MANA_REPAIR_MAX_NODES):
        spare = current_calc["passives"]["maximum"] - current_calc["passives"]["used"]
        reachable = paths_from(current, adjacency)
        options = []
        for key, path in reachable.items():
            node = nodes.get(key, {})
            if (key in current or node.get("ascendancyName") or node.get("isMastery") or not path
                    or node.get("isKeystone") or not node.get("isNotable")):
                continue
            if not MANA_NODE_PATTERN.search(" ".join(node.get("stats", []))):
                continue
            options.append((efficiency_rank(" ".join(node.get("stats", [])), reserving_names),
                            len(path), -heuristic(node, spec), int(key), key, set(path)))
        options.sort()
        removals = tree_reroute_removals(nodes, current, jewels, limit=10)
        removals.sort(key=lambda removed: sum(heuristic(nodes[value], spec) for value in removed))
        best = None
        for _, length, _, _, key, path in options[:MANA_REPAIR_OPTIONS]:
            funding = ([set()] if length <= spare else
                       [removed for removed in removals[:3] if len(removed) + max(0, spare) >= length])
            for removed in funding[:2]:
                if budget.used - start_used >= MANA_REPAIR_SHARE:
                    break
                candidate_nodes = (current - removed) | path
                tried += 1
                result = measure(candidate_nodes)
                if result["passives"]["used"] > result["passives"]["maximum"]:
                    continue
                evaluation = evaluate_calculation(result, spec, context, candidate_nodes, uniques, data)
                gain = margin(result["stats"]) - margin(current_calc["stats"])
                if (evaluation.feasible and gain > 0 and candidate_score(result["stats"], spec) >= floor
                        and (best is None or gain > best[0])):
                    best = (gain, key, candidate_nodes, result, sorted(removed))
        if not best:
            break
        gain, key, new_current, current_calc, removed = best
        protected_nodes.update(new_current - current)
        current = new_current
        accepted.append({"node": nodes[key].get("name", key), "id": key, "marginGain": round(gain, 1),
                         "removed": removed})
        if target_groups is not None:
            if margin(current_calc["stats"]) >= 0:
                break          # the omitted package now fits on top of the current plan
        elif margin(current_calc["stats"]) - margin(base["stats"]) >= shortfall:
            break
    share["used"] += budget.used - start_used
    spec["_protectedNodes"] = sorted(protected_nodes, key=int)
    summary = {"shortfall": shortfall, "target": target, "accepted": accepted, "tried": tried,
               "marginBefore": round(margin(base["stats"]), 1),
               "marginAfter": round(margin(current_calc["stats"]), 1)}
    if trace is not None:
        trace.append({"kind": "mana_repair", **summary})
    return current, summary


def plan_supporting_skills(spec, data, render, worker, budget, items, uniques, supports, calc, trace,
                           context=None, allocated=(), jewels=None):
    """Plan packages; when reservation/herald packages were omitted as repairable mana
    shortfalls, run one bounded whole-build mana repair and re-plan once."""
    first = _plan_supporting_skills_once(spec, data, render, worker, budget, items, uniques, supports, calc,
                                         trace, context=context, allocated=allocated)
    shortfall = mana_shortfall(spec.get("skillPlanSummary"))
    if shortfall <= 0 or not spec.get("skillGroups") or context is None:
        return first
    first_groups, first_summary = spec["skillGroups"], spec["skillPlanSummary"]
    try:
        target_id, target_groups = repair_target(first_summary, spec.pop("_repairGroups", {}))
        new_nodes, summary = repair_mana_tree(spec, data, render, worker, budget, context, allocated,
                                              jewels or {}, uniques, first_groups, shortfall, trace,
                                              target_groups=target_groups, target=target_id)
    except Exception as exc:   # a repair bug must not discard the generated build
        if trace is not None:
            trace.append({"kind": "mana_repair", "error": f"{type(exc).__name__}: {exc}"})
        return first
    spec["manaRepair"] = summary
    if not summary["accepted"]:
        spec.pop("_protectedNodes", None)
        return first
    # Spare sockets are always filled, so total gems cannot show whether the repair unlocked a package;
    # compare the packages that earned their sockets (not the leftover-socket fill).
    def core_packages(summary):
        return {entry["package"].split("+")[0] for entry in (summary or {}).get("accepted", [])
                if not entry.get("fill")}
    before_gems = first_summary.get("socketedGems") or 0
    replanned = _plan_supporting_skills_once(
        spec, data, lambda **kw: render(**{"nodes": new_nodes, **kw}), worker, budget, items, uniques,
        supports, first, trace, context=context, allocated=new_nodes)
    plan_summary = spec.get("skillPlanSummary") or {}
    after_gems = plan_summary.get("socketedGems") or 0
    summary.update({"socketedGemsBefore": before_gems, "socketedGemsAfter": after_gems,
                    "omittedAfter": [entry.get("package") for entry in plan_summary.get("omissions", [])],
                    "omissionReasonsAfter": {entry.get("package"): str(entry.get("reason"))[:160]
                                             for entry in plan_summary.get("omissions", [])}})
    unlocked = core_packages(plan_summary) - core_packages(first_summary)
    summary["unlockedPackages"] = sorted(unlocked)
    if unlocked and spec.get("skillGroups") and replanned is not first:
        summary["applied"] = True
        replanned = dict(replanned)
        replanned["_allocated"] = set(new_nodes)
        return replanned
    summary["applied"] = False   # no improvement: keep the original tree and plan
    spec.pop("_protectedNodes", None)
    spec["skillGroups"], spec["skillPlanSummary"] = first_groups, first_summary
    return first


UNSPENT_REFILL_SHARE = 450


def reconcile_jewel_sockets(context: dict, allocated: set, jewels: dict, unique_prices: dict,
                            calc: dict, trace: list | None, label: str) -> tuple[set, dict]:
    """Guarantee every equipped jewel sits in an allocated, connected socket.

    A phase that trades or prunes tree nodes can orphan a jewel's socket. Reconnect it
    when points allow, otherwise drop that jewel (and its quote) with a trace record, so
    an invalid socket reference never reaches export.
    """
    nodes = context["tree"]["nodes"]
    adjacency = graph(nodes, lambda node: not node.get("ascendancyName") and not node.get("isMastery")
                      and not node.get("isProxy")
                      and ("classStartIndex" not in node or node["classStartIndex"] == 3))
    allocated, jewels = set(allocated), dict(jewels)
    connected = paths_from({key for key in allocated if key in adjacency}, adjacency)
    spare = calc.get("passives", {}).get("maximum", 0) - calc.get("passives", {}).get("used", 0)
    for socket in sorted(jewels, key=int):
        if socket in allocated:
            continue
        path = connected.get(socket) or []
        if path and len(path) <= spare:
            allocated.update(path)
            spare -= len(path)
            action = "reconnected"
        else:
            jewels.pop(socket)
            unique_prices.pop(f"Jewel {socket}", None)
            action = "dropped"
        if trace is not None:
            trace.append({"kind": "jewel_socket_reconciled", "phase": label, "socket": socket,
                          "action": action, "reason": "the socket was no longer allocated after tree changes"})
    return allocated, jewels


def refill_unspent_points(context, spec, allocated, render, worker, stage, budget, calc, trace, jewels=None):
    """Spend a large unspent-point gap with a funded second tree pass (no filler nodes).

    Returns the calculation; the grown allocation is passed back under `_allocated`.
    Only nodes with a measured positive gain are added by search_tree.
    """
    unspent = calc["passives"]["maximum"] - calc["passives"]["used"]
    if unspent <= 8:
        return calc
    budget.extend("tree_refill", UNSPENT_REFILL_SHARE)
    start, before = budget.used, calc["passives"]["used"]
    grown, new_calc = search_tree(context, spec, set(allocated), lambda nodes: render(nodes=nodes), worker,
                                  stage, trace=trace, budget=budget, baseline_calc=calc, budget_reserve=40,
                                  shortlist_size=32, use_beam=False,
                                  protected=jewel_socket_paths(context, set(allocated), set(jewels or {}))
                                  | set(spec.get("_protectedNodes", ())))
    budget.share("tree_refill")["used"] += budget.used - start
    gained = new_calc["passives"]["used"] - before
    if trace is not None:
        trace.append({"kind": "tree_refill", "unspentBefore": unspent, "pointsAdded": gained,
                      "unspentAfter": new_calc["passives"]["maximum"] - new_calc["passives"]["used"],
                      "evaluations": budget.used - start,
                      "reason": ("refilled with measured-positive nodes" if gained > 0 else
                                 "no measured positive node was reachable within the funded share")})
    new_calc = dict(new_calc)
    new_calc["_allocated"] = grown
    return new_calc


def jewel_socket_options(context: dict, spec: dict, allocated: set[str], jewels: dict,
                         adjacency: dict, remaining_points: int, reroute_limit: int = 8) -> tuple[list, dict]:
    """List socket packages: already allocated sockets, reachable sockets, and reroutes.

    Each option is (kind, socket, nodes_to_add, nodes_to_remove). The summary counts
    sockets by outcome so a zero-jewel result can be explained.
    """
    nodes = context["tree"]["nodes"]
    summary = {"allocatedEmpty": 0, "reachable": 0, "needsReroute": 0, "unreachable": 0}
    options = []
    reachable = paths_from(allocated, adjacency)
    all_sockets = [key for key, node in nodes.items() if str(key).isdigit() and node.get("isJewelSocket")
                   and not node.get("isProxy") and not node.get("ascendancyName")]
    for key in sorted((key for key in allocated if key in all_sockets and key not in jewels), key=int):
        summary["allocatedEmpty"] += 1
        options.append(("allocated", key, set(), set()))
    free_sockets = []
    for key in all_sockets:
        if key in allocated or key in jewels:
            continue
        path = reachable.get(key)
        if not path:
            summary["unreachable"] += 1
            continue
        if len(path) <= remaining_points:
            summary["reachable"] += 1
            free_sockets.append((len(path), int(key), key, path))
        else:
            summary["needsReroute"] += 1
            free_sockets.append((len(path), int(key), key, path))
    free_sockets.sort()
    for length, _, key, path in free_sockets[:8]:
        if length <= remaining_points:
            options.append(("path", key, set(path), set()))
    # Reroutes: free points by removing a weak connected branch, then connect the socket.
    reroutes = []
    for length, _, key, path in free_sockets[:6]:
        if length <= remaining_points:
            continue
        for removed in tree_reroute_removals(nodes, allocated, jewels, limit=24):
            remaining = allocated - removed
            new_path = paths_from(remaining, adjacency).get(key)
            if not new_path or len(new_path) > remaining_points + len(removed):
                continue
            strength = sum(heuristic(nodes[value], spec) for value in removed)
            reroutes.append((strength, len(new_path), key, set(new_path), set(removed)))
    for _, _, key, add, removed in sorted(reroutes, key=lambda row: (row[0], row[1], int(row[2])))[:reroute_limit]:
        options.append(("reroute", key, add, removed))
    return options, summary


def pick_unique_jewel_socket(context: dict, spec: dict, allocated: set[str], jewels: dict,
                             calc: dict, required: bool) -> tuple[str | None, set[str]]:
    """Choose a socket for a unique jewel without overwriting another unique jewel.

    Preference: empty allocated socket, then a rare jewel's socket, then (required
    jewels only, or optional ones that fit) the shortest reachable new socket path.
    """
    nodes = context["tree"]["nodes"]
    for key in sorted(allocated, key=int):
        if nodes.get(key, {}).get("isJewelSocket") and key not in jewels:
            return key, set()
    for key in sorted(jewels, key=int):
        if key in allocated and not isinstance(jewels[key], str):
            return key, set()          # replace an ordinary rare jewel, never a unique one
    adjacency = graph(nodes, lambda node: not node.get("ascendancyName") and not node.get("isMastery")
                      and not node.get("isProxy")
                      and ("classStartIndex" not in node or node["classStartIndex"] == 3))
    remaining = calc["passives"]["maximum"] - calc["passives"]["used"]
    options, _ = jewel_socket_options(context, spec, allocated, jewels, adjacency, remaining)
    paths = [(len(add), int(key), key, add) for kind, key, add, removed in options
             if kind == "path" and (required or len(add) <= remaining)]
    if paths:
        _, _, key, add = min(paths)
        return key, add
    return None, set()


def search_ordinary_jewels(context: dict, spec: dict, allocated: set[str], jewels: dict[str, RareItem],
                           templates: list[RareItem], render, worker, calc: dict,
                           budget: SearchBudget, trace: list | None) -> tuple[set[str], dict[str, RareItem], dict]:
    """Test legal socket-plus-jewel packages against PoB.

    Fills sockets that are already allocated (no extra points), compares socket+path
    packages, and reroutes weaker branches when the point cap blocks a better jewel.
    Up to three jewels are added; every outcome is traced so zero jewels is explainable.
    """
    reserve = FINAL_REFINEMENT_RESERVE + mastery_reserve_for(budget) + keystone_reserve_for(budget)
    share = budget.share("jewels")
    used_before = budget.used

    def summarize(reason: str, extra: dict | None = None):
        share["used"] += budget.used - used_before
        if trace is not None:
            trace.append({"kind": "jewel_search_summary", "reason": reason, "templates": len(templates),
                          "jewelsEquipped": len(jewels), "evaluations": budget.used - used_before,
                          **(extra or {})})

    if not templates:
        summarize("no ordinary jewel templates: the live modifier pool produced no legal useful jewel "
                  "(missing jewelMods metadata or no mod relevant to this build)")
        return allocated, jewels, calc
    nodes = context["tree"]["nodes"]
    adjacency = graph(nodes, lambda node: not node.get("ascendancyName") and not node.get("isMastery")
                      and not node.get("isProxy")
                      and ("classStartIndex" not in node or node["classStartIndex"] == 3))
    score_now = score(calc["stats"], spec)
    last_summary = {}
    stop_reason = "no jewel package improved the objective"
    for _ in range(3):
        remaining_points = calc["passives"]["maximum"] - calc["passives"]["used"]
        options, last_summary = jewel_socket_options(context, spec, allocated, jewels, adjacency, remaining_points)
        if not options:
            stop_reason = ("no reachable ordinary jewel socket" if not last_summary.get("needsReroute")
                           else "sockets need a reroute but no branch could be freed")
            break
        best = None
        blocked = False
        for kind, key, add, removed in options:
            for template in templates[:4]:
                if not budget.claim(reserve=reserve):
                    blocked = True
                    break
                candidate_nodes = (allocated - removed) | add
                candidate_jewels = {**jewels, key: template}
                candidate = worker.request("calculate", xml=render(nodes=candidate_nodes,
                                               jewelset=candidate_jewels))
                checks = validate_calculation(candidate)
                legal = (all(check["passed"] for check in checks) and
                         candidate["passives"]["used"] <= candidate["passives"]["maximum"])
                delta = candidate_score(candidate["stats"], spec) - score_now
                accepted = legal and delta > 0.001
                if trace is not None:
                    trace.append({"kind": "jewel_candidate", "node": key, "base": template.base,
                                  "package": kind, "removedNodes": len(removed), "addedNodes": len(add),
                                  "mods": [mod["id"] for mod in template.mods], "eligible": accepted,
                                  "score_delta": round(delta, 6),
                                  "reason": "improved PoB score and passed legality checks; compared with other sockets" if accepted else
                                            "failed legality checks" if not legal else
                                            "did not improve objective"})
                if accepted and (best is None or delta > best[0] or
                                 (delta == best[0] and (int(key), template.base) <
                                  (int(best[1]), best[2].base))):
                    best = (delta, key, template, candidate, candidate_nodes, kind)
            if blocked:
                break
        if best is None:
            if blocked:
                stop_reason = "evaluation reserve for later phases reached before all socket packages were tested"
                budget.reserve_blocked = True
            break
        _, key, template, calc, allocated, kind = best
        jewels[key] = template
        score_now = score(calc["stats"], spec)
        if trace is not None:
            trace.append({"kind": "jewel_selection", "node": key, "base": template.base, "package": kind,
                          "mods": [mod["id"] for mod in template.mods], "selected": True,
                          "reason": "highest scoring legal jewel package for a reachable ordinary socket"})
        if blocked:
            stop_reason = "evaluation reserve reached after a jewel was added"
            break
    summarize(stop_reason if not jewels else f"{len(jewels)} jewel(s) equipped; search ended: {stop_reason}",
              {"sockets": last_summary})
    return allocated, jewels, calc


def support_relevance_score(gem: dict, spec: dict, skill_tags: dict | None = None) -> int:
    """Tag/name relevance of a support to the requested skill (used to rank before exact PoB scoring)."""
    archetype = spec["archetype"]
    skill_tags = skill_tags or {}
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
                        "channeling": 2 if skill_tags.get("channeling") or skill_tags.get("channelling") else 0}
    relevant_weights = {tag: weight for tag, weight in relevant_weights.items() if tag and weight}
    tags = gem.get("tags", {})
    value = sum(weight for tag, weight in relevant_weights.items() if tags.get(tag))
    name = gem.get("name", "").casefold()
    if "damage" in name or "penetration" in name:
        value += 2
    if archetype == "minion" and "minion" in name:
        value += 8
    if any(term in name for term in ("poison", "bleed", "ignite", "burning", "ailment", "sadism")):
        value += 5
    if "chaos" in tags and archetype in {"minion", "attack", "ignite", "dot"}:
        value += 2
    # PoB stat ids (live metadata) say what the support actually modifies, so the shortlist is not decided
    # by gem names alone: e.g. a damage-over-time multiplier for a DoT skill.
    stat_ids = " ".join(gem.get("statIds") or ())
    if stat_ids:
        stat_terms = {"dot": ("damage_over_time", "damaging_ailment", "degen"), "ignite": ("ignite", "burn"),
                      "minion": ("minion_damage", "minion_attack", "minion_cast"),
                      "attack": ("attack_damage", "attack_speed"), "spell": ("spell_damage", "cast_speed")}
        if any(term in stat_ids for term in stat_terms.get(archetype, ())):
            value += 6
        if re.search(r"damage_\+%_final|more_damage|_final", stat_ids):
            value += 2
    if any(term in name for term in ("speed", "echo", "multistrike", "unleash", "intensity")):
        value += 1
    if any(term in name for term in ("mana", "inspiration", "lifetap", "efficiency")):
        value += 1
    return value


def support_candidate_shortlist(identifiers, data: GameData, spec: dict, limit: int = 50) -> list[str]:
    """Keep the most relevant PoB-compatible supports for bounded scoring.

    PoB's compatibility API intentionally returns every legal support, including
    candidates that calculate identically to the baseline. Use installed gem
    tags plus skill tags to rank the broad candidate list before spending full
    design evaluations; all retained candidates still receive exact PoB scores.
    """
    skill_tags = data.gem(spec["skill"]).get("tags", {})

    def relevance(identifier: str) -> tuple[int, str]:
        gem = data.by_id.get(identifier, {})
        return support_relevance_score(gem, spec, skill_tags), gem.get("name", identifier).casefold()

    return sorted(set(identifiers), key=lambda identifier: (-relevance(identifier)[0],
                                                            relevance(identifier)[1], identifier))[:limit]


def support_mechanism_compatible(gem: dict, spec: dict) -> bool:
    """Do not mix poison supports into an explicitly non-poison damage plan."""
    name = gem.get("name", "").casefold()
    poison_support = "poison" in name or bool(gem.get("tags", {}).get("poison"))
    mechanism = spec.get("damageMechanism", "")
    if poison_support and mechanism not in {"poison", "minion_chaos_poison"}:
        return False
    return True


def clear_coverage_role(name: str) -> str | None:
    """Classify common clear supports separately from their single-target DPS."""
    normalized = name.casefold()
    if any(term in normalized for term in ("splash", "increased area of effect", "ignite proliferation",
                                            "burning proliferation", "area of effect")):
        return "area coverage"
    if any(term in normalized for term in ("chain", "fork", "multiple projectile", "pierce", "volley")):
        return "projectile or target coverage"
    return None


def mapping_support_plan(trace: list, supports: list[str]) -> dict:
    """Expose clear-oriented candidates beside the single-target link score."""
    integrated = sorted({name for name in supports if clear_coverage_role(name)})
    alternatives = {}
    for event in trace:
        if event.get("kind") != "support_selection" or not event.get("selected"):
            continue
        boss = next((entry for entry in event.get("candidates", [])
                     if entry.get("name") == event["selected"] and entry.get("feasible")), None)
        if not boss:
            continue
        best_damage = max(0.0, float(boss.get("sustainedDamage", boss.get("damage", 0)) or 0))
        if best_damage <= 0:
            continue
        for candidate in event.get("candidates", []):
            role = clear_coverage_role(candidate.get("name", ""))
            if not role or not candidate.get("feasible") or candidate["name"] in supports:
                continue
            damage = max(0.0, float(candidate.get("sustainedDamage", candidate.get("damage", 0)) or 0))
            retention = damage / best_damage
            current = alternatives.get(candidate["name"])
            if retention >= 0.5 and (current is None or retention > current["singleTargetRetention"]):
                alternatives[candidate["name"]] = {
                    "support": candidate["name"], "role": role,
                    "screeningSocket": event.get("link_index"),
                    "comparedWith": event["selected"],
                    "singleTargetRetention": round(retention, 3),
                    "sustainability": candidate.get("sustainability"),
                }
    return {"bossLink": list(supports), "integratedCoverageSupports": integrated,
            "mappingAlternatives": sorted(alternatives.values(),
                                           key=lambda entry: (-entry["singleTargetRetention"], entry["support"]))[:5],
            "coverageModel": "Area and targeting roles are listed separately; their encounter coverage is not converted into single-target DPS."}


def sustained_damage_value(stats: dict, spec: dict) -> float:
    """Compare actual damage after cast-resource coverage and summon population."""
    resource = sustained_resource_use(stats, spec)
    damage = target_dps(recounted_stats(stats, spec), spec)
    if resource is None:
        return damage
    if "population" in resource and not resource.get("sustainable"):
        return 0.0
    checks = resource.get("checks", [])
    if not checks:
        return damage
    coverage = min(max(0.0, float(check.get("availablePerSecond", 0) or 0)) /
                   max(0.1, float(check.get("usePerSecond", 0) or 0)) for check in checks)
    return damage * min(1.0, coverage)


def support_gain_is_meaningful(baseline: float, candidate: float, minimum_gain: float = 0.005) -> bool:
    """Ignore support socket changes whose measured damage gain is within noise."""
    baseline, candidate = max(0.0, float(baseline)), max(0.0, float(candidate))
    return candidate > baseline * (1 + minimum_gain) if baseline else candidate > 0


def search_curse(spec: dict, data: GameData, render, worker, baseline: dict,
                 budget: SearchBudget | None = None, trace: list | None = None) -> dict:
    """Score compatible offensive curse choices using the installed PoB calculation."""
    current = spec.get("expectedCurse")
    if not current or any(data.gems.get(name, {}).get("tags", {}).get("curse")
                          for name in spec.get("requestedUtilities", [])):
        return baseline
    slot = spec.get("utility", {}).get(current)
    if not slot:
        return baseline
    preferred = {"fire": "Flammability", "cold": "Frostbite", "lightning": "Conductivity",
                 "chaos": "Despair", "physical": "Vulnerability"}.get(spec.get("damageType"), "Vulnerability")
    candidates = [preferred, "Despair", "Vulnerability", "Elemental Weakness",
                  "Flammability", "Frostbite", "Conductivity"]
    candidates = list(dict.fromkeys(name for name in candidates if name in data.gems and
                                    data.gem(name)["tags"].get("curse")))
    baseline_score = sustained_damage_value(baseline.get("stats", {}), spec)
    best = {"name": current, "score": baseline_score, "calc": baseline}
    evaluated = [{"name": current, "damage": baseline_score, "selected": False}]
    original_utility = dict(spec.get("utility", {}))
    for name in candidates:
        if name == current:
            continue
        if budget is not None and not budget.claim(reserve=200):
            break
        spec["utility"] = {key: value for key, value in original_utility.items() if key != current}
        spec["utility"][name] = slot
        spec["expectedCurse"] = name
        candidate = worker.request("calculate", xml=render())
        stats = candidate.get("stats", {})
        score_value = sustained_damage_value(stats, spec) if candidate.get("calculated") else 0
        payable = stats.get("ManaUnreserved", 0) >= stats.get("ManaCost", 0)
        evaluated.append({"name": name, "damage": score_value, "payable": payable,
                          "selected": False})
        if payable and candidate.get("calculated") and score_value > best["score"] * 1.005:
            best = {"name": name, "score": score_value, "calc": candidate}
    spec["utility"] = original_utility
    spec["expectedCurse"] = current
    if best["name"] != current:
        spec["utility"] = {key: value for key, value in original_utility.items() if key != current}
        spec["utility"][best["name"]] = slot
        spec["expectedCurse"] = best["name"]
        spec["curseSelectionReason"] = "PoB measured damage with this curse in the selected utility slot"
    else:
        spec["curseSelectionReason"] = "Damage-type-compatible curse retained after PoB comparison"
    for entry in evaluated:
        entry["selected"] = entry["name"] == best["name"]
    if trace is not None:
        trace.append({"kind": "curse_selection", "slot": slot,
                      "damageMechanism": spec.get("damageMechanism"),
                      "candidates": evaluated, "selected": best["name"],
                      "reason": ("highest PoB-measured, resource-adjusted damage; curse uptime is not assumed"
                                 if best["name"] != current else
                                 "no alternative curse improved measured damage by at least 0.5%")})
    return best["calc"]


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
        return sustained_damage_value(entry["stats"], spec)

    best = max(selection_pool, key=lambda entry: (
        sustained_damage(entry), target_dps(recounted_stats(entry["stats"], spec), spec), entry["id"]))
    return best, sustainability, sustainability_acceptable


MAIN_SUPPORT_TARGET = 5   # main skill + five supports fills a six-link body armour
SUPPORT_EXCLUDED = {"Sacrifice", "Vaal Sacrifice", "Cast on Death"}


def support_candidate_names(identifiers, supports, data: GameData, spec: dict, skip=()) -> list[str]:
    names = []
    for identifier in identifiers:
        gem = data.by_id.get(identifier)
        if (gem and gem["name"] not in supports and gem["name"] not in skip
                and not gem["name"].startswith("Awakened ")
                and not support_is_noncombat(gem)
                and support_mechanism_compatible(gem, spec)
                and gem["name"] not in SUPPORT_EXCLUDED
                and (gem["name"] != "Decay" or spec["archetype"] == "dot")
                and not gem["tags"].get("exceptional") and gem["maxLevel"] >= 20):
            names.append(identifier)
    return names


def support_effect_reason(baseline: dict, candidate: dict, name: str, spec: dict) -> str | None:
    """Return why a support is effective (not filler), or None when no effect is demonstrated.

    Effects: measured sustained-damage gain, a materially lower resource cost with
    little damage loss, a faster use rate, or an explicit clear-coverage role.
    """
    before, after = sustained_damage_value(baseline, spec), sustained_damage_value(candidate, spec)
    if support_gain_is_meaningful(before, after):
        return "sustained damage gain"
    cost_before, cost_after = (float(x.get("ManaCost", 0) or 0) for x in (baseline, candidate))
    if cost_before > 0 and cost_after <= cost_before * 0.9 and after >= before * 0.97:
        return "reduced resource cost"
    speed_before, speed_after = (float(x.get("Speed", 0) or 0) for x in (baseline, candidate))
    # A faster cast/summon rate is an effect for hit skills and temporary-minion summons, not for
    # permanent-minion summons where it only changes how quickly the same minions appear.
    permanent_minions = spec.get("archetype") == "minion" and minion_model(spec) != "temporary"
    if (not permanent_minions and speed_before > 0 and speed_after >= speed_before * 1.05
            and after >= before * 0.97):
        return "faster use rate"
    role = clear_coverage_role(name)
    if role and after >= before * 0.9:
        return role
    return None


def resource_feasible(stats: dict) -> bool:
    return (stats.get("ManaUnreserved", 0) >= stats.get("ManaCost", 0) and
            stats.get("LifeUnreserved", stats.get("Life", 0)) > 0)


def choose_main_link(spec, data, render, worker, stage, trace, budget):
    """Main six-link: Agent 2's beam planner first (sustain-aware value), then whole-link repair.

    Falls back to the single-path `search_links` when the planner cannot complete the
    link or fails. Complete links with a resource deficit are repaired by exchange,
    never shortened.
    """
    from skill_planner import PobMainLinkOps, plan_main_link
    ops = PobMainLinkOps(worker, data, lambda groups, **kw: render(groups=groups, **kw), spec)
    before = budget.used
    try:
        stage("Beam-searching a complete six-link with demonstrated support effects")
        skill_tags = data.gem(spec["skill"]).get("tags", {})
        plan = plan_main_link(spec, data, ops, value=lambda stats: sustained_damage_value(stats, spec),
                              size=MAIN_SUPPORT_TARGET + 1, width=2, shortlist=22,
                              hint=lambda name: support_relevance_score(data.gems[name], spec, skill_tags)
                              if name in data.gems else 0)
    except Exception as exc:
        plan = {"complete": False, "reason": f"{type(exc).__name__}: {exc}", "trace": []}
    budget.used += ops.calls
    if trace is not None:
        trace.append({"kind": "main_link_plan", "complete": plan.get("complete"), "reason": plan.get("reason"),
                      "supports": plan.get("supports"), "supportRoles": plan.get("supportRoles"),
                      "repairNeeded": plan.get("repairNeeded"), "evaluations": ops.calls,
                      "alternatives": plan.get("alternatives")})
    if plan.get("complete") and len(plan["supports"]) >= MAIN_SUPPORT_TARGET:
        spec.pop("linkShortfall", None)
        supports = repair_link_resources(spec, data, lambda links: render(links=links), worker, stage,
                                         list(plan["supports"]), trace, budget)
        mapping = plan.get("mappingLink") or {}
        spec["mappingLinkSupports"] = list(mapping.get("supports") or supports)
        spec["linkSupportCount"] = len(supports)
        budget.share("links")["used"] += budget.used - before
        return supports
    supports = search_links(spec, data, lambda links: render(links=links), worker, stage,
                            trace=trace, budget=budget)
    supports = pad_main_link(spec, data, lambda links: render(links=links), worker, supports, trace, budget)
    budget.share("links")["used"] += budget.used - before
    return supports


def pad_main_link(spec, data, render, worker, supports, trace=None, budget=None):
    """Fill a link that stopped short of six gems with the best remaining compatible supports.

    The planners never add a support without a demonstrated effect while better ones exist; when
    none is left a six-link is still the contract (a socketed body armour wastes sockets otherwise),
    so each missing slot takes the compatible, resource-feasible support with the highest measured
    sustained damage that does not reduce the link's damage (non-combat and legacy supports are never
    candidates). The choice is recorded as ``linkFiller`` and the warning keeps saying the link has
    low-evidence supports.
    """
    supports = list(supports)
    filler = []
    while len(supports) < MAIN_SUPPORT_TARGET:
        xml = render(supports)
        names = support_candidate_names(worker.request("supports", xml=xml)["supports"], supports, data, spec)
        names = [identifier for identifier in names
                 if not support_is_noncombat(data.by_id.get(identifier, {}))]
        if not names:
            break
        names = support_candidate_shortlist(names, data, spec, limit=40)
        baseline = worker.request("calculate", xml=xml)["stats"]
        before = sustained_damage_value(baseline, spec)
        scored = []
        for offset in range(0, len(names), 20):
            scored.extend(worker.request("supportScores", xml=xml,
                                         candidates=sorted(names)[offset:offset + 20])["candidates"])
        if budget is not None:
            budget.used += 1 + len(names)
        pool = [entry for entry in scored if resource_feasible(entry["stats"])] or scored
        # Never accept a support that lowers the link's damage when a neutral or better one exists.
        pool = [entry for entry in pool if sustained_damage_value(entry["stats"], spec) >= before * 0.99] or pool
        if not pool:
            break
        best = max(pool, key=lambda entry: (sustained_damage_value(entry["stats"], spec), entry["id"]))
        name = data.by_id[best["id"]]["name"]
        gain = (sustained_damage_value(best["stats"], spec) - before) / before if before > 0 else 0.0
        supports.append(name)
        filler.append({"name": name, "gain": gain})
    if filler:
        spec["linkFiller"] = filler
        if trace is not None:
            trace.append({"kind": "main_link_filler_padding", "filler": filler,
                          "reason": "no further support with a demonstrated effect; best measured "
                                    "compatible supports fill the six-link"})
    if len(supports) < MAIN_SUPPORT_TARGET:
        spec["linkShortfall"] = spec.get("linkShortfall") or "no further compatible support exists"
    elif filler:
        # The six-link is complete; the low-evidence support is disclosed in ``linkFiller`` instead of
        # leaving the build marked incomplete.
        spec.pop("linkShortfall", None)
    spec["linkSupportCount"] = len(supports)
    return supports


def search_links(spec, data, render, worker, stage, trace=None, budget=None):
    """Fill the main link with effective supports (target: five supports).

    A resource-infeasible best support is kept and handed to a whole-link repair
    instead of silently truncating the link. Supports with no demonstrated effect
    are never added as filler; the shortfall and its reason are recorded.
    """
    supports = []
    spec.pop("linkShortfall", None)
    for index in range(MAIN_SUPPORT_TARGET):
        xml = render(supports)
        candidates = worker.request("supports", xml=xml)["supports"]
        names = support_candidate_names(candidates, supports, data, spec)
        if not names:
            spec["linkShortfall"] = f"no compatible support remains for socket {index + 2}"
            break
        compatible_count = len(names)
        names = support_candidate_shortlist(names, data, spec)
        if budget is not None:
            # Keep room for the final tree pass, but never below what a full
            # six-link needs: every scoring round is mandatory.
            remaining_rounds = MAIN_SUPPORT_TARGET - index
            available = budget.limit - budget.used - 200
            if available <= 1:
                available = max(0, budget.limit - budget.used - 2 * remaining_rounds)
            if available <= 1:
                budget.reserve_blocked = True
                spec["linkShortfall"] = "search budget exhausted before the link was complete"
                break
            names = names[:available - 1]
        baseline = worker.request("calculate", xml=xml)["stats"]
        if budget is not None:
            budget.used += 1
        scored = []
        for offset in range(0, len(names), 20):
            stage(f"Scoring support {index + 1}/{MAIN_SUPPORT_TARGET}: candidates {offset + 1}-{min(offset + 20, len(names))}/{len(names)}")
            batch = sorted(names)[offset:offset + 20]
            if budget is not None:
                budget.used += len(batch)
            scored.extend(worker.request("supportScores", xml=xml, candidates=batch)["candidates"])
        feasible = [entry for entry in scored if resource_feasible(entry["stats"])]
        alive = [entry for entry in scored
                 if entry["stats"].get("LifeUnreserved", entry["stats"].get("Life", 0)) > 0]
        pool = feasible or alive
        if not pool:
            spec["linkShortfall"] = f"no candidate for socket {index + 2} left usable life"
            if trace is not None:
                trace.append({"kind": "support_selection", "link_index": index + 1,
                              "candidates": len(scored), "feasible": 0,
                              "reason": "no candidate left usable life"})
            break
        best, sustainability, sustainability_acceptable = choose_support_candidate(pool, spec)
        effect = support_effect_reason(baseline, best["stats"], data.by_id[best["id"]]["name"], spec)
        if effect is None:
            # The top-ranked support is inert; look for any other effective one.
            ranked = sorted(pool, key=lambda entry: (-sustained_damage_value(entry["stats"], spec), entry["id"]))
            for entry in ranked:
                reason = support_effect_reason(baseline, entry["stats"], data.by_id[entry["id"]]["name"], spec)
                if reason:
                    best, effect = entry, reason
                    break
        if trace is not None:
            trace.append({"kind": "support_selection", "link_index": index + 1,
                          "compatibleCandidates": compatible_count,
                          "shortlistedCandidates": len(scored),
                          "candidates": [{"name": data.by_id.get(entry["id"], {}).get("name", entry["id"]),
                                          "damage": target_dps(recounted_stats(entry["stats"], spec), spec),
                                          "sustainedDamage": sustained_damage_value(entry["stats"], spec),
                                          "feasible": entry in feasible,
                                          "sustainability": sustainability.get(entry["id"])}
                                         for entry in scored],
                          "resourceFeasibleCandidates": len(feasible),
                          "sustainableCandidates": sum(bool(result and result["sustainable"])
                                                       for entry_id, result in sustainability.items()
                                                       if entry_id in {e["id"] for e in feasible}),
                          "selectionRule": ("highest damage among sustained candidates within focus damage tolerance"
                                            if feasible and sustainability_acceptable else
                                            "highest resource-adjusted damage among feasible candidates" if feasible else
                                            "no resource-feasible support; best candidate kept for whole-link resource repair"),
                          "effect": effect,
                          "selected": data.by_id[best["id"]]["name"] if effect else None,
                          **({} if effect else {"reason": "no compatible support improved resource-adjusted damage by at least 0.5% or showed another effect"})})
        if effect is None:
            spec["linkShortfall"] = (f"no compatible support for socket {index + 2} demonstrated an effect "
                                     "(damage, cost, speed or coverage); inert filler was not added")
            break
        supports.append(data.by_id[best["id"]]["name"])
    if not supports:
        raise ValueError(f"PoB found no meaningful resource-adjusted support for {spec['skill']}")
    if len(supports) < MAIN_SUPPORT_TARGET:
        spec.setdefault("linkShortfall", "link shorter than six")
    supports = repair_link_resources(spec, data, render, worker, stage, supports, trace, budget)
    spec["linkSupportCount"] = len(supports)
    return supports


def repair_link_resources(spec, data, render, worker, stage, supports, trace=None, budget=None):
    """Exchange earlier support choices until the whole link is resource-feasible.

    Whole-set repair: for each position, remove that support and rescore every
    compatible replacement against the other supports. The link size is kept.
    """
    stats = worker.request("calculate", xml=render(supports))["stats"]
    if budget is not None:
        budget.used += 1
    rounds = 0
    while not resource_feasible(stats) and rounds < 3 and supports:
        rounds += 1
        best = None
        for position in range(len(supports) - 1, -1, -1):
            others = supports[:position] + supports[position + 1:]
            xml = render(others)
            names = support_candidate_names(
                worker.request("supports", xml=xml)["supports"], others, data, spec,
                skip={supports[position]})
            names = support_candidate_shortlist(names, data, spec, limit=30)
            if budget is not None:
                allowed = budget.limit - budget.used - 100
                if allowed <= 1:
                    budget.reserve_blocked = True
                    break
                names = names[:allowed]
                budget.used += len(names)
            scored = []
            for offset in range(0, len(names), 20):
                scored.extend(worker.request("supportScores", xml=xml,
                                             candidates=sorted(names)[offset:offset + 20])["candidates"])
            for entry in scored:
                if not resource_feasible(entry["stats"]):
                    continue
                value = sustained_damage_value(entry["stats"], spec)
                if value <= 0:
                    continue
                if best is None or value > best[0]:
                    best = (value, position, data.by_id[entry["id"]]["name"], entry["stats"])
        if best is None:
            break
        _, position, name, stats = best
        if trace is not None:
            trace.append({"kind": "link_resource_repair", "replaced": supports[position], "with": name,
                          "position": position + 1, "sustainedDamage": best[0]})
        supports = supports[:position] + [name] + supports[position + 1:]
        stats = worker.request("calculate", xml=render(supports))["stats"]
    if not resource_feasible(stats):
        spec["linkResourceDeficit"] = {"ManaCost": stats.get("ManaCost"),
                                       "ManaUnreserved": stats.get("ManaUnreserved")}
        if trace is not None:
            trace.append({"kind": "link_resource_repair", "repaired": False,
                          "reason": "no whole-link exchange made the link resource-feasible; supports kept"})
    else:
        spec.pop("linkResourceDeficit", None)
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


def unique_market_price(definition: dict, market: dict, links: int | None = None, slot: str = "") -> float | None:
    """Candidate price through the shared resolver (identical to the displayed quote)."""
    raw = definition.get("raw") or ""
    try:
        text = raw if raw.startswith("Rarity: UNIQUE") else current_unique(raw)
    except ValueError:
        return None
    if definition.get("selectedVariantLabel") not in {None, "Current"} and "Selected Variant" not in text:
        text += "\nVariant: " + str(definition["selectedVariantLabel"]) + "\nSelected Variant: 1"
    if not slot:
        slot = "Jewel" if definition.get("type") == "Jewel" else str(definition.get("type") or "")
    return resolve_unique(entry_from_text(text, slot, linked=links), market).get("chaos")


def unrequested_prices(uniques: dict, unique_prices: dict, requested) -> list:
    """Prices of equipped uniques the user did not ask for (requested items always win)."""
    result = []
    for slot, price in (unique_prices or {}).items():
        text = (uniques or {}).get(slot)
        try:
            name = _item_parts(text)[1] if isinstance(text, str) else None
        except ValueError:
            name = None
        if name is not None and name in requested:
            continue
        result.append(price)
    return result


def unique_package_within_budget(current_prices, addition_prices, budget: float | None) -> bool:
    """Require known variant/link quotes and enforce the cumulative package cap (stated budgets)."""
    if budget is None:
        return True
    prices = [*current_prices, *addition_prices]
    return all(price is not None for price in prices) and sum(prices) <= budget


def package_fits(spec: dict, current_prices, addition_prices) -> bool:
    """Stated budget: known quotes within the cap. Standard budget: assumed-price accounting."""
    return unique_policy.within_budget(spec, list(current_prices), list(addition_prices))


JEWEL_SPECIAL_PATTERN = re.compile(r"(?i)radius|allocat(?:e|es|ed|ing)|transforms?|keystone|passive skills? in")
JEWEL_UNIQUES_TESTED = 24


def unique_jewel_shortlist(unique_defs: list[dict], no_uniques: bool, requested: set[str],
                           spec: dict | None = None, market: dict | None = None,
                           unobtainable: frozenset = frozenset()) -> list[dict]:
    """Unique jewels worth measuring: requested ones, else the most relevant plus cheapest relevant ones.

    Jewels that need passive-tree transformation mechanics are excluded up front so they cannot use up
    the measured shortlist.
    """
    if no_uniques:
        return []
    jewels = [entry for entry in unique_defs if entry.get("type") == "Jewel" and entry.get("name")]
    if requested:
        jewels.sort(key=lambda entry: entry.get("name", ""))
        return [entry for entry in jewels if entry.get("name") in requested]
    if spec is None:
        jewels.sort(key=lambda entry: entry.get("name", ""))
        return jewels[:16]
    usable = [entry for entry in jewels if not JEWEL_SPECIAL_PATTERN.search(entry.get("raw") or "")
              and entry.get("name") not in unobtainable
              and not UNMODELED_UPTIME_PATTERN.search(entry.get("raw") or "")]
    cap = unique_policy.budget_cap(spec)
    priced = []
    for entry in usable:
        price = unique_market_price(entry, market) if market is not None else None
        if price is not None and cap is not None and price > cap:
            continue
        priced.append((entry, price))
    def relevance(entry):
        return unique_relevance(entry.get("raw") or "", spec)
    ranked = sorted(priced, key=lambda row: (-relevance(row[0]), row[0].get("name", "")))
    chosen = ranked[:JEWEL_UNIQUES_TESTED]
    cheap = [row for row in ranked[JEWEL_UNIQUES_TESTED:] if relevance(row[0]) > 0 and row[1] is not None
             and (cap is None or row[1] <= cap * 0.1)]
    cheap.sort(key=lambda row: (row[1], -relevance(row[0]), row[0].get("name", "")))
    chosen.extend(cheap[:6])
    return [entry for entry, _ in chosen]


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


SACRIFICE_PATTERN = re.compile(r"Sacrifice (\d+(?:\.\d+)?)% of (?:your )?(?:Life|Mana|Energy Shield)"
                               r" when you (?:Use or Trigger|Use|Trigger|Cast) (?:a )?(?:Spell )?Skill", re.I)


def skill_use_rate(stats: dict, spec: dict, data: GameData | None = None) -> tuple[float, str]:
    """Paid-use cadence of the main skill, with the assumption that produced it.

    `Speed` is the cast rate, not how often a channelled skill is paid for, and
    `LifeCost=0` says nothing about unique drawbacks.
    """
    if spec.get("channelled"):
        return paid_use_rate(stats, spec), f"channelled: one paid use per {CHANNEL_CYCLE_SECONDS:g}s (assumption)"
    return paid_use_rate(stats, spec), "cast rate from PoB"


def unique_drawback_checks(item_texts: dict[str, str], stats: dict, spec: dict,
                           data: GameData | None = None) -> list[dict]:
    """Audit life/ES sacrifice drawbacks of equipped uniques against recovery.

    Sacrifice is not damage: it cannot be mitigated, only out-recovered by regeneration
    and leech. Kill-contingent recovery is not counted for stationary boss sustain.
    """
    checks = []
    life = float(stats.get("Life", 0) or 0)
    rate, assumption = skill_use_rate(stats, spec, data)
    recovery = float(stats.get("LifeRegenRecovery", 0) or 0) + float(stats.get("LifeLeechRate", 0) or 0)
    known = "LifeRegenRecovery" in stats or "LifeLeechRate" in stats
    for slot, text in item_texts.items():
        match = SACRIFICE_PATTERN.search(text or "")
        if not match:
            continue
        pct = float(match[1])
        name = _item_parts(text)[1] if "Rarity" in (text or "") else slot
        per_second = life * pct / 100 * rate
        passed = (recovery >= per_second) if known else False
        checks.append({"name": f"Unique drawback sustained: {name}", "passed": bool(passed),
                       "reason": (f"sacrifices {pct:g}% of {life:.0f} life per use at {rate:.2f} uses/s = "
                                  f"{per_second:.0f}/s against {recovery:.0f}/s regeneration+leech ({assumption})"
                                  if known else "recovery outputs unavailable; sacrifice sustain unknown, rejected")})
    return checks


MIN_UNIQUE_GAIN = unique_policy.MIN_UNIQUE_GAIN
GEAR_BEAM_TESTS_PER_PARENT = 10   # unique swaps measured per beam parent (was 8)
UNIQUES_PER_SLOT = 10          # relevance-ranked candidates measured per equipment slot
CHEAP_UNIQUES_PER_SLOT = 4     # extra cheap, relevant candidates per slot (cheap high-value uniques)
FLASK_UNIQUES_TESTED = 6       # flask effects are unmodeled; only a few are measured per build


def shortlist_slot_uniques(entries: list, spec: dict) -> list:
    """Candidates measured for one slot: the most relevant ones plus the cheapest relevant ones.

    Relevance is text based (see ``unique_relevance``), so a wide list is measured in PoB and the
    measured marginal gain decides; cheap items are guaranteed a place so that inexpensive,
    high-value uniques are never crowded out by expensive ones.
    """
    cap = unique_policy.budget_cap(spec)

    def relevance(option):
        return unique_relevance(option[2], spec)

    if entries and entries[0][1].startswith("Flask "):
        seen, ordered = set(), []
        for option in sorted(entries, key=lambda option: (-relevance(option), option[0])):
            if option[0] not in seen:
                seen.add(option[0])
                ordered.append(option)
        return ordered[:FLASK_UNIQUES_TESTED]
    ranked = sorted(entries, key=lambda option: (-relevance(option), option[0]))
    chosen = ranked[:UNIQUES_PER_SLOT]
    scored = [option for option in ranked[UNIQUES_PER_SLOT:] if relevance(option) > 0]
    affordable = [option for option in scored if option[3] is not None and (
        cap is None or option[3] <= cap * 0.25)]
    affordable.sort(key=lambda option: (option[3], -relevance(option), option[0]))
    chosen.extend(affordable[:CHEAP_UNIQUES_PER_SLOT])
    return chosen


def unique_relevance(text: str, spec: dict) -> int:
    """Mechanic-aware text relevance for shortlisting unique equipment.

    Counts build-enabling terms (damage scaling, cost/reservation enablers, granted
    supports, defense matching the recipe) rather than a flat two-per-slot text match.
    """
    lowered = text.lower()
    if spec["archetype"] == "minion":
        terms = ("minion", "spectre", "zombie", "skeleton", "spirit", "raise", "summon", "cast speed",
                 spec["damageType"], "maximum life", "resistance")
    else:
        terms = ("spell", "cast speed", "critical strike", spec["damageType"], "damage", "maximum life",
                 "resistance")
    score = sum(term in lowered for term in terms)
    # Enablers: item-granted supports/skills, reservation or mana efficiency, penetration.
    score += 2 * sum(term in lowered for term in ("supported by level", "socketed gems", "reservation",
                                                  "mana cost", "penetrates", "gain level"))
    if spec.get("defenseModel") == "ci":
        score += 2 * ("energy shield" in lowered) - 3 * ("maximum life" in lowered and "energy shield" not in lowered)
    return score


UNMODELED_UPTIME_PATTERN = re.compile(r"(?i)\bgain one of the following\b|\brandom(?:ly)? (?:gain|of the following)")


UNOBTAINABLE_PATTERN = re.compile(r"(?im)^Source:\s*No longer obtainable")
_UNOBTAINABLE_CACHE: dict[str, frozenset] = {}


def unobtainable_unique_names(context: dict) -> frozenset:
    """Uniques PoB marks "Source: No longer obtainable" (the worker's catalogue omits that line)."""
    home = context.get("pobHome")
    key = str(home)
    if key not in _UNOBTAINABLE_CACHE:
        names = set()
        try:
            for path in (Path(home) / "Data" / "Uniques").glob("*.lua"):
                for block in re.findall(r"\[\[(.*?)\]\]", path.read_text(encoding="utf-8"), re.S):
                    lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
                    if lines and UNOBTAINABLE_PATTERN.search(block):
                        names.add(lines[0])
        except (OSError, TypeError):
            pass
        _UNOBTAINABLE_CACHE[key] = frozenset(names)
    return _UNOBTAINABLE_CACHE[key]


def unique_options(context, spec, market, items, data, unique_defs=None):
    if spec["noUniques"]:
        return []
    options = []
    cap = spec["budgetChaos"]
    unobtainable = unobtainable_unique_names(context)
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
        if name in unobtainable and name not in spec.get("requestedUniques", ()):
            continue    # "No longer obtainable": cannot be bought at any price
        if (UNMODELED_UPTIME_PATTERN.search(definition.get("raw") or "")
                and name not in spec.get("requestedUniques", ())):
            # A randomly rotating buff (for example "every 5 seconds, gain one of the following") is
            # measured by PoB as if every option were permanently active; the DPS it adds is not real.
            continue
        if name == "The Queen's Hunger" and name not in spec.get("requestedUniques", ()):
            # Its triggered offerings are disabled unless the user explicitly asks for it.
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
            elif (item.slot.startswith("Weapon") and unique_subtype != item.definition.get("subType")
                  and unique_type != "Shield"):
                # Any shield may be tested in the off-hand (its attribute requirements are checked by
                # PoB legality); only the weapon class of a main hand decides whether it can be wielded.
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
            raw = definition.get("raw") or current_unique(definition.get("source", ""))
            if sockets and not re.search(r"(?m)^Sockets:", raw):
                raw += "\nSockets: " + "-".join("B" for _ in range(sockets))
            if item.slot in spec.get("utility", {}).values():
                required_sockets = sum(1 for destination in spec.get("utility", {}).values()
                                       if destination == item.slot)
                actual_sockets = item_socket_count(raw)
                if actual_sockets is not None and actual_sockets < required_sockets:
                    continue
            price = unique_market_price(definition, market, sockets if sockets > 0 else None, item.slot)
            # A stated budget needs a variant/link-specific quote. Without a
            # budget, missing quotes leave the candidate visible as unknown.
            if cap is not None and (price is None or price > cap):
                continue
            standard_cap = unique_policy.budget_cap(spec)
            if (cap is None and price is not None and standard_cap is not None and price > standard_cap
                    and name not in spec.get("requestedUniques", ())):
                continue    # a single item above the standard budget can never be bought
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


def item_socket_count(text: str) -> int | None:
    match = re.search(r"(?m)^Sockets:\s*([RGBW-]+)\s*$", text or "")
    return len(re.findall(r"[RGBW]", match.group(1))) if match else None


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
    def available_utility_sockets(slot):
        if slot in uniques:
            actual = item_socket_count(uniques[slot])
            if actual is not None:
                return actual
        item = next((item for item in items if item.slot == slot), None)
        return min(4, item.definition.get("socketLimit", 0)) if item else 0
    socket_capacity = all(count <= available_utility_sockets(slot) for slot, count in counts.items())
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
    bad_jewels: list[str] = []
    for node_id, jewel in jewels.items():
        ok_before = legal_jewels
        legal_jewels = True
        if isinstance(jewel, str):
            try:
                _, _, base_name = _item_parts(jewel)
            except ValueError:
                legal_jewels = False
                bad_jewels.append(f"{node_id}: unreadable unique jewel")
                continue
            definition = data.bases.get(base_name, {})
            legal_jewels = node_id in allocated and bool(nodes.get(node_id, {}).get("isJewelSocket"))
            legal_jewels = legal_jewels and definition.get("type") == "Jewel" and bool(definition.get("tags", {}).get("jewel"))
        else:
            definition = data.bases.get(jewel.base, {})
            tags = definition.get("tags", {})
            legal_jewels = node_id in allocated and bool(nodes.get(node_id, {}).get("isJewelSocket"))
            legal_jewels = legal_jewels and definition.get("type") == "Jewel" and bool(tags.get("jewel"))
            probe = RareItem("Jewel", jewel.base, definition, item_level=jewel.item_level, quality=jewel.quality)
            for mod in jewel.mods:
                legal_jewels = legal_jewels and probe.can_add(mod)
                probe.mods.append(mod)
        if not legal_jewels:
            bad_jewels.append(f"{node_id}: " + ("socket not allocated" if node_id not in allocated else "illegal jewel or affix"))
        legal_jewels = ok_before and legal_jewels
    return [{"name": "Connected passive tree", "passed": bool(connected),
             "reason": "All regular and ascendancy paths connect through allocated nodes to their own start"},
            {"name": "Legal mastery effects", "passed": bool(valid_masteries),
             "reason": "Masteries require an allocated group notable and a distinct installed effect"},
            {"name": "Utility socket capacity", "passed": socket_capacity,
             "reason": "Utility groups must fit their equipped items' sockets"},
            {"name": "Legal rare affixes", "passed": legal_affixes,
             "reason": "Generated rares use eligible installed tiers, distinct groups and at most three prefixes/suffixes"},
            {"name": "Ordinary jewel sockets and affixes", "passed": legal_jewels,
             "reason": ("Each rare jewel must use one allocated ordinary socket and eligible PoB jewel affixes"
                        + (": " + "; ".join(bad_jewels) if bad_jewels else ""))}]


def build_quality_report(xml: str, spec: dict, details: dict, calculation: dict,
                         data: GameData, price: dict, progression: list[dict]) -> dict:
    """Report gameplay completeness, encounter gaps and price coverage separately from legality."""
    root = ET.fromstring(xml)
    skills = active_skill_set(root)
    groups = skills.findall("Skill") if skills is not None else []
    socketed_names = {gem.get("nameSpec") for group in groups if not group.get("source")
                      for gem in group.findall("Gem") if gem.get("nameSpec")}
    # The final XML is authoritative; the legacy utility map is used only when it has no socketed gems.
    utility_names = ({name for name in socketed_names if name in data.gems and
                      name != spec.get("skill") and not data.gems[name].get("support")}
                     if socketed_names else set(spec.get("utility", {})))
    role_predicates = {
        "movement": lambda tags: any(tags.get(tag) for tag in ("movement", "travel", "blink")),
        "guard": lambda tags: bool(tags.get("guard")),
        "aura": lambda tags: bool(tags.get("aura")),
        "curse": lambda tags: bool(tags.get("curse")),
    }
    required_roles = {"movement", "guard", "aura"}
    if spec.get("expectedCurse"):
        required_roles.add("curse")
    present_roles = {role: sorted(name for name in utility_names if name in data.gems and
                                  predicate(data.gem(name)["tags"]))
                     for role, predicate in role_predicates.items()}
    missing_roles = sorted(role for role in required_roles if not present_roles[role])
    main_group = next((group for group in groups if group.get("includeInFullDPS") == "true"), None)
    main_gems = [gem for gem in main_group.findall("Gem") if gem.get("gemId")] if main_group is not None else []
    item_set = active_item_set(root)
    items_by_id = {item.get("id"): item for item in root.findall("./Items/Item")}
    body_slot = next((slot for slot in item_set.findall("Slot") if slot.get("name") == "Body Armour"), None) if item_set is not None else None
    body = items_by_id.get(body_slot.get("itemId")) if body_slot is not None else None
    socket_line = next((line for line in (body.text or "").splitlines() if line.startswith("Sockets:")), "") if body is not None else ""
    main_socket_count = len(re.findall(r"[RGBW]-?", socket_line.split(":", 1)[-1]))
    spare_main_sockets = max(0, main_socket_count - len(main_gems))
    granted_groups = [group for group in groups if group.get("source", "").startswith("Item:")]
    tree_spec_element = resolve_tree_spec(root)
    active_tree_spec = tree_spec_element
    socket_ids = {socket.get("itemId") for socket in active_tree_spec.findall("./Sockets/Socket")
                  if socket.get("itemId") not in {None, "0"}} if active_tree_spec is not None else set()
    jewel_count = sum(1 for item in root.findall("./Items/Item") if item.get("id") in socket_ids and
                      data.bases.get(_item_parts(item.text or "")[2], {}).get("type") == "Jewel")
    build = root.find("Build")
    config_set = next((entry for entry in root.findall("./Config/ConfigSet")
                       if entry.get("id") == (root.find("./Config").get("activeConfigSet", "1"))), None)
    if config_set is None:
        config_set = root.find("./Config/ConfigSet")
    configured = {entry.get("name"): entry.get("string") for entry in
                  (config_set.findall("Input") if config_set is not None else [])}
    pantheon_selected = all(
        (configured.get(key) or (build.get(key) if build is not None else None) or "None") not in {"None", ""}
        for key in ("pantheonMajorGod", "pantheonMinorGod"))
    stats = calculation.get("stats", {})
    passive_data = calculation.get("passives", {})
    flask_ok = flasks_complete(xml, data, int(spec["level"]))
    selected_masteries = (len(re.findall(r"\{\d+,\d+\}", active_tree_spec.get("masteryEffects", "")))
                          if active_tree_spec is not None else 0)
    completeness_gaps = [f"missing {role} skill role" for role in missing_roles]
    if not flask_ok:
        completeness_gaps.append("fewer than five level-legal equipped flasks")
    if spare_main_sockets:
        completeness_gaps.append(f"{spare_main_sockets} unused main-link socket(s)")
    package = package_report(xml, spec, data)
    for gap in package["gaps"]:
        if gap not in completeness_gaps:
            completeness_gaps.append(gap)
    if spec.get("linkShortfall"):
        completeness_gaps.append("link shortfall: " + str(spec["linkShortfall"]))
    completeness = {
        "status": "gaps" if completeness_gaps else "complete",
        "gaps": completeness_gaps,
        "roles": {"required": sorted(required_roles), "present": present_roles},
        "mainLink": {"gemCount": len(main_gems), "socketCount": main_socket_count,
                     "unusedSockets": spare_main_sockets},
        "flasksComplete": flask_ok,
        "allocatedMasteries": selected_masteries,
        "equippedJewels": jewel_count,
        "itemGrantedSkillGroups": len(granted_groups),
        "socketedGemCount": package["counts"]["socketedGems"],
        "itemGrantedSkillCount": package["counts"]["itemGrantedSkills"],
        "supportedUtilityGroups": package["counts"]["supportedUtilityGroups"],
        "slotOccupancy": package["counts"]["slots"],
        "unspentPassivePoints": max(0, int(passive_data.get("maximum", 0)) - int(passive_data.get("used", 0))),
    }
    readiness_gaps = list(completeness_gaps)
    chaos = float(stats.get("ChaosResist", 0) or 0)
    ci_model = spec.get("defenseModel") == "ci"
    life_pool = (0.0 if ci_model else float(stats.get("Life", 0) or 0)) + float(stats.get("EnergyShield", 0) or 0)
    ehp = float(stats.get("TotalEHP", 0) or 0)
    armour = float(stats.get("Armour", 0) or 0)
    from build_evaluation import load_targets, tier_targets
    targets = load_targets()
    tier = tier_targets(spec, targets) if "archetype" in spec else {"dpsFloor": 0, "tier": ""}
    dps_floor = tier["dpsFloor"]
    dps_now = target_dps(stats, spec) if stats and "archetype" in spec else 0
    if dps_floor and dps_now < dps_floor:
        readiness_gaps.append(f"damage {dps_now:,.0f} is below the {dps_floor:,} screening target for {tier['tier']} builds "
                              f"(data/quality_targets.json v{targets['version']})")
    if chaos < 0 and not ci_model:
        readiness_gaps.append(f"chaos resistance is {chaos:.0f}% (0% target)")
    if life_pool < 6000:
        readiness_gaps.append(f"life plus energy shield is {life_pool:.0f} (6,000 target)")
    if ehp < 15000:
        readiness_gaps.append(f"effective hit pool is {ehp:.0f} (15,000 target)")
    if "Determination" in utility_names and armour < 10000:
        readiness_gaps.append(f"armour is {armour:.0f} with Determination selected (10,000 review target)")
    if not pantheon_selected:
        readiness_gaps.append("major and minor Pantheons are unselected")
    mapping = next((entry for entry in progression if entry.get("act") == 11), {})
    price_coverage = {
        "scope": "Endgame equipped items; rare gear remains a modifier-aware estimate",
        "complete": bool(price.get("complete")),
        "pricedSubtotalChaos": price.get("pricedSubtotalChaos"),
        "unknownSlots": price.get("unknown", []),
        "budgetStatus": price.get("budgetStatus"),
        "mappingUniqueSubtotalChaos": mapping.get("uniquePackageCostChaos"),
        "mappingPackagePriceStatus": ("no Mapping unique package selected"
                                      if not mapping.get("uniquePackage") else
                                      "unknown or unpriced; not a complete gear cost"
                                      if mapping.get("uniquePackageCostChaos") is None else
                                      "priced unique subtotal; rare slots remain unpriced"),
    }
    return {
        "completeness": completeness,
        "encounterReadiness": {"status": "review_gaps" if readiness_gaps else "targets_met",
                               "assessedEnemyLevel": spec.get("enemyLevel", 83),
                               "gaps": readiness_gaps,
                               "note": "These targets describe the configured PoB encounter; they do not predict a guaranteed boss kill."},
        "priceCoverage": price_coverage,
    }


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
    # Equipped jewel sockets and allocated keystones (mechanic decisions such as Chaos
    # Inoculation) are never traded away by a reroute.
    protected = set(jewels or {}) | {key for key in regular if nodes[key].get("isKeystone")}
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


POINT_FILL_SHARE = 220          # evaluations funded for spending leftover passive points
SANITY_BRANCH_LIMIT = 40        # removable branches measured by the tree waste check
WASTE_MIN_POINTS = 3            # a zero-value branch smaller than this (without a notable) is a normal leaf
WASTE_TOLERANCE_FILL = 0.002    # a filler node may cost at most this much objective (ln scale)


def fill_unspent_points(context, spec, allocated, masteries, render, worker, budget, calc, trace, jewels=None):
    """Spend leftover passive points on the best adjacent node (a measured non-loss), never leave them idle.

    The tree search only adds nodes with a measured positive gain, so a few points can stay unspent. A
    player spends every point; here each remaining point goes to the adjacent regular node with the best
    PoB objective (positive gain first, otherwise the least harmful useful node).  Nodes without a
    measurable gain are recorded as filler.
    """
    nodes = context["tree"]["nodes"]
    adjacency = graph(nodes, lambda node: not node.get("ascendancyName") and not node.get("isMastery")
                      and not node.get("isProxy")
                      and ("classStartIndex" not in node or node["classStartIndex"] == 3))
    budget.extend("point_fill", POINT_FILL_SHARE)
    start = budget.used
    allocated = set(allocated)
    current = calc
    base = candidate_score(current["stats"], spec)
    filler = []
    gained = []
    while current["passives"]["maximum"] - current["passives"]["used"] > 0:
        frontier = {neighbor for key in allocated if key in adjacency for neighbor in adjacency[key]} - allocated
        frontier = [key for key in frontier
                    if not nodes[key].get("isKeystone") and not nodes[key].get("isJewelSocket")
                    and "classStartIndex" not in nodes[key]]
        frontier.sort(key=lambda key: (-heuristic(nodes[key], spec), int(key)))
        best = None
        for key in frontier[:28]:
            if not budget.claim():
                break
            candidate = worker.request("calculate", xml=render(nodes=allocated | {key}))
            if candidate["passives"]["used"] > candidate["passives"]["maximum"]:
                continue
            if not all(check["passed"] for check in validate_calculation(candidate)):
                continue
            value = candidate_score(candidate["stats"], spec)
            if value + 1e-9 < base - WASTE_TOLERANCE_FILL:
                continue
            if best is None or value > best[0]:
                best = (value, key, candidate)
        if best is None:
            break
        value, key, current = best
        (gained if value > base + tree_sanity.WASTE_TOLERANCE else filler).append(key)
        allocated.add(key)
        base = max(base, value)
    budget.share("point_fill")["used"] += budget.used - start
    spec["_fillerNodes"] = sorted(set(spec.get("_fillerNodes", [])) | set(filler), key=int)
    if trace is not None:
        trace.append({"kind": "point_fill", "added": len(gained) + len(filler), "measuredGain": len(gained),
                      "filler": len(filler),
                      "unspentAfter": current["passives"]["maximum"] - current["passives"]["used"],
                      "reason": "leftover passive points spent on the best measured adjacent node"})
    current = dict(current)
    current["_allocated"] = allocated
    return current


def remove_wasted_branches(context, spec, allocated, masteries, render, worker, budget, calc, trace, jewels=None):
    """Measure every removable branch in PoB and drop the ones that do nothing (wasted travel/clusters)."""
    nodes = context["tree"]["nodes"]
    protected_fill = set(spec.get("_fillerNodes", ()))
    allocated = set(allocated)
    masteries = dict(masteries)
    removed_total, final_wasted = [], []
    budget.extend("tree_sanity", SANITY_BRANCH_LIMIT * 2 + 10)
    start = budget.used
    for _ in range(2):
        base = candidate_score(calc["stats"], spec)
        removals = tree_reroute_removals(nodes, allocated, jewels, limit=SANITY_BRANCH_LIMIT)

        def without(branch):
            remaining = allocated - branch
            groups = {nodes[key].get("group") for key in remaining if nodes[key].get("isNotable")}
            kept_masteries = {key: effect for key, effect in masteries.items()
                              if nodes[key].get("group") in groups}
            remaining = remaining - (set(masteries) - set(kept_masteries))
            if not budget.claim():
                return None
            candidate = worker.request("calculate", xml=render(nodes=remaining, mastery=kept_masteries))
            if not all(check["passed"] for check in validate_calculation(candidate)):
                return base - 1.0       # removal breaks legality: certainly not wasted
            return candidate_score(candidate["stats"], spec)

        wasted = tree_sanity.waste_report(without, base, removals, nodes)
        wasted = [entry for entry in wasted if not set(entry["nodes"]) <= protected_fill
                  and (entry["points"] >= WASTE_MIN_POINTS or entry["notables"])]
        final_wasted = wasted
        if not wasted:
            break
        # Remove the largest wasted branch, then re-measure: branches overlap.
        branch = set(max(wasted, key=lambda entry: (entry["points"], entry["nodes"]))["nodes"])
        allocated -= branch
        groups = {nodes[key].get("group") for key in allocated if nodes[key].get("isNotable")}
        masteries = {key: effect for key, effect in masteries.items() if nodes[key].get("group") in groups}
        allocated -= {key for key in allocated if nodes[key].get("isMastery") and key not in masteries}
        removed_total.append(sorted(branch, key=int))
        calc = worker.request("calculate", xml=render(nodes=allocated, mastery=masteries))
        final_wasted = []
    budget.share("tree_sanity")["used"] += budget.used - start
    spec["_treeWaste"] = final_wasted
    if trace is not None:
        trace.append({"kind": "tree_waste", "removedBranches": removed_total, "remainingWasted": final_wasted,
                      "reason": "branches whose removal does not lower the PoB objective are wasted travel"})
    calc = dict(calc)
    calc["_allocated"] = allocated
    calc["_masteries"] = masteries
    return calc


def sanity_from_xml(xml: str, context: dict, spec: dict, calc: dict) -> dict:
    """Tree sanity of the exported build (the saved XML is authoritative)."""
    root = ET.fromstring(xml)
    tree_spec = resolve_tree_spec(root)
    nodes = context["tree"]["nodes"]
    allocated = {key for key in (tree_spec.get("nodes", "").split(",") if tree_spec is not None else []) if key}
    masteries = {key: int(effect) for key, effect in re.findall(
        r"\{(\d+),(\d+)\}", tree_spec.get("masteryEffects", "") if tree_spec is not None else "")}
    items = {item.get("id"): item for item in root.findall("./Items/Item")}
    jewels = {socket.get("nodeId"): items[socket.get("itemId")] for socket in
              (tree_spec.findall("./Sockets/Socket") if tree_spec is not None else [])
              if socket.get("itemId") in items}
    wasted = spec.get("_treeWaste")
    return tree_sanity.sanity_report(nodes, spec, allocated, masteries, jewels, calc.get("passives"),
                                     spec["ascendancy"], int(spec["level"]), wasted,
                                     waste_measured=wasted is not None)


COVERAGE_SLOTS = ("Weapon 1", "Weapon 2", "Helmet", "Body Armour", "Gloves", "Boots", "Belt", "Amulet",
                  "Ring 1", "Ring 2", "Flask 1", "Flask 2", "Flask 3", "Flask 4", "Flask 5")


def unique_coverage_report(spec: dict, details: dict, uniques: dict, jewels: dict, price: dict,
                           trace: list | None) -> dict:
    """Per slot: which unique is equipped and why, or why the slot holds a rare (no justified candidate)."""
    prices = {row["slot"]: row for row in price.get("priced", [])}
    prices.update({row["slot"]: row for row in price.get("unknown", [])})
    gains = spec.get("uniqueScreenGain") or {}
    requested = set(spec.get("requestedUniques", ()))
    by_slot: dict[str, list[dict]] = {}
    for entry in trace or ():
        if entry.get("kind") in {"unique_candidate", "unique_candidate_skipped"} and entry.get("slot"):
            by_slot.setdefault(entry["slot"].split()[0] if entry["slot"].startswith(("Ring ", "Flask ")) else
                               entry["slot"], []).append(entry)
    rows = []
    for slot in COVERAGE_SLOTS:
        if slot.startswith("Weapon 2") and not any(row.get("slot") == "Weapon 2" for row in details.get("gear", [])):
            rows.append({"slot": slot, "unique": None, "reason": "slot not used (two-handed or no off-hand)"})
            continue
        text = uniques.get(slot)
        group = slot.split()[0] if slot.startswith(("Ring ", "Flask ")) else slot
        if text:
            name = _item_parts(text)[1]
            quote_row = prices.get(slot, {})
            rows.append({"slot": slot, "unique": name, "chaos": quote_row.get("chaos"),
                         "requested": name in requested,
                         "reason": ("requested by the user" if name in requested else
                                    f"measured gain {gains[name]:+.3f} objective (ln scale) within budget"
                                    if name in gains else "retained by the complete-design search after PoB scoring")})
            continue
        tried = by_slot.get(group, [])
        measured = [entry for entry in tried if entry.get("score_delta") is not None]
        if not tried:
            reason = "no compatible unique candidate in the shortlist"
        elif not measured:
            reason = "all shortlisted candidates exceed the unique budget"
        else:
            best = max(measured, key=lambda entry: entry["score_delta"])
            illegal = sum(bool(entry.get("failed_checks")) for entry in measured)
            reason = (f"{len(measured)} candidate(s) measured in PoB; best {best['name']} "
                      f"{best['score_delta']:+.3f}"
                      + (f" ({illegal} broke resistances/sockets/legality)" if illegal else "")
                      + ("; none improved the build" if best["score_delta"] < MIN_UNIQUE_GAIN else
                         "; gain did not survive combination with the purchases above or no budget left"))
        if group.startswith("Flask"):
            reason += "; flask effects are not modeled in PoB here (flasks are set inactive)"
        rows.append({"slot": slot, "unique": None, "reason": reason})
    jewel_names = [_item_parts(text)[1] for text in jewels.values() if isinstance(text, str)]
    return {"slotsWithUniques": sum(1 for row in rows if row.get("unique")) + len(jewel_names),
            "equipmentSlotsWithUniques": sum(1 for row in rows if row.get("unique")),
            "uniqueJewels": jewel_names, "jewelSockets": len(jewels), "slots": rows}


def complete_design_search(context, spec, data, worker, stage, trace, budget, state,
                           unique_options=(), gear_seeds=()):
    """Refine complete feasible builds in three bounded, deterministic sweeps.

    Every retained state includes its gear, links, tree, masteries, jewels and
    unique package. Neighbours are measured by PoB on that complete state;
    the shared SearchBudget counts each candidate evaluation.
    """
    width = int(spec.get("_designWidth", 6))
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
        jewels = tuple((slot, ("unique", value) if isinstance(value, str) else
                        ("rare", value.base, tuple((mod.get("id"), tuple(mod.get("lines", ())))
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

    def value_score(candidate):
        """Objective of a complete design; the unique budget is a hard constraint, not a score penalty."""
        return candidate["score"]

    def retain(candidates):
        distinct = {}
        for candidate in candidates:
            key = signature(candidate)
            if key not in distinct or value_score(candidate) > value_score(distinct[key]):
                distinct[key] = candidate
        return sorted(distinct.values(), key=lambda value: (-value_score(value), repr(signature(value))))[:width]

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
            if not solve_suffixes(candidate["items"], data, calculation["stats"], chaos_target=chaos_target(spec)):
                break
            if not budget.claim(reserve=reserve):
                for item in candidate["items"]:
                    item.mods = before[item.slot]
                break
            calculation = worker.request("calculate", xml=render_state(candidate))
            calculation = update_population(candidate, calculation)
            if calculation is None:
                return None
        gear_checks = validate_calculation(calculation) + unique_drawback_checks(
            candidate["uniques"], calculation["stats"], spec, data)
        if not all(check["passed"] for check in gear_checks):
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
    resource_utility_reserve = (len(mana_utility_levels(
        spec, data, state["calc"], state["items"], require_deficit=False)) + mastery_reserve_for(budget) +
        keystone_reserve_for(budget))
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
            measured_gain = spec.get("uniqueScreenGain") or {}

            def option_priority(option):
                gain = measured_gain.get(option[0])
                if gain is None:
                    return (1, 0.0, option[0], option[1])       # unmeasured or failed screening: after winners
                return (0 if gain >= unique_policy.min_gain(option[3]) else 2, -gain_per_cost(gain, option[3], spec), option[0], option[1])
            options = sorted(unique_options, key=option_priority)
            for parent in beam:
                tested = 0
                equipped_names = {_item_parts(text)[1] for text in parent["uniques"].values()}
                for name, slot, text, price in options:
                    if budget.limit - budget.used <= future_reserve:
                        break
                    if (tested >= GEAR_BEAM_TESTS_PER_PARENT or name in spec.get("requestedUniques", ()) or name in equipped_names
                            or (slot in parent["uniques"] and
                                _item_parts(parent["uniques"][slot])[1] in spec.get("requestedUniques", ()))):
                        continue
                    primary = parent["uniques"].get("Weapon 1")
                    offhand = parent["uniques"].get("Weapon 2")
                    if ((slot == "Weapon 2" and primary and unique_is_two_handed(primary, data)) or
                            (slot == "Weapon 1" and offhand and unique_is_two_handed(text, data))):
                        gear_name_failures[name] = "incompatible with an equipped two-handed weapon and off-hand slot"
                        continue
                    if not package_fits(spec, [value for key, value in parent.get("unique_prices", {}).items()
                                               if key != slot], [price]):
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
                    if scored["score"] <= parent["score"] + unique_policy.min_gain(price):
                        # An optional unique that does not measurably improve the design (for example a
                        # flask whose conditional effect PoB does not apply) is never kept.
                        gear_name_failures[name] = "did not improve the complete-design objective"
                        tested += 1
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
                and solve_suffixes(candidate["items"], data, calculation["stats"], chaos_target=chaos_target(spec))):
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
    best = sorted(verified, key=lambda candidate: (-value_score(candidate), repr(signature(candidate))))[0]
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


FLOOR_RETRY_EVALUATIONS = 450


def below_dps_floor(spec: dict, stats: dict) -> tuple[bool, dict]:
    """Whether the build's damage is under its (level-scaled) screening floor, plus the tier data."""
    from build_evaluation import tier_targets
    tier = tier_targets(spec)
    now = target_dps(stats, spec) if stats else 0
    return bool(tier["dpsFloor"] and now < tier["dpsFloor"]), {**tier, "dps": now}


def below_quality_floors(spec: dict, stats: dict) -> tuple[bool, dict]:
    """Damage, life-plus-ES pool or effective hit pool under the tier's screening floors."""
    below, tier = below_dps_floor(spec, stats)
    ci = spec.get("defenseModel") == "ci"
    pool = (0.0 if ci else float(stats.get("Life", 0) or 0)) + float(stats.get("EnergyShield", 0) or 0)
    weak = []
    if below:
        weak.append("damage")
    if pool < tier["lifePoolMin"]:
        weak.append("life pool")
    if float(stats.get("TotalEHP", 0) or 0) < tier["ehpMin"]:
        weak.append("effective hit pool")
    return bool(weak), {**tier, "weak": weak, "pool": pool}


def harder_design_pass(spec, context, data, worker, stage, trace, budget, best_design, unique_candidates):
    """When the chosen design is under its damage floor, fund and run a wider second search.

    The extra pass keeps the best design as a seed, widens the beam and spends a separate evaluation
    share; its result replaces the first only if the (value-adjusted) objective improves. The outcome is
    recorded so a build that still misses the floor is reported rather than silently accepted.
    """
    below, tier = below_quality_floors(spec, best_design["calc"]["stats"])
    if not below:
        return best_design
    stage("The design is below its screening floor (" + ", ".join(tier["weak"]) +
          "); running a wider complete-design search")
    budget.extend("floor_retry", FLOOR_RETRY_EVALUATIONS)
    previous_width = spec.get("_designWidth")
    spec["_designWidth"] = 9
    start = budget.used
    try:
        retry = complete_design_search(context, spec, data, worker, stage, trace, budget,
                                       copy.deepcopy(best_design), unique_candidates, [best_design])
    except ValueError as exc:   # the retry must never discard an already valid design
        retry = None
        if trace is not None:
            trace.append({"kind": "floor_retry", "error": str(exc)})
    finally:
        if previous_width is None:
            spec.pop("_designWidth", None)
        else:
            spec["_designWidth"] = previous_width
    budget.share("floor_retry")["used"] += budget.used - start
    gained = retry is not None and candidate_score(retry["calc"]["stats"], spec) >         candidate_score(best_design["calc"]["stats"], spec) + 0.001
    if trace is not None:
        trace.append({"kind": "floor_retry", "floor": tier["dpsFloor"], "dpsBefore": tier["dps"],
                      "weak": tier["weak"],
                      "dpsAfter": target_dps(retry["calc"]["stats"], spec) if retry else None,
                      "accepted": bool(gained)})
    spec["floorRetry"] = {"floor": tier["dpsFloor"], "before": tier["dps"], "weak": tier["weak"],
                          "after": target_dps(retry["calc"]["stats"], spec) if retry else None,
                          "accepted": bool(gained)}
    return retry if gained else best_design


def build_design(spec, context, market, app_root, data_root, stage, data=None, trace=None):
    worker = get_worker(app_root, data_root)
    initial_calls = worker.calls
    initial_export_recoveries = getattr(worker, "export_recoveries", 0)
    budget = SearchBudget(DESIGN_EVALUATION_LIMIT)
    data = data or GameData(worker.request("metadata"))
    allocated = initial_nodes(context, spec)
    items = rare_templates(data, spec["archetype"], spec["weaponType"],
                           damage_type=spec["damageType"],
                           base_damage_type=spec.get("baseDamageType", spec["damageType"]),
                           focus=spec["focus"], defense_model=spec.get("defenseModel", "hybrid"))
    items.extend(flask_templates(data, spec["level"]))
    supports, masteries, uniques = [], {}, {}
    jewels: dict[str, RareItem] = {}
    requested_uniques = set(spec.get("requestedUniques", []))
    if "The Queen's Hunger" in requested_uniques and "Desecrate" in data.gems:
        spec.setdefault("utility", {})["Desecrate"] = "Boots"
        spec["requestedUtilities"] = list(dict.fromkeys([*spec.get("requestedUtilities", []), "Desecrate"]))
    if requested_uniques and spec["noUniques"]:
        raise ValueError("The request asks for a unique item and rares-only equipment at the same time")

    def render(nodes=None, links=None, mastery=None, unique=None, jewelset=None, groups=None,
               main_group_id=None):
        return assemble(spec, context, data, allocated if nodes is None else nodes,
                        supports if links is None else links, items,
                        masteries if mastery is None else mastery, uniques if unique is None else unique,
                        jewels if jewelset is None else jewelset, skill_groups=groups,
                        main_group_id=main_group_id)

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
        if not solve_suffixes(items, data, calc["stats"], chaos_target=chaos_target(spec)):
            break
        if not budget.claim():
            break
        calc = worker.request("calculate", xml=render())
    # Lock a legal, scored main link before passive search. The tree objective
    # must see the same damage mechanism and support costs as the final design.
    supports = choose_main_link(spec, data, render, worker, stage, trace, budget)
    if budget.claim():
        calc = worker.request("calculate", xml=render())
    stage("Comparing damage-appropriate curses in Path of Building")
    calc = search_curse(spec, data, render, worker, calc, budget, trace)
    # Support requirements can add attributes; repair them before evaluating
    # passive overrides so every tree candidate uses legal final links.
    for _ in range(3):
        if not solve_suffixes(items, data, calc["stats"], chaos_target=chaos_target(spec)):
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
    phase_start = budget.used
    allocated, calc = search_tree(context, spec, allocated, lambda nodes: render(nodes=nodes), worker, stage,
                                  trace=trace, budget=budget, baseline_calc=calc,
                                  budget_reserve=(FINAL_REFINEMENT_RESERVE + mastery_reserve_for(budget) +
                                                  keystone_reserve_for(budget) + JEWEL_SEARCH_SHARE))
    budget.share("tree")["used"] += budget.used - phase_start
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
        if budget.limit - budget.used <= FINAL_REFINEMENT_RESERVE + keystone_reserve_for(budget):
            # Keep room for complete-build refinement and mechanics-aware keystones.
            break
        if calc["passives"]["used"] >= calc["passives"]["maximum"]:
            break
        best = None
        # Score up to eight likely effects; the final XML calculation verifies
        # mastery legality and point consumption, including duplicate effects.
        ranked = sorted(effects, key=lambda effect: -heuristic({"stats": effect.get("stats", [])}, spec))[:8]
        for effect in ranked:
            if not budget.claim(reserve=FINAL_REFINEMENT_RESERVE + keystone_reserve_for(budget)):
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
    unique_phase_start = budget.used
    unique_candidates = unique_options(context, spec, market, items, data, unique_defs)
    required_candidates = [option for option in unique_candidates if option[0] in requested_uniques]
    optional_by_slot = {}
    for option in unique_candidates:
        if option[0] not in requested_uniques:
            optional_by_slot.setdefault(option[1], []).append(option)
    # Keep optional equipment search bounded and representative across slots;
    # explicit requests always remain in the shortlist.
    optional_candidates = []
    mapping_unique_candidates = list(required_candidates)
    for slot, entries in sorted(optional_by_slot.items()):
        ranked = shortlist_slot_uniques(entries, spec)
        optional_candidates.extend(ranked)
        mapping_unique_candidates.extend(ranked)
    supported_interactions = []
    for names in UNIQUE_INTERACTIONS_BY_SKILL.get(spec["skill"], ()):
        interaction = []
        for name in names:
            if name in requested_uniques:
                continue
            option = next((entry for entry in unique_candidates if entry[0] == name), None)
            if option is not None:
                if not any(entry[0] == name for entry in optional_candidates):
                    optional_candidates.append(option)
                if not any(entry[0] == name for entry in mapping_unique_candidates):
                    mapping_unique_candidates.append(option)
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
                      "reason": "all requested compatible slot candidates plus up to two optional endgame candidates per equipment slot"})
    unique_screen: dict[tuple[str, str], dict] = {}
    slot_options = {(name, slot): (name, slot, text, price) for name, slot, text, price in unique_candidates}

    def measure_single(name, slot, text, candidate_price, package_reserve, cur_uniques=None, cur_calc=None):
        """One unique in ``slot`` on the given design, rare suffixes repaired; state is restored."""
        cur_uniques = uniques if cur_uniques is None else cur_uniques
        cur_calc = calc if cur_calc is None else cur_calc
        snapshots = {item.slot: list(item.mods) for item in items}
        previous_minion_count = spec.get("minionCount")
        displaced = next((item for item in items if item.slot == slot), None)
        if displaced is not None:
            displaced.mods.clear()
        trial_uniques = {**cur_uniques, slot: text}
        try:
            candidate = worker.request("calculate", xml=render(unique=trial_uniques))
            if sync_permanent_minion_count(spec, candidate):
                if budget.claim(reserve=package_reserve):
                    candidate = worker.request("calculate", xml=render(unique=trial_uniques))
                else:
                    budget.exhausted = True
            for _ in range(3):
                if not solve_suffixes(items, data, candidate["stats"], chaos_target=chaos_target(spec)):
                    break
                if not budget.claim(reserve=package_reserve):
                    budget.exhausted = True
                    break
                candidate = worker.request("calculate", xml=render(unique=trial_uniques))
            checks = validate_calculation(candidate)
            checks.extend(unique_drawback_checks(trial_uniques, candidate["stats"], spec, data))
            outcome = {"candidate": candidate, "checks": checks, "trial_uniques": trial_uniques,
                       "delta": score(candidate["stats"], spec) - score(cur_calc["stats"], spec),
                       "mods": {item.slot: list(item.mods) for item in items},
                       "minion_count": spec.get("minionCount")}
        finally:
            if previous_minion_count is None:
                spec.pop("minionCount", None)
            else:
                spec["minionCount"] = previous_minion_count
            for item in items:
                item.mods = snapshots[item.slot]
        return outcome

    # Screening: measure every shortlisted candidate against the same design (rare-only plus requested
    # items). Nothing is bought yet; the screen only records PoB's marginal gain and legality.
    for name, slot, text, candidate_price in unique_candidates:
        if name in {_item_parts(value)[1] for value in uniques.values()} or slot in uniques:
            continue
        if slot.startswith(("Ring ", "Flask ")) and any(
                other_name == name and other != slot and other.split()[0] == slot.split()[0]
                and (other_name, other) in unique_screen for other_name, other in slot_options):
            continue    # the same unique in a sibling ring/flask slot was already measured
        if not package_fits(spec, unique_prices.values(), [candidate_price]):
            unique_screen[(name, slot)] = {"reason": "does not fit the unique budget on its own"}
            continue
        package_reserve = 0 if name in requested_uniques else FINAL_REFINEMENT_RESERVE
        if not budget.claim(reserve=package_reserve):
            if trace is not None:
                trace.append({"kind": "search_limit", "phase": "unique_search", "used": budget.used})
            break
        outcome = measure_single(name, slot, text, candidate_price, package_reserve)
        legal = all(check["passed"] for check in outcome["checks"])
        unique_screen[(name, slot)] = {"delta": outcome["delta"], "legal": legal, "price": candidate_price,
                                       "failed": [check["name"] for check in outcome["checks"]
                                                  if not check["passed"]]}
    # Purchase: measured gain per cost (quoted items before unquoted ones of equal value), each item
    # re-measured on top of what is already bought; the budget may be fully used, never exceeded.
    # Gains interact (an item that costs elemental resistance can block better partners), so the same
    # candidates are bought in three orders and the complete design with the best objective is kept.
    qualified = {key: row for key, row in unique_screen.items()
                 if row.get("legal") and row["delta"] >= unique_policy.min_gain(row["price"])}
    base_snapshot = {"mods": {item.slot: list(item.mods) for item in items}, "uniques": dict(uniques),
                     "prices": dict(unique_prices), "calc": calc, "minion": spec.get("minionCount")}
    unique_purchase_log = []

    def restore_base():
        for item in items:
            item.mods = list(base_snapshot["mods"][item.slot])
        if base_snapshot["minion"] is None:
            spec.pop("minionCount", None)
        else:
            spec["minionCount"] = base_snapshot["minion"]

    def purchase_pass(label, order):
        restore_base()
        pass_uniques, pass_prices, pass_calc = dict(base_snapshot["uniques"]), dict(base_snapshot["prices"]), base_snapshot["calc"]
        bought, rejected = [], []
        for (name, slot) in order:
            row = qualified[(name, slot)]
            group = slot.split()[0] if slot.startswith(("Ring ", "Flask ")) else None
            group_slots = [slot] if group is None else sorted(
                other for other_name, other in slot_options if other_name == name and other.split()[0] == group)
            free = next((other for other in sorted(group_slots) if other not in pass_uniques), None)
            if free is None or name in {_item_parts(value)[1] for value in pass_uniques.values()}:
                continue
            _, _, text, candidate_price = slot_options[(name, free)]
            if ((free == "Weapon 2" and "Weapon 1" in pass_uniques and unique_is_two_handed(pass_uniques["Weapon 1"], data)) or
                    (free == "Weapon 1" and "Weapon 2" in pass_uniques and unique_is_two_handed(text, data))):
                continue
            if not package_fits(spec, pass_prices.values(), [candidate_price]):
                unique_purchase_log.append({"pass": label, "name": name, "slot": free,
                                            "reason": "no longer fits the remaining budget"})
                continue
            if not budget.claim(reserve=0 if name in requested_uniques else FINAL_REFINEMENT_RESERVE):
                break
            outcome = measure_single(name, free, text, candidate_price, FINAL_REFINEMENT_RESERVE,
                                     pass_uniques, pass_calc)
            checks_ok = all(check["passed"] for check in outcome["checks"])
            gain = outcome["delta"]
            accepted = checks_ok and worth_price(spec, gain, [candidate_price], list(pass_prices.values()))
            unique_purchase_log.append({"pass": label, "name": name, "slot": free, "gain": round(gain, 5),
                                        "accepted": accepted, "legal": checks_ok})
            if trace is not None:
                trace.append({"kind": "unique_candidate", "name": name, "slot": free, "pass": label,
                              "requested": name in requested_uniques, "evaluated": True,
                              "selected": accepted, "quoted_price": candidate_price,
                              "screenDelta": round(row["delta"], 6), "score_delta": round(gain, 6),
                              "gainPerChaos": round(gain_per_cost(gain, candidate_price, spec), 6),
                              "failed_checks": [check["name"] for check in outcome["checks"] if not check["passed"]],
                              "reason": ("improved objective by measured gain per cost and passed legality checks"
                                         if accepted else "failed legality checks" if not checks_ok else
                                         "gain vanished once earlier purchases were in place")})
            if accepted:
                pass_uniques = outcome["trial_uniques"]
                pass_prices[free] = candidate_price
                for item in items:
                    item.mods = outcome["mods"][item.slot]
                if outcome["minion_count"] is not None:
                    spec["minionCount"] = outcome["minion_count"]
                pass_calc = outcome["candidate"]
                bought.append(name)
            else:
                rejected.append((name, slot, gain))
        return {"label": label, "uniques": pass_uniques, "prices": pass_prices, "calc": pass_calc,
                "mods": {item.slot: list(item.mods) for item in items}, "minion": spec.get("minionCount"),
                "bought": bought, "rejected": rejected, "score": score(pass_calc["stats"], spec)}

    by_ratio = sorted(qualified, key=lambda key: (-gain_per_cost(qualified[key]["delta"], qualified[key]["price"], spec), key))
    by_gain = sorted(qualified, key=lambda key: (-qualified[key]["delta"], key))
    passes = []
    if qualified:
        passes.append(purchase_pass("gain per cost", by_ratio))
        if len(qualified) > 1 and budget.limit - budget.used > FINAL_REFINEMENT_RESERVE:
            passes.append(purchase_pass("absolute gain", by_gain))
        # Items that were legal alone but rejected after earlier purchases go first in a third order.
        blocked = [(name, slot) for entry in passes for name, slot, _ in entry["rejected"]
                   if (name, slot) in qualified]
        blocked = sorted(dict.fromkeys(blocked), key=lambda key: -qualified[key]["delta"])
        if blocked and budget.limit - budget.used > FINAL_REFINEMENT_RESERVE:
            third = blocked + [key for key in by_ratio if key not in blocked]
            if [tuple(key) for key in third] != [tuple(key) for key in by_ratio]:
                passes.append(purchase_pass("blocked partners first", third))
    if passes:
        best_pass = max(passes, key=lambda entry: (round(entry["score"], 6), -len(entry["bought"])))
        uniques, unique_prices, calc = best_pass["uniques"], best_pass["prices"], best_pass["calc"]
        for item in items:
            item.mods = best_pass["mods"][item.slot]
        if best_pass["minion"] is None:
            spec.pop("minionCount", None)
        else:
            spec["minionCount"] = best_pass["minion"]
        if trace is not None:
            trace.append({"kind": "unique_purchase_passes",
                          "passes": [{"label": entry["label"], "bought": entry["bought"],
                                      "score": round(entry["score"], 5)} for entry in passes],
                          "chosen": best_pass["label"],
                          "reason": "the same measured candidates were bought in three orders; the best complete design was kept"})
    else:
        restore_base()
    spec["uniqueScreenGain"] = {name: round(row["delta"], 5) for (name, _), row in unique_screen.items()
                                if row.get("delta") is not None and row.get("legal")}
    if trace is not None:
        for (name, slot), row in unique_screen.items():
            if row.get("delta") is None:
                trace.append({"kind": "unique_candidate_skipped", "name": name, "slot": slot,
                              "reason": row.get("reason", "not measured")})
                continue
            if row.get("delta") is not None and not any(
                    entry.get("kind") == "unique_candidate" and entry.get("name") == name for entry in trace):
                trace.append({"kind": "unique_candidate", "name": name, "slot": slot,
                              "requested": name in requested_uniques, "evaluated": True, "selected": False,
                              "quoted_price": row.get("price"), "score_delta": round(row["delta"], 6),
                              "failed_checks": row.get("failed", []),
                              "reason": ("failed legality checks" if not row.get("legal") else
                                         "did not improve objective" if row["delta"] < unique_policy.min_gain(row.get("price")) else
                                         "slot or budget taken by a purchase with a better gain per cost")})
    # Some uniques are useful only as a package: a reservation-enabling item
    # can make a high-cost aura setup viable, or two items can compensate for
    # each other's displaced rare affixes. Test a bounded, deterministic set
    # of full PoB equipment pairs, including pairs anchored by a selected
    # requested/single-item unique.
    pair_candidates = unique_pair_shortlist(unique_candidates, uniques, spec["archetype"],
                                            spec["damageType"], limit=20)
    pair_candidates = [(first, second) for first, second in pair_candidates
                       if not unique_pair_has_weapon_conflict(first, second, data)]
    package_best = None
    pair_design_seeds = []
    for pair_index, (first, second) in enumerate(pair_candidates):
        additions = [member for member in (first, second) if not member[4]]
        if not package_fits(spec, [value for key, value in unique_prices.items()
                                   if key not in {member[1] for member in additions}],
                            [member[3] for member in additions]):
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
            if not solve_suffixes(items, data, candidate["stats"], chaos_target=chaos_target(spec)):
                break
            candidate = worker.request("calculate", xml=render(unique=trial_uniques))
        checks = validate_calculation(candidate)
        checks.extend(unique_drawback_checks(trial_uniques, candidate["stats"], spec, data))
        delta = score(candidate["stats"], spec) - score(calc["stats"], spec)
        accepted = all(check["passed"] for check in checks) and delta >= MIN_UNIQUE_GAIN and worth_price(
            spec, delta, [member[3] for member in additions if member[0] not in requested_uniques],
            list(unique_prices.values()))
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
    jewel_unique_defs = unique_jewel_shortlist(unique_defs, spec["noUniques"], requested_uniques, spec, market,
                                                   unobtainable_unique_names(context))
    for definition in jewel_unique_defs:
        name = definition["name"]
        raw = definition.get("raw") or ""
        special = re.search(r"(?i)radius|allocat(?:e|es|ed|ing)|transforms?|keystone|passive skills? in", raw)
        if special:
            if name in requested_uniques:
                raise ValueError(f"Requested unique jewel '{name}' needs unsupported passive-tree transformation mechanics")
            continue
        socket, extra_nodes = pick_unique_jewel_socket(context, spec, allocated, jewels, calc,
                                                       name in requested_uniques)
        if socket is None:
            if name in requested_uniques:
                raise ValueError(f"Requested unique jewel '{name}' needs an available allocated ordinary jewel socket")
            continue
        jewel_quote = unique_market_price(definition, market)
        if not package_fits(spec, unique_prices.values(), [jewel_quote]):
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
        if (jewel_base not in data.bases or data.bases[jewel_base].get("type") != "Jewel"
                or not data.bases[jewel_base].get("tags", {}).get("jewel")):
            # Abyss/other jewel bases need their own socket types, not an ordinary tree socket.
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
        candidate = worker.request("calculate", xml=render(nodes=allocated | extra_nodes))
        previous_minion_count = spec.get("minionCount")
        if sync_permanent_minion_count(spec, candidate):
            if budget.claim():
                candidate = worker.request("calculate", xml=render(nodes=allocated | extra_nodes))
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
        accepted = all(check["passed"] for check in checks) and (
            name in requested_uniques or (delta >= MIN_UNIQUE_GAIN and worth_price(
                spec, delta, [jewel_quote], list(unique_prices.values()))))
        if trace is not None:
            trace.append({"kind": "unique_jewel_candidate", "name": name, "node": socket,
                          "requested": name in requested_uniques, "quoted_price": jewel_quote,
                          "score_delta": round(delta, 6), "selected": accepted,
                          "reason": "requested or improved objective and passed legality checks" if accepted else
                                   "failed legality checks" if not all(check["passed"] for check in checks) else
                                   "did not improve objective"})
        if accepted:
            jewels[socket], calc = text, candidate
            allocated = allocated | extra_nodes
            unique_prices[f"Jewel {socket}"] = jewel_quote
        else:
            if previous_minion_count is not None:
                spec["minionCount"] = previous_minion_count
            if previous is None:
                jewels.pop(socket, None)
            else:
                jewels[socket] = previous
    budget.share("uniques")["used"] += budget.used - unique_phase_start
    selected_unique_names = {_item_parts(value)[1] for value in uniques.values()}
    selected_unique_names.update(_item_parts(value)[1] for value in jewels.values() if isinstance(value, str))
    missing_requested = requested_uniques - selected_unique_names
    if missing_requested:
        reasons = []
        for name in sorted(missing_requested):
            reason = missing_unique_reason(name, unique_defs, market, spec["budgetChaos"])
            failed = sorted({check for entry in (trace or []) if entry.get("name") == name
                             for check in entry.get("failed_checks", [])})
            if failed:
                reason += " (failed checks: " + "; ".join(failed) + ")"
            reasons.append(reason)
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
    design_phase_start = budget.used
    best_design = complete_design_search(context, spec, data, worker, stage, trace, budget,
                                         base_state, optional_candidates,
                                         [all_rare_seed, *pair_design_seeds])
    budget.share("complete_design")["used"] += budget.used - design_phase_start
    best_design = harder_design_pass(spec, context, data, worker, stage, trace, budget, best_design,
                                     optional_candidates)
    allocated = best_design["nodes"]
    supports = best_design["supports"]
    spec["mappingSupportPlan"] = mapping_support_plan(trace or [], supports)
    if spec.get("mappingLinkSupports"):
        spec["mappingSupportPlan"]["mappingLink"] = list(spec["mappingLinkSupports"])
    items = best_design["items"]
    masteries = best_design["masteries"]
    uniques = best_design["uniques"]
    unique_prices = best_design.get("unique_prices", unique_prices)
    spec["unmodeledFlaskEffects"] = sorted({_item_parts(text)[1] for slot, text in uniques.items()
                                               if slot.startswith("Flask ")})
    jewels = best_design["jewels"]
    calc = best_design["calc"]
    allocated, jewels = reconcile_jewel_sockets(context, allocated, jewels, unique_prices, calc, trace,
                                                "complete_design_search")
    # Tree reroutes can remove an old mastery group and connect a new one.
    # Score those newly reachable effects against the final complete package,
    # using the evaluation share protected from optional refinements above.
    used_effects = {effect for effect in masteries.values()}
    post_refinement_utility_reserve = len(mana_utility_levels(
        spec, data, calc, items, require_deficit=False))
    newly_unlocked = mastery_choices(context, allocated)
    newly_unlocked.sort(key=lambda entry: -heuristic(context["tree"]["nodes"][entry[0]], spec))
    for node, effects in newly_unlocked:
        if node in masteries or calc["passives"]["used"] >= calc["passives"]["maximum"]:
            continue
        best = None
        ranked = sorted(effects, key=lambda effect: -heuristic({"stats": effect.get("stats", [])}, spec))[:8]
        for effect in ranked:
            if not budget.claim(reserve=keystone_reserve_for(budget) + post_refinement_utility_reserve):
                break
            identifier = effect.get("effect")
            if identifier in used_effects:
                continue
            candidate = worker.request("calculate", xml=render(
                nodes=allocated | {node}, mastery={**masteries, node: identifier}))
            if (candidate["passives"]["used"] <= candidate["passives"]["maximum"] and
                    candidate_score(candidate["stats"], spec) > candidate_score(calc["stats"], spec) + 0.001):
                if best is None or candidate_score(candidate["stats"], spec) > candidate_score(best[1]["stats"], spec):
                    best = (identifier, candidate)
        if best:
            masteries[node], calc = best
            allocated.add(node)
            used_effects.add(best[0])
            if trace is not None:
                trace.append({"kind": "mastery_selection", "node": node, "effect": best[0],
                              "reason": "best compatible PoB score after complete-build tree refinement"})
    calc = repair_chaos_resistance(spec, data, items, render, worker, budget, calc, trace)
    save_path = os.environ.get("WITCHCRAFT_SAVE_STATE")
    if save_path:   # debugging aid: replay the package/Pantheon/socket phases offline against real PoB
        import pickle
        with open(save_path, "wb") as handle:
            pickle.dump({"spec": copy.deepcopy(spec), "items": copy.deepcopy(items), "uniques": dict(uniques),
                         "supports": list(supports), "masteries": dict(masteries), "jewels": copy.deepcopy(jewels),
                         "allocated": set(allocated), "calc": calc, "unique_prices": dict(unique_prices)}, handle)
    stage("Planning supporting skill packages against the complete build")
    calc = plan_supporting_skills(spec, data, render, worker, budget, items, uniques, supports, calc, trace,
                                  context=context, allocated=allocated, jewels=jewels)
    spec.pop("_repairGroups", None)
    if "_allocated" in calc:
        allocated = calc.pop("_allocated")
        allocated, jewels = reconcile_jewel_sockets(context, allocated, jewels, unique_prices, calc, trace,
                                                    "mana_repair")
    stage("Testing mechanics-compatible keystone packages in PoB")
    calc = refill_unspent_points(context, spec, allocated, render, worker, stage, budget, calc, trace, jewels)
    allocated = calc.pop("_allocated", allocated)
    allocated, jewels = reconcile_jewel_sockets(context, allocated, jewels, unique_prices, calc, trace,
                                                "tree_refill")
    keystone_candidates = keystone_package_candidates(context, spec, calc, data, items, allocated,
                                                       masteries, jewels)
    current_score = candidate_score(calc["stats"], spec)
    best_keystone = None
    for name, node, path, rationale, removed, candidate_masteries in keystone_candidates:
        if not budget.claim(reserve=post_refinement_utility_reserve):
            break
        candidate_nodes = (allocated - set(removed)) | set(path)
        candidate = worker.request("calculate", xml=render(nodes=candidate_nodes,
                                                              mastery=candidate_masteries))
        candidate_value = candidate_score(candidate["stats"], spec)
        legal = (candidate["passives"]["used"] <= candidate["passives"]["maximum"] and
                 all(check["passed"] for check in validate_calculation(candidate)) and
                 all(check["passed"] for check in validate_design(context, spec, candidate_nodes,
                             candidate_masteries, items, uniques, data, jewels)))
        selected = legal and candidate_value > current_score + 0.001
        if trace is not None:
            trace.append({"kind": "keystone_package", "name": name, "node": node,
                          "path": path, "eligibleMechanic": rationale,
                          "removedNodes": removed,
                          "skill": spec["skill"], "enemyLevel": spec.get("enemyLevel", 83),
                          "scoreDelta": round(candidate_value - current_score, 6),
                          "selected": selected, "passedLegality": legal,
                          "comparison": "Same PoB skill, enemy level and configuration; only the connected keystone path changed."})
        if selected and (best_keystone is None or candidate_value > best_keystone[0]):
            best_keystone = (candidate_value, name, node, candidate_nodes, candidate_masteries, candidate)
    if best_keystone:
        _, name, node, allocated, masteries, calc = best_keystone
        allocated, jewels = reconcile_jewel_sockets(context, allocated, jewels, unique_prices, calc, trace,
                                                    "keystone_package")
        if trace is not None:
            trace.append({"kind": "keystone_selection", "name": name, "node": node,
                          "reason": "mechanics-compatible package improved the same-encounter PoB score and passed legality"})
    stage("Checking the passive tree for wasted travel and unspent points")
    spec.pop("_fillerNodes", None)
    filled = fill_unspent_points(context, spec, allocated, masteries, render, worker, budget, calc, trace, jewels)
    allocated = filled.pop("_allocated")
    calc = filled
    pruned = remove_wasted_branches(context, spec, allocated, masteries, render, worker, budget, calc, trace, jewels)
    allocated, masteries, calc = pruned.pop("_allocated"), pruned.pop("_masteries"), pruned
    filled = fill_unspent_points(context, spec, allocated, masteries, render, worker, budget, calc, trace, jewels)
    allocated = filled.pop("_allocated")
    calc = filled
    allocated, jewels = reconcile_jewel_sockets(context, allocated, jewels, unique_prices, calc, trace, "tree_sanity")
    if not spec.get("skillGroups") and mana_utility_levels(spec, data, calc, items):
        stage("Testing Clarity levels to sustain repeated mana use")
        calc = search_mana_utility(spec, data, items, render, worker, calc, budget, trace)
    reconcile_unique_package_trace(trace, {_item_parts(text)[1] for text in uniques.values()})
    calc = settle_reservations(spec, render, worker, budget, calc, trace)
    stage("Selecting the major and minor Pantheon for this build")
    calc = select_pantheon(spec, render, worker, budget, calc, trace)
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
    xml, disabled_item_skills = disable_conditional_item_skill_groups(xml)
    if disabled_item_skills:
        spec["disabledConditionalItemSkills"] = disabled_item_skills
        calc = export_with_pob(xml, app_root, data_root)
        xml = calc.pop("xml")
        if disable_conditional_item_skill_groups(xml)[1]:
            raise ValueError("PoB re-enabled unsupported conditional item-granted skills during exact export")
    checks, details = validate_structure(xml, context, spec["ascendancy"], spec["skill"])
    checks.extend(validate_calculation(calc))
    checks.extend(validate_design(context, spec, allocated, masteries, items, uniques, data, jewels))
    resources_ok = calc["stats"].get("ManaUnreserved", 0) >= calc["stats"].get("ManaCost", 0)
    if not resources_ok and len(supports) >= MAIN_SUPPORT_TARGET:
        # A complete six-link is kept; the unresolved deficit is reported by the
        # quality assessment instead of discarding the whole build.
        spec["linkResourceDeficit"] = {"ManaCost": calc["stats"].get("ManaCost"),
                                       "ManaUnreserved": calc["stats"].get("ManaUnreserved")}
        resources_ok = True
    checks.append({"name": "Usable main skill resources", "passed": resources_ok,
                   "reason": "Unreserved mana must cover the main skill's cost"})
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
    # Stage consistency: the Mapping stage may only equip a (cheaper) subset of the uniques the final
    # build keeps, so nothing bought for Mapping is thrown away at the Endgame.
    final_unique_slots = {slot: _item_parts(text)[1] for slot, text in uniques.items()
                          if not slot.startswith("Flask ")}
    mapping_unique_candidates = [option for option in unique_options_all
                                 if final_unique_slots.get(option[1]) == option[0]]
    prior_calls = worker.calls
    close_worker()
    worker = get_worker(app_root, data_root)
    worker.calls = prior_calls
    xml, progression = add_progression(xml, spec, context, data, worker, stage,
                                       unique_candidates=mapping_unique_candidates, market=market)
    stage("Exporting and verifying the complete campaign-to-endgame PoB")
    expected_endgame = progression[-1].pop("_calculation")
    final = export_with_pob(xml, app_root, data_root)
    xml = final.pop("xml")
    final_disabled, disabled_again = disable_conditional_item_skill_groups(xml)
    if disabled_again:
        xml = final_disabled
        spec["disabledConditionalItemSkills"] = disabled_again
        final = export_with_pob(xml, app_root, data_root)
        xml = final.pop("xml")
    active_conditional_skills = disable_conditional_item_skill_groups(xml)[1]
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
    structural, details = validate_structure(xml, context, spec["ascendancy"], spec["skill"],
                                             required_main_links=MAIN_SUPPORT_TARGET + 1)
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
    checks.append({"name": "Conditional item-granted skill uptime", "passed": not active_conditional_skills,
                   "reason": ("No unmodeled triggered offering is enabled"
                              if not active_conditional_skills else
                              "An item-granted offering is enabled without modeled trigger and corpse uptime")})
    price = quote(details["gear"], market, spec["budgetChaos"] if spec["budgetChaos"] is not None else 10_000_000)
    if spec["budgetChaos"] is None:
        price["budgetChaos"] = None   # budgetStatus comes from the single unique_pricing resolver
    price["policy"] = policy_summary(spec)
    budget_rows = [{"slot": row["slot"], "name": row["name"], "chaos": row.get("chaos"),
                    "requested": row["name"] in requested_uniques} for row in price.get("priced", [])]
    budget_rows += [{"slot": row["slot"], "name": row["name"], "chaos": None,
                     "requested": row["name"] in requested_uniques} for row in price.get("unknown", [])]
    price["uniqueBudget"] = budget_report(spec, budget_rows)
    if spec["budgetChaos"] is None:
        report = price["uniqueBudget"]
        price["budgetStatus"] = (
            f"Standard budget {report['budgetDivine']:g} divine ({report['budgetChaos']:g} chaos): "
            f"{report['spentChaos']:g} spent, {report['remainingChaos']:g} remaining"
            + (f"; unquoted assumed at {unique_policy.assumed_unpriced(spec):g} chaos each: "
               + ", ".join(report["unpricedAssumed"]) if report["unpricedAssumed"] else ""))
        if not report["withinBudget"] and not report["requestedExceedBudget"]:
            raise ValueError(f"Unique equipment costs {report['spentChaos']:g} chaos, over the standard budget "
                             f"of {report['budgetChaos']:g} chaos")
    if spec["budgetChaos"] is not None and price["pricedSubtotalChaos"] > spec["budgetChaos"]:
        raise ValueError("Priced equipment alone exceeds the requested budget")
    unspent_points = max(0, calc["passives"]["maximum"] - calc["passives"]["used"])
    passive_reason = (f"{unspent_points} points remain; the evaluation limit stopped further refinements."
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
              "mechanics": {**profile, "damageType": spec["damageType"],
                            "damageMechanism": spec.get("damageMechanism")},
              "resourcePlan": {"reserveFraction": spec.get("resourceReserveFraction", 0.15),
                               "purpose": "Reserve part of calculated recovery for movement, curses and other utility casts",
                               "sustain": sustained_resource_use(calc.get("stats", {}), spec)},
              "mappingPlan": spec.get("mappingSupportPlan", mapping_support_plan(trace or [], supports)),
              "conditionalItemSkillsDisabled": spec.get("disabledConditionalItemSkills", []),
              "selectionReasons": {"ascendancy": ("Selected from the requested skill tags and explicit prompt choices" +
                                                     (": " + ", ".join(selected_ascendancy) if selected_ascendancy else ".")),
                                   "passives": "PoB-calculated candidate gains; neutral leaves pruned; " + passive_reason,
                                   "importantPassives": important_passives[:12],
                                   "utility": ([spec["resourceUtilityReason"]]
                                               if spec.get("resourceUtilityReason") else []),
                                   "curse": ([f"{expected_curse} selected: {spec.get('curseSelectionReason', 'damage-type-compatible curse')}; ongoing uptime is not assumed"]
                                             if (expected_curse := spec.get("expectedCurse")) else []),
                                   "uniques": unique_selection_reasons,
                                   "jewels": [f"Rare jewel selected in passive socket {node} after PoB scoring"
                                              for node in sorted(jewels, key=int)]},
              "assumptions": population_assumptions(spec),
              "constraints": spec, "evaluations": worker.calls - initial_calls,
              "exportRecoveries": getattr(worker, "export_recoveries", 0) - initial_export_recoveries,
              "unmodeledFlaskEffects": list(spec.get("unmodeledFlaskEffects", [])),
              "designEvaluations": budget.used,
              "searchShares": {name: dict(share) for name, share in budget.shares.items()},
              "searchLimit": budget.limit, "reserveBlocked": budget.reserve_blocked,
              "skillPackagePlan": spec.get("skillPlanSummary"),
              "manaRepair": spec.get("manaRepair"),
              "defenseModel": spec.get("defenseModel", "hybrid"),
              "linkShortfall": spec.get("linkShortfall"), "linkFiller": spec.get("linkFiller"),
              "searchLimitWarning": (f"Search stopped at {budget.used}/{budget.limit} design evaluations after preserving mandatory search budget; best feasible candidate retained"
                                     if budget.exhausted else None)}
    recipe["progression"] = progression
    recipe["qualityDiagnostics"] = build_quality_report(xml, spec, details, calc, data, price, progression)
    sanity = sanity_from_xml(xml, context, spec, calc)
    recipe["treeSanity"] = sanity
    checks.extend(sanity["checks"])
    if not sanity["passed"]:
        readiness = recipe["qualityDiagnostics"].get("encounterReadiness")
        if readiness is not None:
            readiness["gaps"] = [*readiness.get("gaps", []), *[
                f"{check['name']}: {check['reason']}" for check in sanity["checks"] if not check["passed"]]]
            readiness["status"] = "review_gaps"
    recipe["uniqueCoverage"] = unique_coverage_report(spec, details, uniques, jewels, price, trace)
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
    if "The Queen's Hunger" in spec["requestedUniques"] and "Desecrate" in data.gems:
        # Give its corpse-consuming triggered offerings an explicit boss-corpse source.
        spec["utility"]["Desecrate"] = "Boots"
        spec["requestedUtilities"] = list(dict.fromkeys([*spec.get("requestedUtilities", []), "Desecrate"]))
    xml, recipe, checks, details, calculation, price = build_design(
        spec, context, market, app_root, data_root, stage, data)
    mechanics = recipe.get("mechanics") or mechanic_profile(spec)
    recipe["mechanics"] = mechanics
    mechanic_checks = assess_mechanics(spec, calculation, mechanics, xml, context)
    quality_status, quality_warnings = assess_quality(
        spec, calculation, mechanics, mechanic_checks, bool(recipe.get("searchLimitWarning")),
        recipe.get("qualityDiagnostics"))
    quality_report = final_quality_report(
        xml, spec, calculation, mechanic_checks, recipe.get("qualityDiagnostics", {}), data,
        search_limited=bool(recipe.get("searchLimitWarning")), assessed_status=quality_status).to_dict()
    quality_status = quality_report["status"] if quality_status == "validated" else quality_status
    recipe["qualityReport"] = quality_report
    return {"id": "g" + secrets.token_hex(10), "name": spec["skill"] + " " + spec["ascendancy"],
            "class": "Witch", "ascendancy": spec["ascendancy"], "mainSkill": spec["skill"],
            "level": details["level"], "gems": details["gems"], "treeNodes": details["treeNodes"],
            "ascendancyPoints": details["ascendancyPoints"], "gear": details["gear"],
            "validation": checks, "mechanicChecks": mechanic_checks,
            "qualityStatus": quality_status, "qualityWarnings": quality_warnings,
            "qualityReport": quality_report,
            "loadout": loadout_view(xml, data.gems),
            "completeness": recipe.get("qualityDiagnostics", {}).get("completeness"),
            "encounterReadiness": recipe.get("qualityDiagnostics", {}).get("encounterReadiness"),
            "priceCoverage": recipe.get("qualityDiagnostics", {}).get("priceCoverage"),
            "stats": calculation["stats"], "pobVersion": calculation.get("version"),
            "quote": price, "recipe": recipe, "modelUsed": model, "prompt": prompt,
            "modelIntent": f"{spec['focus'].capitalize()} focus with {spec['skill']}; links and passive clusters scored in PoB.",
            "progression": recipe.get("progression", []),
            "league": context["league"], "treeVersion": context["treeVersion"],
            "officialTreeRelease": context["officialRelease"], "createdAt": int(time.time()),
            "shareStatus": "pending", "shareUrl": None, "_xml": xml, "_fingerprint": mechanics_fingerprint(xml)}
