"""Shared contracts between the build engine (Agent 1) and skills/pricing/UI (Agent 2).

Single writer: Agent 1. Request changes through the handoff process.
Everything here is plain data + Protocol signatures; no heavy imports so every
module can import it. Dataclasses expose to_dict()/from_dict() so candidates can
cross JSON boundaries and be cloned without sharing mutable state.

Compatibility: the legacy shapes still in use are
  spec["utility"]  -> {gem_name: slot}            (see utility_to_groups)
  supports         -> [support gem names] on the main Body Armour group
Use `groups_from_legacy` / `legacy_from_groups` while callers migrate.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol

CONTRACT_VERSION = 1

# ---- skill groups -----------------------------------------------------------

ROLES = ("main", "movement", "reservation", "defense", "curse", "herald", "guard",
         "trigger", "minion_support", "offering", "buff", "other")
DELIVERY = ("socketed", "manual", "trigger", "item_granted", "aura", "persistent")


@dataclass
class GemRecord:
    """One gem *instance*. Never key level/quality by name: the same support can
    appear in several groups with different settings."""
    name: str
    support: bool = False
    level: int = 20
    quality: int = 0
    enabled: bool = True
    count: int = 1                  # minion count etc. (PoB `count`)
    instance_id: str = ""           # stable, unique within the candidate

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "GemRecord":
        return cls(**{k: raw[k] for k in cls.__dataclass_fields__ if k in raw})


@dataclass
class SkillGroup:
    """A PoB socket group: gems that are physically linked in `slot`."""
    id: str                         # stable, e.g. "main", "movement-1"
    role: str                       # one of ROLES
    slot: str                       # "Body Armour", "Helmet", ... ; "" for item-granted
    link_group: int = 0             # index of the linked sockets within the item
    gems: list[GemRecord] = field(default_factory=list)  # actives first, then supports
    main_active: str = ""           # selected active skill (name) for this group
    delivery: str = "socketed"      # one of DELIVERY
    conditions: list[str] = field(default_factory=list)  # e.g. "power_charges", "curse_applied"
    include_in_full_dps: bool = False
    enabled: bool = True
    reason: str = ""                # why this package exists / why it is useful
    source: str = ""                # item name for item_granted

    @property
    def actives(self) -> list[GemRecord]:
        return [gem for gem in self.gems if not gem.support]

    @property
    def supports(self) -> list[GemRecord]:
        return [gem for gem in self.gems if gem.support]

    def socket_count(self) -> int:
        return len(self.gems) if self.delivery != "item_granted" else 0

    def to_dict(self) -> dict:
        raw = asdict(self)
        return raw

    @classmethod
    def from_dict(cls, raw: dict) -> "SkillGroup":
        data = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw and k != "gems"}
        data["gems"] = [GemRecord.from_dict(g) for g in raw.get("gems", [])]
        return cls(**data)


def groups_from_legacy(spec: dict, supports: list[str], is_support=lambda name: False) -> list[SkillGroup]:
    """Adapter: legacy main skill + supports + utility dict -> structured groups."""
    levels = spec.get("gemLevels", {})
    main = SkillGroup(id="main", role="main", slot="Body Armour", main_active=spec["skill"],
                      include_in_full_dps=True, reason="main damage link")
    main.gems.append(GemRecord(spec["skill"], False, levels.get(spec["skill"], 20),
                               count=spec.get("minionCount", 1), instance_id="main:0"))
    for index, name in enumerate(supports, 1):
        main.gems.append(GemRecord(name, True, levels.get(name, 20), instance_id=f"main:{index}"))
    groups = [main]
    for index, (name, slot) in enumerate(spec.get("utility", {}).items(), 1):
        groups.append(SkillGroup(id=f"utility-{index}", role="other", slot=slot,
                                 gems=[GemRecord(name, False, levels.get(name, 20),
                                                 instance_id=f"utility-{index}:0")],
                                 main_active=name))
    return groups


def legacy_from_groups(groups: list[SkillGroup]) -> tuple[list[str], dict[str, str]]:
    main = next((g for g in groups if g.role == "main"), None)
    supports = [g.name for g in main.supports] if main else []
    utility = {}
    for group in groups:
        if group.role != "main" and group.actives:
            utility[group.actives[0].name] = group.slot
    return supports, utility


# ---- complete candidate -----------------------------------------------------

@dataclass
class Candidate:
    """Complete build state. Evaluate/clone this, never mutate a shared spec."""
    spec: dict                                  # intent: skill, level, ascendancy, focus, budget...
    nodes: set[str] = field(default_factory=set)            # allocated passive node ids
    masteries: dict[str, int] = field(default_factory=dict)  # node id -> effect id
    items: list[Any] = field(default_factory=list)           # RareItem (rares, per slot)
    uniques: dict[str, str] = field(default_factory=dict)    # slot -> PoB item text
    jewels: dict[str, Any] = field(default_factory=dict)     # socket node id -> RareItem | text
    groups: list[SkillGroup] = field(default_factory=list)   # structured skill groups
    damage_mechanism: str = ""                  # e.g. "cold_hit", "minion", "ignite"
    defense_model: str = "life"                 # "life" | "hybrid" | "ci" | "low_life"
    encounter: dict = field(default_factory=dict)            # see Encounter below
    resource_plan: dict = field(default_factory=dict)        # reservation/cost/regen assumptions
    mechanic_dependencies: list[str] = field(default_factory=list)
    quotes: dict[str, dict] = field(default_factory=dict)    # slot/key -> UniqueQuote dict
    stage: str = "Endgame"
    calculation: dict | None = None             # last PoB calc (output of evaluate)
    checks: dict | None = None                  # last EvaluationResult.checks

    def clone(self) -> "Candidate":
        """Deep copy of everything mutable so alternatives cannot contaminate each other."""
        return Candidate(
            spec=copy.deepcopy(self.spec), nodes=set(self.nodes), masteries=dict(self.masteries),
            items=[copy.copy(i) for i in self.items], uniques=dict(self.uniques),
            jewels=dict(self.jewels), groups=copy.deepcopy(self.groups),
            damage_mechanism=self.damage_mechanism, defense_model=self.defense_model,
            encounter=copy.deepcopy(self.encounter), resource_plan=copy.deepcopy(self.resource_plan),
            mechanic_dependencies=list(self.mechanic_dependencies),
            quotes=copy.deepcopy(self.quotes), stage=self.stage,
            calculation=copy.deepcopy(self.calculation), checks=copy.deepcopy(self.checks))

    def signature(self) -> tuple:
        """Mechanical identity for caching/diversity. Includes skill/utility choices."""
        group_sig = tuple((g.id, g.role, g.slot, g.link_group, g.delivery, g.enabled, g.main_active,
                           tuple((x.name, x.support, x.level, x.quality, x.enabled, x.count) for x in g.gems))
                          for g in self.groups)
        return (tuple(sorted(self.nodes, key=int)), tuple(sorted(self.masteries.items())),
                tuple(sorted(self.uniques.items())), tuple(sorted((k, str(v)) for k, v in self.jewels.items())),
                tuple((getattr(i, "slot", ""), getattr(i, "signature", lambda: id(i))()
                       if callable(getattr(i, "signature", None)) else str(i)) for i in self.items),
                group_sig, self.defense_model)


Encounter = dict  # keys: enemyLevel, mode ("mapping"|"boss"), conditions: list[str], bossName

# ---- evaluation -------------------------------------------------------------

@dataclass
class Check:
    id: str
    ok: bool
    severity: str = "error"         # "error" | "warning" | "info"
    message: str = ""
    unknown: bool = False           # data missing: neither safe nor failed


@dataclass
class EvaluationResult:
    """Output of evaluate(candidate, encounter). Dimensions are kept separate."""
    calculation: dict               # PoB outputs (FullDPS, Life, ES, resources, charges...)
    legality: list[Check] = field(default_factory=list)       # sockets, links, reqs, points, items
    mechanics: list[Check] = field(default_factory=list)      # defense model, sustain, conditions
    completeness: list[Check] = field(default_factory=list)   # six-link, role packages, jewels
    resource_deficits: dict[str, float] = field(default_factory=dict)  # e.g. {"mana_per_sec": 3.1}
    score: float = 0.0
    metrics: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    cost: int = 1                   # SearchBudget units consumed
    cached: bool = False

    @property
    def feasible(self) -> bool:
        return all(c.ok or c.unknown or c.severity != "error"
                   for c in self.legality + self.mechanics + self.completeness)

    def failures(self) -> list[Check]:
        return [c for c in self.legality + self.mechanics + self.completeness
                if not c.ok and not c.unknown and c.severity == "error"]


class Evaluator(Protocol):
    """Agent 1 implements. Agent 2's planners call it; every call must consume SearchBudget."""
    def __call__(self, candidate: Candidate, encounter: Encounter,
                 budget: "SearchBudget | None" = None) -> EvaluationResult: ...


