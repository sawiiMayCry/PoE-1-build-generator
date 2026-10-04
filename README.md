# Witchcraft - local PoE 1 Witch build generator

Describe a Witch build in plain language. Ollama interprets the request; deterministic code generates a fresh Path of Building export from current gem, passive and item definitions, scores candidates in your installed PoB Community, validates the exact result, prices equipment and shares it on pobb.in.

## Project layout

- `server.py` and `start.bat` start the local app; the Python modules beside them implement generation, validation, pricing and service access.
- `web/` holds the served page, JavaScript and CSS. The browser paths remain `/`, `/app.js` and `/app.css`.
- `web/archive/` holds earlier stylesheets that the current page does not load.
- `pob_bridge/` contains the Lua bridge to Path of Building.
- `tests/` contains regression checks and fixtures; `data/` contains local snapshots and generated builds.

## Start on Windows

Install and update [Path of Building Community](https://github.com/PathOfBuildingCommunity/PathOfBuilding), [Ollama](https://ollama.com/) and Python 3.10 or later. Pull a local model, for example `ollama pull qwen2.5:7b`. Start Ollama, then double-click `start.bat` or run `python server.py`. Open <http://127.0.0.1:4173>.

If Python is not on PATH, set `WITCHCRAFT_PYTHON` to its executable. `WITCHCRAFT_POB_HOME` overrides PoB detection; `WITCHCRAFT_OLLAMA_URL`, `WITCHCRAFT_MODEL` and `PORT` configure Ollama and the server. Browser assets have no CDN or npm dependencies.

Try "ethereal knives elementalist, 20 div", "winter orb elementalist, tanky" or "zombie necromancer, level 89". Generation performs hundreds of real calculations and can take several minutes; the status panel reports progress.

## Generation

The API entry point remains `prompt_generator.generate`; it delegates to `real_generator.py`. Generation no longer loads the reference catalogue, copies a source build or requires a matching public skill pattern. Cached references remain available for development comparisons.

1. Check the current trade league, official passive tree release and installed PoB version. Production fails closed when required current data is unavailable or versions disagree.
2. Parse the prompt into a validated skill, ascendancy, level, budget and focus. Explicit names and numeric constraints override model replies. Invalid model identifiers/JSON receive deterministic fallbacks. The default target level is 90; explicit levels 80-100 are preserved.
3. Generate rare item templates from installed bases and explicit modifiers. Respect item eligibility, tier level, modifier groups and the three-prefix/three-suffix limits. Allocate resistance and attribute suffixes against measured PoB deficits.
4. Generate connected paths from the Witch start and allocate up to eight ascendancy points using curated archetype priorities. Rank passive clusters with stat-text hints, score all shortlisted paths with PoB's `GetMiscCalculator` overrides, and prune neutral leaves. Use PoB's actual paid-point count, including masteries and effects granting extra points.
5. Greedily score compatible supports for a five- or six-link. Compatibility uses PoB's skill-type expressions, including added/excluded types and minion rules. Search ordinary level-20 gems; exceptional, awakened, trigger and life-sacrifice setups need dedicated recipes. Utility skills come from archetype tables and explicit requests.
6. Score unlocked mastery effects, legal rare jewel packages in reachable ordinary sockets, and current unique definitions across equipment slots, including flasks. Optional unique comparisons are bounded per slot; explicit compatible requests are required. A budgeted shortlist also compares up to 14 two-item equipment packages in complete PoB calculations, including pairs whose individual items do not improve the all-rare setup. Direct-effect unique jewels can use ordinary allocated sockets. Radius and passive-tree transformation jewels fail with a specific unsupported-mechanic error. Requested unique flasks are equipped and preserved through the final progression loadout; flask effects stay disabled in conservative calculations because permanent uptime is not modeled, and the build is warned about this limitation.
7. Assemble clean candidate XML with `build_assembly.py`, serialize it with PoB's own exporter (including saved stats and passive-tree URL), and reimport it to verify unchanged calculations. Validate, fingerprint and publish exactly that exported XML.
8. Build matching Tree, Skills, Items and Configuration loadouts: Act 1 start, Acts 1-10, Mapping at level 75, and Endgame at the requested level. Use plain matching titles so PoB and pobb.in associate the stages without brace codes. Select Endgame by default. Serialize and validate the complete export before publishing.

`build_progression.py` uses connected subsets of the generated endgame tree, cumulative campaign quest budgets and Labyrinth unlocks (0/2/4/6/8 ascendancy points). It generates lower-level rare bases and affix tiers, level-legal gems, campaign three/four-links and a five-link for Mapping. Campaign stages use a normal skill or a named starter rather than requiring Vaal/transfigured drops. Kitava penalties are explicit per configuration set. Flask bases are included with effects disabled, and a selected endgame unique flask is retained when the completed loadout is assembled. Every stage is calculated in PoB, then recalculated from the merged export to check that equipment IDs and active sets still match.

The app has a progression dropdown showing each stage's links, gem levels, equipment and calculated defenses. Exported PoB Notes explain gem sources, skill transitions, bandits, Labyrinth timing, flask use, resistance changes and mapping upgrades. Act levels are end-of-act checkpoints; PoB has one global character level, so change that level to the selected stage's label when checking campaign calculations in the desktop app.

## Development diagnostics

Run `python diagnose_generation.py --prompt "Level 90 Summon Raging Spirit Necromancer, 2000 chaos"` to build from the saved `data/development_context.json`, `data/development_market.json` and `data/pob_metadata.json` snapshots. Pass `--expected-pob-version` to require an exact installed PoB version, or override the snapshot paths with `--context`, `--market` and `--metadata`. The runner writes a local XML and JSON candidate trace under `data/diagnostics/` and never publishes. Diagnostics require the same installed PoB version and matching game/market snapshots; regenerate snapshots when those change.

Builds without completed mechanic-specific acceptance checks are labeled **Experimental recipe**. The generator remains open to installed Witch main skills outside the initial mechanic profiles, subject to the requested skill, supported equipment model and mandatory PoB legality checks. Unsupported required interactions fail explicitly.

`pob_engine.PobWorker` retains one Lua state and loads PoB once. Each request imports fresh XML; candidate support batches change gems inside that imported build, and node batches use PoB's calculation overrides. All operations share a process lock, restore environment/cwd afterward and reset the Lua state on error. Worker shutdown closes the state.

## Validation and limits

Checks cover class, level, installed gem/item definitions, linked sockets, utility socket capacity, connected allocated paths, legal mastery effects, legal rare affixes, compatible supports, PoB passive/ascendancy limits, life plus ES >= 3,000, nonzero offense, capped elemental resistances, attribute requirements and enough unreserved mana for a main-skill use.

These are minimum legality and viability checks, not a gameplay-quality guarantee. Generated rares are modifier targets to acquire, rather than market listings. The optimizer has a shared 2,000-evaluation budget and runs up to three width-six complete-design refinement sweeps across gear, links and passive paths. Of the budget remaining when those sweeps begin, gear may use up to 15%, link alternatives 25%, and passive refinement 60%; up to 20 additional evaluations are reserved to test legal Clarity levels when mana sustain needs repair. Earlier searches can still leave a small remainder, which is reported as an experimental quality warning. Candidate scores discount damage by the lowest sustained life or mana coverage, matching the mechanic quality check. Direct-stat two-item unique packages are compared, but interaction-specific mechanics beyond PoB's modeled item effects are unsupported. Unique flask items can be requested and retained, but their effects are disabled because flask uptime is not modeled. Chaos Inoculation, low-life and ward-loop requests fail with a specific recipe limitation. Unspecified buffs, charges, flasks and custom damage modifiers are not enabled. Zombie limits are recalculated after relevant candidate changes. For SRS, the reported temporary population is conservatively estimated from PoB duration, cast rate and mana regeneration, then capped by the skill limit; mana leech and on-kill recovery are not included. Mechanic profiles stay experimental until their resource and regression gates pass.

PoB stats are the only numerical claims shown for a generated result. The local model interprets intent and cannot author item mods, passive IDs, prices or output stats.

## Prices and sharing

Unique quotes and divine-to-chaos conversion come from the current-league poe.ninja API. Rare templates and gems lack reliable modifier-aware quotes, so budgets remain unverified when those costs are unknown. Reject an explicit budget if the priced equipment subtotal alone exceeds it. Without a stated budget, report "not specified".

The publishing flow uploads the validated PoB code to [pobb.in](https://pobb.in), reads it back and compares mechanics fingerprints. Generated results are saved in `data/generated/`. If sharing fails, the local result and import code remain available with Retry sharing. Retry also repairs earlier minimal exports that lack saved stats by exporting through PoB and revalidating. HTTP 400 reports a format rejection rather than suggesting that waiting will fix the same payload; readable service error details are retained. The prompt stays in the local result and is absent from public PoB XML.

## Development checks

Run pure regression tests:

```powershell
python -m unittest discover -s tests -v
```

Run a local generation comparison without publishing; add `--model` to test Ollama intent parsing:

```powershell
python verify_generation.py "winter orb elementalist, tanky" --model qwen2.5:7b --reference data/reference_cache/h8klSvqllefw.xml
```

`inspect_generation.py` writes explicitly version-checked development snapshots of installed definitions, context and market data. Only the developer CLI's `--snapshot` option uses these snapshots; production generation continues to require current data.

`diagnose_generation.py --prompt "Level 90 Summon Raging Spirit Necromancer, 2000 chaos" --expected-pob-version 2.67.2` records a non-publishing candidate trace and exact PoB export. Startup also compares Witch-relevant official node IDs, connections and mastery effects with the installed PoB tree.

`compare_builds.py BEFORE.xml AFTER.xml --level 90 --skill "Summon Raging Spirit" --summon-count 2` calculates both exports in installed PoB with the same selected stage, enemy level, conditional-config inputs and summon count. Its JSON separates offense, defenses, passive-point use and quote coverage.

```powershell
python inspect_generation.py
python verify_generation.py "ethereal knives elementalist, 20 div" --snapshot
python benchmark_pob.py data/verification/ethereal_knives.xml --iterations 5
$env:WITCHCRAFT_TEST_POB_XML = "data/verification/ethereal_knives.xml"
python -m unittest discover -s tests -v
```

The benchmark compares warm calculation outputs with the original cold engine. Pure progression tests check quest/lab budgets, gem unlocks, low-level affix limits, connected allocation order and loadout item-ID remapping. Opt-in real-engine tests check alternating builds, concurrent callers, export roundtrips, error recovery and restoration of process state. Verification XML and reports are written to `data/verification/` (ignored by Git).

PoB calculation requires Windows and PoB's bundled `lua51.dll`. The server binds to localhost, rejects a second listener on the same port and accepts changes only as JSON from the local page.

## Skills, links, unique pricing and the app report

- **Skill groups** (`skill_packages.py`, `skill_planner.py`): the planner builds a complete six-gem main link
  (`plan_main_link`, filler-free, resource deficits reported as repairs rather than dropping supports) and
  supporting groups (movement, guard, reservation, defense, herald, curse delivery, minion helpers) packed onto
  real linked sockets. Each package is justified by PoB stats or a declared role; omissions carry reasons.
  `build_assembly.assemble(..., skill_groups=...)` serializes groups per instance (level/quality/enabled),
  validates physical sockets (`SocketConflict`) and never emits item-granted skills.
- **Counts** come only from the final XML (`loadout_summary.py`); item-granted skills such as `EnemyExplode`
  are reported separately from socketed gems.
- **Prices** (`unique_pricing.py`): one resolver for selection and display. Only uniques (equipment, jewels,
  flasks) are priced; rares, gems and link crafting are excluded scope, not missing quotes. A link-priced
  six-link needs an actual six-link quote; variants and bases never fall back to a cheaper listing.
- **Progression**: Mapping and Endgame are six-links; stage summaries (gear, groups, jewels, unique prices) are
  derived from each stage's exported XML. Chaos Inoculation is deferred to the Endgame respec.
- `python validate_skill_packages.py data/generated/<id>.json` re-plans a saved build with installed PoB.
