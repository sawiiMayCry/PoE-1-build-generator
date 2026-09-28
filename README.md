# Witchcraft — local PoE 1 Witch build generator

Describe a Witch build in plain language. A local Ollama model chooses a current skill core, passive, support gem, and proposed equipment changes. Witchcraft constructs a new Path of Building (PoB) export, checks it with your installed PoB Community, keeps source gear when a swap breaks requirements, prices equipped items, and publishes the exact validated export to pobb.in.

## Start on Windows

Install and update [Path of Building Community](https://github.com/PathOfBuildingCommunity/PathOfBuilding), [Ollama](https://ollama.com/), and Python 3.10 or later. Pull a local model such as `ollama pull qwen2.5:7b` or `ollama pull gemma3:4b`. Start Ollama, then double-click `start.bat` or run `python server.py`. Open <http://127.0.0.1:4173>.

If Python is not on `PATH`, set `WITCHCRAFT_PYTHON` to its executable and run `start.bat`. `WITCHCRAFT_POB_HOME` overrides PoB detection; `WITCHCRAFT_OLLAMA_URL`, `WITCHCRAFT_MODEL`, and `PORT` configure Ollama and the local server. The browser assets have no CDN or npm dependencies.

Try prompts such as “zombie necromancer”, “tanky Bane of Condemnation Occultist”, or “cold Vortex Occultist with more damage”. The model selector chooses the local Ollama model, not a predefined build. The server has no four-build selection endpoint.

## Generation and validation

The generator assembles real PoB components from cached current-tree Witch references, plus official installed gem and tree data. The model plans the combination from the prompt; it cannot supply arbitrary passive, item, or gem IDs. Raise Zombie uses PoB's installed gem definition in a current Necromancer minion core. The available skill range depends on compatible source builds. If a named skill has no viable pattern, the app reports that instead of silently replacing it. Source PoBs are cached in `data/reference_cache/`; generated exports are saved in `data/generated/`.

Each candidate must use the current official passive tree and installed PoB definitions, fit its main skill into equipped sockets, preserve a complete gear set, and stay within passive and ascendancy limits. Witchcraft calculates the exact generated XML with PoB and checks health, elemental resistances, attribute requirements, and nonzero offense. Invalid plans are retried with failure feedback. These checks are a minimum viability gate, not a guarantee of optimized gameplay.

Passive limits use PoB's paid-point count, including bandit and secondary ascendancy effects. Some source exports contain a planned tree beyond their saved character level. Without a level in the prompt, the generator targets a level that supports that tree and one new passive choice, up to level 100, and shows the adjustment. An explicit `level 90` constraint is respected; a tree that needs a higher level is rejected. Resistance and attribute passives are preserved, and ignite setups receive compatible support choices.

The app reads the active PoE 1 trade league from [poe.ninja](https://poe.ninja/docs/api), passive nodes from [Grinding Gear Games](https://github.com/grindinggear/skilltree-export), and local game definitions from PoB Community. It stops if required current data is unavailable.

## Prices and sharing

Unique quotes and the divine-to-chaos rate come from current-league poe.ninja data. Rare and magic items generally have no reliable name-only quote, so the displayed figure is a priced subtotal. An explicit budget is rejected if that subtotal alone exceeds it; the full budget remains unverified while slots are unpriced. Without a stated budget, the result says “not specified”.

Publishing uploads the validated PoB code to [pobb.in](https://pobb.in), reads the public raw code back, and compares build mechanics. A public URL appears only after this succeeds. If sharing fails, the local result and PoB import code remain available with a Retry sharing action. Sharing exposes generated gear and configuration publicly; the written prompt is kept in the local result and is not embedded in the public PoB.

PoB calculation currently requires Windows and PoB's bundled `lua51.dll`. The server binds to localhost and rejects a second listener on the same port. Its API accepts requests only for the local host; changes require JSON from the local page.
