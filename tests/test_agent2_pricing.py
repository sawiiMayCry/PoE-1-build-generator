import unittest

from unique_pricing import (NO_MATCH, category_of, dedupe_by_slot, entry_from_text, normalize_variant,
                            price_unique_equipment, resolve_unique, resolve_unique_text,
                            unique_package_price, unique_price, upgrade_legacy_quote)


def market(listings=None, **extra):
    base = {"league": "Test", "divineChaos": 100.0, "updated": 1_000_000, "source": "test", "errors": [],
            "listings": listings or {}, "prices": {name: [row["chaos"] for row in rows]
                                                   for name, rows in (listings or {}).items()}}
    base.update(extra)
    return base


def entry(slot="Body Armour", name="Example", rarity="unique", **extra):
    return {"slot": slot, "name": name, "base": extra.pop("base", "Vaal Regalia"), "rarity": rarity,
            "variant": None, "links": None, "corrupted": False, **extra}


class UniqueQuoteResolver(unittest.TestCase):
    def test_six_link_requires_an_actual_six_link_quote(self):
        listings = {"Example": [{"chaos": 20, "links": 0}, {"chaos": 300, "links": 6}, {"chaos": 90, "links": 5}]}
        six = resolve_unique(entry(links=6), market(listings))
        self.assertEqual((six["status"], six["chaos"], six["matchKind"]), ("quoted", 300.0, "variant+6-link"))
        self.assertEqual(resolve_unique(entry(links=5), market(listings))["chaos"], 90.0)
        # A shorter link or helmet uses the unlinked listing and excludes crafting.
        four = resolve_unique(entry(links=4), market(listings))
        self.assertEqual(four["chaos"], 20.0)
        self.assertIn("socket and link crafting", four["excludedCosts"])

    def test_missing_six_link_quote_is_unknown_not_the_unlinked_price(self):
        listings = {"Example": [{"chaos": 20, "links": 0}, {"chaos": 90, "links": 5}]}
        quote = resolve_unique(entry(links=6), market(listings))
        self.assertEqual(quote["status"], "unquoted")
        self.assertIsNone(quote["chaos"])
        self.assertEqual(quote["reason"], NO_MATCH)
        self.assertIn("6-link", quote["detail"])
        self.assertEqual(quote["referenceUnlinkedChaos"], 20.0)

    def test_links_do_not_matter_for_jewellery_jewels_and_flasks(self):
        listings = {"Ring": [{"chaos": 7, "links": 0}], "Jewel": [{"chaos": 3}], "Flask": [{"chaos": 2}]}
        for slot, name, base in (("Ring 1", "Ring", "Gold Ring"), ("Jewel 5", "Jewel", "Cobalt Jewel"),
                                 ("Flask 1", "Flask", "Granite Flask")):
            quote = resolve_unique(entry(slot=slot, name=name, base=base, links=4), market(listings))
            self.assertEqual(quote["status"], "quoted", slot)
            self.assertEqual(quote["excludedCosts"], [])

    def test_variants_normalize_but_never_substitute_a_cheaper_incompatible_variant(self):
        listings = {"Example": [{"chaos": 10, "variant": "Added Cold"}, {"chaos": 500, "variant": "Added Fire"}]}
        cold = resolve_unique(entry(variant="Added Cold"), market(listings))
        self.assertEqual(cold["chaos"], 10.0)
        fire = resolve_unique(entry(variant="added   fire"), market(listings))
        self.assertEqual(fire["chaos"], 500.0)
        current = resolve_unique(entry(variant=None), market(listings))
        self.assertEqual(current["status"], "unquoted")
        self.assertIn("quoted variants", current["detail"])
        self.assertEqual(normalize_variant("Current"), None)
        self.assertEqual(normalize_variant("Phys Damage"), "physical damage")

    def test_base_type_must_match_when_the_market_distinguishes_it(self):
        listings = {"Example": [{"chaos": 5, "baseType": "Gold Ring"}]}
        self.assertEqual(resolve_unique(entry(slot="Ring 1", base="Gold Ring"), market(listings))["chaos"], 5.0)
        other = resolve_unique(entry(slot="Ring 1", base="Coral Ring"), market(listings))
        self.assertEqual(other["status"], "unquoted")
        self.assertIn("quoted bases", other["detail"])

    def test_corruption_is_matched_when_the_market_records_it(self):
        listings = {"Example": [{"chaos": 5, "corrupted": False}, {"chaos": 50, "corrupted": True}]}
        self.assertEqual(resolve_unique(entry(corrupted=True), market(listings))["chaos"], 50.0)
        self.assertEqual(resolve_unique(entry(corrupted=False), market(listings))["chaos"], 5.0)
        only_clean = {"Example": [{"chaos": 5, "corrupted": False}]}
        self.assertEqual(resolve_unique(entry(corrupted=True), market(only_clean))["status"], "unquoted")
        # poe.ninja lines have no corruption field: they match either state.
        self.assertEqual(resolve_unique(entry(corrupted=True), market({"Example": [{"chaos": 9}]}))["chaos"], 9.0)

    def test_league_mismatch_and_snapshot_without_listings_stay_unknown_with_reasons(self):
        quote = resolve_unique(entry(), market({"Example": [{"chaos": 9}]}), expected_league="Other")
        self.assertEqual(quote["status"], "unquoted")
        self.assertIn("league", quote["reason"])
        snapshot = market()
        snapshot["prices"] = {"Example": [10.0, 300.0]}
        legacy = resolve_unique(entry(), snapshot)
        self.assertEqual(legacy["reason"], "Snapshot has no variant/link-specific unique quote")
        missing = resolve_unique(entry(name="Nothing"), market({"Example": [{"chaos": 1}]}))
        self.assertEqual(missing["reason"], "Unique has no current-league quote")

    def test_category_failure_is_reported_separately(self):
        failing = market(errors=["UniqueArmour: HTTP 500"], categoryErrors={"UniqueArmour": "HTTP 500"})
        quote = resolve_unique(entry(), failing)
        self.assertIn("UniqueArmour", quote["reason"])
        ring = resolve_unique(entry(slot="Ring 1", name="R", base="Gold Ring"),
                              market({"R": [{"chaos": 4}]}, errors=["UniqueArmour: HTTP 500"]))
        self.assertEqual(ring["status"], "quoted")

    def test_rares_magic_and_normal_items_are_excluded_not_failed_quotes(self):
        for rarity in ("rare", "magic", "normal"):
            quote = resolve_unique(entry(rarity=rarity, name="Witchcraft Body Armour"), market())
            self.assertEqual(quote["status"], "excluded")
            self.assertIsNone(quote["chaos"])
        self.assertTrue(category_of(entry(slot="Jewel 40", base="Cobalt Jewel")).startswith("unique_jewel"))
        report = price_unique_equipment(
            [entry(rarity="rare"), entry(slot="Helmet", rarity="rare"), entry(slot="Flask 1", rarity="normal",
                                                                              base="Granite Flask")], market(), 100)
        self.assertEqual(report["uniqueCount"], 0)
        self.assertTrue(report["noUniques"])
        self.assertEqual(report["coverageStatus"], "No unique items equipped")
        self.assertEqual(report["unknown"], [])
        self.assertTrue(report["complete"])
        categories = {row["category"]: row["count"] for row in report["excluded"]["categories"]}
        self.assertEqual(categories, {"rare equipment": 2, "normal flask": 1})
        self.assertIn("gems", " ".join(report["excluded"]["alwaysExcluded"]))
        self.assertIn("No unique items", report["budgetStatus"])

    def test_report_prices_unique_jewels_and_flasks_and_keeps_unknowns(self):
        listings = {"Ring": [{"chaos": 40}], "Flask": [{"chaos": 12}], "Jewel A": [{"chaos": 8}]}
        gear = [entry(slot="Ring 1", name="Ring", base="Gold Ring"),
                entry(slot="Flask 3", name="Flask", base="Granite Flask"),
                entry(slot="Jewel 61", name="Jewel A", base="Cobalt Jewel"),
                entry(slot="Boots", name="Mystery Boots", base="Serpentscale Boots"),
                entry(slot="Amulet", rarity="rare", name="Witchcraft Amulet", base="Amber Amulet")]
        report = price_unique_equipment(gear, market(listings), 50)
        self.assertEqual((report["uniqueCount"], report["quotedUniqueCount"], report["unquotedUniqueCount"]), (4, 3, 1))
        self.assertEqual(report["uniqueSubtotalChaos"], 60.0)
        self.assertEqual(report["pricedSubtotalChaos"], 60.0)
        self.assertEqual([row["name"] for row in report["unknown"]], ["Mystery Boots"])
        self.assertEqual(report["unknown"][0]["reason"], "Unique has no current-league quote")
        self.assertFalse(report["complete"])
        self.assertEqual(report["budgetStatus"], "unique subtotal exceeds budget")
        self.assertEqual(report["scope"], "equipped_uniques")
        self.assertEqual(report["uniqueSubtotalDivine"], 0.6)
        self.assertIn("unverified", report["fullBuildBudget"])
        within = price_unique_equipment(gear, market(listings), 500)
        self.assertIn("unverified: unquoted uniques remain", within["budgetStatus"])

    def test_replaced_items_are_not_charged_and_slots_are_not_repeated(self):
        listings = {"Old": [{"chaos": 999}], "New": [{"chaos": 5}]}
        gear = [entry(slot="Ring 1", name="Old", base="Gold Ring"), entry(slot="Ring 1", name="New", base="Gold Ring")]
        self.assertEqual([row["name"] for row in dedupe_by_slot(gear)], ["New"])
        self.assertEqual(price_unique_equipment(gear, market(listings))["uniqueSubtotalChaos"], 5.0)

    def test_selection_and_display_use_the_identical_resolver(self):
        listings = {"Example": [{"chaos": 20, "variant": None, "links": 0},
                                {"chaos": 300, "variant": None, "links": 6},
                                {"chaos": 11, "variant": "Legacy", "links": 0}]}
        text = ("Rarity: UNIQUE\nExample\nVaal Regalia\nVariant: Legacy\nVariant: Current\n"
                "Selected Variant: 2\nSockets: B-B-B-B-B-B\nImplicits: 0")
        selected = unique_price(text, "Body Armour", market(listings))
        shown = price_unique_equipment([{**entry(links=6), "variant": None}], market(listings))["priced"][0]["chaos"]
        self.assertEqual(selected, shown)
        self.assertEqual(selected, 300.0)
        self.assertEqual(entry_from_text(text, "Body Armour")["links"], 6)
        legacy_variant = text.replace("Selected Variant: 2", "Selected Variant: 1").replace("B-B-B-B-B-B", "B-B")
        self.assertEqual(unique_price(legacy_variant, "Body Armour", market(listings)), 11.0)
        self.assertEqual(resolve_unique_text(text, "Body Armour", market(listings))["matchKind"], "variant+6-link")

    def test_package_price_is_unknown_when_any_member_is_unquoted(self):
        listings = {"A": [{"chaos": 4}]}
        total, quotes = unique_package_price([entry(slot="Ring 1", name="A", base="Gold Ring"),
                                              entry(slot="Boots", name="B", base="Serpentscale Boots")], market(listings))
        self.assertIsNone(total)
        self.assertEqual([quote["status"] for quote in quotes], ["quoted", "unquoted"])
        total, _ = unique_package_price([entry(slot="Ring 1", name="A", base="Gold Ring")], market(listings))
        self.assertEqual(total, 4.0)

    def test_freshness_is_reported_without_changing_the_quote(self):
        quote = resolve_unique(entry(slot="Ring 1", name="A", base="Gold Ring"), market({"A": [{"chaos": 4}]}),
                               now=1_000_000 + 3 * 24 * 3600)
        self.assertTrue(quote["stale"])
        self.assertEqual(quote["chaos"], 4.0)

    def test_old_saved_quotes_are_relabelled_unique_only(self):
        old = {"pricedSubtotalChaos": 102.0, "pricedSubtotalDivine": 0.31,
               "priced": [{"slot": "Boots", "name": "Mutewind Whispersteps", "chaos": 3.0}],
               "unknown": [{"slot": "Weapon 1", "name": "Prophecy Wand",
                            "reason": "Rare/magic item needs a modifier-aware trade search"},
                           {"slot": "Ring 1", "name": "Ixchel's Temptation", "reason": "Unique has no current-league quote"}]}
        upgraded = upgrade_legacy_quote(old)
        self.assertTrue(upgraded["legacy"])
        self.assertEqual([row["name"] for row in upgraded["unknown"]], ["Ixchel's Temptation"])
        self.assertEqual(upgraded["excluded"]["categories"][0]["count"], 1)
        self.assertEqual(upgraded["uniqueCount"], 2)
        current = {"schema": 2, "unknown": []}
        self.assertIs(upgrade_legacy_quote(current), current)


if __name__ == "__main__":
    unittest.main()
