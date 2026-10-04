"""Structured skill groups, socket capacity/packing and utility package catalogue.

A *skill group* is one PoB socket group: a stable ``id``, a ``role``, the
equipment ``slot`` it is socketed in, and gem records with per-instance
level/quality/enabled state.  Groups are plain JSON-serializable dicts so they
can live inside a candidate state, be evaluated, and be serialized by
``build_assembly`` without mutating shared state::

    {"id": "main", "role": "main", "slot": "Body Armour",
     "gems": [{"instance": "main:1", "name": "Winter Orb", "kind": "active",
               "level": 20, "quality": 0, "enabled": true, "count": 1}, ...],
     "mainActive": "Winter Orb", "delivery": "permanent",
     "includeInFullDPS": true, "requires": [], "justification": "..."}

Gem settings are keyed by ``instance`` (never by name) because supports such
as Arcane Surge or Power Charge On Critical may appear in several groups with
different settings.

Everything here is pure and offline: gem knowledge comes from the
``GameData`` object (installed PoB metadata); anything that needs PoB's
calculation goes through the evaluator callbacks in ``skill_planner``.
"""
from __future__ import annotations

import copy
import re
from typing import Iterable

from mechanics import delivery as mechanic_delivery, minion_model

ROLE_ORDER = ("main", "movement", "guard", "aura", "defense", "herald", "curse", "offering",
              "minion_helper", "trigger", "utility")
# Slot preference per role (tie-break only; any slot with capacity may be used).
SLOT_PREFERENCE = {"movement": ("Boots", "Weapon 2", "Weapon 1"), "aura": ("Helmet", "Gloves"),
                   "herald": ("Gloves", "Helmet"), "curse": ("Gloves", "Helmet"),
                   "guard": ("Gloves", "Boots"), "defense": ("Helmet", "Gloves"),
                   "offering": ("Boots", "Gloves"), "minion_helper": ("Boots", "Gloves")}
ARMOUR_SLOTS = ("Helmet", "Gloves", "Boots")
MAX_GROUP_GEMS = 6
SUPPORT_ABSENT = "Metadata/Items/Gems/Support"


# ----------------------------------------------------------------------------
# Gem and group records
# ----------------------------------------------------------------------------

def gem_record(name: str, kind: str, *, group_id: str, position: int, level: int | None = None,
               quality: int = 0, enabled: bool = True, count: int = 1) -> dict:
    return {"instance": f"{group_id}:{position}", "name": name, "kind": kind, "level": level,
            "quality": quality, "enabled": enabled, "count": count}


def make_group(group_id: str, role: str, gems: Iterable[tuple[str, str] | dict], *, slot: str | None = None,
               main_active: str | None = None, delivery: str = "permanent",
               include_in_full_dps: bool = False, requires: Iterable[str] = (),
               justification: str = "", level_for=None, uptime: str | None = None,
               evidence: str | None = None) -> dict:
    """Create a group.  ``gems`` is ``[(name, kind), ...]`` or ready records."""
    records = []
    for position, entry in enumerate(gems, 1):
        if isinstance(entry, dict):
            record = dict(entry)
            record.setdefault("instance", f"{group_id}:{position}")
        else:
            name, kind = entry
            record = gem_record(name, kind, group_id=group_id, position=position)
        if record.get("level") is None and level_for is not None:
            record["level"] = level_for(record["name"])
        records.append(record)
    actives = [gem for gem in records if gem["kind"] == "active" and gem["enabled"]]
    return {"id": group_id, "role": role, "slot": slot, "gems": records,
            "mainActive": main_active or (actives[0]["name"] if actives else None),
            "delivery": delivery, "includeInFullDPS": include_in_full_dps,
            "requires": list(requires), "justification": justification, "uptime": uptime,
            "evidence": evidence}


def gem_kind(data, name: str) -> str:
    return "support" if data.gem(name).get("support") else "active"


def group_names(group: dict) -> list[str]:
    return [gem["name"] for gem in group["gems"]]


def socketed_gem_count(groups: Iterable[dict]) -> int:
    return sum(len(group["gems"]) for group in groups)


def clone_groups(groups: Iterable[dict]) -> list[dict]:
    return copy.deepcopy(list(groups))


def legacy_groups(spec: dict, supports: list[str], data, *, level_for=None) -> list[dict]:
    """Adapter for the old ``spec["utility"]`` (name -> slot) + ``supports`` shape.

    Each legacy utility gem is its own single-gem group in its named slot,
    exactly as the old serializer emitted them.
    """
    main_gems = [(spec["skill"], "active"), *[(name, "support") for name in supports]]
    groups = [make_group("main", "main", main_gems, slot="Body Armour", main_active=spec["skill"],
                         include_in_full_dps=True, level_for=level_for,
                         justification="Main damage link")]
    for index, (name, slot) in enumerate(spec.get("utility", {}).items(), 1):
        if name not in data.gems:
            continue
        tags = data.gem(name).get("tags", {})
        role = ("movement" if tags.get("movement") or tags.get("travel") or tags.get("blink") else
                "guard" if tags.get("guard") else "aura" if tags.get("aura") else
                "curse" if tags.get("curse") else "utility")
        groups.append(make_group(f"utility{index}", role, [(name, gem_kind(data, name))], slot=slot,
                                 level_for=level_for, delivery="manual" if role != "aura" else "permanent"))
    return groups


# ----------------------------------------------------------------------------
# Gem levels
# ----------------------------------------------------------------------------

def gem_level_for(data, name: str, character_level: int | None = None, cap: int = 20) -> int:
    """Highest usable level at ``character_level`` (or ``cap`` if unconstrained)."""
    gem = data.gem(name)
    levels = gem.get("levels") or []
    maximum = min(int(gem.get("maxLevel", 20) or 20), cap)
    if not levels or character_level is None:
        return maximum
    usable = [entry["level"] for entry in levels
              if entry["requiredLevel"] <= character_level and entry["level"] <= maximum]
    return max(usable, default=0)


