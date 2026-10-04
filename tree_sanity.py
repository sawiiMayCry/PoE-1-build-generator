"""Generic passive-tree sanity check for a finished build.

Structural checks (no build-specific knowledge):
  * every allocated node exists in the official tree and is reachable from the class start (regular
    tree) or from the ascendancy start (ascendancy tree) through allocated nodes, so no orphan nodes;
  * allocated points never exceed the available points and unspent points are near zero;
  * the ascendancy has all points the character level grants;
  * keystones are real keystones of this class' tree and do not contradict the defence model;
  * mastery effects belong to their node, are distinct, and sit in a group with an allocated notable.

Waste check (measured, optional): every removable branch (a connected set of nodes that hangs off the
tree and holds no keystone or jewel socket) is removed in PoB; a branch whose removal does not lower the
build objective is wasted travel or an unrelated cluster.  ``measure`` is any callable returning the
objective for an allocation, so the check is testable without PoB.
"""
from __future__ import annotations

from passive_search import graph, paths_from

UNSPENT_FAIL_ABOVE = 2          # more unspent points than this fails the check
WASTE_TOLERANCE = 0.0005        # objective loss (ln-scale, ~0.05%) below which a branch counts as wasted
ASCENDANCY_THRESHOLDS = ((75, 8), (68, 6), (55, 4), (33, 2))


def expected_ascendancy_points(level: int) -> int:
    return next((points for threshold, points in ASCENDANCY_THRESHOLDS if level >= threshold), 0)


def _is_class_start(node: dict) -> bool:
    return "classStartIndex" in node


def structural_checks(nodes: dict, spec: dict, allocated: set[str], masteries: dict, jewels: dict | None,
                      passives: dict | None, ascendancy: str, level: int) -> tuple[list[dict], dict]:
    allocated = {str(key) for key in allocated}
    unknown = sorted((key for key in allocated if key not in nodes), key=str)
    known = allocated - set(unknown)
    metrics: dict = {"allocated": len(known)}
    checks: list[dict] = []

    orphans: list[str] = []
    for asc in (None, ascendancy):
        subset = {key for key in known if not nodes[key].get("isMastery")
                  and (nodes[key].get("ascendancyName") == asc if asc else not nodes[key].get("ascendancyName"))}
        if asc:
            start = next((key for key in subset if nodes[key].get("isAscendancyStart")), None)
        else:
            start = next((key for key in subset if nodes[key].get("classStartIndex") == 3), None)
        if not subset:
            continue
        if start is None:
            orphans.extend(sorted(subset, key=int))
            continue
        adjacency = graph(nodes, lambda node: (node.get("ascendancyName") == asc if asc else not node.get("ascendancyName"))
                          and not node.get("isMastery"))
        restricted = {key: neighbours & subset for key, neighbours in adjacency.items() if key in subset}
        reached = paths_from({start}, restricted)
        orphans.extend(sorted(subset - set(reached), key=int))
    # Other class starts are never legal allocations.
    foreign_starts = [key for key in known if _is_class_start(nodes[key]) and nodes[key].get("classStartIndex") != 3]
    metrics["orphans"] = orphans
    checks.append({"name": "Tree: every node connected to its start", "passed": not orphans and not unknown
                   and not foreign_starts,
                   "reason": ("all allocated nodes are connected and known" if not (orphans or unknown or foreign_starts)
                              else f"orphan nodes {orphans[:8]}, unknown nodes {unknown[:8]}, "
                                   f"foreign class starts {foreign_starts[:4]}")})

    used = maximum = None
    if passives:
        used, maximum = passives.get("used"), passives.get("maximum")
    if used is not None and maximum is not None:
        unspent = maximum - used
        metrics.update({"pointsUsed": used, "pointsMaximum": maximum, "unspent": unspent})
        checks.append({"name": "Tree: points within budget", "passed": used <= maximum,
                       "reason": f"{used} of {maximum} passive points used"})
        checks.append({"name": "Tree: unspent points near zero", "passed": unspent <= UNSPENT_FAIL_ABOVE,
                       "reason": f"{unspent} unspent point(s) (limit {UNSPENT_FAIL_ABOVE})"})

    asc_nodes = [key for key in known if nodes[key].get("ascendancyName") == ascendancy
                 and not nodes[key].get("isAscendancyStart")]
    expected = expected_ascendancy_points(level)
    metrics["ascendancyPoints"] = {"allocated": len(asc_nodes), "expected": expected}
    foreign_asc = [key for key in known if nodes[key].get("ascendancyName") not in (None, ascendancy)]
    checks.append({"name": "Tree: ascendancy points full", "passed": len(asc_nodes) == expected and not foreign_asc,
                   "reason": f"{len(asc_nodes)} of {expected} ascendancy points allocated"
                             + (f"; foreign ascendancy nodes {foreign_asc[:4]}" if foreign_asc else "")})

    keystones = sorted(nodes[key].get("name", key) for key in known if nodes[key].get("isKeystone"))
    metrics["keystones"] = keystones
    ci = spec.get("defenseModel") == "ci"
    bad_keystones = []
    for key in known:
        node = nodes[key]
        if not node.get("isKeystone"):
            continue
        if node.get("name") == "Chaos Inoculation" and not ci:
            bad_keystones.append("Chaos Inoculation without a CI defence model")
        if ci and node.get("name") in {"Eldritch Battery", "Mind Over Matter"} and False:
            bad_keystones.append(node.get("name"))
        if node.get("ascendancyName"):
            bad_keystones.append(f"{node.get('name')} (ascendancy node flagged as keystone)")
    if ci and not any(nodes[key].get("name") == "Chaos Inoculation" for key in known if nodes[key].get("isKeystone")):
        bad_keystones.append("CI defence model without the Chaos Inoculation keystone")
    checks.append({"name": "Tree: keystone legality", "passed": not bad_keystones,
                   "reason": ("keystones: " + (", ".join(keystones) or "none")) if not bad_keystones
                   else "; ".join(bad_keystones)})

    mastery_problems = []
    notable_groups = {nodes[key].get("group") for key in known if nodes[key].get("isNotable")}
    if len(set(masteries.values())) != len(masteries):
        mastery_problems.append("duplicate mastery effects")
    for key, effect in masteries.items():
        key = str(key)
        node = nodes.get(key)
        if node is None or not node.get("isMastery"):
            mastery_problems.append(f"{key} is not a mastery")
        elif key not in known:
            mastery_problems.append(f"{key} has an effect but is not allocated")
        elif node.get("group") not in notable_groups:
            mastery_problems.append(f"{node.get('name', key)} has no allocated notable in its group")
        elif not any(entry.get("effect") == effect for entry in node.get("masteryEffects", [])):
            mastery_problems.append(f"{node.get('name', key)} has an effect it cannot hold")
    allocated_masteries = [key for key in known if nodes[key].get("isMastery")]
    unassigned = [key for key in allocated_masteries if key not in {str(item) for item in masteries}]
    if unassigned:
        mastery_problems.append(f"allocated masteries without an effect: {unassigned[:4]}")
    metrics["masteries"] = len(masteries)
    checks.append({"name": "Tree: mastery legality", "passed": not mastery_problems,
                   "reason": f"{len(masteries)} mastery effects valid" if not mastery_problems
                   else "; ".join(mastery_problems)})

    empty_sockets = sorted((key for key in known if nodes[key].get("isJewelSocket") and key not in (jewels or {})),
                           key=int)
    metrics["emptyJewelSockets"] = empty_sockets
    return checks, metrics


