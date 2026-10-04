"""Generic, mechanic-derived skill classification for any Witch main skill.

Nothing here is keyed by a skill name except the explicit, optional override file
``data/mechanic_overrides.json`` and two small fallback tables used only when a spec was built
without gem tags (hand-written test specs, legacy callers).  Everything else is derived from

* the installed PoB gem tags (``minion``, ``duration``, ``brand``, ``totem``, ``channelling`` ...), and
* PoB's own calculated outputs (``ActiveMinionLimit``, ``SummonedMinionsPerCast``, ``Duration``,
  ``Speed``, ``ManaCost`` ...).

A skill can fail a mechanic check only for a specific demonstrated defect (zero calculated damage,
an unsustainable minion population, a failed resource check); lacking a tested profile is never a defect.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

OVERRIDES_PATH = Path(__file__).resolve().parent / "data" / "mechanic_overrides.json"
DEFAULT_RESERVE = 0.15

# Used only when ``spec["skillTags"]`` is absent (specs not produced by normalize_intent).
FALLBACK_MINION_MODEL = {"Raise Zombie": "permanent", "Raise Spectre": "permanent",
                         "Summon Raging Spirit": "temporary", "Animate Weapon": "temporary",
                         "Summon Skeletons": "temporary"}
# Skills whose damage PoB reports as damage over time although the gem tag set does not say so.
DOT_SKILLS_WITHOUT_TAG = {"Vortex", "Cold Snap", "Bane", "Bane of Condemnation", "Essence Drain",
                          "Contagion", "Soulrend"}
DELIVERY_TAG_ORDER = ("totem", "trap", "mine", "brand")


def skill_tags(spec: dict) -> dict | None:
    tags = spec.get("skillTags")
    if tags is None:
        return None
    return {tag: True for tag in tags} if isinstance(tags, (list, tuple, set)) else dict(tags)


def minion_model(spec: dict) -> str | None:
    """'permanent' (minion without a lifetime), 'temporary' (minion with a duration) or None.

    A minion skill whose gem has the ``duration`` tag summons minions that expire, so their population
    is a rate x lifetime balance capped by the PoB limit; without it the minions stay until killed.
    """
    explicit = spec.get("minionModel")
    if explicit in {"permanent", "temporary"}:
        return explicit
    tags = skill_tags(spec)
    if tags is not None:
        if not tags.get("minion"):
            return None
        return "temporary" if tags.get("duration") else "permanent"
    return FALLBACK_MINION_MODEL.get(spec.get("skill"))


def retarget(spec: dict, skill: str, tags: dict) -> dict:
    """Copy of ``spec`` for a different skill (e.g. a leveling-stage skill) with its own derived mechanics."""
    result = {**spec, "skill": skill, "skillTags": sorted(tag for tag, on in tags.items() if on)}
    result["minionModel"] = None
    result["minionModel"] = minion_model(result)
    result.pop("_populationSustainable", None)
    return result


def delivery(spec: dict) -> str:
    """Mechanic tier key: minion_permanent, minion_temporary, attack_minion, totem, trap, mine, brand,
    ignite, dot, attack or spell."""
    model = minion_model(spec)
    tags = skill_tags(spec) or {}
    if model:
        return "attack_minion" if tags.get("attack") else f"minion_{model}"
    for tag in DELIVERY_TAG_ORDER:
        if tags.get(tag):
            return tag
    archetype = spec.get("archetype")
    if archetype in {"ignite", "dot", "attack"}:
        return archetype
    return "spell"


def is_dot_skill(name: str, tags: dict) -> bool:
    return bool(tags.get("dot")) or name in DOT_SKILLS_WITHOUT_TAG


@lru_cache(maxsize=1)
def load_overrides() -> tuple:
    try:
        raw = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8")).get("overrides", [])
    except (OSError, ValueError):
        raw = []
    return tuple(raw)


def find_override(spec: dict) -> dict | None:
    for entry in load_overrides():
        if entry.get("skill") not in {spec.get("skill"), "*"}:
            continue
        if entry.get("ascendancy") not in {spec.get("ascendancy"), "*"}:
            continue
        if entry.get("archetype") and entry["archetype"] != spec.get("archetype"):
            continue
        return dict(entry["profile"], override=True, source="override")
    return None


def scaling_axes(spec: dict) -> list[str]:
    kind = delivery(spec)
    damage = spec.get("damageType", "physical")
    base = {"minion_permanent": ["minion damage", "minion life", "minion count"],
            "minion_temporary": ["minion damage", "summon rate", "minion lifetime", "minion limit"],
            "attack_minion": ["minion damage", "attack speed of minions", "minion limit"],
            "attack": ["attack speed", "weapon damage", "accuracy"],
            "ignite": ["ignite damage", "ignite duration", "hit damage that ignites"],
            "dot": ["damage over time multiplier", "skill duration"],
            "totem": ["totem count", "spell damage", "cast speed"],
            "trap": ["trap throwing speed", "area damage", "trap count"],
            "mine": ["mine laying speed", "area damage", "mine count"],
            "brand": ["brand count", "spell damage", "activation frequency"],
            "spell": ["spell damage", "cast speed", "critical strike"]}[kind]
    return [f"{damage} damage", *base]


def derive_profile(spec: dict) -> dict:
    """Mechanic profile derived from tags/archetype; never reports 'untested'."""
    kind = delivery(spec)
    model = minion_model(spec)
    channelled = bool(spec.get("channelled"))
    if model == "temporary":
        resource = "mana/life per cast; only the casts needed to refresh the minion population are paid"
        summon = "population = min(PoB minion limit, cast rate x minions per cast x duration)"
        interactions = ["PoB minion limit", "minions per cast", "minion lifetime", "resource-limited refresh rate"]
    elif model == "permanent":
        resource = "mana per summon (one-off; permanent minions are not re-summoned continuously)"
        summon = "permanent minion count = PoB-reported minion limit"
        interactions = ["PoB minion limit"]
    else:
        resource = ("mana per channel cycle (paid about once a second)" if channelled
                    else "mana/life per use at the calculated use rate")
        summon = "none"
        interactions = ["sustained channeling"] if channelled else []
    source = {"minion_permanent": "permanent_minion", "minion_temporary": "temporary_minion",
              "attack_minion": "attack_minion"}.get(kind, f"player_{kind}")
    hit = {"ignite": "ignite", "dot": "damage over time"}.get(
        kind, "minion hit" if model else f"{spec.get('damageType', 'physical')} hit")
    return {"name": f"derived_{kind}", "derived": True, "override": False, "source": "derived",
            "delivery": kind, "damageSource": source, "hitOrAilment": hit,
            "conversion": "not modeled explicitly", "resourceUse": resource, "summonModel": summon,
            "scaling": scaling_axes(spec), "compatibleAscendancies": [], "compatibleUtilityChoices": [],
            "requiredNodes": [], "requiredInteractions": interactions}


def mechanic_profile(spec: dict) -> dict:
    """Optional tested override when one exists, otherwise the derived profile (with delivery info)."""
    profile = find_override(spec)
    derived = derive_profile(spec)
    if profile is None:
        return derived
    profile = {**derived, **profile}
    profile["delivery"] = derived["delivery"]
    profile["scaling"] = derived["scaling"]
    return profile


def _reserve(spec: dict) -> float:
    return min(0.5, max(0.0, float(spec.get("resourceReserveFraction", DEFAULT_RESERVE))))


def minion_population(output: dict, spec: dict) -> tuple[int, bool] | None:
    """(population, sustainable) from PoB outputs, or None when the skill summons no minions.

    permanent: the PoB-reported limit (``ActiveMinionLimit``), always sustained.
    temporary: arrival rate x minions per cast x lifetime, capped by the limit.  The arrival rate is
    the smaller of the cast rate and what mana/life recovery (after the utility reserve) can pay.
    Skills PoB reports as multi-minion summons (``SummonedMinionsPerCast`` > 1) are sustainable only
    if the whole limit can be kept up; single-minion summons need at least one live minion.
    """
    model = minion_model(spec)
    if model is None:
        return None
    reported_limit = int(output.get("ActiveMinionLimit", 0) or 0)
    fallback = max(1, int(spec.get("minionCount", 1) or 1))
    if model == "permanent":
        return max(1, reported_limit or fallback), True
    limit = max(1, reported_limit or fallback)
    reported_per_cast = int(output.get("SummonedMinionsPerCast", 0) or 0)
    per_cast = max(1, reported_per_cast)
    duration = max(0.0, float(output.get("Duration", 0) or 0))
    rates = [max(0.0, float(output.get("Speed", 0) or 0))]
    for cost_key, regen_key in (("ManaCost", "ManaRegen"), ("LifeCost", "LifeRegenRecovery")):
        cost = max(0.0, float(output.get(cost_key, 0) or 0))
        if cost > 0:
            regen = max(0.0, float(output.get(regen_key, 0) or 0)) * (1 - _reserve(spec))
            rates.append(regen / cost)
    alive = max(0.0, min(rates)) * per_cast * duration
    sustainable = alive >= (limit if reported_per_cast > 1 else 1)
    return max(1, min(limit, int(alive))), sustainable