def gem_required_level(data, name: str, level: int) -> int | None:
    entry = next((row for row in data.gem(name).get("levels") or [] if row["level"] == level), None)
    return entry["requiredLevel"] if entry else None


def attribute_requirements(data, name: str, level: int) -> dict[str, int]:
    entry = next((row for row in data.gem(name).get("levels") or [] if row["level"] == level), None)
    return {attr: int(entry.get(attr, 0) or 0) for attr in ("str", "dex", "int")} if entry else {}


def trigger_level_legal(data, trigger: str, trigger_level: int, triggered: str, triggered_level: int) -> tuple[bool, str]:
    """Cast when Damage Taken (and similar) cap the supported gem's *level requirement*.

    The cap equals the trigger gem's own level requirement at its level
    (PoB: ``local_support_gem_max_skill_level_requirement_to_support``).
    """
    cap = gem_required_level(data, trigger, trigger_level)
    need = gem_required_level(data, triggered, triggered_level)
    if cap is None or need is None:
        return False, "gem level tables unavailable; trigger legality cannot be verified"
    if need > cap:
        return False, (f"{triggered} level {triggered_level} (requires {need}) exceeds "
                       f"{trigger} level {trigger_level}'s supported-skill cap ({cap})")
    return True, f"{triggered} requirement {need} <= {trigger} cap {cap}"


# ----------------------------------------------------------------------------
# Socket capacity and packing
# ----------------------------------------------------------------------------

def parse_socket_runs(text: str | None) -> list[int] | None:
    """Linked-run sizes from an item's ``Sockets:`` line (gem sockets only)."""
    match = re.search(r"(?m)^Sockets:\s*(.+?)\s*$", text or "")
    if not match:
        return None
    runs = []
    for group in match.group(1).split():
        size = sum(1 for part in group.split("-") if part in {"R", "G", "B", "W"})
        if size:
            runs.append(size)
    return runs


def slot_socket_cap(slot: str, definition: dict | None, *, two_handed: bool = False) -> int:
    """Physical gem-socket limit for a rare item in ``slot``."""
    limit = int((definition or {}).get("socketLimit", 0) or 0)
    if slot == "Body Armour":
        return min(6, limit or 6)
    if slot in ARMOUR_SLOTS:
        return min(4, limit)
    if slot.startswith("Weapon"):
        return min(6 if two_handed else 3, limit)
    return 0


FIXED_SOCKET_MOD = re.compile(r"(?im)^(?:Has \d+ (?:\w+ )?Sockets?|Has no Sockets|Sockets cannot be "
                              r"(?:modified|linked)|Linked Sockets|Has \d+ Linked Sockets?)")


def has_fixed_sockets(text: str | None) -> bool:
    """True for uniques whose socket layout is an item property (not craftable)."""
    return bool(FIXED_SOCKET_MOD.search(text or ""))


def _item_base_name(text: str | None) -> str | None:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    index = next((i for i, line in enumerate(lines) if line.upper().startswith("RARITY:")), None)
    return lines[index + 2] if index is not None and index + 2 < len(lines) else None


def capacity_from_equipment(items: Iterable, uniques: dict[str, str] | None = None, data=None) -> dict[str, dict]:
    """``{slot: {"total": n, "fixedRuns": [...]|None, "unique": bool, "item": base}}``.

    ``items`` are ``RareItem``-like objects with ``slot``/``base``/``definition``.
    Uniques are re-linkable up to their base's socket limit unless the item
    itself fixes its sockets (``has_fixed_sockets``), in which case the exact
    ``Sockets:`` layout is kept.  Without base data a unique keeps the socket
    count already written in its text.
    """
    uniques = uniques or {}
    capacity: dict[str, dict] = {}
    for item in items:
        if item.slot.startswith("Flask"):
            continue
        capacity[item.slot] = {"total": slot_socket_cap(item.slot, item.definition),
                               "fixedRuns": None, "unique": False, "item": item.base}
    for slot, text in uniques.items():
        if slot.startswith("Flask"):
            continue
        runs = parse_socket_runs(text)
        base_name = _item_base_name(text)
        definition = data.bases.get(base_name) if data is not None and base_name else None
        if has_fixed_sockets(text):
            capacity[slot] = {"total": sum(runs or []), "fixedRuns": runs or [], "unique": True,
                              "item": base_name}
            continue
        if definition is not None:
            two_handed = bool((definition.get("tags") or {}).get("two_hand_weapon"))
            total = slot_socket_cap(slot, definition, two_handed=two_handed)
        else:
            total = sum(runs or [])
        capacity[slot] = {"total": total, "fixedRuns": None, "unique": True, "item": base_name}
    return capacity


def _plan_slot(sizes: list[int], total: int, fixed_runs: list[int] | None) -> list[int] | None:
    """Return the linked-run sizes needed for ``sizes`` in one slot, or ``None``.

    Multi-gem groups need dedicated runs; singles need one socket each.  With
    a fixed run layout (unique items) groups are matched to existing runs.
    """
    if fixed_runs is None:
        return sorted(sizes, reverse=True) if sum(sizes) <= total else None
    free = sorted(fixed_runs)
    taken = [False] * len(free)
    capacity = list(free)
    for size in sorted(sizes, reverse=True):
        if size >= 2:
            index = next((i for i, run in enumerate(free) if not taken[i] and capacity[i] == run and run >= size), None)
            if index is None:
                return None
            taken[index], capacity[index] = True, 0
        else:
            index = next((i for i in sorted(range(len(free)), key=lambda i: (-capacity[i], i))
                          if not taken[i] and capacity[i] >= 1), None)
            if index is None:
                return None
            capacity[index] -= 1
    return list(fixed_runs)


