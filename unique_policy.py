"""Unique-item budget policy.

The STANDARD budget for the whole Endgame build, when the user states none, is five divine orbs of
unique gear (unique weapons, armour, jewellery, belts, unique jewels and unique flasks; rares and gems
are not counted).  The divine is converted to chaos with the Divine->Chaos rate of the market data in
use (live poe.ninja or the saved snapshot); only if no rate is available a documented constant is used.

An explicit user budget replaces this one (every quote must then be known and the package must fit),
and explicitly requested uniques are always equipped.  Inside the budget there is no price penalty:
the search measures the marginal gain of every candidate in PoB and spends the budget by gain per
cost, so the budget may be fully used but is never exceeded.

Unknown prices are handled conservatively without blocking: a unique with no quote is assumed to cost
``UNPRICED_ASSUMED_SHARE`` of the budget (counted as spent), quoted candidates are preferred over
unquoted ones with the same gain, at most ``MAX_UNPRICED_UNIQUES`` unquoted uniques are equipped, and
each one is flagged in the result.
"""
from __future__ import annotations

DEFAULT_BUDGET_DIVINE = 5.0
# Used only when the market data carries no Divine->Chaos rate (a rough league-mid value; documented).
FALLBACK_DIVINE_CHAOS = 150.0
UNPRICED_ASSUMED_SHARE = 0.10      # an unquoted unique is assumed to cost this share of the budget
UNPRICED_RATIO_PENALTY = 1.5       # gain-per-cost ranking multiplies the assumed cost of unquoted items
MAX_UNPRICED_UNIQUES = 2
MAPPING_BUDGET_SHARE = 0.25        # a character starting maps owns a proportional (cheaper) subset
MIN_UNIQUE_GAIN = 0.01             # a unique must measurably help (about 1% of the objective); no padding
UNPRICED_MIN_GAIN = 0.05           # an unquoted unique must clear a higher bar (its cost and variant are unknown)
MIN_RANK_COST_CHAOS = 1.0          # cost floor so free items do not get an infinite ratio


def divine_rate(market: dict | None) -> tuple[float, str]:
    """Divine->Chaos rate and where it came from."""
    rate = None
    try:
        rate = float((market or {}).get("divineChaos") or 0) or None
    except (TypeError, ValueError):
        rate = None
    if rate:
        return rate, str((market or {}).get("source") or "market data")
    return FALLBACK_DIVINE_CHAOS, "fallback constant (no market rate available)"


def apply_default_budget(spec: dict, market: dict | None) -> dict:
    """Record the standard budget on the spec (never touches an explicit ``budgetChaos``)."""
    rate, source = divine_rate(market)
    spec["policyBudget"] = {"divine": DEFAULT_BUDGET_DIVINE, "chaos": round(DEFAULT_BUDGET_DIVINE * rate, 1),
                            "divineChaosRate": rate, "rateSource": source}
    return spec


def is_stated(spec: dict) -> bool:
    return spec.get("budgetChaos") is not None


def policy_active(spec: dict) -> bool:
    """The standard budget applies when the user stated none."""
    return not is_stated(spec)


def budget_cap(spec: dict) -> float | None:
    """Total unique budget in chaos (stated, else the standard), or None if neither is known."""
    if is_stated(spec):
        return float(spec["budgetChaos"])
    policy = spec.get("policyBudget")
    return float(policy["chaos"]) if policy else None


def assumed_unpriced(spec: dict) -> float:
    cap = budget_cap(spec)
    return (cap or DEFAULT_BUDGET_DIVINE * FALLBACK_DIVINE_CHAOS) * UNPRICED_ASSUMED_SHARE


def effective_price(price, spec: dict) -> float:
    """Spent cost counted for a unique (assumed share of the budget when it has no quote)."""
    return assumed_unpriced(spec) if price is None else max(0.0, float(price))


def rank_cost(price, spec: dict) -> float:
    cost = effective_price(price, spec) * (UNPRICED_RATIO_PENALTY if price is None else 1.0)
    return max(MIN_RANK_COST_CHAOS, cost)


def gain_per_cost(delta: float, price, spec: dict) -> float:
    return delta / rank_cost(price, spec)


def package_total(spec: dict, prices) -> float:
    return sum(effective_price(price, spec) for price in prices)


