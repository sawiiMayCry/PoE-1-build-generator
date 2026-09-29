# Witchcraft - local PoE 1 Witch build generator

Describe a Witch build in plain language. Ollama interprets the request; deterministic code generates a fresh Path of Building export from current gem, passive and item definitions, scores candidates in your installed PoB Community, validates the exact result, prices equipment and shares it on pobb.in.

## Start on Windows

Install and update [Path of Building Community](https://github.com/PathOfBuildingCommunity/PathOfBuilding), [Ollama](https://ollama.com/) and Python 3.10 or later. Pull a local model, for example `ollama pull qwen2.5:7b`. Start Ollama, then double-click `start.bat` or run `python server.py`. Open <http://127.0.0.1:4173>.

If Python is not on PATH, set `WITCHCRAFT_PYTHON` to its executable. `WITCHCRAFT_POB_HOME` overrides PoB detection; `WITCHCRAFT_OLLAMA_URL`, `WITCHCRAFT_MODEL` and `PORT` configure Ollama and the server. Browser assets have no CDN or npm dependencies.

Try "ethereal knives elementalist, 20 div", "winter orb elementalist, tanky" or "zombie necromancer, level 89". Generation performs hundreds of real calculations and can take several minutes; the status panel reports progress.

## Generation

The API entry point remains `prompt_generator.generate`; it delegates to `real_generator.py`. Generation no longer loads the reference catalogue, copies a source build or requires a matching public skill pattern. Cached references remain available for development comparisons.

1. Check the current trade league, official passive tree release and installed PoB version. Production fails closed when required current data is unavailable or versions disagree.
2. Parse the prompt into a validated skill, ascendancy, level, budget and focus. Explicit names and numeric constraints override model replies. Invalid model identifiers/JSON receive deterministic fallbacks. The default target level is 90; explicit levels 80-100 are preserved.
3. Generate rare item templates from installed bases and explicit modifiers. Respect item eligibility, tier level, modifier groups and the three-prefix/three-suffix limits. Allocate resistance and attribute suffixes against measured PoB deficits.
4. Generate connected paths from the Witch start and allocate up to eight ascendancy points using curated archetype priorities. Rank passive clusters with stat-text hints, then score shortlisted paths with PoB's `GetMiscCalculator` overrides. Use PoB's actual paid-point count, including masteries and effects granting extra points.
5. Greedily score compatible supports for a five- or six-link. Compatibility uses PoB's skill-type expressions, including added/excluded types and minion rules. Search ordinary level-20 gems; exceptional, awakened, trigger and life-sacrifice setups need dedicated recipes. Utility skills come from archetype tables and explicit requests.
6. Score unlocked mastery effects, compare a small pool of affordable current uniques, and use remaining points for measured improvements/repairs. Recalculate after links introduce new requirements.
7. Assemble clean candidate XML with `build_assembly.py`, serialize it with PoB's own exporter (including saved stats and passive-tree URL), and reimport it to verify unchanged calculations. Validate, fingerprint and publish exactly that exported XML.
8. Build matching Tree, Skills, Items and Configuration loadouts: Act 1 start, Acts 1-10, Mapping at level 75, and Endgame at the requested level. Use plain matching titles so PoB and pobb.in associate the stages without brace codes. Select Endgame by default. Serialize and validate the complete export before publishing.

`build_progression.py` uses connected subsets of the generated endgame tree, cumulative campaign quest budgets and Labyrinth unlocks (0/2/4/6/8 ascendancy points). It generates lower-level rare bases and affix tiers, level-legal gems, campaign three/four-links and a five-link for Mapping. Campaign stages use a normal skill or a named starter rather than requiring Vaal/transfigured drops. Kitava penalties are explicit per configuration set. Flask bases are included with effects disabled. Every stage is calculated in PoB, then recalculated from the merged export to check that equipment IDs and active sets still match.

The app has a progression dropdown showing each stage's links, gem levels, equipment and calculated defenses. Exported PoB Notes explain gem sources, skill transitions, bandits, Labyrinth timing, flask use, resistance changes and mapping upgrades. Act levels are end-of-act checkpoints; PoB has one global character level, so change that level to the selected stage's label when checking campaign calculations in the desktop app.

`pob_engine.PobWorker` retains one Lua state and loads PoB once. Each request imports fresh XML; candidate support batches change gems inside that imported build, and node batches use PoB's calculation overrides. All operations share a process lock, restore environment/cwd afterward and reset the Lua state on error. Worker shutdown closes the state.

## Validation and limits

Checks cover class, level, installed gem/item definitions, linked sockets, utility socket capacity, connected allocated paths, legal mastery effects, legal rare affixes, compatible supports, PoB passive/ascendancy limits, life plus ES >= 3,000, nonzero offense, capped elemental resistances, attribute requirements and enough unreserved mana for a main-skill use.

These are minimum viability checks. They do not establish sustained resource recovery, ailment immunity, chaos resistance, boss uptime or gameplay quality. Generated rares are modifier targets to acquire, rather than market listings. The curated unique pool is small and the greedy search can miss stronger combinations. Chaos Inoculation, low-life and ward-loop requests currently fail with a specific recipe limitation. Other specialized mechanics may fail validation and need additional recipes. Unspecified buffs, charges, flasks and custom damage modifiers are not enabled. Permanent zombie totals use PoB's calculated summon limit; temporary summons need further uptime modelling.

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

```powershell
python inspect_generation.py
python verify_generation.py "ethereal knives elementalist, 20 div" --snapshot
python benchmark_pob.py data/verification/ethereal_knives.xml --iterations 5
$env:WITCHCRAFT_TEST_POB_XML = "data/verification/ethereal_knives.xml"
python -m unittest discover -s tests -v
```

The benchmark compares warm calculation outputs with the original cold engine. Pure progression tests check quest/lab budgets, gem unlocks, low-level affix limits, connected allocation order and loadout item-ID remapping. Opt-in real-engine tests check alternating builds, concurrent callers, export roundtrips, error recovery and restoration of process state. Verification XML and reports are written to `data/verification/` (ignored by Git).

PoB calculation requires Windows and PoB's bundled `lua51.dll`. The server binds to localhost, rejects a second listener on the same port and accepts changes only as JSON from the local page.
