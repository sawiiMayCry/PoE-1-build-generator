"""Prompt -> validated intent -> deterministic, PoB-scored generated build."""
from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

from build_assembly import assemble
from build_progression import add_progression
from build_generator import mechanics_fingerprint, offense_value, quote, validate_calculation, validate_structure
from generation_data import GameData, rare_templates, roll_line, solve_suffixes
from ollama_service import DEFAULT_MODEL, ask_json
from passive_search import graph, paths_from, initial_nodes, mastery_choices, score, search_tree, heuristic
from pob_engine import export_with_pob, get_worker
from services import game_context, market_data


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
    focus = ("defense" if re.search(r"\b(tanky|defen[cs]e|survivability)\b", prompt, re.I) else
             "damage" if re.search(r"\b(dps|more damage)\b", prompt, re.I) else str(reply.get("focus", "balanced")))
    if focus not in {"damage", "defense", "balanced"}:
        focus = "balanced"
    level_match = re.search(r"\b(?:level|lvl)\s*(\d+)\b", prompt, re.I)
    level = int(level_match[1]) if level_match else 90
    if not 80 <= level <= 100:
        raise ValueError("Requested level must be between 80 and 100")
    damage = next((tag for tag in ("chaos", "cold", "lightning", "fire", "physical") if tags.get(tag)), "physical")
    ignite = bool(re.search(r"\b(ignite|burning)\b", prompt, re.I))
    if asc == "Elementalist" and gem["name"] in {"Ethereal Knives", "Penance Brand", "Wave of Conviction"}:
        ignite = ignite or not re.search(r"\b(hit[- ]based|non[- ]ignite|no ignite)\b", prompt, re.I)
    archetype = ("minion" if tags.get("minion") else "attack" if tags.get("attack") else
                 "ignite" if ignite else "dot" if tags.get("dot") or gem["name"] in {
                     "Vortex", "Cold Snap", "Bane", "Bane of Condemnation", "Essence Drain", "Contagion", "Soulrend"} else "spell")
    if ignite and asc != "Elementalist" and not tags.get("fire"):
        raise ValueError("This ignite recipe requires a fire skill or Elementalist's Shaper of Flames")
    weapon_types = gem.get("weaponTypes") or {}
    weapon = next((kind for kind in ("Wand", "Bow", "Staff", "Claw", "Dagger", "One Handed Sword",
                                   "One Handed Axe", "One Handed Mace", "Sceptre") if weapon_types.get(kind)), "Wand")
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
    for name in requested_utilities:
        utility[name] = "Helmet" if data.gem(name)["tags"].get("aura") else "Boots"
    spec = {"skill": gem["name"], "ascendancy": asc, "level": level, "focus": focus,
            "damageType": "fire" if ignite else damage, "archetype": archetype,
            "budgetChaos": budget_from_prompt(prompt, market["divineChaos"]), "weaponType": weapon,
            "utility": utility, "noUniques": bool(re.search(r"\b(no uniques?|rares? only)\b", prompt, re.I)),
            "intent": str(reply.get("intent", ""))[:350]}
    # These mechanics require dedicated gear/tree recipes. Fail specifically
    # instead of producing a different build which happens to pass numeric gates.
    for pattern, label in ((r"\bchaos inoculation\b|\bCI\b", "Chaos Inoculation"),
                           (r"\blow[- ]life\b", "low life"), (r"\bward loop\b", "ward loop")):
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


def search_links(spec, data, render, worker, stage):
    supports = []
    for index in range(5):
        xml = render(supports)
        candidates = worker.request("supports", xml=xml)["supports"]
        names = []
        for identifier in candidates:
            gem = data.by_id.get(identifier)
            if (gem and gem["name"] not in supports and not gem["name"].startswith("Awakened ")
                    and gem["name"] not in {"Sacrifice", "Vaal Sacrifice", "Cast on Death"}
                    and (gem["name"] != "Decay" or spec["archetype"] == "dot")
                    and not gem["tags"].get("exceptional") and gem["maxLevel"] >= 20):
                names.append(identifier)
        if not names:
            break
        baseline = worker.request("calculate", xml=xml)["stats"]
        scored = []
        for offset in range(0, len(names), 20):
            stage(f"Scoring support {index + 1}/5: candidates {offset + 1}-{min(offset + 20, len(names))}/{len(names)}")
            scored.extend(worker.request("supportScores", xml=xml,
                                         candidates=sorted(names)[offset:offset + 20])["candidates"])
        # Resource availability is an actual constraint, not an assumed buff.
        feasible = [entry for entry in scored if entry["stats"].get("ManaUnreserved", 0) >=
                    entry["stats"].get("ManaCost", 0) and
                    entry["stats"].get("LifeUnreserved", entry["stats"].get("Life", 0)) > 0]
        if not feasible:
            break
        best = max(feasible, key=lambda entry: (target_dps(entry["stats"], spec), entry["id"]))
        # Fill a legal five-link even if the fourth support is chiefly utility.
        if len(supports) >= 4 and target_dps(best["stats"], spec) <= target_dps(baseline, spec):
            break
        supports.append(data.by_id[best["id"]]["name"])
    if len(supports) < 4:
        raise ValueError(f"PoB could not construct a compatible five-link for {spec['skill']}")
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