def apply_stage_caps(capacity: dict[str, dict], utility_sockets: int | None,
                     body_links: int | None = None) -> dict[str, dict]:
    """Copy of ``capacity`` with sockets capped by the leveling-stage policy.

    ``utility_sockets`` caps every non-body, non-fixed slot; ``body_links`` caps
    the body armour at the stage's link size (a six-socket body is not assumed
    in early acts).
    """
    result = {slot: dict(info) for slot, info in capacity.items()}
    for slot, info in result.items():
        if info["fixedRuns"] is not None:
            continue
        if slot == "Body Armour":
            if body_links is not None:
                info["total"] = min(info["total"], int(body_links))
        elif utility_sockets is not None:
            info["total"] = min(info["total"], int(utility_sockets))
    return result


def pack_groups(groups: list[dict], capacity: dict[str, dict], *,
                pinned: dict[str, str] | None = None) -> dict:
    """Place groups into slots.  Returns ``{"placements", "runs", "unplaced", "errors"}``.

    ``placements`` maps group id -> slot.  Groups with a preset ``slot`` keep
    it (a legacy/explicit placement); the rest are placed best-fit, largest
    first, honouring ``SLOT_PREFERENCE`` as a tie-break.  The main group must
    be placed in Body Armour (or its own preset slot).
    """
    pinned = pinned or {}
    placements: dict[str, str] = {}
    sizes: dict[str, list[int]] = {slot: [] for slot in capacity}
    errors: list[str] = []
    unplaced: list[str] = []

    def fits(slot: str, size: int) -> bool:
        info = capacity.get(slot)
        return bool(info) and _plan_slot(sizes[slot] + [size], info["total"], info["fixedRuns"]) is not None

    def place(group: dict, slot: str):
        placements[group["id"]] = slot
        sizes[slot].append(len(group["gems"]))

    ordered = sorted(groups, key=lambda g: (g["role"] != "main", g.get("slot") is None,
                                           -len(g["gems"]),
                                           ROLE_ORDER.index(g["role"]) if g["role"] in ROLE_ORDER else 99,
                                           g["id"]))
    for group in ordered:
        size = len(group["gems"])
        if size > MAX_GROUP_GEMS:
            errors.append(f"Group {group['id']} has {size} gems; a link has at most {MAX_GROUP_GEMS}")
            unplaced.append(group["id"])
            continue
        preset = pinned.get(group["id"]) or group.get("slot")
        if preset:
            if fits(preset, size):
                place(group, preset)
            else:
                total = capacity.get(preset, {}).get("total", 0)
                used = sum(sizes.get(preset, []))
                errors.append(f"{group['id']} ({size} gems) does not fit {preset}: "
                              f"{used}/{total} sockets already assigned")
                unplaced.append(group["id"])
            continue
        preferred = SLOT_PREFERENCE.get(group["role"], ())
        candidates = [slot for slot in capacity if fits(slot, size)]
        if group["role"] == "main":
            candidates = [slot for slot in candidates if slot == "Body Armour"] or candidates
        if not candidates:
            errors.append(f"No equipment slot has {size} free linked socket(s) for {group['id']} ({group['role']})")
            unplaced.append(group["id"])
            continue
        def remaining(slot):
            info = capacity[slot]
            return info["total"] - sum(sizes[slot]) - size
        # Armour slots first (weapon gems depend on the equipped weapon set),
        # then the role's preferred slots, then the tightest fit.
        best = min(candidates, key=lambda slot: (
            slot.startswith("Weapon") and slot not in preferred,
            preferred.index(slot) if slot in preferred else 99, remaining(slot), slot))
        place(group, best)
    if unplaced and not any("has" in e and "gems; a link has at most" in e for e in errors):
        exact = _exact_pack(ordered, capacity, pinned)
        if exact is not None:
            placements, unplaced, errors = exact, [], []
            sizes = {slot: [] for slot in capacity}
            for group in groups:
                sizes[placements[group["id"]]].append(len(group["gems"]))
    runs = {slot: (_plan_slot(values, capacity[slot]["total"], capacity[slot]["fixedRuns"]) or [])
            for slot, values in sizes.items() if values}
    return {"placements": placements, "runs": runs, "unplaced": unplaced, "errors": errors}


def _exact_pack(ordered: list[dict], capacity: dict[str, dict], pinned: dict[str, str],
                node_limit: int = 50_000) -> dict[str, str] | None:
    """Backtracking packing used when the greedy placement leaves a group out.

    The greedy order can fragment sockets (two 2-gem groups plus singles that would fit exactly).
    Groups are few (about a dozen) and slots fewer, so a bounded depth-first search over slots, in
    the same preference order, finds a complete placement whenever one exists.
    """
    sizes: dict[str, list[int]] = {slot: [] for slot in capacity}
    placements: dict[str, str] = {}
    visited = {"n": 0}

    def candidates(group: dict) -> list[str]:
        preset = pinned.get(group["id"]) or group.get("slot")
        if preset:
            return [preset] if preset in capacity else []
        preferred = SLOT_PREFERENCE.get(group["role"], ())
        slots = list(capacity)
        if group["role"] == "main" and "Body Armour" in capacity:
            return ["Body Armour"] + [slot for slot in slots if slot != "Body Armour"]
        return sorted(slots, key=lambda slot: (slot.startswith("Weapon") and slot not in preferred,
                                                preferred.index(slot) if slot in preferred else 99, slot))

    def fits(slot: str, size: int) -> bool:
        info = capacity[slot]
        return _plan_slot(sizes[slot] + [size], info["total"], info["fixedRuns"]) is not None

    def search(index: int) -> bool:
        if index == len(ordered):
            return True
        visited["n"] += 1
        if visited["n"] > node_limit:
            return False
        group = ordered[index]
        size = len(group["gems"])
        for slot in candidates(group):
            if fits(slot, size):
                sizes[slot].append(size)
                placements[group["id"]] = slot
                if search(index + 1):
                    return True
                sizes[slot].pop()
                del placements[group["id"]]
        return False

    return dict(placements) if search(0) else None