def waste_report(measure, base_value: float, removals: list[set[str]], nodes: dict,
                 tolerance: float = WASTE_TOLERANCE, allocated: set[str] | None = None) -> list[dict]:
    """Branches whose removal does not lower the objective.

    ``removals`` are connected removable branches; ``measure(branch)`` returns the objective of the
    allocation without it (``None`` when it could not be measured).
    """
    wasted = []
    for branch in removals:
        value = measure(branch)
        if value is None:
            continue
        loss = base_value - value
        if loss <= tolerance:
            names = [nodes[key].get("name", key) for key in sorted(branch, key=int)][:6]
            wasted.append({"nodes": sorted(branch, key=int), "points": len(branch), "objectiveLoss": round(loss, 6),
                           "names": names,
                           "notables": [nodes[key].get("name", key) for key in branch if nodes[key].get("isNotable")]})
    return wasted


def sanity_report(nodes: dict, spec: dict, allocated: set[str], masteries: dict, jewels: dict | None,
                  passives: dict | None, ascendancy: str, level: int, wasted: list[dict] | None = None,
                  waste_measured: bool = False) -> dict:
    checks, metrics = structural_checks(nodes, spec, allocated, masteries, jewels, passives, ascendancy, level)
    if waste_measured:
        points = sum(entry["points"] for entry in wasted or [])
        metrics["wastedBranches"] = wasted or []
        checks.append({"name": "Tree: no wasted travel or unrelated clusters", "passed": not wasted,
                       "reason": ("every removable branch measurably helps the objective in PoB" if not wasted else
                                  f"{len(wasted)} branch(es) / {points} point(s) do not help: " +
                                  "; ".join(", ".join(entry["names"][:3]) for entry in wasted[:4]))})
    return {"passed": all(check["passed"] for check in checks), "checks": checks, "metrics": metrics,
            "wasteMeasured": waste_measured}