class PlanAlternatives(Protocol):
    """Agent 2 implements (skill_planner.py). Returns alternatives, including repairable
    six-links (is_complete False with `repair` hints) rather than truncating to fit mana."""
    def __call__(self, candidate: Candidate, evaluate: Evaluator,
                 budget: "SearchBudget") -> list["SkillPlan"]: ...


@dataclass
class SkillPlan:
    groups: list[SkillGroup]
    complete: bool = True
    repair: list[str] = field(default_factory=list)  # e.g. "mana", "reservation", "chaos_res"
    notes: list[str] = field(default_factory=list)
    omitted_roles: dict[str, str] = field(default_factory=dict)  # role -> reason


# ---- quote resolver (Agent 2 implements in unique_pricing.py) ---------------

@dataclass
class UniqueQuote:
    name: str
    status: str                     # "quoted" | "unquoted" | "not_applicable"
    price_chaos: float | None = None
    reason: str = ""                # required when unquoted
    base: str = ""
    variant: str = ""
    links: int | None = None
    corrupted: bool | None = None
    category: str = ""
    league: str = ""
    source: str = ""                # "live" | "snapshot"
    fetched_at: str = ""
    confidence: str = ""            # listing count / match status
    match: str = ""                 # "exact" | "variant" | "name_only" | "fuzzy"

    def to_dict(self) -> dict:
        return asdict(self)


