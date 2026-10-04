"""Connected point-budgeted tree search scored with PoB's node overrides."""
from __future__ import annotations

import math
import re
from collections import deque

from build_generator import offense_value
from mechanics import minion_model, minion_population

DEFAULT_UTILITY_RESOURCE_RESERVE = 0.15
CHANNEL_CYCLE_SECONDS = 1.0   # modeled assumption: a channelled spell is paid for ~once per second


def paid_use_rate(output: dict, spec: dict) -> float:
    """Paid uses per second. PoB's Speed is the cast rate; a channelled skill (Winter Orb,
    Vaal-less channellers) is cast once and held, so cost is paid per channel cycle, not per
    tooltip cast. All other skills pay on every cast."""
    speed = max(0.0, float(output.get("Speed", 0) or 0))
    if spec.get("channelled"):
        return min(speed, 1.0 / CHANNEL_CYCLE_SECONDS) if speed else 1.0 / CHANNEL_CYCLE_SECONDS
    return speed


def is_ci(spec: dict) -> bool:
    """Chaos Inoculation recipe: maximum life is 1 and ES is the whole pool."""
    return spec.get("defenseModel") == "ci"


def chaos_inoculation_path(context: dict, start: str) -> list[str]:
    """Shortest connected path from the class start to the Chaos Inoculation keystone."""
    nodes = context["tree"]["nodes"]
    target = next((key for key, node in nodes.items() if node.get("name") == "Chaos Inoculation"
                   and node.get("isKeystone")), None)
    if target is None:
        return []
    adjacency = graph(nodes, lambda node: not node.get("ascendancyName"))
    return paths_from({start}, adjacency).get(target, [])


def tree_pool_target(spec: dict) -> int:
    """Life+ES target aligned with endgame quality gates and focus."""
    return {"damage": 6000, "balanced": 6500, "defense": 9000}[spec["focus"]]


def tree_defenses_met(output: dict, spec: dict) -> bool:
    # Under CI the life pool is exactly 1; only ES counts towards the target.
    pool = (0.0 if is_ci(spec) else float(output.get("Life", 0) or 0)) + float(output.get("EnergyShield", 0) or 0)
    return pool >= tree_pool_target(spec) and float(output.get("TotalEHP", 0) or 0) >= 15000


def temporary_minion_population(output: dict, spec: dict) -> tuple[int, bool] | None:
    """(population, sustainable) for any minion skill, derived from PoB outputs (see mechanics)."""
    return minion_population(output, spec)


def skeleton_population(output: dict, spec: dict) -> tuple[int, bool]:
    """Backwards-compatible name: the generic temporary-minion model applied to a temporary summon."""
    return minion_population(output, {**spec, "minionModel": "temporary"})


def candidate_minion_count(output: dict, spec: dict) -> int | None:
    population = temporary_minion_population(output, spec)
    return population[0] if population is not None else None


def sustained_resource_use(output: dict, spec: dict) -> dict | None:
    """Assess continuous cast costs without treating temporary minions as permanent."""
    model = minion_model(spec)
    if model == "temporary":
        population = temporary_minion_population(output, spec)
        count, sustainable = population if population is not None else (0, False)
        return {"sustainable": sustainable, "population": count,
                "model": "cast rate, duration, resource sustain and minion limit"}
    if model == "permanent":
        return None

    speed = paid_use_rate(output, spec)
    checks = []
    for label, cost_key, regen_key in (("mana", "ManaCost", "ManaRegen"),
                                       ("life", "LifeCost", "LifeRegenRecovery")):
        use_rate = max(0.0, float(output.get(cost_key, 0) or 0)) * speed
        available = max(0.0, float(output.get(regen_key, 0) or 0))
        if use_rate > 0:
            reserve = min(0.5, max(0.0, float(spec.get(
                "resourceReserveFraction", DEFAULT_UTILITY_RESOURCE_RESERVE))))
            net_available = available * (1 - reserve)
            checks.append({"resource": label, "usePerSecond": use_rate,
                           "grossAvailablePerSecond": available,
                           "reservedPerSecond": available * reserve,
                           "utilityReserveFraction": reserve,
                           "availablePerSecond": net_available,
                           "sustainable": net_available + 1e-6 >= use_rate})
    if not checks:
        return {"sustainable": True, "checks": [], "model": "no sustained resource cost"}
    return {"sustainable": all(check["sustainable"] for check in checks),
            "checks": checks, "model": "PoB cost per use multiplied by calculated use rate"}