def socket_line(runs: list[int], colors: list[str] | None = None, *, spare: int = 0) -> str:
    """``Sockets:`` value for linked runs: ``[3, 1]`` -> ``B-B-B B``.

    ``colors`` (one per gem socket, in run order) sets socket colours; the
    default is blue.  ``spare`` adds unlinked, unused sockets.
    """
    palette = list(colors or [])
    parts = []
    for size in runs:
        run = [palette.pop(0) if palette else "B" for _ in range(size)]
        parts.append("-".join(run))
    parts.extend("B" for _ in range(spare))
    return " ".join(parts)


def gem_socket_color(data, name: str) -> str:
    """Socket colour matching a gem's attribute (white for colourless)."""
    tags = data.gem(name).get("tags", {})
    for tag, color in (("strength", "R"), ("dexterity", "G"), ("intelligence", "B")):
        if tags.get(tag):
            return color
    return "W"


def validate_groups(groups: list[dict], capacity: dict[str, dict], data=None) -> list[str]:
    """Physical/semantic problems that must stop serialization (empty = valid)."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    instances: set[str] = set()
    for group in groups:
        if group["id"] in seen_ids:
            problems.append(f"Duplicate group id {group['id']}")
        seen_ids.add(group["id"])
        if not group["gems"]:
            problems.append(f"Group {group['id']} has no gems")
        for gem in group["gems"]:
            if gem["instance"] in instances:
                problems.append(f"Duplicate gem instance {gem['instance']}")
            instances.add(gem["instance"])
            if data is not None and gem["name"] not in data.gems:
                problems.append(f"Unknown installed PoB gem: {gem['name']}")
        actives = [gem for gem in group["gems"] if gem["kind"] == "active" and gem["enabled"]]
        supports = [gem for gem in group["gems"] if gem["kind"] == "support"]
        if group["role"] != "main" and supports and not actives:
            problems.append(f"Group {group['id']} has supports but no active gem")
        if group.get("mainActive") and group["mainActive"] not in [g["name"] for g in actives]:
            problems.append(f"Group {group['id']} main active skill {group['mainActive']} is not an enabled active gem")
        if len(group["gems"]) > MAX_GROUP_GEMS:
            problems.append(f"Group {group['id']} has {len(group['gems'])} gems (maximum {MAX_GROUP_GEMS})")
    packing = pack_groups(groups, capacity)
    problems.extend(packing["errors"])
    return problems


# ----------------------------------------------------------------------------
# Package catalogue
# ----------------------------------------------------------------------------

DAMAGE_AURA = {"cold": ("Hatred",), "fire": ("Anger",), "lightning": ("Wrath",),
               "chaos": ("Malevolence",), "physical": ("Hatred", "Anger")}
# Determination is a defensive aura: it is only worth its reservation when armour is a real defense layer.
ARMOUR_AURA_MIN_ARMOUR = 10_000
CURSE_BY_DAMAGE = {"fire": "Flammability", "cold": "Frostbite", "lightning": "Conductivity",
                   "chaos": "Despair", "physical": "Vulnerability"}
HERALD_BY_DAMAGE = {"cold": "Herald of Ice", "fire": "Herald of Ash", "lightning": "Herald of Thunder",
                    "chaos": "Herald of Agony", "physical": "Herald of Purity"}
DEFENSE_AURAS = (("Discipline", "energy shield"), ("Determination", "armour"), ("Grace", "evasion"),
                 ("Flesh and Stone", "armour/avoidance"), ("Arctic Armour", "cold avoidance"))
# Level cap keeps Steelskin's strength requirement modest (existing policy).
LEVEL_CAPS = {"Steelskin": 10}


def _available(data, *names: str) -> bool:
    return all(name in data.gems and not data.gem(name).get("unsupported") for name in names)


def _levelled(data, name: str, character_level: int | None, cap: int = 20) -> int:
    return gem_level_for(data, name, character_level, min(cap, LEVEL_CAPS.get(name, cap)))


def build_facts(spec: dict, items: Iterable = (), uniques: dict[str, str] | None = None, data=None) -> dict:
    """Equipment/build facts used to decide which packages apply."""
    items = list(items)
    by_slot = {item.slot: item for item in items}
    shield = False
    weapon2 = by_slot.get("Weapon 2")
    if weapon2 is not None and "Shield" in str(weapon2.definition.get("type", "")):
        shield = True
    if uniques and uniques.get("Weapon 2"):
        text = uniques["Weapon 2"]
        shield = bool(re.search(r"(?i)shield|buckler|spirit shield", text))
    main_tags = {}
    if data is not None and spec.get("skill") in data.gems:
        main_tags = data.gem(spec["skill"]).get("tags", {})
    return {"shield": shield, "level": int(spec.get("level", 80)),
            "damageType": spec.get("damageType", "physical"),
            "archetype": spec.get("archetype", "spell"), "focus": spec.get("focus", "balanced"),
            "ascendancy": spec.get("ascendancy"), "skill": spec.get("skill"),
            "mainTags": dict(main_tags), "hasMana": True,
            "requested": set(spec.get("requestedUtilities", ())),
            "delivery": spec.get("mechanicDelivery") or mechanic_delivery(spec),
            "minionModel": minion_model(spec)}


def _package(pkg_id: str, role: str, gems: list[tuple[str, str]], function: str, *, evidence: str,
             delivery: str = "permanent", priority: int = 50, requires: tuple[str, ...] = (),
             alternatives: tuple[str, ...] = (), optional_supports: tuple[str, ...] = (),
             trigger: tuple[str, str] | None = None, reserves: bool = False,
             uncounted: str | None = None, full_dps: bool = False,
             min_stats: dict | None = None) -> dict:
    return {"id": pkg_id, "role": role, "gems": gems, "function": function, "evidence": evidence,
            "delivery": delivery, "priority": priority, "requires": list(requires),
            "alternatives": list(alternatives), "optionalSupports": list(optional_supports),
            "trigger": trigger, "reserves": reserves, "uncountedBenefit": uncounted,
            "fullDps": full_dps, "minStats": dict(min_stats or {})}


def candidate_packages(spec: dict, data, facts: dict, *, main_support_names: Iterable[str] = ()) -> list[dict]:
    """Ordered supporting-package candidates for this build.

    Each package states its declared function and the evidence type that
    justifies it (``stat`` = a PoB stat must improve, ``role`` = legality and
    declared function, since its benefit is not a main-skill tooltip stat).
    Gems missing from the installed PoB, or locked by character level, are
    skipped here (and reported as omissions by the planner).
    """
    damage = facts["damageType"]
    archetype = facts["archetype"]
    level = facts["level"]
    packages: list[dict] = []
    duplicates = set(main_support_names)

    def usable(*names):
        return _available(data, *names) and all(gem_level_for(data, n, level) >= 1 for n in names)

    # --- movement ------------------------------------------------------
    # Shield Charge is a second, attack-based movement skill: one movement skill is the default, so it
    # is offered only when the user asked for it by name.
    if facts["shield"] and "Shield Charge" in facts.get("requested", ())             and usable("Shield Charge", "Faster Attacks", "Momentum"):
        packages.append(_package(
            "movement_shield_charge", "movement",
            [("Shield Charge", "active"), ("Faster Attacks", "support"), ("Momentum", "support")],
            "Shield Charge movement skill: Faster Attacks shortens its use time and Momentum adds "
            "movement speed after use", evidence="role", delivery="manual", priority=12,
            requires=("shield",)))
    blink = "Frostblink" if damage == "cold" and usable("Frostblink") else \
        "Flame Dash" if usable("Flame Dash") else None
    if blink:
        gems = [(blink, "active")]
        extras = tuple(name for name in ("Second Wind", "Faster Casting") if usable(name))
        gems += [(name, "support") for name in extras]
        packages.append(_package(
            "movement_blink", "movement", gems,
            f"{blink} gap-closer; Second Wind adds charges and Faster Casting shortens the cast",
            evidence="role", delivery="manual", priority=10,
            alternatives=("Flame Dash alone",), optional_supports=extras))
    # --- guard ----------------------------------------------------------
    if usable("Steelskin"):
        packages.append(_package(
            "guard_steelskin", "guard", [("Steelskin", "active")] +
            ([("Second Wind", "support")] if usable("Second Wind") and blink is None else []),
            "Steelskin guard buff absorbs damage for a short window (manual use)",
            evidence="role", delivery="manual", priority=30))
    # --- reservation / auras -----------------------------------------------
    aura_names = list(dict.fromkeys(
        name for name in (*(("Zealotry",) if archetype in {"spell", "ignite", "dot"} else ()),
                          *DAMAGE_AURA.get(damage, ()),
                          *(("Malevolence",) if archetype in {"ignite", "dot", "minion"} else ()),
                          *(("Hatred", "Anger", "Wrath") if archetype in {"attack", "minion"} else ()))
        if usable(name)))
    for name in aura_names:
        packages.append(_package(
            f"aura_{name.lower().replace(' ', '_')}", "aura", [(name, "active")],
            f"{name} damage aura: PoB must show the main skill's damage improving",
            evidence="stat:damage", priority=20, reserves=True))
    if archetype == "minion":
        for name in ("Determination",):
            pass
    for name, label in DEFENSE_AURAS:
        if usable(name):
            floor = {"Armour": ARMOUR_AURA_MIN_ARMOUR} if name == "Determination" else None
            packages.append(_package(
                f"defense_{name.lower().replace(' ', '_')}", "defense", [(name, "active")],
                f"{name} ({label}): PoB must show a defensive stat improving without breaking resources"
                + (f"; kept only when armour with it reaches {ARMOUR_AURA_MIN_ARMOUR:,}" if floor else ""),
                evidence="stat:defense", priority=40, reserves=True, min_stats=floor))
    if usable("Clarity"):
        packages.append(_package("aura_clarity", "aura", [("Clarity", "active")],
                                 "Clarity mana regeneration: only kept when it repairs a mana-sustain deficit",
                                 evidence="stat:mana", priority=60, reserves=True))
    if usable("Enlighten"):
        packages.append(_package("support_enlighten", "aura", [("Enlighten", "support")],
                                 "Enlighten reduces reservation of every aura in its link",
                                 evidence="stat:reservation", priority=70,
                                 uncounted="exceptional gem: availability is not priced"))
    # --- herald -------------------------------------------------------------
    herald = HERALD_BY_DAMAGE.get(damage)
    if herald and usable(herald) and archetype in {"spell", "ignite", "dot", "attack"}:
        extras = []
        if usable("Scornful Herald"):
            extras.append("Scornful Herald")
        packages.append(_package(
            "herald", "herald", [(herald, "active")] + [(n, "support") for n in extras],
            f"{herald}: reserved herald that adds its own hits when its trigger condition occurs "
            f"(uptime is not assumed in PoB)", evidence="role", priority=35, reserves=True,
            optional_supports=tuple(extras),
            uncounted="herald trigger uptime is not modeled by PoB's conservative calculation"))
    # --- curse delivery ---------------------------------------------------
    curse = CURSE_BY_DAMAGE.get(damage)
    if curse and usable(curse):
        gems = [(curse, "active")]
        trigger = None
        if usable("Cast when Damage Taken"):
            gems.append(("Cast when Damage Taken", "support"))
            trigger = ("Cast when Damage Taken", curse)
        packages.append(_package(
            "curse", "curse", gems,
            f"{curse} on enemies via manual cast, with Cast when Damage Taken as an automatic delivery "
            "only when its level cap legally supports the curse", evidence="role",
            delivery="trigger" if trigger else "manual", priority=25, trigger=trigger,
            alternatives=("manual cast only",)))
    # --- minion helpers ----------------------------------------------------
    if archetype == "minion":
        names = [n for n in ("Desecrate", "Flesh Offering", "Bone Offering") if usable(n)]
        if len(names) >= 2:
            packages.append(_package(
                "minion_offerings", "offering", [(n, "active") for n in names],
                "Desecrate creates corpses and offerings buff minions (manual use, corpse dependent)",
                evidence="role", delivery="manual", priority=28,
                uncounted="offering uptime and corpse supply are not modeled"))
        if usable("Convocation"):
            packages.append(_package("minion_convocation", "minion_helper", [("Convocation", "active")],
                                     "Convocation repositions minions", evidence="role",
                                     delivery="manual", priority=45))
    return sorted(packages, key=lambda pkg: (pkg["priority"], pkg["id"]))


# Candidate gems for spare sockets, keyed by the mechanic they suit ("minion" builds, "non_minion"
# builds, or "any"), not by the main skill.  Each is kept only on its stated evidence (a PoB-measured
# damage gain, or a declared role); the planner reports which.  This is catalogue data.
FILL_HELPERS = (
    {"id": "fill_absolution", "gem": "Absolution", "supports": ("Minion Damage", "Minion Speed"),
     "when": "minion", "evidence": "stat:damage", "priority": 80, "fullDps": True,
     "function": "Absolution Sentinel is a second minion that benefits from the same minion gear and tree; "
                 "kept only if PoB measures a damage gain (counted at one Sentinel)"},
    {"id": "fill_zombie", "gem": "Raise Zombie", "supports": ("Minion Damage", "Minion Life"),
     "when": "minion", "evidence": "role", "priority": 85,
     "function": "Permanent zombies absorb hits and add damage; their damage is not counted (one zombie "
                 "cannot be assumed) so this is a role-justified minion group",
     "uncounted": "zombie population and damage are not included in the build's DPS"},
    {"id": "fill_stone_golem", "gem": "Summon Stone Golem", "supports": ("Minion Life", "Minion Speed"),
     "when": "minion", "evidence": "role", "priority": 88,
     "function": "Stone Golem taunts and tanks for the player; defensive minion helper (not counted by PoB)",
     "uncounted": "golem taunt/tanking is not modeled by PoB"},
    {"id": "fill_molten_shell", "gem": "Molten Shell", "supports": (), "when": "any", "evidence": "role",
     "priority": 89, "delivery": "manual",
     "function": "Molten Shell guard skill: absorbs a burst of physical damage on demand (manual use); a "
                 "single-gem defensive utility that fits any lone socket",
     "uncounted": "guard skill uptime is not modeled by PoB"},
    {"id": "fill_enfeeble", "gem": "Enfeeble", "supports": (), "when": "any", "evidence": "role",
     "priority": 96, "delivery": "manual",
     "function": "Enfeeble curse: cast on dangerous rares and bosses to reduce the damage and accuracy of "
                 "enemies (manual use; the curse limit means it replaces the build's main curse while active)",
     "uncounted": "defensive curse effect is not modeled by PoB's calculation"},
    {"id": "fill_temporal_chains", "gem": "Temporal Chains", "supports": (), "when": "any", "evidence": "role",
     "priority": 97, "delivery": "manual",
     "function": "Temporal Chains curse: slows enemies for safer mapping (manual use; replaces the main curse "
                 "while active)", "uncounted": "slow effect is not modeled by PoB's calculation"},
    {"id": "fill_vaal_molten_shell", "gem": "Vaal Molten Shell", "supports": (), "when": "any",
     "evidence": "role", "priority": 98, "delivery": "manual",
     "function": "Vaal Molten Shell: large one-off physical damage absorb for emergencies (soul-charged, manual "
                 "use)", "uncounted": "vaal skill uptime is not modeled"},
    {"id": "fill_decoy_totem", "gem": "Decoy Totem", "supports": (), "when": "any", "evidence": "role",
     "priority": 99, "delivery": "manual",
     "function": "Decoy Totem taunts enemies away from the character (manual use)",
     "uncounted": "taunt is not modeled by PoB's calculation"},
    {"id": "fill_stone_golem", "gem": "Summon Stone Golem", "supports": ("Minion Life", "Minion Speed"),
     "when": "non_minion", "evidence": "role", "priority": 95,
     "function": "Stone Golem taunts and tanks for the player; defensive helper for sockets left over by the "
                 "plan (not counted by PoB)",
     "uncounted": "golem taunt/tanking is not modeled by PoB"},
)


def fill_packages(spec: dict, data, facts: dict, present: Iterable[str], *, stats: dict | None = None,
                  roles: Iterable[str] = ()) -> list[dict]:
    """Packages for sockets the main plan left empty (weapons, shield, spare armour sockets).

    Ordered by how well each is justified.  ``stat:damage`` packages are kept only if PoB measures a
    damage gain; ``role`` packages are legal, role-fitting utility whose benefit PoB's conservative
    calculation does not count (the planner reports that).  Gems already socketed are not repeated, so
    this never pads a build with duplicates.
    """
    archetype, level = facts["archetype"], facts["level"]
    have = set(present)

    def usable(*names):
        return (_available(data, *names) and all(gem_level_for(data, n, level) >= 1 for n in names)
                and not (set(names[:1]) & have))

    def supports(*names):
        return [(n, "support") for n in names if _available(data, n) and gem_level_for(data, n, level) >= 1]

    packages: list[dict] = []
    for entry in FILL_HELPERS:
        if entry["when"] == "minion" and archetype != "minion":
            continue
        if entry["when"] == "non_minion" and archetype == "minion":
            continue
        if usable(entry["gem"]):
            packages.append(_package(
                entry["id"], "minion_helper", [(entry["gem"], "active"), *supports(*entry["supports"])],
                entry["function"], evidence=entry["evidence"], delivery=entry.get("delivery", "permanent"),
                priority=entry["priority"], full_dps=entry.get("fullDps", False),
                uncounted=entry.get("uncounted")))
    stats = stats or {}
    if usable("Phase Run") and "movement" not in set(roles):
        packages.append(_package(
            "fill_phase_run", "movement",
            [("Phase Run", "active"), *supports("Faster Casting")],
            "Phase Run: the build's movement skill (none was placed); lets the character pass through "
            "enemies while mapping; manual use", evidence="role", delivery="manual", priority=90))
    # Vaal utility: gems whose role the build's measured stats justify rank first; the rest are a
    # last-resort filler (lowest priority) so a spare socket is not left empty. The planner bundles the
    # Vaal fillers of one item into a single linked utility group.
    es, life = float(stats.get("EnergyShield", 0) or 0), float(stats.get("Life", 0) or 0)
    evasion = float(stats.get("Evasion", 0) or 0)
    vaal = (
        ("Vaal Discipline", 91, es >= max(1500.0, life * 0.4),
         "energy shield recovery burst; the build has a real energy shield pool"),
        ("Vaal Haste", 92, facts["archetype"] in {"minion", "attack"},
         "short action-speed burst for you and your minions or attacks"),
        ("Vaal Grace", 93, evasion >= 5000,
         "short evasion and dodge burst; evasion is a real defensive layer here"),
        ("Vaal Clarity", 94, bool(stats) and float(stats.get("ManaUnreserved", 1e9) or 0)
         < 1.5 * float(stats.get("ManaCost", 0) or 0),
         "short window of free skill casts; unreserved mana is tight for the main skill"))
    for gem_name, priority, justified, why in vaal:
        if usable(gem_name):
            note = why if justified else "last-resort filler for an otherwise empty socket: " +                 "no measured need for it on this build"
            packages.append(_package(
                "fill_" + gem_name.lower().replace(" ", "_"), "utility", [(gem_name, "active")],
                f"{gem_name}: souls-based emergency utility ({note}); no reservation or mana cost, manual use "
                "(PoB's conservative calculation does not count it)", evidence="role", delivery="manual",
                priority=priority if justified else priority + 10, uncounted="vaal skill uptime is not modeled"))
    return sorted(packages, key=lambda pkg: (pkg["priority"], pkg["id"]))


def package_group(pkg: dict, data, character_level: int | None, *, index: int = 1,
                  drop: Iterable[str] = ()) -> dict:
    """Instantiate a package as a skill group (optionally dropping optional supports)."""
    dropped = set(drop)
    gems = [(name, kind) for name, kind in pkg["gems"] if name not in dropped]
    group_id = pkg["id"]
    return make_group(group_id, pkg["role"], gems, delivery=pkg["delivery"], justification=pkg["function"],
                      level_for=lambda name: _levelled(data, name, character_level),
                      requires=pkg["requires"], evidence=pkg["evidence"], uptime=pkg.get("uncountedBenefit"),
                      include_in_full_dps=bool(pkg.get("fullDps")))


# ----------------------------------------------------------------------------
# Completeness fixture helpers (screenshot comparison)
# ----------------------------------------------------------------------------

REFERENCE_WINTER_ORB_OCCULTIST = {
    "total": 23,
    "groups": {
        "main": ["Winter Orb", "Infused Channelling", "Cold Penetration", "Power Charge On Critical",
                 "Inspiration", "Focused Channelling"],
        "curse": ["Frostbite", "Arcane Surge", "Cast when Damage Taken"],
        "reservation": ["Flesh and Stone", "Discipline", "Enlighten", "Arctic Armour"],
        "herald": ["Herald of Ice", "Scornful Herald", "Ice Bite", "Power Charge On Critical"],
        "utility": ["Frostblink", "Frost Shield", "Pact of Lycia"],
        "movement": ["Shield Charge", "Faster Attacks", "Momentum"],
    },
}
GENERATED_WINTER_ORB_OCCULTIST_BASELINE = {
    "total": 9,
    "groups": {"main": ["Winter Orb", "Arcane Surge", "Culling Strike", "Inspiration"],
               "curse": ["Frostbite"], "reservation": ["Hatred", "Clarity"],
               "utility": ["Steelskin"], "movement": ["Flame Dash"]},
}


def role_coverage(groups: Iterable[dict]) -> dict[str, list[str]]:
    coverage: dict[str, list[str]] = {}
    for group in groups:
        coverage.setdefault(group["role"], []).append(group["id"])
    return coverage


# ----------------------------------------------------------------------------
# Conversion to/from the shared contract (build_contracts.SkillGroup)
# ----------------------------------------------------------------------------

ROLE_TO_CONTRACT = {"main": "main", "movement": "movement", "guard": "guard", "aura": "reservation",
                    "defense": "defense", "herald": "herald", "curse": "curse", "offering": "offering",
                    "minion_helper": "minion_support", "trigger": "trigger", "utility": "other"}
ROLE_FROM_CONTRACT = {"reservation": "aura", "minion_support": "minion_helper", "buff": "utility",
                      "other": "utility"}
DELIVERY_TO_CONTRACT = {"permanent": "persistent", "manual": "manual", "trigger": "trigger",
                        "aura": "aura", "reservation": "aura", "persistent": "persistent",
                        "socketed": "socketed", "item_granted": "item_granted"}


def to_contract_groups(groups: Iterable[dict]) -> list:
    """Planner dicts -> ``build_contracts.SkillGroup`` objects."""
    from build_contracts import GemRecord, SkillGroup  # local: keep this module import-light
    result = []
    for group in groups:
        delivery = DELIVERY_TO_CONTRACT.get(group.get("delivery", "permanent"), "persistent")
        role = ROLE_TO_CONTRACT.get(group["role"], "other")
        if role == "reservation":
            delivery = "aura" if delivery == "persistent" else delivery
        gems = [GemRecord(name=gem["name"], support=gem["kind"] == "support",
                          level=gem.get("level") or 20, quality=gem.get("quality", 0),
                          enabled=gem.get("enabled", True), count=gem.get("count", 1),
                          instance_id=gem["instance"]) for gem in group["gems"]]
        result.append(SkillGroup(id=group["id"], role=role, slot=group.get("slot") or "", gems=gems,
                                 main_active=group.get("mainActive") or "", delivery=delivery,
                                 conditions=list(group.get("requires", [])),
                                 include_in_full_dps=bool(group.get("includeInFullDPS")),
                                 reason=group.get("justification", "")))
    return result


def as_group_dicts(groups: Iterable) -> list[dict]:
    """Accept planner dicts *or* contract ``SkillGroup`` objects; return planner dicts."""
    result = []
    for group in groups:
        if isinstance(group, dict):
            result.append(group)
            continue
        role = ROLE_FROM_CONTRACT.get(group.role, group.role)
        gems = [{"instance": gem.instance_id or f"{group.id}:{position}", "name": gem.name,
                 "kind": "support" if gem.support else "active", "level": gem.level,
                 "quality": gem.quality, "enabled": gem.enabled, "count": gem.count}
                for position, gem in enumerate(group.gems, 1)]
        delivery = {"persistent": "permanent", "aura": "permanent"}.get(group.delivery, group.delivery)
        result.append({"id": group.id, "role": role, "slot": group.slot or None, "gems": gems,
                       "mainActive": group.main_active or None, "delivery": delivery,
                       "includeInFullDPS": group.include_in_full_dps, "requires": list(group.conditions),
                       "justification": group.reason, "uptime": None, "evidence": None,
                       "enabled": getattr(group, "enabled", True)})
    return result


# ----------------------------------------------------------------------------
# Recover groups from a final XML (so later stages derive from what was exported)
# ----------------------------------------------------------------------------

def infer_role(data, names: Iterable[str]) -> str:
    tags_by_name = [data.gems[n].get("tags", {}) for n in names if n in data.gems
                    and not data.gems[n].get("support")]
    # Vaal aura gems carry the aura tag, but only their souls-based Vaal skill is enabled here (the plain
    # aura is disabled), so they reserve nothing and must not be treated as reservations.
    if names and all(n.startswith("Vaal ") for n in names if n in data.gems and not data.gems[n].get("support")):
        return "utility"
    for role, test in (("herald", lambda t: t.get("herald")), ("curse", lambda t: t.get("curse") or t.get("hex")),
                       ("movement", lambda t: t.get("movement") or t.get("travel") or t.get("blink")),
                       ("guard", lambda t: t.get("guard")), ("aura", lambda t: t.get("aura") or t.get("stance"))):
        if any(test(tags) for tags in tags_by_name):
            return role
    return "utility"


def groups_from_xml(xml, data) -> list[dict]:
    """Planner groups for the *socketed* groups of an XML's active skill set.

    Item-granted groups are excluded.  The main group is the PoB main socket
    group; ids are ``main`` or ``<role>-<index>`` and instance IDs are unique.
    """
    from loadout_summary import summarize_loadout
    summary = summarize_loadout(xml, data.gems)
    groups = []
    for group in summary["groups"]:
        if group["itemGranted"] or not group["socketedGemCount"]:
            continue
        names = [gem["name"] for gem in group["gems"] if gem["socketed"]]
        role = "main" if group["isMain"] else infer_role(data, names)
        group_id = "main" if group["isMain"] else f"{role}-{group['index']}"
        gems = [{"instance": f"{group_id}:{position}", "name": gem["name"], "kind": gem["kind"],
                 "level": gem["level"], "quality": gem["quality"], "enabled": gem["enabled"], "count": 1}
                for position, gem in enumerate((g for g in group["gems"] if g["socketed"]), 1)]
        groups.append({"id": group_id, "role": role, "slot": group["slot"], "gems": gems,
                       "mainActive": group["mainActive"], "delivery": "permanent" if role in {"main", "aura"} else "manual",
                       "includeInFullDPS": group["includeInFullDPS"], "requires": [], "justification": "",
                       "uptime": None, "evidence": None, "enabled": group["enabled"]})
    return groups


# ----------------------------------------------------------------------------
# Skill-package completeness (independent of the main six-link)
# ----------------------------------------------------------------------------

ROLE_GROUPS_FOR_REQUIRED = {"movement": ("movement",), "protection": ("guard", "defense"), "reservation": ("aura",),
                            "curse": ("curse",)}


def package_completeness(groups: Iterable[dict], omissions: Iterable[dict] = (), *, main_size: int = 6,
                         min_supported_utility_groups: int = 2, require_curse: bool = True) -> dict:
    """Is the whole skill package complete, not only the main link?

    A required role is satisfied by a present group, or *explained* when the
    planner recorded a mechanic-specific omission for that role.  Six main
    gems with five isolated utility gems (the 11-gem repair of the 9-gem
    screenshot build) fails: supported utility groups are required.
    """
    groups = list(groups)
    main = next((g for g in groups if g["role"] == "main"), None)
    utility = [g for g in groups if g["role"] != "main"]
    supported = [g for g in utility if any(x["kind"] == "active" for x in g["gems"])
                 and any(x["kind"] == "support" for x in g["gems"])]
    present: dict[str, list[str]] = {}
    for group in utility:
        present.setdefault(group["role"], []).append(group["id"])
    explained = {row.get("role"): row["reason"] for row in omissions if row.get("role")}
    gaps, excused = [], {}
    for label, roles in ROLE_GROUPS_FOR_REQUIRED.items():
        if label == "curse" and not require_curse:
            continue
        if any(role in present for role in roles):
            continue
        reason = next((explained[role] for role in roles if role in explained), None)
        if reason:
            excused[label] = reason
        else:
            gaps.append(f"missing {label} skill role")
    main_gems = len(main["gems"]) if main else 0
    if main_gems < main_size:
        gaps.append(f"main link has {main_gems} of {main_size} gems")
    if len(supported) < min_supported_utility_groups:
        gaps.append(f"{len(supported)} supported utility group(s); at least {min_supported_utility_groups} required")
    socketed = sum(len(g["gems"]) for g in groups)
    return {"complete": not gaps, "gaps": gaps, "excusedRoles": excused, "socketedGems": socketed,
            "supportedUtilityGroups": len(supported), "roles": present, "mainLinkGems": main_gems}
