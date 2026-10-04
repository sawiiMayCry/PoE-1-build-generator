"""Bounded external data and publishing services for Witchcraft."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pob_engine import find_pob_installation, get_worker

_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()
USER_AGENT = "WitchcraftLocalGenerator/4.0 (local PoE build planner)"


def fetch(url: str, *, data: bytes | None = None, timeout: int = 20,
          headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        result = response.read(8_000_001)
        if len(result) > 8_000_000:
            raise ValueError("External response exceeded the size limit")
        return result


def json_get(url: str) -> dict | list:
    return json.loads(fetch(url, headers={"Accept": "application/json"}))


def _normalize_lines(lines):
    return " ".join(" ".join(str(line).split()) for line in (lines or []))


def compare_tree_metadata(official_nodes: dict, installed: dict, ascendancies: set[str] | None = None) -> dict:
    """Compare the official node graph and mastery definitions with installed PoB."""
    ascendancies = ascendancies or set()

    def relevant(node):
        ascendancy = node.get("ascendancyName")
        return (not node.get("isProxy") and not node.get("isBloodline") and
                (ascendancy is None or ascendancy in ascendancies))

    official = {str(key): value for key, value in official_nodes.items()
                if str(key).isdigit() and isinstance(value, dict) and value.get("group") is not None
                and relevant(value)}
    all_local = {str(key): value for key, value in installed.get("nodes", {}).items() if relevant(value)}
    orphan_extras = sorted((key for key, node in all_local.items()
                            if key not in official and not node.get("links")), key=int)
    local = {key: value for key, value in all_local.items() if key not in orphan_extras}
    missing = sorted(set(official) - set(local), key=int)
    extra = sorted(set(local) - set(official), key=int)

    def links(nodes):
        result = {}
        for key, node in nodes.items():
            neighbors = set(map(str, node.get("out", []))) | set(map(str, node.get("in", []))) \
                if "out" in node or "in" in node else set(map(str, node.get("links", [])))
            result[key] = {neighbor for neighbor in neighbors if neighbor in nodes and neighbor != key}
        return result

    official_links, local_links = links(official), links(local)
    connection_mismatches = [key for key in sorted(set(official) & set(local), key=int)
                             if official_links[key] != local_links[key]]
    official_effects = {}
    for node in official.values():
        for effect in node.get("masteryEffects", []):
            effect_id = str(effect.get("effect", effect.get("id", "")))
            if effect_id:
                official_effects[effect_id] = {"name": effect.get("name"),
                                               "stats": _normalize_lines(effect.get("stats", []))}
    local_effects = {}
    relevant_effect_ids = {str(effect_id) for node in local.values()
                           for effect_id in node.get("masteryEffects", [])}
    for key, value in installed.get("masteryEffects", {}).items():
        if str(key) not in relevant_effect_ids:
            continue
        if isinstance(value, list):
            local_effects[str(key)] = {"name": None, "stats": _normalize_lines(value)}
        elif isinstance(value, dict):
            local_effects[str(key)] = {"name": value.get("name"),
                                       "stats": _normalize_lines(value.get("stats", []))}
    mastery_mismatches = sorted(key for key in set(official_effects) | set(local_effects)
                                if official_effects.get(key) != local_effects.get(key))
    return {"passed": not (missing or extra or connection_mismatches or mastery_mismatches),
            "officialNodeCount": len(official), "installedNodeCount": len(local),
            "missingNodeIds": missing[:30], "extraNodeIds": extra[:30],
            "ignoredOrphanNodeIds": orphan_extras[:30],
            "connectionMismatchCount": len(connection_mismatches),
            "connectionMismatchNodeIds": connection_mismatches[:30],
            "masteryMismatchCount": len(mastery_mismatches),
            "masteryMismatchEffectIds": mastery_mismatches[:30]}


def cached(key: str, ttl: int, loader):
    with _lock:
        found = _cache.get(key)
        if found and time.time() - found[0] < ttl:
            return found[1]
    value = loader()
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def game_context() -> dict:
    """Fail closed unless the market, official tree and installed PoB agree."""
    def load():
        home = find_pob_installation()
        if not home:
            raise RuntimeError("Install an up-to-date Path of Building Community or set WITCHCRAFT_POB_HOME.")
        versions = (home / "GameVersions.lua").read_text(encoding="utf-8")
        listing = re.search(r"treeVersionList\s*=\s*\{(.*?)\}", versions, re.S)
        if not listing:
            raise RuntimeError("Could not read the installed PoB tree version")
        all_versions = re.findall(r'"(\d+_\d+(?:_[a-z_]+)?)"', listing.group(1))
        local_tree = next((v for v in reversed(all_versions) if re.fullmatch(r"\d+_\d+", v)), None)
        if not local_tree:
            raise RuntimeError("Installed PoB has no normal league tree")
        releases = json_get("https://api.github.com/repos/grindinggear/skilltree-export/releases/latest")
        tag = str(releases.get("tag_name", ""))
        version_match = re.search(r"(\d+)\.(\d+)", tag)
        if not version_match:
            raise RuntimeError("Official passive tree release has no recognizable version")
        official_tree = f"{version_match.group(1)}_{version_match.group(2)}"
        if local_tree != official_tree:
            raise RuntimeError(f"Installed PoB tree {local_tree.replace('_', '.')} is stale; official tree is {tag}.")
        leagues = json_get("https://poe.ninja/poe1/api/economy/leagues")
        if not isinstance(leagues, list) or not leagues:
            raise RuntimeError("poe.ninja did not return an active PoE 1 trade league")
        league = leagues[0].get("id") or leagues[0].get("name")
        if not league:
            raise RuntimeError("Active trade league has no ID")
        tree_url = ("https://raw.githubusercontent.com/grindinggear/skilltree-export/"
                    + urllib.parse.quote(tag, safe="") + "/data.json")
        tree = json_get(tree_url)
        if not isinstance(tree.get("nodes"), dict) or len(tree["nodes"]) < 1000:
            raise RuntimeError("Official passive tree data is unavailable")
        app_root = Path(__file__).resolve().parent
        installed_tree = get_worker(app_root, app_root / "data").request("treeMetadata")
        if installed_tree.get("version") != local_tree:
            raise RuntimeError(f"Installed PoB loaded tree {installed_tree.get('version')}, expected {local_tree}.")
        witch = next((entry for entry in tree.get("classes", []) if entry.get("name") == "Witch"), {})
        ascendancies = {entry.get("name") for entry in witch.get("ascendancies", []) if entry.get("name")}
        consistency = compare_tree_metadata(tree["nodes"], installed_tree, ascendancies)
        if not consistency["passed"]:
            raise RuntimeError("Official passive tree differs from installed PoB: " +
                               json.dumps(consistency, separators=(",", ":")))
        return {"league": league, "leagueName": leagues[0].get("name", league),
                "treeVersion": local_tree, "officialRelease": tag, "tree": tree,
                "treeSource": tree_url, "treeConsistency": consistency,
                "pobHome": home, "checkedAt": int(time.time())}
    return cached("game-context", 900, load)


def market_data(league: str) -> dict:
    def load():
        base = "https://poe.ninja/poe1/api/economy/stash/current"
        query = urllib.parse.quote(league, safe="")
        currency = json_get(f"{base}/currency/overview?league={query}&type=Currency")
        divine = next((line.get("chaosEquivalent") for line in currency.get("lines", [])
                       if line.get("currencyTypeName") == "Divine Orb"), None)
        if not divine or float(divine) <= 0:
            raise RuntimeError("Current divine to chaos conversion is unavailable")
        prices: dict[str, list[float]] = {}
        listings: dict[str, list[dict]] = {}
        errors = []
        category_errors: dict[str, str] = {}
        for kind in ("UniqueWeapon", "UniqueArmour", "UniqueAccessory", "UniqueFlask", "UniqueJewel"):
            try:
                result = json_get(f"{base}/item/overview?league={query}&type={kind}")
                for line in result.get("lines", []):
                    name, value = line.get("name"), line.get("chaosValue")
                    if name and value and float(value) > 0:
                        prices.setdefault(name, []).append(float(value))
                        listings.setdefault(name, []).append({
                            "chaos": float(value),
                            "variant": line.get("variant") or line.get("variantName"),
                            "links": line.get("links") or line.get("linkCount"),
                            "corrupted": line.get("corrupted"),
                            "listingCount": line.get("listingCount", line.get("count")),
                            "baseType": line.get("baseType"),
                            "category": kind,
                            "detailsId": line.get("detailsId"),
                        })
            except Exception as exc:
                errors.append(f"{kind}: {exc}")
                category_errors[kind] = str(exc)
        return {"league": league, "divineChaos": float(divine), "prices": prices, "listings": listings,
                "updated": int(time.time()), "source": "poe.ninja economy API", "errors": errors,
                "categoryErrors": category_errors, "schema": 2}
    return cached("market:" + league, 300, load)


def reference_xml(build_id: str, decoder) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,32}", build_id):
        raise ValueError("Invalid reference ID")
    def load():
        # pobb.in IDs are immutable. Keep decoded references locally so every
        # generation does not repeat the same public fetch or hit rate limits.
        cache_dir = Path(__file__).resolve().parent / "data" / "reference_cache"
        path = cache_dir / (build_id + ".xml")
        if path.is_file():
            return path.read_text(encoding="utf-8")
        xml = decoder(fetch(f"https://pobb.in/{build_id}/raw",
                            headers={"Accept": "text/plain"}).decode("utf-8"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        temp = cache_dir / f"{build_id}.{os.getpid()}.{threading.get_ident()}.tmp"
        temp.write_text(xml, encoding="utf-8")
        temp.replace(path)
        return xml
    return cached("reference:" + build_id, 3600, load)


def publish(code: str, decoder, expected_fingerprint: str, fingerprint) -> str:
    if len(code) > 2_000_000:
        raise ValueError("The generated PoB export is too large for sharing")
    try:
        raw = fetch("https://pobb.in/pob/", data=code.encode("ascii"), timeout=30,
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/plain"})
    except urllib.error.HTTPError as exc:
        detail = exc.read(1024).decode("utf-8", errors="replace").strip()
        if detail.startswith("{"):
            try:
                error = json.loads(detail)
                detail = error.get("message") or error.get("error") or ""
            except (ValueError, AttributeError):
                detail = ""
        if not isinstance(detail, str) or "<" in detail:
            detail = ""
        detail = re.sub(r"\s+", " ", detail)[:240]
        message = (f"pobb.in rejected the build format (HTTP {exc.code})" if exc.code == 400 else
                   f"pobb.in rejected the export (HTTP {exc.code}); retry sharing later")
        raise RuntimeError(message + (": " + detail if detail else "")) from exc
    value = raw.decode("utf-8", errors="replace").strip()
    match = re.fullmatch(r"(?:https?://(?:www\.)?pobb\.in/)?([A-Za-z0-9_-]{4,32})/?", value)
    if not match:
        raise RuntimeError("pobb.in returned a malformed share response; retry sharing later")
    build_id = match.group(1)
    returned = fetch(f"https://pobb.in/{build_id}/raw", timeout=30,
                     headers={"Accept": "text/plain"}).decode("utf-8")
    if fingerprint(decoder(returned)) != expected_fingerprint:
        raise RuntimeError("pobb.in returned an export with different build mechanics")
    return f"https://pobb.in/{build_id}"