def unique_options(context, spec, market):
    if spec["noUniques"]:
        return []
    wanted = {"Midnight Bargain", "Clayshaper"} if spec["archetype"] == "minion" else {
        "Lifesprig", "Singularity", "Divinarius", "Obliteration"}
    options = []
    cap = spec["budgetChaos"] if spec["budgetChaos"] is not None else float("inf")
    for path in (context["pobHome"] / "Data" / "Uniques").glob("*.lua"):
        for block in re.findall(r"\[\[(.*?)\]\]", path.read_text(encoding="utf-8"), re.S):
            name = block.strip().splitlines()[0]
            prices = market["prices"].get(name)
            if name in wanted and prices and max(prices) <= cap:
                options.append((name, current_unique(block) + "\nSockets: B-B-B"))
    return options


def validate_design(context, spec, allocated, masteries, items, uniques, data):
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
    socket_capacity = all(count <= next((min(4, item.definition.get("socketLimit", 0)) for item in items
                                         if item.slot == slot), 0) for slot, count in counts.items())
    legal_affixes = True
    for item in items:
        if item.slot in uniques:
            continue
        copy = type(item)(item.slot, item.base, item.definition)
        for mod in item.mods:
            legal_affixes = legal_affixes and copy.can_add(mod)
            copy.mods.append(mod)
    return [{"name": "Connected passive tree", "passed": bool(connected),
             "reason": "All regular and ascendancy paths connect through allocated nodes to their own start"},
            {"name": "Legal mastery effects", "passed": bool(valid_masteries),
             "reason": "Masteries require an allocated group notable and a distinct installed effect"},
            {"name": "Utility socket capacity", "passed": socket_capacity,
             "reason": "Utility groups must fit their equipped items' sockets"},
            {"name": "Legal rare affixes", "passed": legal_affixes,
             "reason": "Generated rares use eligible installed tiers, distinct groups and at most three prefixes/suffixes"}]


