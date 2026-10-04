"""Installed definitions and legal, modifier-aware rare templates."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

UTILITY_TAGS = {"aura", "herald", "guard", "warcry", "blessing", "curse", "hex", "mark",
                "movement", "travel", "blink", "link", "banner", "stance"}


def roll_line(line: str) -> str:
    """Use an ordinary mid-tier roll, preserving text PoB itself understands."""
    def replace(match):
        lo, hi = float(match[1]), float(match[2])
        value = (lo + hi) / 2
        if "." not in match[1] and "." not in match[2]:
            return str(int(value))
        return str(round(value, 2))
    return re.sub(r"\((-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)\)", replace, line)


def eligible(mod: dict, base: dict, item_level: int = 80) -> bool:
    if mod.get("level", 1) > item_level or mod.get("kind") not in {"Prefix", "Suffix"}:
        return False
    for key, weight in zip(mod.get("weightKey", []), mod.get("weightVal", [])):
        if base.get("tags", {}).get(key):
            return weight > 0
    return False


def base_required_level(base: dict) -> int:
    requirements = base.get("req", {})
    if isinstance(requirements, dict):
        return int(requirements.get("level", 1) or 1)
    if isinstance(requirements, (list, tuple)):
        # Older PoB snapshots encode requirements as [str, dex, int, level].
        return int(requirements[3]) if len(requirements) > 3 else 1
    return 1


@dataclass
class RareItem:
    slot: str
    base: str
    definition: dict
    mods: list[dict] = field(default_factory=list)
    item_level: int = 80
    quality: int = 20

    def can_add(self, mod: dict) -> bool:
        maximum = 2 if self.slot == "Jewel" else 3
        return (eligible(mod, self.definition, self.item_level) and
                sum(entry["kind"] == mod["kind"] for entry in self.mods) < maximum and
                all(entry["group"] != mod["group"] for entry in self.mods))

    def text(self, sockets: int) -> str:
        implicit = self.definition.get("implicit", "")
        implicit_lines = implicit.split("\n") if isinstance(implicit, str) else list(implicit or [])
        lines = ["Rarity: RARE", "Witchcraft " + self.slot, self.base,
                 "Item Level: " + str(self.item_level), "Quality: " + str(self.quality)]
        if sockets:
            lines.append("Sockets: " + "-".join("B" for _ in range(sockets)))
        lines.append("Implicits: " + str(len(implicit_lines)))
        lines.extend(roll_line(line) for line in implicit_lines)
        for mod in self.mods:
            lines.extend(roll_line(line) for line in mod["lines"])
        return "\n".join(lines)


# PoB skill types (Worker metadata ``skillTypes``) that mark a skill as a buff/utility rather than a
# damage source: an offering or aura only modifies other skills, whatever its generic tags say.
NON_DAMAGE_SKILL_TYPES = {"Offering", "Aura", "Warcry", "Herald", "Guard", "Blessing", "Banner",
                          "Stance", "Meta"}


NON_COMBAT_STAT = re.compile(r"dropped_item|item_found|monster_dropped|experience_gain|_quantity|_rarity")


def support_is_noncombat(gem: dict) -> bool:
    """A support is unusable in a damage link when PoB marks it legacy or its only stats are item-drop
    or experience effects (decided from the gem's own stat ids, not its name)."""
    if gem.get("legacy"):
        return True
    stats = list(gem.get("statIds") or ())
    return bool(stats) and all(NON_COMBAT_STAT.search(stat) for stat in stats)


def is_main_skill_candidate(name: str, gem: dict) -> bool:
    """Whether a gem can be a build's main (damage) skill, decided from PoB data.

    Uses PoB skill types when the metadata carries them (offerings, auras, warcries and the like are
    excluded; a minion-tagged skill must actually create minions), falling back to gem tags for older
    metadata. Hexes qualify only when they deal damage.
    """
    if gem.get("support") or gem.get("unsupported"):
        return False
    tags = set(gem.get("tags", {}))
    skill_types = set(gem.get("skillTypes") or ())
    if "base_deal_no_damage" in set(gem.get("statIds") or ()):
        return False   # PoB's own flag for skills that deal no damage
    if skill_types:
        if skill_types & NON_DAMAGE_SKILL_TYPES:
            return False
        if "minion" in tags and "createsMinions" in gem and not gem["createsMinions"]:
            return False
        if "Buff" in skill_types and not gem.get("baseEffectiveness") and "CreatesMinion" not in skill_types:
            return False
    if tags & UTILITY_TAGS:
        damaging_hex = bool(tags & {"hex", "curse", "mark"}) and "DamageOverTime" in skill_types             and bool(gem.get("baseEffectiveness"))
        return damaging_hex or name.startswith(("Bane", "Hexblast"))
    return True


class GameData:
    def __init__(self, metadata: dict):
        self.gems = {gem["name"]: gem for gem in sorted(metadata["gems"], key=lambda g: g["id"])
                     if gem.get("gameId") and gem.get("skillId")}
        self.by_id = {gem["id"]: gem for gem in self.gems.values()}
        self.bases = metadata["bases"]
        self.mods = metadata["mods"]
        self.jewel_mods = metadata.get("jewelMods", [])
        self.unique_items = metadata.get("uniqueItems")
        self.main_names = sorted(name for name, gem in self.gems.items() if is_main_skill_candidate(name, gem))

    def gem(self, name: str) -> dict:
        match = next((gem for key, gem in self.gems.items() if key.casefold() == name.casefold()), None)
        if match is None:
            raise ValueError(f"Unknown installed PoB gem: {name}")
        return match

    def pick_mod(self, item: RareItem, pattern: str, target: float) -> dict | None:
        choices = []
        for mod in self.mods:
            if not item.can_add(mod) or len(mod["lines"]) != 1:
                continue
            line = roll_line(mod["lines"][0])
            match = re.fullmatch(pattern, line)
            if match:
                value = float(match[1])
                choices.append((abs(value - target), mod["id"], mod))
        return min(choices, key=lambda row: row[:2])[2] if choices else None

    def add_mod(self, item: RareItem, pattern: str, target: float):
        mod = self.pick_mod(item, pattern, target)
        if mod:
            item.mods.append(mod)


def rare_templates(data: GameData, archetype: str, weapon_type: str = "Wand", *,
                   character_level: int | None = None, damage_type: str = "physical",
                   base_damage_type: str | None = None, focus: str = "balanced",
                   defense_model: str = "hybrid") -> list[RareItem]:
    weapon_bases = {"Wand": "Prophecy Wand", "Bow": "Thicket Bow", "Staff": "Judgement Staff",
                    "Claw": "Imperial Claw", "Dagger": "Imperial Skean",
                    "One Handed Sword": "Jewelled Foil", "One Handed Axe": "Siege Axe",
                    "One Handed Mace": "Behemoth Mace", "Sceptre": "Void Sceptre"}
    slots = {"Weapon 1": weapon_bases.get(weapon_type, "Prophecy Wand"), "Helmet": "Hubris Circlet",
             "Body Armour": "Vaal Regalia", "Gloves": "Sorcerer Gloves", "Boots": "Sorcerer Boots",
             "Belt": "Leather Belt", "Amulet": "Amber Amulet", "Ring 1": "Coral Ring", "Ring 2": "Coral Ring"}
    if archetype == "minion" and weapon_type == "Wand":
        slots["Weapon 1"] = "Convoking Wand"
    if weapon_type not in {"Bow", "Staff"}:
        slots["Weapon 2"] = "Titanium Spirit Shield"
    result = []
    base_damage_type = base_damage_type or damage_type
    title_damage = damage_type.title()
    title_base_damage = base_damage_type.title()
    for slot, base in slots.items():
        if base not in data.bases:
            raise ValueError(f"Installed PoB has no required template base {base}")
        if character_level is not None:
            wanted = data.bases[base]
            choices = [(name, entry) for name, entry in data.bases.items()
                       if entry["type"] == wanted["type"] and entry.get("subType") == wanted.get("subType")
                       and entry.get("tags", {}).get("default")
                       and base_required_level(entry) <= character_level]
            ordinary = [row for row in choices if not row[1].get("tags", {}).get("experimental_base")]
            choices = ordinary or choices      # league-crafted experimental bases (own sockets/implicits)
            if slot in {"Belt", "Amulet", "Ring 1", "Ring 2"}:
                # Jewellery-slot bases with their own socket (abyss belts) would leave a socket the
                # skill plan cannot use; prefer socketless bases of the same type.
                plain = [row for row in choices if not row[1].get("socketLimit")]
                choices = plain or choices
            if not choices:
                raise ValueError(f"No level-{character_level} base for {slot}")
            base, _ = max(choices, key=lambda row: (base_required_level(row[1]), row[0]))
        item = RareItem(slot, base, data.bases[base], item_level=character_level or 80,
                        quality=0 if character_level is not None else 20)
        scale = min(1, character_level / 80) if character_level is not None else 1
        if slot == "Weapon 1":
            if archetype == "minion":
                data.add_mod(item, r"Minions deal (\d+(?:\.\d+)?)% increased Damage", 65 * scale)
                # Convoking Wands can roll two independent global minion
                # affixes in addition to damage.  Leaving either slot empty
                # materially weakens every minion build before the complete
                # design search even begins.
                data.add_mod(item, r"\+(\d+(?:\.\d+)?) to Level of all Minion Skill Gems", 1)
                data.add_mod(item, r"Minions have (\d+(?:\.\d+)?)% increased Attack and Cast Speed", 18 * scale)
            elif weapon_type in {"Wand", "Staff", "Sceptre", "Dagger"} and archetype != "attack":
                data.add_mod(item, r"(\d+(?:\.\d+)?)% increased Spell Damage", 70 * scale)
                if archetype in {"spell", "ignite", "dot"}:
                    data.add_mod(item, rf"\+(\d+(?:\.\d+)?) to Level of all {title_base_damage} Spell Skill Gems", 1)
                    data.add_mod(item, rf"(\d+(?:\.\d+)?)% increased {title_damage} Damage", 35 * scale)
                data.add_mod(item, r"(\d+(?:\.\d+)?)% increased Cast Speed", 15 * scale)
            else:
                data.add_mod(item, r"(\d+(?:\.\d+)?)% increased Physical Damage", 120 * scale)
                data.add_mod(item, r"(\d+(?:\.\d+)?)% increased Attack Speed", 15 * scale)
        else:
            if defense_model == "ci":
                # Chaos Inoculation sets maximum life to 1: maximum-life affixes
                # are worthless, so jewelry and armour carry ES instead.
                if data.bases[base]["type"] not in {"Helmet", "Body Armour", "Gloves", "Boots", "Shield"}:
                    data.add_mod(item, r"\+(\d+(?:\.\d+)?) to maximum Energy Shield", 60 * scale)
            else:
                data.add_mod(item, r"\+(\d+(?:\.\d+)?) to maximum Life", 110 * scale)
            if slot == "Amulet" and archetype in {"spell", "ignite", "dot"}:
                data.add_mod(item, rf"\+(\d+(?:\.\d+)?) to Level of all {title_base_damage} Skill Gems", 1)
                if archetype in {"ignite", "dot"}:
                    data.add_mod(item, r"\+(\d+(?:\.\d+)?)% to Damage over Time Multiplier", 18)
                    if damage_type in {"fire", "cold", "lightning", "chaos"}:
                        data.add_mod(item, rf"(\d+(?:\.\d+)?)% increased {title_damage} Damage", 20)
                else:
                    data.add_mod(item, r"(\d+(?:\.\d+)?)% increased Spell Damage", 35 * scale)
            if (slot.startswith("Ring") and focus == "damage" and
                    archetype in {"spell", "ignite", "dot"} and
                    damage_type in {"fire", "cold", "lightning", "chaos"}):
                data.add_mod(item, rf"(\d+(?:\.\d+)?)% increased {title_damage} Damage", 20 * scale)
            if data.bases[base]["type"] in {"Helmet", "Body Armour", "Gloves", "Boots", "Shield"}:
                data.add_mod(item, r"\+(\d+(?:\.\d+)?) to maximum Energy Shield", 65 * scale)
                if slot == "Boots":
                    data.add_mod(item, r"(\d+(?:\.\d+)?)% increased Movement Speed", max(10, 25 * scale))
                elif slot == "Weapon 2" and archetype in {"spell", "ignite", "dot"} and focus == "damage":
                    data.add_mod(item, rf"(\d+(?:\.\d+)?)% increased {title_damage} Damage", 55 * scale)
                elif slot == "Weapon 2" and archetype in {"spell", "ignite", "dot"} and focus == "balanced":
                    data.add_mod(item, r"(\d+(?:\.\d+)?)% increased Spell Damage", 55 * scale)
                else:
                    data.add_mod(item, r"(\d+(?:\.\d+)?)% increased Energy Shield", 80 * scale)
        result.append(item)
    return result


def solve_suffixes(items: list[RareItem], data: GameData, output: dict, *, resistance_target: int = 75,
                   chaos_target: int | None = None) -> int:
    """Allocate only needed resistance/attribute affixes within legal slot limits.

    Deficits come from a real PoB calculation. Each pass adds one affix at a
    time to the most constrained requirement, accounting for its actual roll.
    A subsequent calculation confirms the result (and newly active equipment).
    """
    deficits = {element: max(0, resistance_target - output.get(element + "Resist", -60))
                for element in ("Fire", "Cold", "Lightning")}
    deficits.update({attr: max(0, output.get("Req" + attr, 0) - output.get(attr, 0))
                     for attr in ("Str", "Dex", "Int")})
    if chaos_target is not None:
        # Chaos resistance is a hard repair target (not a nice-to-have): the deficit is measured by PoB
        # like the elemental ones and repaired with legal single-line "+N% to Chaos Resistance" affixes.
        deficits["Chaos"] = max(0, chaos_target - output.get("ChaosResist", -60))
    patterns = {element: rf"\+(\d+(?:\.\d+)?)% to {element} Resistance"
                for element in ("Fire", "Cold", "Lightning", "Chaos")}
    patterns.update({attr: rf"\+(\d+(?:\.\d+)?) to {name}"
                     for attr, name in (("Str", "Strength"), ("Dex", "Dexterity"), ("Int", "Intelligence"))})
    added = 0
    while any(value > 0 for value in deficits.values()):
        options = {}
        for requirement, deficit in deficits.items():
            if deficit <= 0:
                continue
            target = 35 if requirement in {"Fire", "Cold", "Lightning", "Chaos"} else 45
            choices = [(item, data.pick_mod(item, patterns[requirement], target)) for item in items
                       if not item.slot.startswith("Flask ")]
            options[requirement] = [(item, mod) for item, mod in choices if mod]
        available = [key for key in options if options[key]]
        if not available:
            break
        key = min(available, key=lambda key: (len(options[key]) / deficits[key], key))
        # Reserve jewelry for attributes: resists are available on armour too.
        def rank(choice):
            item, mod = choice
            jewelry = item.slot in {"Amulet", "Ring 1", "Ring 2", "Belt"}
            return (jewelry if key in {"Fire", "Cold", "Lightning", "Chaos"} else not jewelry,
                    len(item.mods), item.slot, mod["id"])
        item, mod = min(options[key], key=rank)
        item.mods.append(mod)
        deficits[key] -= float(re.fullmatch(patterns[key], roll_line(mod["lines"][0]))[1])
        added += 1
    return added
