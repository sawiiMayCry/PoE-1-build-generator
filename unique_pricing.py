"""One unique-item quote resolver for candidate selection and every report.

Only *unique* equipment, unique jewels and unique flasks are priced.  Rare,
magic and normal items, skill gems and socket/link crafting are intentionally
excluded; they are reported as excluded scope, never as failed quotes.

The resolver is pure: it needs a market snapshot (``services.market_data``
schema, or an old snapshot) and an item identity.  Candidate search and the
final/stage price panels call exactly the same ``resolve_unique`` function so
a budgeted candidate and the identical equipped item always get the same
quote.  An item is *unquoted* (price ``None``) with a reason when no listing
matches its variant, relevant link count and corruption state; a missing
quote is never turned into zero and a different variant or an unlinked price
is never substituted for a required six-link.
"""
from __future__ import annotations

import re
import time
from typing import Iterable

from loadout_summary import parse_item

PRICE_SCHEMA_VERSION = 2
LINKABLE_SLOTS = ("Weapon 1", "Weapon 2", "Body Armour", "Helmet", "Gloves", "Boots")
# poe.ninja only prices separate 5- and 6-link listings; shorter links share
# the unlinked listing.
LINK_PRICED_COUNTS = (5, 6)
STALE_AFTER_SECONDS = 24 * 3600
NO_MATCH = "No quote matches the equipped unique variant and links"
ALWAYS_EXCLUDED = ("skill gems", "rare, magic and normal items", "socket, link and colour crafting",
                   "flask enchants and rare-item crafting")
NINJA_CATEGORY = {"unique_equipment": ("UniqueWeapon", "UniqueArmour", "UniqueAccessory"),
                  "unique_jewel": ("UniqueJewel",), "unique_flask": ("UniqueFlask",)}
_VARIANT_ALIASES = {"phys": "physical", "ele": "elemental", "lightning": "lightning", "lite": "lightning",
                    "chaos res": "chaos resistance", "es": "energy shield"}