def recounted_stats(output: dict, spec: dict) -> dict:
    """Score a candidate using its own permanent/temporary minion population.

    PoB's batched passive and support overrides leave the socketed summon
    count unchanged. Rescale population DPS from the candidate's calculated
    minion limit, or from its own cast-rate/duration/mana sustain for SRS.
    The selected design is then fully recalculated with that count.
    """
    candidate_count = candidate_minion_count(output, spec)
    if candidate_count is None:
        return output
    result = dict(output)
    current = max(1, int(spec.get("minionCount", 1) or 1))
    if candidate_count != current:
        ratio = candidate_count / current
        for key in ("FullDPS", "FullDotDPS", "CombinedDPS", "TotalDPS", "TotalDotDPS",
                    "IgniteDPS", "WithIgniteDPS"):
            if key in result:
                result[key] = float(result[key] or 0) * ratio
    return result


def candidate_score(output: dict, spec: dict) -> float:
    return score(recounted_stats(output, spec), spec)


def resource_deficit(output: dict, spec: dict) -> tuple[float, float] | None:
    """Return the largest active-cast resource rate and its available rate."""
    if spec.get("archetype") == "minion" or minion_model(spec) == "permanent":
        return None
    speed = paid_use_rate(output, spec)
    deficits = []
    for cost_key, regen_key in (("ManaCost", "ManaRegen"),
                                ("LifeCost", "LifeRegenRecovery")):
        use = max(0.0, float(output.get(cost_key, 0) or 0)) * speed
        available = max(0.0, float(output.get(regen_key, 0) or 0))
        reserve = min(0.5, max(0.0, float(spec.get(
            "resourceReserveFraction", DEFAULT_UTILITY_RESOURCE_RESERVE))))
        available *= 1 - reserve
        if use > available and use > 0:
            deficits.append((use, available))
    return max(deficits, key=lambda pair: pair[0] - pair[1]) if deficits else None