def build_design(spec, context, market, app_root, data_root, stage, data=None):
    worker = get_worker(app_root, data_root)
    initial_calls = worker.calls
    data = data or GameData(worker.request("metadata"))
    allocated = initial_nodes(context, spec)
    items = rare_templates(data, spec["archetype"], spec["weaponType"])
    supports, masteries, uniques = [], {}, {}

    def render(nodes=None, links=None, mastery=None, unique=None):
        return assemble(spec, context, data, allocated if nodes is None else nodes,
                        supports if links is None else links, items,
                        masteries if mastery is None else mastery, uniques if unique is None else unique)

    stage("Solving equipment resistances and attributes with installed modifier tiers")
    calc = worker.request("calculate", xml=render())
    if spec["skill"] == "Raise Zombie" and calc["stats"].get("ActiveMinionLimit", 0) > 0:
        spec["minionCount"] = int(calc["stats"]["ActiveMinionLimit"])
        calc = worker.request("calculate", xml=render())
    for _ in range(3):
        if not solve_suffixes(items, data, calc["stats"]):
            break
        calc = worker.request("calculate", xml=render())
    stage("Building a connected passive tree and scoring clusters in PoB")
    allocated, calc = search_tree(context, spec, allocated, lambda nodes: render(nodes=nodes), worker, stage)
    supports = search_links(spec, data, lambda links: render(links=links), worker, stage)
    calc = worker.request("calculate", xml=render())
    # Links can introduce new attribute requirements. Repair those specifically.
    for _ in range(3):
        if not solve_suffixes(items, data, calc["stats"]):
            break
        calc = worker.request("calculate", xml=render())
    stage("Scoring available mastery effects")
    used_effects = set()
    candidates = mastery_choices(context, allocated)
    candidates.sort(key=lambda entry: -heuristic(context["tree"]["nodes"][entry[0]], spec))
    for node, effects in candidates:
        if calc["passives"]["used"] >= calc["passives"]["maximum"]:
            break
        best = None
        # Score up to eight likely effects; the final XML calculation verifies
        # mastery legality and point consumption, including duplicate effects.
        ranked = sorted(effects, key=lambda effect: -heuristic({"stats": effect.get("stats", [])}, spec))[:8]
        for effect in ranked:
            identifier = effect.get("effect")
            if identifier in used_effects:
                continue
            candidate = worker.request("calculate", xml=render(nodes=allocated | {node},
                                        mastery={**masteries, node: identifier}))
            if candidate["passives"]["used"] > candidate["passives"]["maximum"]:
                continue
            if score(candidate["stats"], spec) > score(calc["stats"], spec) + 0.001:
                if best is None or score(candidate["stats"], spec) > score(best[1]["stats"], spec):
                    best = (identifier, candidate)
        if best:
            masteries[node], calc = best
            used_effects.add(best[0])
            allocated.add(node)
    stage("Comparing affordable uniques with generated equipment")
    for name, text in unique_options(context, spec, market):
        candidate = worker.request("calculate", xml=render(unique={"Weapon 1": text}))
        if (all(check["passed"] for check in validate_calculation(candidate)) and
                score(candidate["stats"], spec) > score(calc["stats"], spec)):
            uniques["Weapon 1"] = text
            calc = candidate
    # Use remaining points for measured repairs/final improvements after links.
    if calc["passives"]["used"] < calc["passives"]["maximum"]:
        allocated, calc = search_tree(context, spec, allocated, lambda nodes: render(nodes=nodes),
                                       worker, stage, reserve=0)
    if spec["skill"] == "Raise Zombie" and calc["stats"].get("ActiveMinionLimit", 0) > 0:
        spec["minionCount"] = int(calc["stats"]["ActiveMinionLimit"])
    xml = render()
    stage("Validating the exact generated XML in Path of Building")
    # Share a real PoB save, including computed stats required by pobb.in.
    # Reimport checks the exact exported XML before validation/fingerprinting.
    calc = export_with_pob(xml, app_root, data_root)
    xml = calc.pop("xml")
    checks, details = validate_structure(xml, context, spec["ascendancy"], spec["skill"])
    checks.extend(validate_calculation(calc))
    checks.extend(validate_design(context, spec, allocated, masteries, items, uniques, data))
    checks.append({"name": "Usable main skill resources", "passed": calc["stats"].get("ManaUnreserved", 0) >=
                   calc["stats"].get("ManaCost", 0), "reason": "Unreserved mana must cover the main skill's cost"})
    compatibility = worker.request("supports", xml=xml)["supports"]
    checks.append({"name": "Support compatibility", "passed": all(data.gem(name)["id"] in compatibility
                   for name in supports), "reason": "PoB's skill-type expressions must accept every selected support"})
    failures = [check["name"] + ": " + check["reason"] for check in checks if not check["passed"]]
    if failures:
        raise ValueError("Generated build failed validation: " + " | ".join(failures))
    xml, progression = add_progression(xml, spec, context, data, worker, stage)
    stage("Exporting and verifying the complete campaign-to-endgame PoB")
    final = export_with_pob(xml, app_root, data_root)
    xml = final.pop("xml")
    if final != calc:
        raise ValueError("Progression export changed the validated endgame calculation")
    structural, details = validate_structure(xml, context, spec["ascendancy"], spec["skill"])
    if any(not check["passed"] for check in structural):
        raise ValueError("Complete build failed final structural validation")
    checks = [check for check in checks if check["name"] not in {entry["name"] for entry in structural}]
    checks[:0] = structural
    checks.append({"name": "Campaign to endgame progression", "passed": True,
                   "reason": f"{len(progression)} matched loadouts; every stage calculated and checked for levels, points, sockets, attributes, resistances and mana"})
    price = quote(details["gear"], market, spec["budgetChaos"] if spec["budgetChaos"] is not None else 10_000_000)
    if spec["budgetChaos"] is None:
        price["budgetChaos"] = None
        price["budgetStatus"] = "not specified; unpriced slots remain" if price["unknown"] else "not specified"
    if spec["budgetChaos"] is not None and price["pricedSubtotalChaos"] > spec["budgetChaos"]:
        raise ValueError("Priced equipment alone exceeds the requested budget")
    recipe = {"generation": "from-game-data", "treeChange": f"Generated {calc['passives']['used']} paid passives from the Witch start",
              "changedMainLinks": supports, "changedSlots": [item.slot for item in items],
              "masteries": masteries, "modelReason": "Generated and scored in real PoB; rare templates and gems remain unpriced.",
              "assumptions": ([f"All {spec['minionCount']} permanent zombies active"] if spec.get("minionCount") else []),
              "constraints": spec, "evaluations": worker.calls - initial_calls}
    recipe["progression"] = progression
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
    stage(f"Parsing build intent with {model}")
    spec = parse_intent(prompt, model, data, market)
    xml, recipe, checks, details, calculation, price = build_design(
        spec, context, market, app_root, data_root, stage, data)
    return {"id": "g" + secrets.token_hex(10), "name": spec["skill"] + " " + spec["ascendancy"],
            "class": "Witch", "ascendancy": spec["ascendancy"], "mainSkill": spec["skill"],
            "level": details["level"], "gems": details["gems"], "treeNodes": details["treeNodes"],
            "ascendancyPoints": details["ascendancyPoints"], "gear": details["gear"],
            "validation": checks, "stats": calculation["stats"], "pobVersion": calculation.get("version"),
            "quote": price, "recipe": recipe, "modelUsed": model, "prompt": prompt,
            "modelIntent": f"{spec['focus'].capitalize()} focus with {spec['skill']}; links and passive clusters scored in PoB.",
            "progression": recipe.get("progression", []),
            "league": context["league"], "treeVersion": context["treeVersion"],
            "officialTreeRelease": context["officialRelease"], "createdAt": int(time.time()),
            "shareStatus": "pending", "shareUrl": None, "_xml": xml, "_fingerprint": mechanics_fingerprint(xml)}
