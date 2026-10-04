"""Main-link and supporting-package planning against an evaluator callback.

Nothing here talks to PoB directly.  The caller supplies callbacks:

``ops`` for main-link planning (``MainLinkOps`` protocol):
    ``ops.candidates(supports) -> list[str]``   compatible support names for the
        main skill given the supports chosen so far;
    ``ops.score(supports, names) -> {name: stats}``  PoB stats with each
        candidate appended to ``supports``;
    ``ops.stats(supports) -> stats``            PoB stats for exactly ``supports``.

``evaluate(groups) -> {"stats": {...}, "ok": bool, "reasons": [..]}`` for
utility-package planning: a whole-candidate evaluation of the complete list of
skill groups (main group first).  ``legality(groups, group) -> [problems]`` is
optional and reports supports PoB would reject on a utility group.

Both planners return candidate state and never mutate their inputs, so the
caller (the generator's search) can compare alternatives, including
*repairable* links whose mana cost does not fit yet: a support is never
dropped just to fit present mana, and a compatible support with no
demonstrated effect is never used as padding.
"""
from __future__ import annotations

import copy
from typing import Callable, Iterable, Protocol

from generation_data import support_is_noncombat
from skill_packages import (
    candidate_packages, capacity_from_equipment, clone_groups, build_facts, fill_packages, gem_level_for,
    make_group, package_group, pack_groups, role_coverage, socketed_gem_count, trigger_level_legal,
    validate_groups, group_names, attribute_requirements)

DPS_KEYS = ("FullDPS", "FullDotDPS", "CombinedDPS", "TotalDPS", "TotalDotDPS")
MIN_SUPPORT_GAIN = 0.005          # a support must add 0.5% sustained damage ...
MIN_COST_REDUCTION = 0.03         # ... or cut mana cost 3% ...
MAX_OFFENSE_LOSS = 0.03           # utility may not cost more than 3% main damage
MIN_AURA_GAIN = 0.015
MIN_DEFENSE_GAIN = 0.01
COVERAGE_SUPPORTS = {
    "Increased Area of Effect": ("area", "area"), "Concentrated Effect": ("area", None),
    "Greater Multiple Projectiles": ("projectile", "projectile"),
    "Lesser Multiple Projectiles": ("projectile", "projectile"),
    "Chain": ("projectile", "projectile"), "Pierce": ("projectile", "projectile"),
    "Fork": ("projectile", "projectile"), "Volley": ("projectile", "projectile"),
    "Ignite Proliferation": ("spread", "fire"), "Spell Echo": ("repeat", "spell"),
}
BANNED_MAIN_SUPPORTS = {"Sacrifice", "Vaal Sacrifice", "Cast on Death"}


def offense(stats: dict) -> float:
    return max((float(stats.get(key, 0) or 0) for key in DPS_KEYS), default=0.0)


def resource_deficits(stats: dict) -> dict[str, float]:
    """Positive entries are shortfalls of the *present* resources, not rejections."""
    result = {}
    mana_cost, mana_free = float(stats.get("ManaCost", 0) or 0), stats.get("ManaUnreserved")
    if mana_free is not None and mana_cost > float(mana_free):
        result["mana"] = mana_cost - float(mana_free)
    life_free = stats.get("LifeUnreserved", stats.get("Life"))
    if life_free is not None and float(life_free) <= 0:
        result["life"] = 1.0
    return result


def attribute_deficits(stats: dict) -> dict[str, float]:
    out = {}
    for attr in ("Str", "Dex", "Int"):
        need, have = float(stats.get("Req" + attr, 0) or 0), stats.get(attr)
        if have is not None and need > float(have):
            out[attr] = need - float(have)
    return out


class MainLinkOps(Protocol):
    def candidates(self, supports: list[str]) -> list[str]: ...
    def score(self, supports: list[str], names: list[str]) -> dict[str, dict]: ...
    def stats(self, supports: list[str]) -> dict: ...


# ----------------------------------------------------------------------------
# Main link: complete six-gem links
# ----------------------------------------------------------------------------

def support_role(name: str, with_stats: dict, without_stats: dict, value: Callable[[dict], float],
                 data, main_tags: dict) -> dict:
    """Why a support is in the link, from measured stats.  ``role=None`` means filler."""
    base, now = value(without_stats), value(with_stats)
    gain = (now - base) / base if base > 0 else (1.0 if now > 0 else 0.0)
    cost_before, cost_after = float(without_stats.get("ManaCost", 0) or 0), float(with_stats.get("ManaCost", 0) or 0)
    cost_cut = (cost_before - cost_after) / cost_before if cost_before > 0 else 0.0
    if gain >= MIN_SUPPORT_GAIN:
        return {"name": name, "role": "damage", "gain": gain, "detail": f"+{gain:.1%} sustained damage"}
    if cost_cut >= MIN_COST_REDUCTION:
        return {"name": name, "role": "cost", "gain": gain, "costReduction": cost_cut,
                "detail": f"-{cost_cut:.0%} mana cost"}
    area_before, area_after = without_stats.get("AreaOfEffectRadius"), with_stats.get("AreaOfEffectRadius")
    if area_before and area_after and area_after > area_before * 1.03:
        return {"name": name, "role": "clear", "gain": gain, "detail": "larger area of effect"}
    return {"name": name, "role": None, "gain": gain, "detail": "no demonstrated effect"}