class QuoteResolver(Protocol):
    """resolve_unique_quote(item_text_or_def, market, *, slot, links=None, league=None) -> UniqueQuote.
    Same function must be used during budgeted selection and for final display.
    Rares/magic/normal items and gems return status "not_applicable" (never a price)."""
    def __call__(self, item: Any, market: dict, *, slot: str = "", links: int | None = None,
                 league: str | None = None) -> UniqueQuote: ...


def summarize_quotes(quotes: list[UniqueQuote], budget: float | None = None) -> dict:
    """Unique-only pricing summary shape (scope=equipped_uniques)."""
    unique = [q for q in quotes if q.status != "not_applicable"]
    quoted = [q for q in unique if q.status == "quoted"]
    subtotal = round(sum(q.price_chaos or 0 for q in quoted), 2)
    return {"scope": "equipped_uniques", "uniqueCount": len(unique), "quotedCount": len(quoted),
            "unquoted": [{"name": q.name, "reason": q.reason} for q in unique if q.status != "quoted"],
            "uniqueSubtotalChaos": subtotal,
            "excluded": ["rare items", "magic/normal items", "gems", "socket/link acquisition"],
            "withinBudget": None if budget is None or len(quoted) != len(unique) else subtotal <= budget,
            "message": "No unique items equipped" if not unique else ""}


# ---- stage policy (Agent 2 builds stages; Agent 1 evaluates with the policy) -

@dataclass
class StagePolicy:
    stage: str                      # "Campaign 1".. "Mapping", "Endgame"
    level: int
    min_links: int = 6              # required body-armour link size
    defense_model: str = "life"     # CI only when transition is functional
    allow_ci: bool = False
    required_roles: list[str] = field(default_factory=list)
    min_sustain: dict[str, float] = field(default_factory=dict)  # numeric targets (versioned data)


class StageEvaluator(Protocol):
    def __call__(self, candidate: Candidate, policy: StagePolicy) -> EvaluationResult: ...


# ---- search budget ----------------------------------------------------------

@dataclass
class BudgetShare:
    name: str                       # "links","packages","jewels","uniques","repair","verify","tree"
    reserved: int
    used: int = 0
    skipped_reason: str = ""

# The concrete counter is passive_search.SearchBudget (limit/used/exhausted/reserve_blocked,
# claim(count, reserve)). `shares` (name -> BudgetShare) is attached by real_generator and
# exposed through SearchBudget.shares; planners should call
# budget.claim(n, reserve=<reserve for later phases>) and record cost in EvaluationResult.cost.
from passive_search import SearchBudget  # noqa: E402  (re-export for planners)


# ---- final quality report ---------------------------------------------------

@dataclass
class QualityReport:
    """Authoritative readiness. Dimensions are independent; `status` derives from them."""
    legality: str = "unknown"             # "pass" | "fail"
    mechanics: str = "unknown"            # "pass" | "fail" | "unsupported"
    completeness: str = "unknown"         # "complete" | "incomplete"
    encounter_readiness: str = "unknown"  # "ready" | "not_ready"
    price_coverage: dict = field(default_factory=dict)  # summarize_quotes() output
    status: str = "experimental"          # "validated" | "experimental" | "failed"
    gaps: list[str] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)
    counts: dict[str, Any] = field(default_factory=dict)  # socketedGems, itemGrantedSkills,
    #   utilityGroups, masteries, jewels, uniques, links per slot, unspentPoints
    groups: list[dict] = field(default_factory=list)      # SkillGroup.to_dict() from FINAL xml
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def validated(self) -> bool:
        return (self.legality == "pass" and self.mechanics == "pass"
                and self.completeness == "complete" and self.encounter_readiness == "ready")
