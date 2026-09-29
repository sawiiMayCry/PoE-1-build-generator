"""Connected point-budgeted tree search scored with PoB's node overrides."""
from __future__ import annotations

import math
import re
from collections import deque

from build_generator import offense_value


def graph(nodes: dict, allowed) -> dict[str, set[str]]:
    adjacency = {str(key): set() for key, node in nodes.items() if str(key).isdigit() and allowed(node)}
    for key in adjacency:
        for neighbor in nodes[key].get("in", []) + nodes[key].get("out", []):
            neighbor = str(neighbor)
            if neighbor in adjacency:
                adjacency[key].add(neighbor)
                adjacency[neighbor].add(key)
    return adjacency


def paths_from(allocated: set[str], adjacency: dict) -> dict[str, list[str]]:
    paths = {node: [] for node in sorted(allocated, key=int) if node in adjacency}
    queue = deque(paths)
    while queue:
        node = queue.popleft()
        for neighbor in sorted(adjacency[node], key=int):
            if neighbor not in paths:
                paths[neighbor] = [*paths[node], neighbor]
                queue.append(neighbor)
    return paths


def initial_nodes(context: dict, spec: dict) -> set[str]:
    nodes = context["tree"]["nodes"]
    start = next(key for key, node in nodes.items() if node.get("classStartIndex") == 3)
    asc = spec["ascendancy"]
    asc_start = next(key for key, node in nodes.items()
                     if node.get("ascendancyName") == asc and node.get("isAscendancyStart"))
    adjacency = graph(nodes, lambda node: node.get("ascendancyName") == asc)
    allocated = {asc_start}
    priorities = {
        "Necromancer": ["Mindless Aggression", "Unnatural Strength", "Bone Barrier", "Commander of Darkness"],
        "Occultist": (["Void Beacon", "Withering Presence", "Profane Bloom", "Unholy Authority"]
                      if spec["damageType"] in {"chaos", "poison"} else
                      ["Void Beacon", "Frigid Wake", "Profane Bloom", "Unholy Authority"]),
        "Elementalist": (["Shaper of Flames", "Mastermind of Discord", "Heart of Destruction", "Bastion of Elements"]
                         if spec["archetype"] == "ignite" else
                         ["Mastermind of Discord", "Heart of Destruction", "Bastion of Elements",
                          "Shaper of Winter" if spec["damageType"] == "cold" else "Shaper of Storms"]),
    }
    remaining = 8
    for name in priorities[asc]:
        target = next((key for key, node in nodes.items() if node.get("ascendancyName") == asc
                       and node.get("name") == name), None)
        path = paths_from(allocated, adjacency).get(target)
        if path and len(path) <= remaining:
            allocated.update(path)
            remaining -= len(path)
    # Fill renamed/new notables deterministically rather than exceeding eight.
    while remaining:
        paths = paths_from(allocated, adjacency)
        choices = [(len(path), key, path) for key, path in paths.items()
                   if path and len(path) <= remaining and nodes[key].get("isNotable")]
        if not choices:
            break
        _, _, path = min(choices)
        allocated.update(path)
        remaining -= len(path)
    return {start, *allocated}


def heuristic(node: dict, spec: dict) -> float:
    text = " ".join(node.get("stats", [])).lower()
    if not text:
        return 0
    score = 0.0
    minion = spec["archetype"] == "minion"
    # Minion damage does not improve the player; player damage does not improve
    # minions. PoB subsequently scores these hints with the actual chosen skill.
    if ("minion" in text) == minion:
        weapon_specific = bool(re.search(r"with (?:attack skills|bows|swords|axes|maces|claws|wands)|melee (?:physical )?damage", text))
        if "damage" in text and (minion or spec["archetype"] == "attack" or not weapon_specific):
            score += 4
        if not minion and spec["archetype"] != "attack" and "spell damage" in text:
            score += 5
        if "critical strike" in text:
            score += 3
        for tag in (spec["damageType"], "cast speed" if not minion else "attack speed"):
            if tag in text:
                score += 3
        if spec["archetype"] == "ignite" and ("burning" in text or "ignite" in text or "over time" in text):
            score += 6
    defense_scale = 0.2 if spec.get("_defensesMet") else 1
    if "maximum life" in text and "minion" not in text:
        score += defense_scale * (8 if spec["focus"] == "defense" else 5)
    if "energy shield" in text and "minion" not in text:
        score += defense_scale * 4
    if "reservation efficiency" in text:
        score += 4
    if "resistance" in text and "penetr" not in text:
        score += 1
    if "to strength" in text or "to dexterity" in text:
        score += 1
    return score