def within_budget(spec: dict, current_prices, addition_prices) -> bool:
    """Whether adding ``addition_prices`` to ``current_prices`` keeps the unique package inside the budget."""
    prices = [*current_prices, *addition_prices]
    cap = budget_cap(spec)
    if is_stated(spec):
        return all(price is not None for price in prices) and sum(prices) <= cap + 1e-9
    if cap is None:
        return True
    if sum(price is None for price in prices) > MAX_UNPRICED_UNIQUES:
        return False
    return package_total(spec, prices) <= cap + 1e-9


def min_gain(price) -> float:
    """Smallest objective gain that justifies a unique (higher for an unquoted one)."""
    return UNPRICED_MIN_GAIN if price is None else MIN_UNIQUE_GAIN


def worth_price(spec: dict, delta: float, added_prices, committed_prices=()) -> bool:
    """A measured improvement is worth buying when it clears the gain bar and fits the budget.

    There is no price penalty inside the budget; only unquoted items face a higher gain bar.
    """
    added = list(added_prices)
    need = max([min_gain(price) for price in added] or [MIN_UNIQUE_GAIN])
    return delta >= need and within_budget(spec, committed_prices, added)


def mapping_cap(spec: dict) -> float | None:
    """Unique spend available while starting maps (a proportional share of the total budget)."""
    cap = budget_cap(spec)
    if cap is None:
        return None
    return cap if is_stated(spec) else cap * MAPPING_BUDGET_SHARE


def policy_summary(spec: dict, mapping: bool = False) -> dict:
    """Statement of the policy in force, for the result and the UI."""
    if is_stated(spec):
        return {"mode": "stated budget", "budgetChaos": spec["budgetChaos"],
                "text": f"Unique gear is limited to the stated budget of {spec['budgetChaos']:g} chaos."}
    policy = spec.get("policyBudget") or {}
    cap = budget_cap(spec)
    return {"mode": "standard budget", "budgetDivine": DEFAULT_BUDGET_DIVINE, "budgetChaos": cap,
            "divineChaosRate": policy.get("divineChaosRate"), "rateSource": policy.get("rateSource"),
            "unpricedAssumedChaos": round(assumed_unpriced(spec), 1),
            "mappingBudgetChaos": round(mapping_cap(spec) or 0, 1),
            "text": (f"No budget was stated, so the standard budget of {DEFAULT_BUDGET_DIVINE:g} divine "
                     f"({cap:g} chaos at {policy.get('divineChaosRate', 0):g} chaos/divine, "
                     f"{policy.get('rateSource', 'unknown source')}) applies to all unique gear "
                     f"(weapons, armour, jewellery, unique jewels and flasks; rares and gems are not counted). "
                     f"Uniques are bought by measured PoB gain per cost until the budget is used. "
                     f"A unique without a quote is assumed to cost {assumed_unpriced(spec):g} chaos and is "
                     f"flagged. Mapping gear uses a {MAPPING_BUDGET_SHARE * 100:.0f}% share. "
                     f"State a budget to override.")}


def budget_report(spec: dict, rows: list[dict]) -> dict:
    """Budget, spent, remaining and per-unique prices for the result.

    ``rows`` are ``{"slot", "name", "chaos" (None if unquoted), "requested"}`` entries of the equipped
    uniques (including jewels and flasks).
    """
    cap = budget_cap(spec)
    items = []
    spent = 0.0
    for row in rows:
        price = row.get("chaos")
        cost = effective_price(price, spec)
        spent += cost
        items.append({"slot": row["slot"], "name": row["name"], "chaos": price,
                      "countedChaos": round(cost, 1), "assumed": price is None,
                      "requested": bool(row.get("requested"))})
    policy = spec.get("policyBudget") or {}
    return {"mode": "stated" if is_stated(spec) else "standard",
            "budgetChaos": cap, "budgetDivine": (round(cap / policy["divineChaosRate"], 2)
                                                 if cap and policy.get("divineChaosRate") else None),
            "divineChaosRate": policy.get("divineChaosRate"), "rateSource": policy.get("rateSource"),
            "spentChaos": round(spent, 1), "remainingChaos": None if cap is None else round(cap - spent, 1),
            "withinBudget": cap is None or spent <= cap + 1e-6,
            "requestedExceedBudget": bool(cap is not None and sum(
                item["countedChaos"] for item in items if item["requested"]) > cap + 1e-6),
            "unpricedAssumed": [item["name"] for item in items if item["assumed"]],
            "uniques": items}