def normalize_name(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").replace("’", "'").replace("‘", "'")).strip().casefold()


def normalize_variant(variant: str | None) -> str | None:
    """Canonical variant label; ``None`` means the ordinary/current version."""
    if variant is None:
        return None
    text = re.sub(r"[^a-z0-9.]+", " ", str(variant).replace("’", "'").casefold()).strip()
    if text in {"", "current", "none"}:
        return None
    tokens = [_VARIANT_ALIASES.get(token, token) for token in text.split()]
    return " ".join(tokens)


def _variant_key(variant: str | None):
    text = normalize_variant(variant)
    return None if text is None else frozenset(text.split())


def category_of(entry: dict) -> str:
    """Pricing category of one equipped entry (``excluded:*`` for unpriced scope)."""
    slot = entry.get("slot", "") or ""
    rarity = (entry.get("rarity") or "").lower()
    is_jewel = slot.startswith("Jewel") or (entry.get("base") or "").endswith("Jewel")
    is_flask = slot.startswith("Flask")
    if rarity != "unique" and rarity != "relic":
        kind = "jewel" if is_jewel else "flask" if is_flask else "equipment"
        label = {"rare": "rare", "magic": "magic", "normal": "normal"}.get(rarity, rarity or "unidentified")
        return f"excluded:{label} {kind}"
    return "unique_jewel" if is_jewel else "unique_flask" if is_flask else "unique_equipment"


def is_priced_category(category: str) -> bool:
    return not category.startswith("excluded:")


def entry_from_text(text: str, slot: str, *, linked: int | None = None) -> dict:
    """Identity of a PoB item text equipped in ``slot``."""
    item = parse_item(text)
    fixed = bool(re.search(r"(?mi)^(?:Has Fixed|Sockets are Linked)", text or ""))
    return {"slot": slot, "name": item["name"], "base": item["base"], "rarity": item["rarity"],
            "variant": item["variant"], "corrupted": item["corrupted"],
            "links": linked if linked is not None else (item["linked"] or None),
            "fixedSockets": fixed}


def _listing_corrupted(listing: dict):
    value = listing.get("corrupted")
    return None if value is None else bool(value)


def _market_listings(market: dict, name: str) -> list[dict]:
    listings = market.get("listings") or {}
    direct = listings.get(name)
    if direct is not None:
        return list(direct)
    wanted = normalize_name(name)
    for key, value in listings.items():
        if normalize_name(key) == wanted:
            return list(value)
    return []


def _category_error(market: dict, category: str) -> str | None:
    wanted = NINJA_CATEGORY.get(category, ())
    failures = (market.get("categoryErrors") or {})
    for kind in wanted:
        if kind in failures:
            return f"{kind}: {failures[kind]}"
    for message in market.get("errors") or []:
        if any(str(message).startswith(kind) for kind in wanted):
            return str(message)
    return None


def _describe_variants(listings: Iterable[dict]) -> str:
    seen = sorted({str(entry.get("variant") or "Current") for entry in listings})
    return ", ".join(seen[:5]) + ("..." if len(seen) > 5 else "")


def resolve_unique(entry: dict, market: dict, *, expected_league: str | None = None,
                   now: float | None = None) -> dict:
    """Quote one unique.  Returns a record with ``status`` quoted/unquoted.

    ``entry`` keys: slot, name, base, rarity, variant, links (largest linked
    group), corrupted, fixedSockets.  Non-unique entries return
    ``status="excluded"``; they are never quoted or counted as missing.
    """
    category = category_of(entry)
    base = {"slot": entry.get("slot"), "name": entry.get("name"), "base": entry.get("base"),
            "category": category, "variant": entry.get("variant"), "links": entry.get("links"),
            "corrupted": bool(entry.get("corrupted")), "chaos": None, "kind": "estimated"}
    if not is_priced_category(category):
        return {**base, "status": "excluded", "reason": "Only unique items are priced"}
    slot = entry.get("slot", "") or ""
    linkable = (slot in LINKABLE_SLOTS and not entry.get("fixedSockets"))
    required_links = (entry.get("links") or 0) if linkable else 0
    result = {**base, "status": "unquoted", "requiredLinks": required_links or None,
              "excludedCosts": [], "league": market.get("league"), "source": market.get("source"),
              "updated": market.get("updated")}
    if market.get("updated"):
        age = max(0, (now if now is not None else time.time()) - float(market["updated"]))
        result["ageSeconds"] = int(age)
        result["stale"] = age > STALE_AFTER_SECONDS
    if expected_league and market.get("league") and normalize_name(expected_league) != normalize_name(market["league"]):
        result["reason"] = f"Quote league {market['league']} differs from the requested league {expected_league}"
        return result
    listings = [row for row in _market_listings(market, entry.get("name") or "")
                if row.get("chaos") and float(row["chaos"]) > 0]
    if not listings:
        old = (market.get("prices") or {}).get(entry.get("name"))
        error = _category_error(market, category)
        if old and not market.get("listings"):
            result["reason"] = "Snapshot has no variant/link-specific unique quote"
        elif error:
            result["reason"] = f"Market category failed ({error}); no quote for this unique"
        else:
            result["reason"] = "Unique has no current-league quote"
        return result
    wanted_base = normalize_name(entry.get("base"))
    if wanted_base and any(row.get("baseType") for row in listings):
        based = [row for row in listings if not row.get("baseType")
                 or normalize_name(row["baseType"]) == wanted_base]
        if not based:
            bases = sorted({str(row["baseType"]) for row in listings if row.get("baseType")})
            result["reason"] = NO_MATCH
            result["detail"] = (f"No quote matches the equipped base '{entry.get('base')}' "
                                f"(quoted bases: {', '.join(bases[:4])})")
            return result
        listings = based
    wanted_variant = _variant_key(entry.get("variant"))
    candidates = [row for row in listings if _variant_key(row.get("variant")) == wanted_variant]
    if not candidates:
        wanted = entry.get("variant") or "Current"
        result["reason"] = NO_MATCH
        result["detail"] = (f"No quote matches the equipped variant '{wanted}' "
                            f"(quoted variants: {_describe_variants(listings)})")
        return result
    corrupted = bool(entry.get("corrupted"))
    exact_state = [row for row in candidates if _listing_corrupted(row) in (None, corrupted)]
    if not exact_state:
        result["reason"] = NO_MATCH
        result["detail"] = ("No quote matches the corruption state "
                            f"({'corrupted' if corrupted else 'uncorrupted'} item)")
        return result
    candidates = exact_state
    link_priced = any((row.get("links") or 0) in LINK_PRICED_COUNTS for row in listings)
    match_kind = "variant"
    if linkable and link_priced:
        if required_links >= 5:
            candidates = [row for row in candidates if (row.get("links") or 0) == required_links]
            match_kind = f"variant+{required_links}-link"
            if not candidates:
                unlinked = [row for row in exact_state if not (row.get("links") or 0)]
                if unlinked:
                    result["referenceUnlinkedChaos"] = round(min(float(row["chaos"]) for row in unlinked), 1)
                result["reason"] = NO_MATCH
                result["detail"] = (f"No {required_links}-link quote for this link-priced unique"
                                    + ("; an unlinked price exists but excludes the required links"
                                       if unlinked else ""))
                return result
        else:
            candidates = [row for row in candidates if (row.get("links") or 0) < 5]
            if not candidates:
                result["reason"] = NO_MATCH
                result["detail"] = "Only 5/6-link quotes exist; no quote for the equipped link count"
                return result
            result["excludedCosts"].append("socket and link crafting")
    elif linkable and required_links >= 5:
        # A category that does not carry link data cannot confirm a six-link price.
        if any(row.get("links") for row in listings):
            candidates = [row for row in candidates if (row.get("links") or 0) == required_links]
        if not candidates:
            result["reason"] = NO_MATCH
            result["detail"] = f"No {required_links}-link quote for this unique"
            return result
        result["excludedCosts"].append(f"{required_links}-link crafting not itemized in the quote")
        match_kind = "variant+generic-links"
    elif linkable and required_links >= 2:
        result["excludedCosts"].append("socket and link crafting")
    best = min(candidates, key=lambda row: (float(row["chaos"]), str(row.get("detailsId") or "")))
    counts = [row.get("listingCount") or row.get("count") for row in candidates]
    known = [int(value) for value in counts if isinstance(value, (int, float))]
    best_count = best.get("listingCount") or best.get("count")
    confidence = ("unknown" if not known else "low" if (best_count or 0) < 5 else
                  "medium" if (best_count or 0) < 20 else "high")
    result.update({"status": "quoted", "chaos": round(float(best["chaos"]), 1), "matchKind": match_kind,
                   "variant": best.get("variant") if normalize_variant(best.get("variant")) else None,
                   "confidence": confidence, "listingCount": best_count,
                   "detailsId": best.get("detailsId"), "ninjaCategory": best.get("category"),
                   "reason": None})
    return result


def resolve_unique_text(text: str, slot: str, market: dict, *, linked: int | None = None, **options) -> dict:
    """Quote an item given as PoB item text (used for candidate search)."""
    return resolve_unique(entry_from_text(text, slot, linked=linked), market, **options)


def unique_price(text: str, slot: str, market: dict, *, linked: int | None = None, **options) -> float | None:
    """Candidate-selection price: identical to the displayed quote, or ``None``."""
    return resolve_unique_text(text, slot, market, linked=linked, **options).get("chaos")


def unique_package_price(entries: Iterable[dict], market: dict, **options) -> tuple[float | None, list[dict]]:
    """Subtotal of equipped uniques, or ``None`` when any unique is unquoted."""
    quotes = [resolve_unique(entry, market, **options) for entry in dedupe_by_slot(entries)]
    quotes = [quote for quote in quotes if quote["status"] != "excluded"]
    if any(quote["status"] != "quoted" for quote in quotes):
        return None, quotes
    return round(sum(quote["chaos"] for quote in quotes), 1), quotes


def dedupe_by_slot(entries: Iterable[dict]) -> list[dict]:
    """The equipped set: a later entry replaces an earlier one in the same slot."""
    by_slot: dict[str, dict] = {}
    for index, entry in enumerate(entries):
        by_slot[entry.get("slot") or f"#{index}"] = entry
    return list(by_slot.values())


def price_unique_equipment(gear: Iterable[dict], market: dict, budget_chaos: float | None = None, *,
                           scope: str = "equipped_uniques", label: str | None = None,
                           expected_league: str | None = None, now: float | None = None) -> dict:
    """Unique-only price report for a final equipped set (Endgame or a stage).

    ``gear`` is the *resulting* equipped set including jewels and flasks, so
    replaced items are never charged.  The old keys (``priced``, ``unknown``,
    ``pricedSubtotalChaos``, ``complete``, ``budgetStatus`` ...) are kept for
    older consumers; ``unknown`` now lists only unquoted *uniques*.
    """
    equipped = dedupe_by_slot(gear)
    quotes = [resolve_unique(entry, market, expected_league=expected_league, now=now) for entry in equipped]
    priced = [quote for quote in quotes if quote["status"] == "quoted"]
    unknown = [quote for quote in quotes if quote["status"] == "unquoted"]
    excluded: dict[str, list[str]] = {}
    for entry, quote in zip(equipped, quotes):
        if quote["status"] == "excluded":
            excluded.setdefault(quote["category"].split(":", 1)[1], []).append(entry.get("slot") or "")
    subtotal = round(sum(quote["chaos"] for quote in priced), 1)
    divine = float(market.get("divineChaos") or 0) or None
    unique_count = len(priced) + len(unknown)
    source_errors = list(market.get("errors") or [])
    if unique_count == 0:
        status = "No unique items equipped"
    elif unknown:
        status = f"{len(priced)} of {unique_count} unique items quoted; {len(unknown)} unquoted"
    else:
        status = f"All {unique_count} unique items quoted"
    if budget_chaos is None:
        budget_status = "No budget stated"
    elif unique_count == 0:
        budget_status = "No unique items to price; rares, gems and crafting are outside the unique subtotal"
    elif subtotal > budget_chaos:
        budget_status = "unique subtotal exceeds budget"
    elif unknown:
        budget_status = "unverified: unquoted uniques remain (quoted unique subtotal is within budget)"
    else:
        budget_status = "unique subtotal within budget"
    return {
        "schema": PRICE_SCHEMA_VERSION, "scope": scope, "label": label,
        "uniqueCount": unique_count, "quotedUniqueCount": len(priced), "unquotedUniqueCount": len(unknown),
        "noUniques": unique_count == 0, "coverageStatus": status,
        "uniqueSubtotalChaos": subtotal,
        "uniqueSubtotalDivine": round(subtotal / divine, 2) if divine else None,
        "priced": [{**quote, "kind": "estimated"} for quote in priced],
        "unknown": [{"slot": quote["slot"], "name": quote["name"], "reason": quote["reason"],
                     "detail": quote.get("detail"), "category": quote["category"],
                     "variant": quote.get("variant"), "links": quote.get("links"),
                     "referenceUnlinkedChaos": quote.get("referenceUnlinkedChaos")} for quote in unknown],
        "excluded": {"categories": [{"category": category, "count": len(slots), "slots": slots}
                                    for category, slots in sorted(excluded.items())],
                     "alwaysExcluded": list(ALWAYS_EXCLUDED)},
        "excludedNote": ("Only unique equipment, jewels and flasks are priced. Rare items, gems and "
                         "link/socket crafting are not priced and are not missing quotes."),
        "fullBuildBudget": "unverified: rares, gems and crafting are not priced" if budget_chaos is not None else None,
        "budgetChaos": budget_chaos, "budgetCheck": "unique subtotal versus budget",
        "budgetStatus": budget_status,
        # Legacy keys.
        "pricedSubtotalChaos": subtotal, "pricedSubtotalDivine": round(subtotal / divine, 2) if divine else None,
        "complete": not unknown, "source": market.get("source"), "updated": market.get("updated"),
        "league": market.get("league"), "divineChaos": market.get("divineChaos"),
        "sourceErrors": source_errors,
    }


def upgrade_legacy_quote(quote: dict | None) -> dict | None:
    """Label an older saved quote so excluded rares are not shown as missing quotes.

    Old results listed every unpriced slot (including rares) as ``unknown``.
    Without item rarity we can only separate them by the old reason text.
    """
    if not isinstance(quote, dict) or quote.get("schema", 0) >= PRICE_SCHEMA_VERSION:
        return quote
    unknown = quote.get("unknown") or []
    rare = [row for row in unknown if "modifier-aware" in str(row.get("reason", ""))
            or "Rare/magic" in str(row.get("reason", ""))]
    uniques = [row for row in unknown if row not in rare]
    upgraded = dict(quote)
    upgraded.update({
        "schema": 1, "legacy": True, "scope": quote.get("scope", "legacy_all_slots"),
        "unknown": uniques, "unquotedUniqueCount": len(uniques),
        "quotedUniqueCount": len(quote.get("priced") or []),
        "uniqueCount": len(uniques) + len(quote.get("priced") or []),
        "excluded": {"categories": [{"category": "rare equipment", "count": len(rare),
                                      "slots": [row.get("slot") for row in rare]}] if rare else [],
                     "alwaysExcluded": list(ALWAYS_EXCLUDED)},
        "uniqueSubtotalChaos": quote.get("pricedSubtotalChaos"),
        "uniqueSubtotalDivine": quote.get("pricedSubtotalDivine"),
        "coverageStatus": "Saved with an earlier price schema; unique-only coverage is inferred",
    })
    return upgraded