def _coverage_relevant(name: str, main_tags: dict, spec: dict) -> bool:
    kind = COVERAGE_SUPPORTS.get(name)
    if not kind:
        return False
    needs = kind[1]
    if needs is None:
        return True
    if needs == "fire":
        return bool(main_tags.get("fire") and (spec.get("archetype") == "ignite" or
                                               spec.get("damageMechanism") == "ignite"))
    return bool(main_tags.get(needs))


def plan_main_link(spec: dict, data, ops: MainLinkOps, *, size: int = 6, width: int = 3,
                   shortlist: int = 22, value: Callable[[dict], float] = offense,
                   allow_exceptional: bool = False, max_repairs: int = 3,
                   hint: Callable[[str], float] | None = None,
                   banned: Iterable[str] = ()) -> dict:
    """Beam-search a complete ``size``-gem main link.

    Returns ``{"complete", "supports", "alternatives", "mappingLink", "bossLink",
    "supportRoles", "repairNeeded", "trace", "reason"}``.  ``alternatives`` holds
    other complete links (including ones with a present resource deficit that a
    whole-build repair could resolve).  ``supports`` is only filler-free: if
    fewer than ``size - 1`` supports have a demonstrated effect, ``complete`` is
    ``False`` and the reason says why; the link is never padded.
    """
    main_tags = data.gem(spec["skill"]).get("tags", {})
    forbidden = set(BANNED_MAIN_SUPPORTS) | set(banned)
    trace: list[dict] = []
    need = size - 1
    cache: dict[tuple, dict] = {}

    def stats_for(supports: list[str]) -> dict:
        key = tuple(supports)
        if key not in cache:
            cache[key] = ops.stats(list(supports))
        return cache[key]

    def eligible(names: Iterable[str], chosen: list[str]) -> list[str]:
        result = []
        for name in names:
            gem = data.gems.get(name)
            if gem is None or name in chosen or name in forbidden or not gem.get("support"):
                continue
            if name.startswith("Awakened ") or support_is_noncombat(gem):
                continue
            exceptional = bool(gem.get("tags", {}).get("exceptional")) or gem.get("maxLevel", 20) < 20
            if exceptional and not allow_exceptional:
                continue
            result.append(name)
        return result

    def rank(name: str) -> float:
        if hint:
            return hint(name)
        return 1.0 if _coverage_relevant(name, main_tags, spec) else 0.0

    def search() -> list[dict]:
        states = [{"supports": [], "stats": stats_for([])}]
        states[0]["value"] = value(states[0]["stats"])
        for depth in range(need):
            expanded: dict[tuple, dict] = {}
            for state in states:
                names = eligible(ops.candidates(state["supports"]), state["supports"])
                names = sorted(names, key=lambda n: (-rank(n), n))[:shortlist]
                if not names:
                    continue
                scored = ops.score(state["supports"], names)
                for name, stats in scored.items():
                    supports = [*state["supports"], name]
                    key = tuple(sorted(supports))
                    cache.setdefault(tuple(supports), stats)
                    candidate = {"supports": supports, "stats": stats, "value": value(stats)}
                    if key not in expanded or candidate["value"] > expanded[key]["value"]:
                        expanded[key] = candidate
                trace.append({"kind": "main_link_expand", "depth": depth + 1, "from": list(state["supports"]),
                              "candidates": len(names), "scored": len(scored)})
            if not expanded:
                break
            ranked_states = sorted(expanded.values(), key=lambda e: e["value"], reverse=True)
            feasible = [e for e in ranked_states if not resource_deficits(e["stats"])][:width]
            repairable = [e for e in ranked_states if resource_deficits(e["stats"])][:max(1, width // 2)]
            states = feasible + repairable
        return [s for s in states if len(s["supports"]) == need]

    result = {"complete": False, "supports": [], "alternatives": [], "supportRoles": [],
              "repairNeeded": {}, "trace": trace, "reason": None, "mappingLink": None,
              "bossLink": None, "size": size}
    chosen = None
    last_ranked: list[dict] = []
    for attempt in range(max_repairs + 1):
        complete = search()
        if not complete:
            if attempt > 0 and last_ranked:
                best_roles = _link_roles(spec, data, stats_for, last_ranked[0]["supports"], value, main_tags)
                useful = [row["name"] for row in best_roles if row["role"]]
                result.update({"supports": useful, "supportRoles": best_roles,
                               "reason": "Compatible supports without a demonstrated effect were rejected as filler; "
                                         f"{len(useful)} of {need} supports are useful"})
            else:
                result["reason"] = f"Fewer than {need} compatible supports could be scored; no padding was added"
            return result
        last_ranked = sorted(complete, key=lambda s: (not resource_deficits(s["stats"]), s["value"]), reverse=True)
        filler_here: set[str] = set()
        for candidate in last_ranked:
            roles = _link_roles(spec, data, stats_for, candidate["supports"], value, main_tags)
            filler = {row["name"] for row in roles if row["role"] is None}
            if not filler:
                chosen = {**candidate, "roles": roles}
                break
            trace.append({"kind": "main_link_filler", "supports": candidate["supports"],
                          "filler": sorted(filler), "attempt": attempt + 1})
            if not filler_here:
                filler_here = filler
        if chosen:
            break
        forbidden.update(filler_here)
    if chosen is None:
        best = last_ranked[0]
        roles = _link_roles(spec, data, stats_for, best["supports"], value, main_tags)
        useful = [row["name"] for row in roles if row["role"]]
        result.update({"supports": useful, "supportRoles": roles,
                       "reason": "Compatible supports without a demonstrated effect were rejected as filler; "
                                 f"{len(useful)} of {need} supports are useful"})
        return result
    result.update({"complete": True, "supports": chosen["supports"], "supportRoles": chosen["roles"],
                   "repairNeeded": resource_deficits(chosen["stats"]), "stats": chosen["stats"]})
    for other in last_ranked:
        if other["supports"] != chosen["supports"] and len(result["alternatives"]) < 4:
            result["alternatives"].append({"supports": other["supports"], "value": other["value"],
                                            "repairNeeded": resource_deficits(other["stats"])})
    result["bossLink"] = list(chosen["supports"])
    result["mappingLink"] = plan_mapping_link(spec, data, ops, chosen, value, main_tags, trace, stats_for)
    return result


def _link_roles(spec, data, stats_for, supports: list[str], value, main_tags) -> list[dict]:
    full = stats_for(supports)
    roles = []
    for name in supports:
        without = stats_for([s for s in supports if s != name])
        role = support_role(name, full, without, value, data, main_tags)
        if role["role"] is None and _coverage_relevant(name, main_tags, spec):
            role = {**role, "role": "clear", "detail": "coverage support with a real skill interaction",
                    "coverageKind": COVERAGE_SUPPORTS[name][0]}
        roles.append(role)
    return roles


def plan_mapping_link(spec: dict, data, ops, boss: dict, value, main_tags: dict, trace: list,
                      stats_for, *, min_retention: float = 0.6, swaps: int = 2) -> dict:
    """Swap the weakest supports for coverage supports with a real skill interaction."""
    base_value = value(boss["stats"])
    marginal = sorted((base_value - value(stats_for([s for s in boss["supports"] if s != name])), name)
                      for name in boss["supports"])
    compat = set(ops.candidates(list(boss["supports"][:-1]))) | set(boss["supports"])
    coverage = [n for n in COVERAGE_SUPPORTS if n in data.gems and _coverage_relevant(n, main_tags, spec)
                and n not in boss["supports"] and n in compat]
    best = None
    for name in coverage:
        for _, victim in marginal[:swaps]:
            supports = [name if s == victim else s for s in boss["supports"]]
            stats = stats_for(supports)
            retention = value(stats) / base_value if base_value > 0 else 0.0
            record = {"supports": supports, "swap": f"{victim} -> {name}", "coverage": COVERAGE_SUPPORTS[name][0],
                      "singleTargetRetention": retention, "repairNeeded": resource_deficits(stats)}
            trace.append({"kind": "mapping_swap", "swap": record["swap"],
                          "singleTargetRetention": retention})
            if retention >= min_retention and (best is None or retention > best["singleTargetRetention"]):
                best = record
    if best is None:
        return {"supports": list(boss["supports"]), "swap": None, "coverage": None,
                "singleTargetRetention": 1.0, "repairNeeded": {},
                "reason": "No coverage support with a real interaction kept enough single-target damage"}
    return best


# ----------------------------------------------------------------------------
# Supporting packages
# ----------------------------------------------------------------------------

def _stat(stats: dict, key: str) -> float:
    return float(stats.get(key, 0) or 0)


def judge_package(pkg: dict, before: dict, after: dict, *, focus: str = "balanced") -> dict:
    """Accept/reject one package from PoB stats.  Never pads: stat packages must move their stat."""
    problems = []
    deficits = resource_deficits(after)
    already = resource_deficits(before)
    new_deficits = {key: value for key, value in deficits.items() if key not in already or value > already[key]}
    if new_deficits:
        problems.append("resource shortfall: " + ", ".join(f"{k} {v:,.0f}" for k, v in new_deficits.items()))
    attrs = attribute_deficits(after)
    new_attrs = {k: v for k, v in attrs.items() if k not in attribute_deficits(before)}
    if new_attrs:
        problems.append("attribute requirement not met: " + ", ".join(f"{k} short by {v:,.0f}" for k, v in new_attrs.items()))
    loss_limit = 0.08 if focus == "defense" else MAX_OFFENSE_LOSS
    base, now = offense(before), offense(after)
    loss = (base - now) / base if base > 0 else 0.0
    if loss > loss_limit and pkg["evidence"] != "stat:damage":
        problems.append(f"costs {loss:.1%} main-skill damage (limit {loss_limit:.0%})")
    metrics = {"offenseChange": (now - base) / base if base > 0 else 0.0}
    evidence = pkg["evidence"]
    verdict = "role"
    if evidence == "stat:damage":
        gain = metrics["offenseChange"]
        verdict = "stat"
        if gain < MIN_AURA_GAIN:
            problems.append(f"PoB shows only {gain:+.1%} damage (needs +{MIN_AURA_GAIN:.1%})")
    elif evidence == "stat:defense":
        verdict = "stat"
        gains = {}
        for key in ("TotalEHP", "EnergyShield", "Life", "Armour", "Evasion"):
            b, a = _stat(before, key), _stat(after, key)
            if b > 0:
                gains[key] = (a - b) / b
        resist_gain = sum(_stat(after, k + "Resist") - _stat(before, k + "Resist")
                          for k in ("Fire", "Cold", "Lightning", "Chaos"))
        metrics.update({"gains": gains, "resistGain": resist_gain})
        useful = (gains.get("TotalEHP", 0) >= MIN_DEFENSE_GAIN or gains.get("EnergyShield", 0) >= 0.02
                  or gains.get("Armour", 0) >= 0.10 or gains.get("Evasion", 0) >= 0.10 or resist_gain >= 5)
        if not useful:
            problems.append("PoB shows no measurable defensive gain")
    elif evidence == "stat:mana":
        verdict = "stat"
        if _stat(after, "ManaRegen") <= _stat(before, "ManaRegen") * 1.02 or not already:
            problems.append("no mana-sustain deficit to repair")
    elif evidence == "stat:reservation":
        verdict = "stat"
        if _stat(after, "ManaUnreserved") <= _stat(before, "ManaUnreserved"):
            problems.append("does not free any reserved mana")
    for key, floor in (pkg.get("minStats") or {}).items():
        if _stat(after, key) < floor:
            problems.append(f"{key} is {_stat(after, key):,.0f} with it, below the {floor:,.0f} that makes "
                            f"the reservation worthwhile (mechanically unjustified)")
    return {"ok": not problems, "problems": problems, "evidence": verdict, "metrics": metrics}


def _with_group(groups: list[dict], group: dict) -> list[dict]:
    return [*clone_groups(groups), copy.deepcopy(group)]


def _merge_reservation(groups: list[dict], additions: list[tuple[str, str]], data, level) -> list[dict]:
    """Add gems to the shared ``reservation`` group (created if absent)."""
    result = clone_groups(groups)
    group = next((g for g in result if g["id"] == "reservation"), None)
    if group is None:
        group = make_group("reservation", "aura", [], delivery="permanent",
                           justification="Reserved auras and buffs linked with reservation support")
        result.append(group)
    for name, kind in additions:
        group["gems"].append({"instance": f"reservation:{len(group['gems']) + 1}", "name": name,
                              "kind": kind, "level": gem_level_for(data, name, level), "quality": 0,
                              "enabled": True, "count": 1})
    actives = [g for g in group["gems"] if g["kind"] == "active"]
    group["mainActive"] = actives[0]["name"] if actives else None
    return result


def fit_levels_to_attributes(groups: list[dict], known: set[str], stats: dict, data) -> list[str]:
    """Lower newly added gems to the highest level whose attribute needs are met.

    Gems already evaluated (``known`` instance IDs) are untouched.  Returns the
    names of gems that cannot be used even at level 1 with the current
    attributes.
    """
    blocked = []
    have = {attr: stats.get(attr.capitalize()) for attr in ("str", "dex", "int")}
    for group in groups:
        for gem in group["gems"]:
            if gem["instance"] in known:
                continue
            table = data.gem(gem["name"]).get("levels") or []
            if not table or not gem.get("level"):
                continue
            usable = [row["level"] for row in table if row["level"] <= gem["level"]
                      and all(have[attr] is None or row.get(attr, 0) <= have[attr]
                              for attr in ("str", "dex", "int"))]
            if not usable:
                blocked.append(gem["name"])
            elif max(usable) < gem["level"]:
                gem["level"] = max(usable)
                gem["levelNote"] = "lowered to fit attribute requirements"
    return blocked


def plan_utility_groups(spec: dict, data, main_group: dict, evaluate: Callable[[list[dict]], dict], *,
                        capacity: dict[str, dict], items: Iterable = (), uniques: dict[str, str] | None = None,
                        legality: Callable[[list[dict], dict], list[str]] | None = None,
                        main_support_names: Iterable[str] | None = None,
                        max_evaluations: int = 80, requested: Iterable[str] = (),
                        attribute_repair: Callable[[list[dict]], dict | None] | None = None) -> dict:
    """Plan movement/guard/aura/defense/herald/curse/helper groups around ``main_group``.

    ``attribute_repair(candidate_groups)`` may add attribute affixes to the rare gear so a package's gems
    can be socketed; it returns ``{"stats", "rollback"}`` (rollback undoes the gear change if the package
    is not accepted) or ``None`` when the attributes cannot be found.

    ``evaluate`` is called with the full group list (main first).  Returns the
    chosen groups (packed into slots), the omitted packages with reasons, role
    coverage, per-item socket occupancy and a trace.  Omissions are explained,
    not hidden: a package is omitted for sockets, legality, resources, a
    failed declared-function check, an unavailable gem, or no budget left.
    Supports PoB rejects on a package's active gem are dropped individually
    (and reported) rather than discarding the whole package.
    """
    facts = build_facts(spec, items, uniques, data)
    # A package that needs an off-hand shield is only offered when that slot exists and has sockets
    # for it (a two-handed or socketless Weapon 2 cannot host it).
    facts["shield"] = bool(facts["shield"] and capacity.get("Weapon 2", {}).get("total", 0) >= 3)
    level = facts["level"]
    support_names = list(main_support_names if main_support_names is not None else
                         [g["name"] for g in main_group["gems"] if g["kind"] == "support"])
    packages = candidate_packages(spec, data, facts, main_support_names=support_names)
    skipped = []
    for want in requested:
        if want in data.gems and not any(want in [n for n, _ in p["gems"]] for p in packages):
            skipped.append({"package": want, "reason": "explicitly requested utility has no matching package; "
                                                         "it is handled by the caller as a standalone group"})
    evals = {"used": 0}
    repair_groups: dict[str, list[dict]] = {}   # package id -> the whole candidate that failed on mana only
    trace: list[dict] = []
    groups = [copy.deepcopy(main_group)]
    omissions: list[dict] = []
    accepted: list[dict] = []
    dropped_supports: list[dict] = []

    def run(candidate_groups: list[dict]) -> dict | None:
        if evals["used"] >= max_evaluations:
            return None
        evals["used"] += 1
        return evaluate(candidate_groups)

    base_eval = run(groups)
    if base_eval is None or base_eval.get("ok") is False:
        return {"groups": groups, "omissions": [], "accepted": [], "trace": trace,
                "error": "baseline main group failed evaluation: " +
                         "; ".join((base_eval or {}).get("reasons", ["no evaluation"])),
                "evaluations": evals["used"]}
    current = base_eval["stats"]

    def omit(pkg: dict, reason: str, **extra):
        omissions.append({"package": pkg["id"], "role": pkg["role"], "reason": reason, **extra})
        return False

    def try_state(pkg: dict, candidate_groups: list[dict], label: str) -> bool:
        hold: dict = {}
        accepted_state = _try_state(pkg, candidate_groups, label, hold)
        if not accepted_state and hold.get("rollback"):
            hold["rollback"]()          # a gear change made for a package that was not accepted is undone
        return accepted_state

    def _try_state(pkg: dict, candidate_groups: list[dict], label: str, hold: dict) -> bool:
        nonlocal groups, current
        known = {gem["instance"] for g in groups for gem in g["gems"]}
        blocked = fit_levels_to_attributes(candidate_groups, known, current, data)
        if blocked and attribute_repair is not None and label != "fill":   # spare-socket fillers never reshape gear
            trial = copy.deepcopy(candidate_groups)
            repaired = attribute_repair(trial)
            if repaired:
                still = fit_levels_to_attributes(trial, known, repaired["stats"], data)
                if still:
                    repaired["rollback"]()
                else:
                    hold["rollback"] = repaired["rollback"]
                    candidate_groups[:] = trial
                    blocked = []
        optional = set(pkg.get("optionalSupports") or ())
        if blocked and optional and all(name in optional for name in blocked):
            # An optional support the attributes cannot carry is dropped; the package's active gem stays
            # ("Flame Dash alone" is a declared alternative of the movement package).
            for group in candidate_groups:
                group["gems"] = [gem for gem in group["gems"]
                                 if not (gem["name"] in blocked and gem["instance"] not in known)]
            for name in sorted(blocked):
                dropped_supports.append({"package": pkg["id"], "support": name,
                                         "reason": "attribute requirements cannot be met; optional support dropped"})
            blocked = fit_levels_to_attributes(candidate_groups, known, current, data)
        if blocked:
            return omit(pkg, "attribute requirements cannot be met even at level 1: " + ", ".join(blocked),
                        repairable=False)
        # Sockets first: PoB legality renders the whole candidate, which fails outright if it cannot be packed.
        ok, packing = pack_ok(candidate_groups)
        if not ok:
            return omit(pkg, "no equipment sockets: " + "; ".join(packing["errors"][:2]))
        # Legality: drop individual supports PoB rejects, keep the rest of the package.
        if legality is not None:
            old_ids = {g["id"] for g in groups}
            for group in candidate_groups:
                if group["id"] in old_ids and group["id"] not in {"reservation"} and group["role"] != "herald":
                    continue
                for _ in range(3):
                    problems = legality(candidate_groups, group)
                    if not problems:
                        break
                    bad = {text.split(" cannot support ")[0] for text in problems}
                    removable = [gem for gem in group["gems"] if gem["kind"] == "support" and gem["name"] in bad
                                 and gem["instance"] not in known]
                    if not removable:
                        return omit(pkg, "PoB rejects the link: " + "; ".join(problems))
                    for gem in removable:
                        group["gems"].remove(gem)
                        dropped_supports.append({"package": pkg["id"], "support": gem["name"],
                                                 "reason": "PoB: cannot support the group's active skill"})
        ok, packing = pack_ok(candidate_groups)
        if not ok:
            return omit(pkg, "no equipment sockets: " + "; ".join(packing["errors"][:2]))
        evaluation = run(candidate_groups)
        if evaluation is None:
            return omit(pkg, "evaluation budget exhausted")
        if evaluation.get("ok") is False:
            return omit(pkg, "candidate failed evaluation: " + "; ".join(evaluation.get("reasons", [])))
        verdict = judge_package(pkg, current, evaluation["stats"], focus=facts["focus"])
        trace.append({"kind": "package", "package": pkg["id"], "label": label, "ok": verdict["ok"],
                      "problems": verdict["problems"], "evidence": verdict["evidence"],
                      "metrics": verdict["metrics"]})
        if not verdict["ok"]:
            repairable = any(p.startswith("resource") for p in verdict["problems"])
            omit(pkg, "; ".join(verdict["problems"]), repairable=repairable)
            if repairable:
                repair_groups[pkg["id"]] = copy.deepcopy(candidate_groups)
            return False
        groups = candidate_groups
        current = evaluation["stats"]
        accepted.append({"package": pkg["id"], "role": pkg["role"], "function": pkg["function"],
                         "evidence": verdict["evidence"], "metrics": verdict["metrics"],
                         "uncountedBenefit": pkg.get("uncountedBenefit"), "label": label})
        return True

    def pack_ok(candidate_groups):
        packing = pack_groups(candidate_groups, capacity)
        return not packing["unplaced"], packing

    by_id = {pkg["id"]: pkg for pkg in packages}
    enlighten = by_id.get("support_enlighten")

    def has_enlighten(candidate_groups) -> bool:
        return any(gem["name"] == "Enlighten" for g in candidate_groups for gem in g["gems"])

    # Non-reserving packages first (movement, guard, curse, helpers).
    for pkg in packages:
        if pkg["reserves"] or pkg["id"] == "support_enlighten":
            continue
        if pkg["role"] == "movement" and any(entry["role"] == "movement" for entry in accepted):
            omit(pkg, "one movement skill is the default; another was already placed")
            continue
        drop: list[str] = []
        if pkg["trigger"]:
            trigger, triggered = pkg["trigger"]
            ok, why = trigger_level_legal(data, trigger, gem_level_for(data, trigger, level),
                                          triggered, gem_level_for(data, triggered, level))
            if not ok:
                drop.append(trigger)
                trace.append({"kind": "trigger_dropped", "package": pkg["id"], "reason": why,
                              "plan": "retain a manual-use plan"})
        group = package_group(pkg, data, level, drop=drop)
        if drop:
            group["delivery"] = "manual"
        try_state(pkg, _with_group(groups, group), "group")

    # Reservation: auras share one linked group; Enlighten repairs reservation.
    reserving = [pkg for pkg in packages if pkg["reserves"] and pkg["role"] in {"aura", "defense"}]
    if facts["focus"] == "defense":
        reserving.sort(key=lambda p: (p["role"] != "defense", p["priority"]))
    for pkg in reserving:
        additions = [(n, k) for n, k in pkg["gems"]]
        if try_state(pkg, _merge_reservation(groups, additions, data, level), "aura"):
            continue
        last = omissions[-1]
        if enlighten and last.get("repairable") and not has_enlighten(groups):
            omissions.pop()
            repaired = _merge_reservation(groups, [*additions, ("Enlighten", "support")], data, level)
            if not try_state({**pkg, "id": pkg["id"] + "+enlighten"}, repaired, "aura with Enlighten"):
                omissions[-1]["package"] = pkg["id"]
                omissions[-1]["reason"] = "reservation does not fit even with Enlighten: " + omissions[-1]["reason"]
    for pkg in packages:
        if pkg["role"] != "herald":
            continue
        group = package_group(pkg, data, level)
        if try_state(pkg, _with_group(groups, group), "herald"):
            continue
        if enlighten and omissions[-1].get("repairable") and not has_enlighten(groups):
            omissions.pop()
            with_enlighten = package_group(pkg, data, level)
            with_enlighten["gems"].append({"instance": f"{pkg['id']}:{len(with_enlighten['gems']) + 1}",
                                           "name": "Enlighten", "kind": "support",
                                           "level": gem_level_for(data, "Enlighten", level), "quality": 0,
                                           "enabled": True, "count": 1})
            if not try_state({**pkg, "id": pkg["id"]}, _with_group(groups, with_enlighten),
                             "herald with Enlighten"):
                omissions[-1]["reason"] = "reservation does not fit even with Enlighten: " + omissions[-1]["reason"]

    # Spare sockets: every socket on every equipped item gets a justified group, or is reported.
    def spare_by_slot(candidate_groups):
        packing = pack_groups(candidate_groups, capacity)
        used = {slot: 0 for slot in capacity}
        for g in candidate_groups:
            slot = packing["placements"].get(g["id"])
            if slot in used:
                used[slot] += len(g["gems"])
        return {slot: info["total"] - used[slot] for slot, info in capacity.items()}

    fill_report: list[dict] = []
    fill_exhausted = False
    if spare_by_slot(groups) and max(spare_by_slot(groups).values(), default=0) > 0:
        present_names = {gem["name"] for g in groups for gem in g["gems"] if gem["kind"] == "active"}
        for pkg in fill_packages(spec, data, facts, present_names, stats=current,
                                 roles={entry["role"] for entry in accepted}):
            spare = spare_by_slot(groups)
            largest = max(spare.values(), default=0)
            if largest <= 0:
                break
            if any(name in {gem["name"] for g in groups for gem in g["gems"]} for name, kind in pkg["gems"][:1]):
                continue
            if pkg["role"] == "movement" and any(entry["role"] == "movement" for entry in accepted):
                continue   # one movement skill by default
            trimmed = pkg["gems"][:largest]
            if len(trimmed) < len(pkg["gems"]):
                trimmed_pkg = {**pkg, "gems": trimmed}
            else:
                trimmed_pkg = pkg
            group = package_group(trimmed_pkg, data, level)
            before = len(accepted)
            if try_state({**trimmed_pkg, "id": pkg["id"]}, _with_group(groups, group), "fill"):
                accepted[-1]["fill"] = True
                fill_report.append({"package": pkg["id"], "role": pkg["role"], "gems": [n for n, _ in trimmed],
                                    "evidence": accepted[-1]["evidence"], "function": pkg["function"]})
            elif omissions and omissions[-1]["package"] == pkg["id"]:
                omissions[-1]["fill"] = True
        else:
            # The loop ran through every catalogue candidate (no ``break`` for lack of sockets).
            fill_exhausted = max(spare_by_slot(groups).values(), default=0) > 0
    groups = _bundle_vaal_fillers(groups, capacity)
    final_packing = pack_groups(groups, capacity)
    placed = []
    for group in groups:
        entry = copy.deepcopy(group)
        entry["slot"] = final_packing["placements"].get(group["id"], group.get("slot"))
        placed.append(entry)
    problems = validate_groups(placed, capacity, data)
    occupancy = {}
    for slot, info in capacity.items():
        used = sum(len(g["gems"]) for g in placed if g["slot"] == slot)
        occupancy[slot] = {"used": used, "available": info["total"], "spare": max(0, info["total"] - used),
                           "unique": info.get("unique", False)}
    leftovers = {slot: info["spare"] for slot, info in occupancy.items() if info["spare"] > 0}
    return {"groups": placed, "accepted": accepted, "omissions": omissions + skipped,
            "fill": fill_report, "fillExhausted": fill_exhausted, "spareSockets": leftovers, "repairGroups": repair_groups,
            "droppedSupports": dropped_supports,
            "roles": role_coverage(placed), "socketedGemCount": socketed_gem_count(placed),
            "occupancy": occupancy, "runs": final_packing["runs"], "problems": problems,
            "trace": trace, "evaluations": evals["used"], "stats": current}


def _bundle_vaal_fillers(groups: list[dict], capacity: dict) -> list[dict]:
    """Link the single-gem Vaal fillers that share an item into one utility group (one linked run)."""
    fillers = [g for g in groups if g["id"].startswith("fill_vaal_") and len(g["gems"]) == 1]
    if len(fillers) < 2:
        return groups
    placements = pack_groups(groups, capacity)["placements"]
    by_slot: dict[str, list[dict]] = {}
    for group in fillers:
        by_slot.setdefault(placements.get(group["id"], ""), []).append(group)
    result = list(groups)
    for slot, members in by_slot.items():
        if len(members) < 2:
            continue
        bundle = copy.deepcopy(members[0])
        bundle["id"] = "fill_vaal_utilities"
        for other in members[1:]:
            bundle["gems"].extend(copy.deepcopy(other["gems"]))
        bundle["justification"] = ("Vaal utility gems linked together on one item: " +
                                   ", ".join(g["gems"][0]["name"] for g in members))
        trial = [g for g in result if g not in members]
        if "fill_vaal_utilities" in {g["id"] for g in trial}:
            continue
        trial.append(bundle)
        if not pack_groups(trial, capacity)["unplaced"]:
            result = trial
    return result


def plan_skill_loadout(spec: dict, data, main_supports: list[str], evaluate, *, capacity, items=(),
                       uniques=None, legality=None, max_evaluations: int = 80, requested=(),
                       attribute_repair=None) -> dict:
    """Main six-link group plus supporting packages (candidate state for the generator)."""
    level = int(spec.get("level", 80))
    main = make_group("main", "main",
                      [(spec["skill"], "active"), *[(name, "support") for name in main_supports]],
                      slot="Body Armour", main_active=spec["skill"], include_in_full_dps=True,
                      level_for=lambda name: gem_level_for(data, name, level),
                      justification="Main damage link")
    plan = plan_utility_groups(spec, data, main, evaluate, capacity=capacity, items=items, uniques=uniques,
                               legality=legality, main_support_names=main_supports,
                               max_evaluations=max_evaluations, requested=requested,
                               attribute_repair=attribute_repair)
    plan["mainLinkComplete"] = len(main["gems"]) >= 6
    return plan


# ----------------------------------------------------------------------------
# Adapters: real PoB worker and Agent 1's Evaluator contract
# ----------------------------------------------------------------------------

class PobMainLinkOps:
    """``MainLinkOps`` over the PoB worker.

    ``render(groups, **kw) -> xml`` serializes a complete candidate for the given
    skill groups (main group first).  ``main_group`` builds the main group from
    a support list.
    """

    def __init__(self, worker, data, render: Callable, spec: dict, level: int | None = None):
        self.worker, self.data, self.render, self.spec = worker, data, render, spec
        self.level = level if level is not None else int(spec.get("level", 80))
        self.calls = 0

    def _main(self, supports: list[str]) -> list[dict]:
        return [make_group("main", "main", [(self.spec["skill"], "active"), *[(n, "support") for n in supports]],
                           slot="Body Armour", main_active=self.spec["skill"], include_in_full_dps=True,
                           level_for=lambda name: gem_level_for(self.data, name, self.level))]

    def candidates(self, supports: list[str]) -> list[str]:
        self.calls += 1
        ids = self.worker.request("supports", xml=self.render(self._main(supports)))["supports"]
        return [self.data.by_id[i]["name"] for i in ids if i in self.data.by_id]

    def score(self, supports: list[str], names: list[str]) -> dict[str, dict]:
        xml = self.render(self._main(supports))
        result: dict[str, dict] = {}
        for offset in range(0, len(names), 20):
            batch = names[offset:offset + 20]
            self.calls += len(batch)
            rows = self.worker.request("supportScores", xml=xml,
                                       candidates=[self.data.gem(n)["id"] for n in batch])["candidates"]
            for row in rows:
                gem = self.data.by_id.get(row["id"])
                if gem:
                    result[gem["name"]] = row["stats"]
        return result

    def stats(self, supports: list[str]) -> dict:
        self.calls += 1
        return self.worker.request("calculate", xml=self.render(self._main(supports)))["stats"]


class PobGroupEvaluator:
    """``evaluate(groups)`` and ``legality(groups, group)`` over the PoB worker."""

    def __init__(self, worker, data, render: Callable):
        self.worker, self.data, self.render = worker, data, render
        self.calls = 0

    def __call__(self, groups: list[dict]) -> dict:
        self.calls += 1
        try:
            calc = self.worker.request("calculate", xml=self.render(groups))
        except Exception as exc:  # a failed PoB import is an evaluation failure, not a crash
            return {"stats": {}, "ok": False, "reasons": [str(exc)[:200]]}
        return {"stats": calc["stats"], "ok": bool(calc.get("calculated", True)), "reasons": [],
                "passives": calc.get("passives")}

    def legality(self, groups: list[dict], group: dict) -> list[str]:
        """Supports PoB would not allow on the group's selected active skill."""
        supports = [g for g in group["gems"] if g["kind"] == "support"]
        if not supports or not any(g["kind"] == "active" for g in group["gems"]):
            return []
        self.calls += 1
        xml = self.render(groups, main_group_id=group["id"])
        compatible = set(self.worker.request("supports", xml=xml)["supports"])
        problems = []
        for gem in supports:
            record = self.data.gem(gem["name"])
            if record["id"] not in compatible and not record.get("tags", {}).get("trigger"):
                problems.append(f"{gem['name']} cannot support {group.get('mainActive')}")
        return problems


def evaluation_from_result(result) -> dict:
    """Convert ``build_contracts.EvaluationResult`` into the planner's evaluation dict."""
    reasons = [check.message or check.id for check in result.failures()]
    return {"stats": (result.calculation or {}).get("stats", {}), "ok": result.feasible,
            "reasons": reasons, "resourceDeficits": dict(result.resource_deficits),
            "score": result.score}


def make_contract_evaluate(evaluator, candidate, encounter: dict, budget=None) -> Callable[[list[dict]], dict]:
    """Planner ``evaluate`` backed by Agent 1's ``Evaluator`` and a ``Candidate``.

    Each call clones the candidate, installs the planner's groups (as contract
    ``SkillGroup`` objects) and evaluates it, consuming ``budget`` through the
    evaluator.  The caller's candidate is never mutated.
    """
    from skill_packages import to_contract_groups

    def evaluate(groups: list[dict]) -> dict:
        trial = candidate.clone()
        trial.groups = to_contract_groups(groups)
        return evaluation_from_result(evaluator(trial, encounter, budget))
    return evaluate