class SearchBudget:
    """Count candidate design evaluations in the bounded optimization pass."""
    def __init__(self, limit: int = 2000):
        self.limit = limit
        self.used = 0
        self.exhausted = False
        self.reserve_blocked = False
        self.shares: dict[str, dict] = {}   # name -> {"reserved", "used", "skipped"}

    def share(self, name: str, reserved: int = 0) -> dict:
        entry = self.shares.setdefault(name, {"reserved": reserved, "used": 0, "skipped": ""})
        if reserved:
            entry["reserved"] = reserved
        return entry

    def extend(self, name: str, count: int) -> None:
        """Fund a later phase beyond the design limit; the share is recorded, not hidden."""
        self.limit += count
        self.exhausted = self.used >= self.limit
        entry = self.share(name, count)
        entry["fundedBeyondDesignLimit"] = entry.get("fundedBeyondDesignLimit", 0) + count

    def spend(self, name: str, count: int = 1, reserve: int = 0) -> int:
        """claim() attributed to a named phase share (visible in diagnostics)."""
        allowed = self.claim(count, reserve=reserve)
        entry = self.share(name)
        entry["used"] += allowed
        if allowed < count:
            entry["skipped"] = "budget exhausted" if self.exhausted else "blocked by later-phase reserve"
        return allowed

    def claim(self, count: int = 1, reserve: int = 0) -> int:
        absolute_remaining = max(0, self.limit - self.used)
        remaining = max(0, absolute_remaining - reserve)
        allowed = min(max(0, count), remaining)
        self.used += allowed
        if self.used >= self.limit:
            self.exhausted = True
        if allowed < count:
            if allowed >= absolute_remaining:
                self.exhausted = True
            else:
                self.reserve_blocked = True
        return allowed


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
                      ["Void Beacon", "Frigid Wake", "Profane Bloom", "Unholy Authority"]
                      if spec["damageType"] == "cold" else
                      ["Profane Bloom", "Unholy Authority", "Withering Presence", "Void Beacon"]),
        "Elementalist": (["Shaper of Flames", "Mastermind of Discord", "Heart of Destruction", "Bastion of Elements"]
                         if spec["archetype"] == "ignite" else
                         ["Shaper of Winter", "Mastermind of Discord", "Heart of Destruction", "Bastion of Elements"]
                         if spec["damageType"] == "cold" else
                         ["Shaper of Storms", "Mastermind of Discord", "Heart of Destruction", "Bastion of Elements"]
                         if spec["damageType"] == "lightning" else
                         ["Heart of Destruction", "Bastion of Elements", "Mastermind of Discord", "Shaper of Flames"]),
    }
    remaining = 8
    for name in priorities[asc]:
        target = next((key for key, node in nodes.items() if node.get("ascendancyName") == asc
                       and node.get("name") == name), None)
        path = paths_from(allocated, adjacency).get(target)
        if path and len(path) <= remaining:
            allocated.update(path)
            remaining -= len(path)
    # Leave points free when the installed tree lacks a preferred notable.
    # An arbitrary nearby notable is not a mechanic-compatible fallback.
    result = {start, *allocated}
    if is_ci(spec):
        result.update(chaos_inoculation_path(context, start))
    return result


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
    ci = is_ci(spec)
    if "maximum life" in text and "minion" not in text and not ci:
        # Life is never a goal under CI (the pool is 1); keep it only for
        # recipes whose damage or defense actually scales from maximum life.
        score += defense_scale * (8 if spec["focus"] == "defense" else 5)
    if "energy shield" in text and "minion" not in text:
        score += defense_scale * (7 if ci else 4)
    if ci and "chaos resistance" in text:
        score -= 1   # chaos damage is not taken; do not spend points on it
    if any(term in text for term in ("armour", "evasion", "chance to block", "spell suppression",
                                     "damage taken as", "reduced damage taken", "maximum resistances")):
        score += defense_scale * 4
    if "reservation efficiency" in text:
        score += 4
    if spec.get("_resourceSustainGap") and any(term in text for term in
                                                ("mana regeneration", "reduced mana cost", "cost of skills")):
        score += 7
    if "resistance" in text and "penetr" not in text:
        score += 1
    if "to strength" in text or "to dexterity" in text:
        score += 1
    return score


def score(output: dict, spec: dict) -> float:
    dps = (output.get("IgniteDPS", 0) if spec["archetype"] == "ignite" else
           max(output.get("FullDotDPS", 0), output.get("TotalDotDPS", 0))
           if spec["archetype"] == "dot" else offense_value(output))
    pool = (0 if is_ci(spec) else output.get("Life", 0)) + output.get("EnergyShield", 0)
    weight = {"damage": 0.35, "balanced": 0.85, "defense": 1.8}[spec["focus"]]
    # Continue valuing real life/ES after the focus-specific target has been
    # reached. A capped pool score made extra defense nearly worthless and
    # allowed the beam to trade away substantial EHP for small DPS gains.
    pool_utility = math.log(max(1, pool))
    ehp_weight = {"damage": 0.1, "balanced": 0.25, "defense": 0.7}[spec["focus"]]
    ehp_utility = math.log(max(1, float(output.get("TotalEHP", 0) or 0)))
    resource_model = sustained_resource_use(output, spec)
    resource_checks = resource_model.get("checks", []) if resource_model else []
    sustain_penalty = 0.0
    if resource_checks:
        # Score the damage a character can actually sustain over time. A small
        # fixed penalty let high-tooltip-DPS but resource-starved candidates
        # dominate complete-build selection. The minimum coverage across life
        # and mana uses the same model as the mechanic quality checks.
        coverage = min(
            min(1.0, max(0.0, check["availablePerSecond"]) /
                max(0.1, check["usePerSecond"]))
            for check in resource_checks)
        sustain_penalty = -math.log(max(0.1, coverage))
    deficits = sum(max(0, output.get("Req" + attr, 0) - output.get(attr, 0)) for attr in ("Str", "Dex", "Int"))
    deficits += sum(max(0, 75 - output.get(element + "Resist", -60)) for element in ("Fire", "Cold", "Lightning"))
    return (math.log(max(1, dps)) + weight * pool_utility + ehp_weight * ehp_utility - sustain_penalty
            - max(0, 3500 - pool) / 1000 - deficits / 50)


