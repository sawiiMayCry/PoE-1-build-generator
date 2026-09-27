# Witchcraft — Path of Exile Witch Build Planner

Small no-dependency Node.js app for current-league Witch build references, PoB passive-tree previews, and live unique-item market estimates.

## Run it on Windows

Double-click [`start.bat`](start.bat). It starts the local app and opens it in your browser at <http://127.0.0.1:4173>. Keep the command window open while using the planner; closing it stops the local server.

## Run it on another computer or operating system

Requires Node.js 18 or newer.

```sh
node server.js
```

Then open <http://127.0.0.1:4173>. The local server serves the app and proxies/caches the supported poe.ninja economy endpoints. Price data is cached for five minutes. The passive tree is read from GGG's public skill-tree export, and allocations are decoded from the public Path of Building exports.

The project folder can be copied to another computer. Install Node.js 18 or newer there and run `node server.js` (or double-click `start.bat` on Windows). Nothing is deployed or shared automatically.

## Data and limits

- Curated Witch builds are league-specific 3.29 Curse of the Allflame guides. Verify the selected variant and patch before spending currency if the game has moved to another league; the app flags when its market league no longer matches these references.
- Unique item prices and the Divine Orb exchange rate come from poe.ninja's public economy endpoints. They are market estimates, not guaranteed trade listings.
- Rare items are shown with their actual base types when available, but are not assigned fabricated prices. Their value depends on the rolled modifiers. The budget cap compares prices for the listed uniques and calls out when the result is incomplete.
- The tree preview highlights the active allocation from each linked PoB export against GGG's skill-tree data. The linked guide/PoB remains the source for leveling trees, item sets, and gem variants.
- poe.ninja's supported public API is its economy API; this app does not call or scrape its private builds API.

## Current build references

- Elementalist: [Ronarray's 3.29 Ethereal Knives Ignite guide](https://mobalytics.gg/poe/builds/ignite-ethereal-knives-elementalist-build-league-starter-to-endgame), [progression PoB](https://pobb.in/eOtcfO47VWsG)
- Necromancer: [BalorMage's 3.29 Poison SRS guide](https://www.poe-vault.com/guides/balormage-summon-raging-spirits-necromancer-build-guide), [Path of Building export](https://pobb.in/7_6IS6EVRZfT)
- Occultist: [Ronarray's 3.29 Winter Orb guide](https://mobalytics.gg/poe/profile/ronarray/builds/3-29-winter-orb-occultist-witch-build-from-league-starter-to-ubers), [Path of Building export](https://pobb.in/8cQ3YmPIEgrg)