def score(output: dict, spec: dict) -> float:
    dps = (output.get("IgniteDPS", 0) if spec["archetype"] == "ignite" else
           max(output.get("FullDotDPS", 0), output.get("TotalDotDPS", 0))
           if spec["archetype"] == "dot" else offense_value(output))
    pool = output.get("Life", 0) + output.get("EnergyShield", 0)
    weight = {"damage": 0.35, "balanced": 0.85, "defense": 1.8}[spec["focus"]]
    pool_target = {"damage": 3500, "balanced": 4500, "defense": 6000}[spec["focus"]]
    pool_utility = math.log(max(1, min(pool, pool_target))) + 0.08 * math.log(max(1, pool / pool_target))
    # Strongly prioritize the minimum pool before optimizing damage.
    deficits = sum(max(0, output.get("Req" + attr, 0) - output.get(attr, 0)) for attr in ("Str", "Dex", "Int"))
    deficits += sum(max(0, 75 - output.get(element + "Resist", -60)) for element in ("Fire", "Cold", "Lightning"))
    return (math.log(max(1, dps)) + weight * pool_utility
            - max(0, 3500 - pool) / 1000 - deficits / 50)


def search_tree(context, spec, allocated, render, worker, stage, *, reserve=4):
    nodes = context["tree"]["nodes"]
    adjacency = graph(nodes, lambda node: not node.get("ascendancyName") and not node.get("isMastery")
                      and not node.get("isProxy") and not node.get("isJewelSocket")
                      and ("classStartIndex" not in node or node["classStartIndex"] == 3))
    baseline = worker.request("calculate", xml=render(allocated))
    # PoB counts bandit-granted points and excludes the class start for us.
    normal_start = next(key for key in allocated if nodes[key].get("classStartIndex") == 3)
    rounds = 0
    while baseline["passives"]["used"] < baseline["passives"]["maximum"] - reserve:
        paths = paths_from(allocated, adjacency)
        remaining = baseline["passives"]["maximum"] - baseline["passives"]["used"] - reserve
        candidates = []
        pool_target = {"damage": 3500, "balanced": 4500, "defense": 6000}[spec["focus"]]
        hints = {**spec, "_defensesMet": baseline["stats"].get("Life", 0) +
                 baseline["stats"].get("EnergyShield", 0) >= pool_target}
        for key, path in paths.items():
            node = nodes[key]
            # Arbitrary keystones can invalidate whole archetypes. They need
            # explicit mechanic recipes rather than a local numeric heuristic.
            if not path or len(path) > remaining or node.get("isKeystone"):
                continue
            if any(nodes[value].get("isKeystone") for value in path):
                continue
            hint = sum(heuristic(nodes[value], hints) for value in path) / len(path)
            if hint > 0 and (node.get("isNotable") or len(path) == 1):
                candidates.append((hint, key, path))
        shortlist = sorted(candidates, key=lambda row: (-row[0], int(row[1])))[:72]
        if not shortlist:
            break
        lookup = {key: path for _, key, path in shortlist}
        base_score = score(baseline["stats"], spec)
        ranked = []
        for offset in range(0, len(shortlist), 18):
            results = worker.request("nodes", xml=render(allocated), candidates=[
                {"id": key, "nodes": path} for _, key, path in shortlist[offset:offset + 18]])["candidates"]
            ranked.extend(((score(entry["stats"], spec) - base_score) / len(lookup[entry["id"]]),
                           entry["id"], entry["stats"]) for entry in results)
            # Broaden the shortlist when heuristic favourites have no measured
            # value, instead of abandoning a half-spent passive budget.
            if any(row[0] > 0 for row in ranked):
                break
        gain, key, _ = max(ranked, key=lambda row: (row[0], -int(row[1])))
        if gain <= 0:
            break
        allocated.update(lookup[key])
        baseline = worker.request("calculate", xml=render(allocated))
        rounds += 1
        if rounds % 4 == 0:
            stage(f"Scoring passive clusters: {baseline['passives']['used']} paid points allocated")
    assert normal_start in allocated
    return allocated, baseline


def mastery_choices(context: dict, allocated: set[str]):
    nodes = context["tree"]["nodes"]
    groups = {node.get("group") for key, node in nodes.items() if key in allocated and node.get("isNotable")}
    return [(key, node.get("masteryEffects", [])) for key, node in nodes.items()
            if node.get("isMastery") and node.get("group") in groups and node.get("masteryEffects")]