def expand_tree_beam(context, spec, beam, render, worker, adjacency, reserve, budget,
                     budget_reserve, trace):
    """Explore up to six connected passive designs for three bounded rounds."""
    nodes = context["tree"]["nodes"]
    for depth in range(3):
        children = []
        for state_index, state in enumerate(beam):
            allocated, calculation = state["nodes"], state["calc"]
            remaining = calculation["passives"]["maximum"] - calculation["passives"]["used"] - reserve
            if remaining <= 0:
                continue
            paths = paths_from(allocated, adjacency)
            hints = {**spec, "_defensesMet": tree_defenses_met(calculation["stats"], spec),
                     "_resourceSustainGap": resource_deficit(calculation["stats"], spec) is not None}
            candidates = []
            for key, path in paths.items():
                node = nodes[key]
                if (not path or len(path) > remaining or node.get("isKeystone") or
                        any(nodes[value].get("isKeystone") for value in path)):
                    continue
                hint = sum(heuristic(nodes[value], hints) for value in path) / len(path)
                if hint > 0 and (node.get("isNotable") or len(path) == 1 or heuristic(node, hints) >= 3):
                    candidates.append((hint, key, path))
            shortlist = sorted(candidates, key=lambda row: (-row[0], int(row[1])))[:72]
            if not shortlist:
                continue
            available_states = len(beam) - state_index
            available = budget.limit - budget.used - budget_reserve if budget is not None else len(shortlist)
            per_state = (min(len(shortlist), max(0, available // max(1, available_states * 2)))
                         if budget is not None else len(shortlist))
            if per_state == 0:
                continue
            shortlist = shortlist[:per_state]
            candidate_lookup = {key: path for _, key, path in shortlist}
            ranked = []
            baseline_score = candidate_score(calculation["stats"], spec)
            for offset in range(0, len(shortlist), 18):
                if budget is not None:
                    allowed = budget.claim(min(18, len(shortlist) - offset), reserve=budget_reserve)
                    if not allowed:
                        break
                    batch = shortlist[offset:offset + allowed]
                else:
                    batch = shortlist[offset:offset + 18]
                measured = worker.request("nodes", xml=render(allocated), candidates=[
                    {"id": key, "nodes": path} for _, key, path in batch])["candidates"]
                ranked.extend((candidate_score(entry["stats"], spec) - baseline_score,
                               entry["id"], entry["stats"])
                              for entry in measured)
                if len(batch) < min(18, len(shortlist) - offset):
                    break
            for gain, key, output in sorted(ranked, key=lambda row: (-row[0], int(row[1])))[:6]:
                if gain <= 0:
                    continue
                path = candidate_lookup[key]
                child_nodes = allocated | set(path)
                child_calc = {"stats": recounted_stats(output, spec), "passives": {
                    "used": calculation["passives"]["used"] + len(path),
                    "maximum": calculation["passives"]["maximum"]}}
                children.append({"nodes": child_nodes, "calc": child_calc,
                                 "score": score(child_calc["stats"], spec), "gain": gain,
                                 "parent": state["nodes"], "selected": key})
        if not children:
            break
        combined = beam + children
        unique = {}
        for state in combined:
            signature = tuple(sorted(state["nodes"], key=int))
            if signature not in unique or state["score"] > unique[signature]["score"]:
                unique[signature] = state
        beam = sorted(unique.values(), key=lambda state: (-state["score"], tuple(sorted(state["nodes"], key=int))))[:6]
        if trace is not None:
            trace.append({"kind": "passive_beam_round", "round": depth + 1,
                          "candidateDesigns": len(children), "retained": len(beam),
                          "bestPaidPassives": max(state["calc"]["passives"]["used"] for state in beam),
                          "reason": "retained six highest-scoring complete connected tree candidates"})
    return beam


def search_tree(context, spec, allocated, render, worker, stage, *, reserve=4, trace=None, budget=None,
                baseline_calc=None, budget_reserve=650, shortlist_size=72, use_beam=True,
                protected=frozenset()):
    nodes = context["tree"]["nodes"]
    adjacency = graph(nodes, lambda node: not node.get("ascendancyName") and not node.get("isMastery")
                      and not node.get("isProxy") and not node.get("isJewelSocket")
                      and ("classStartIndex" not in node or node["classStartIndex"] == 3))
    if budget is not None and not budget.claim(reserve=budget_reserve):
        return allocated, baseline_calc
    baseline = baseline_calc or worker.request("calculate", xml=render(allocated))
    # PoB counts bandit-granted points and excludes the class start for us.
    normal_start = next(key for key in allocated if nodes[key].get("classStartIndex") == 3)
    if use_beam and baseline["passives"]["used"] < baseline["passives"]["maximum"] - reserve:
        beam = expand_tree_beam(context, spec, [{"nodes": set(allocated), "calc": baseline,
                                                "score": candidate_score(baseline["stats"], spec)}],
                                render, worker, adjacency, reserve, budget, budget_reserve, trace)
        best = max(beam, key=lambda state: (state["score"], tuple(sorted(state["nodes"], key=int))))
        if best["nodes"] != allocated:
            allocated = set(best["nodes"])
            candidate_count = candidate_minion_count(best["calc"]["stats"], spec)
            if candidate_count is not None:
                spec["minionCount"] = candidate_count
            if budget is None or budget.claim(reserve=budget_reserve):
                baseline = worker.request("calculate", xml=render(allocated))
            else:
                baseline = best["calc"]
    rounds = 0
    while baseline["passives"]["used"] < baseline["passives"]["maximum"] - reserve:
        paths = paths_from(allocated, adjacency)
        remaining = baseline["passives"]["maximum"] - baseline["passives"]["used"] - reserve
        candidates = []
        hints = {**spec, "_defensesMet": tree_defenses_met(baseline["stats"], spec),
                 "_resourceSustainGap": resource_deficit(baseline["stats"], spec) is not None}
        for key, path in paths.items():
            node = nodes[key]
            # Arbitrary keystones can invalidate whole archetypes. They need
            # explicit mechanic recipes rather than a local numeric heuristic.
            if not path or len(path) > remaining or node.get("isKeystone"):
                continue
            if any(nodes[value].get("isKeystone") for value in path):
                continue
            hint = sum(heuristic(nodes[value], hints) for value in path) / len(path)
            if hint > 0 and (node.get("isNotable") or len(path) == 1 or heuristic(node, hints) >= 3):
                candidates.append((hint, key, path))
        shortlist = sorted(candidates, key=lambda row: (-row[0], int(row[1])))[:shortlist_size]
        if not shortlist:
            break
        lookup = {key: path for _, key, path in shortlist}
        base_score = candidate_score(baseline["stats"], spec)
        ranked = []
        for offset in range(0, len(shortlist), 18):
            if budget is not None:
                allowed = budget.claim(len(shortlist[offset:offset + 18]), reserve=budget_reserve)
                if allowed == 0:
                    break
                batch = shortlist[offset:offset + allowed]
            else:
                batch = shortlist[offset:offset + 18]
            results = worker.request("nodes", xml=render(allocated), candidates=[
                {"id": key, "nodes": path} for _, key, path in batch])["candidates"]
            ranked.extend(((candidate_score(entry["stats"], spec) - base_score) / len(lookup[entry["id"]]),
                           entry["id"], entry["stats"]) for entry in results)
            if len(batch) < min(18, len(shortlist) - offset):
                break
        if not ranked:
            if trace is not None:
                trace.append({"kind": "search_limit", "phase": "passive_tree",
                              "used": budget.used if budget is not None else None})
            break
            # Score every shortlisted batch. A strong path can occur after an
            # early positive batch, so stopping early makes results depend on
            # heuristic ordering rather than measured PoB value.
        gain, key, selected_stats = max(ranked, key=lambda row: (row[0], -int(row[1])))
        if trace is not None:
            trace.append({"kind": "passive_path_batch", "candidates": [
                {"node": node_id, "gain_per_point": round(value, 6)}
                for value, node_id, _ in sorted(ranked, key=lambda row: (-row[0], int(row[1])))],
                "selected": key if gain > 0 else None,
                "reason": "highest measured gain per point" if gain > 0 else "no positive measured gain"})
        if gain <= 0:
            break
        allocated.update(lookup[key])
        selected_adjusted_stats = recounted_stats(selected_stats, spec)
        candidate_count = candidate_minion_count(selected_stats, spec)
        if candidate_count is not None:
            spec["minionCount"] = candidate_count
        if budget is not None and not budget.claim(reserve=budget_reserve):
            # Candidate score is already an exact PoB node override. Keep its
            # stats and let final export perform the authoritative calculation.
            baseline = {"passives": {**baseline["passives"],
                                     "used": baseline["passives"]["used"] + len(lookup[key])},
                        "stats": selected_adjusted_stats}
            if trace is not None:
                trace.append({"kind": "search_limit", "phase": "passive_tree", "used": budget.used})
            break
        baseline = worker.request("calculate", xml=render(allocated))
        rounds += 1
        if rounds % 4 == 0:
            stage(f"Scoring passive clusters: {baseline['passives']['used']} paid points allocated")
    # Remove allocated regular-tree leaves that PoB says provide no value.
    # Only remove one at a time and recalculate after each change so paths that
    # connect another useful node remain intact.
    while True:
        removable = []
        for key in sorted(allocated, key=int):
            node = nodes.get(key, {})
            if (node.get("classStartIndex") == 3 or node.get("ascendancyName")
                    or node.get("isMastery") or node.get("isJewelSocket")
                    or node.get("isKeystone")   # a deliberately allocated keystone (e.g. CI) is never "neutral"
                    or key in protected):       # e.g. the connecting path of an equipped jewel socket
                continue
            neighbors = adjacency.get(key, set()) & allocated
            if len(neighbors) == 1:
                removable.append(key)
        removed = False
        for key in removable:
            if budget is not None and not budget.claim(reserve=budget_reserve):
                if trace is not None:
                    trace.append({"kind": "search_limit", "phase": "passive_pruning", "used": budget.used})
                removed = False
                break
            trial = allocated - {key}
            candidate = worker.request("calculate", xml=render(trial))
            if candidate_score(candidate["stats"], spec) >= candidate_score(baseline["stats"], spec) - 0.001:
                if trace is not None:
                    trace.append({"kind": "passive_prune", "node": key, "removed": True,
                                  "reason": "PoB objective unchanged or improved"})
                allocated = trial
                baseline = candidate
                removed = True
                break
            elif trace is not None:
                trace.append({"kind": "passive_prune", "node": key, "removed": False,
                              "reason": "PoB objective decreased"})
        if not removed:
            break
    assert normal_start in allocated
    return allocated, baseline


def mastery_choices(context: dict, allocated: set[str]):
    nodes = context["tree"]["nodes"]
    groups = {node.get("group") for key, node in nodes.items() if key in allocated and node.get("isNotable")}
    return [(key, node.get("masteryEffects", [])) for key, node in nodes.items()
            if node.get("isMastery") and node.get("group") in groups and node.get("masteryEffects")]


def jewel_socket_paths(context: dict, allocated: set[str], sockets) -> set[str]:
    """Allocated nodes that connect the class start to the given (equipped) jewel sockets."""
    nodes = context["tree"]["nodes"]
    adjacency = graph(nodes, lambda node: not node.get("ascendancyName") and not node.get("isMastery")
                      and not node.get("isProxy")
                      and ("classStartIndex" not in node or node["classStartIndex"] == 3))
    start = next((key for key in allocated if nodes.get(key, {}).get("classStartIndex") == 3), None)
    if start is None:
        return set()
    restricted = {key: neighbors & allocated for key, neighbors in adjacency.items() if key in allocated}
    paths = paths_from({start}, restricted)
    protected = set()
    for socket in sockets:
        protected.update(paths.get(socket, []))
    return protected
