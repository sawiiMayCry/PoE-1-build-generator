import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
new vm.Script(source);
class Element {
  constructor(tag, className, value) { this.tag = tag; this.className = className || ''; this.textContent = value; this.children = []; }
  append(...children) { this.children.push(...children); }
  add(child) { this.append(child); }
  replaceChildren(...children) { this.children = children; }
}
const sandbox = {el: (...args) => new Element(...args), fmt: value => Number(value || 0).toLocaleString('en-US', {maximumFractionDigits: 1}),
  Option: function(text, value) { this.text = text; this.value = value; }, Date};
vm.createContext(sandbox);
vm.runInContext(source.slice(source.indexOf("const SLOT_ORDER"), source.indexOf("function defenseMetrics(")), sandbox);
const texts = node => [node.textContent || node.text || '', ...(node.children || []).flatMap(texts)].join(' ');
const classes = node => [node.className, ...(node.children || []).flatMap(classes)];

// 1. Older saved results: a "validated" badge over known gaps is downgraded and labelled.
const legacy = {qualityStatus: 'validated', completeness: {status: 'gaps', gaps: ['2 unused main-link socket(s)']},
  encounterReadiness: {gaps: ['chaos resistance is -52% (0% target)', '2 unused main-link socket(s)']},
  mechanicChecks: [{passed: true}], gems: ['Winter Orb', 'Arcane Surge'],
  quote: {pricedSubtotalChaos: 102, priced: [{slot: 'Boots', name: 'Mutewind Whispersteps', chaos: 3}],
    unknown: [{slot: 'Weapon 1', name: 'Prophecy Wand', reason: 'Rare/magic item needs a modifier-aware trade search'},
      {slot: 'Ring 1', name: "Ixchel's Temptation", reason: 'Unique has no current-league quote'}]}};
const old = sandbox.normalizeBuild(legacy);
assert.equal(old.status, 'experimental');
assert.equal(old.gaps.length, 2);
assert.equal(old.legacyNotes.length, 3);
assert.equal(sandbox.qualityLabel('experimental'), 'Experimental recipe');
const oldPrice = sandbox.priceSummary(legacy.quote);
assert.equal(oldPrice.unknown.length, 1);               // the rare is not a missing unique quote
assert.equal(oldPrice.unknown[0].name, "Ixchel's Temptation");
assert.equal(oldPrice.excluded[0].count, 1);
assert.equal(oldPrice.count, 2);

// 2. The combined report gates the label: every dimension must pass.
const report = {status: 'validated', legality: 'pass', mechanics: 'pass', completeness: 'complete',
  encounter_readiness: 'ready', gaps: [], repairs: []};
assert.equal(sandbox.normalizeBuild({qualityReport: report, loadout: {}, quote: {schema: 2}}).status, 'validated');
assert.equal(sandbox.normalizeBuild({qualityReport: {...report, completeness: 'incomplete', gaps: ['main link has 4 of 6 gems']},
  loadout: {}, quote: {schema: 2}}).status, 'experimental');
assert.equal(sandbox.normalizeBuild({qualityReport: {...report, encounter_readiness: 'not_ready'}, loadout: {}, quote: {schema: 2}}).status, 'experimental');
assert.equal(sandbox.normalizeBuild({qualityReport: {...report, status: 'failed', legality: 'fail'}, loadout: {}, quote: {schema: 2}}).status, 'failed');
assert.equal(sandbox.normalizeBuild({qualityReport: report, loadout: {}, quote: {schema: 2}}).legacyNotes.length, 0);

// 3. Per-slot skills: actual counts, unique names, links and spare sockets; item-granted skills stay separate.
const gem = (name, kind, level) => ({name, kind, level, quality: 0, enabled: true});
const loadout = {schema: 2, counts: {socketedGems: 17, socketedGroups: 6, supportedGroups: 5, itemGrantedSkills: 1, jewels: 1},
  groups: [{index: 1, slot: 'Body Armour', isMain: true, mainActive: 'Winter Orb', label: 'Main',
    gems: [gem('Winter Orb', 'active', 20), gem('Arcane Surge', 'support', 20), gem('Cruelty', 'support', 20)]},
    {index: 2, slot: 'Boots', isMain: false, mainActive: 'Shield Charge', label: 'Movement',
      gems: [gem('Shield Charge', 'active', 20), gem('Faster Attacks', 'support', 20)]}],
  slots: {Boots: {item: {name: 'Mutewind Whispersteps', base: 'Serpentscale Boots'}, isUnique: true, sockets: 4,
      linkedRuns: [2, 1], used: 2, spare: 1, groups: [2]},
    'Body Armour': {item: {name: 'Witchcraft Body Armour', base: 'Vaal Regalia'}, isUnique: false, sockets: 6,
      linkedRuns: [6], used: 3, spare: 3, groups: [1]}},
  itemGranted: [{index: 7, source: 'Explode', skills: ['EnemyExplode']}],
  jewels: [{node: '26725', name: 'Watcher\'s Eye', base: 'Prismatic Jewel', isUnique: true, lines: ['+10% to Cold Resistance']}]};
const card = sandbox.loadoutCard({loadout});
const cardText = texts(card);
assert.match(cardText, /17 socketed gems in 6 groups/);
assert.match(cardText, /5 supported groups/);
assert.match(cardText, /1 item-granted skill \(not counted as gems\)/);
assert.match(cardText, /Mutewind Whispersteps/);
assert.match(cardText, /Serpentscale Boots/);
assert.match(cardText, /3\/6 sockets used · 6L · 3 spare/);
assert.match(cardText, /2\/4 sockets used · 2L · 1L · 1 spare/);
assert.match(cardText, /Winter Orb 20/);
assert.match(cardText, /EnemyExplode/);
assert.ok(classes(card).includes('unique-name'));
const order = card.children.find(child => child.className === 'slot-grid').children.map(slot => texts(slot).trim().split(/\s+/)[0]);
assert.deepEqual(order.slice(0, 2), ['Body', 'Boots']);     // Body Armour renders before Boots
assert.match(texts(sandbox.loadoutCard({gems: ['Winter Orb']})), /predates per-slot skill reports/);

// 4. Jewels with locations; no jewels and old saves are stated explicitly.
const jewels = sandbox.jewelsCard({loadout});
assert.match(texts(jewels), /Watcher's Eye/);
assert.match(texts(jewels), /passive socket 26725/);
assert.match(texts(sandbox.jewelsCard({loadout: {jewels: []}})), /No jewels are equipped/);
assert.match(texts(sandbox.jewelsCard({})), /not saved with this older result/);

// 5. Unique-only prices with correct unknown / excluded wording.
const priceNone = sandbox.priceCard({}, {schema: 2, uniqueCount: 0, uniqueSubtotalChaos: 0, coverageStatus: 'No unique items equipped',
  budgetStatus: 'No budget stated', unknown: [], priced: [], excluded: {categories: [{category: 'rare equipment', count: 8, slots: []}]},
  excludedNote: 'Only unique equipment, jewels and flasks are priced.', divineChaos: 200, source: 'poe.ninja', updated: 1});
const noneText = texts(priceNone);
assert.match(noneText, /No unique items equipped/);
assert.doesNotMatch(noneText, /slots have no reliable quote/);
assert.match(noneText, /8 rare equipment/);
assert.match(noneText, /Only unique equipment, jewels and flasks are priced/);
const priceSome = sandbox.priceCard({}, {schema: 2, uniqueCount: 3, quotedUniqueCount: 2, uniqueSubtotalChaos: 43,
  uniqueSubtotalDivine: 0.22, coverageStatus: '2 of 3 unique items quoted; 1 unquoted', budgetStatus: 'unverified: unquoted uniques remain',
  priced: [{slot: 'Body Armour', name: "Kaom's Heart", chaos: 40, links: 6, confidence: 'high'}, {slot: 'Jewel 5', name: 'Jewel A', chaos: 3}],
  unknown: [{slot: 'Boots', name: 'Mystery', reason: 'No quote matches the equipped unique variant and links',
    detail: 'No 6-link quote for this link-priced unique'}], excluded: {categories: []}, divineChaos: 200, source: 'poe.ninja', updated: 1});
const someText = texts(priceSome);
assert.match(someText, /43 chaos unique subtotal/);
assert.match(someText, /2 of 3 unique items quoted/);
assert.match(someText, /Kaom's Heart · ≈ 40 chaos · 6L quote · high confidence/);
assert.match(someText, /Jewel 5: Jewel A/);
assert.match(someText, /Mystery — No 6-link quote/);
assert.match(someText, /unverified: unquoted uniques remain/);

// 5b. Standard five-divine budget: budget, spent, remaining and per-unique prices are shown.
const priceBudget = sandbox.priceCard({}, {schema: 2, uniqueCount: 2, quotedUniqueCount: 1, uniqueSubtotalChaos: 40,
  coverageStatus: '1 of 2 unique items quoted; 1 unquoted', budgetStatus: 'Standard budget 5 divine',
  priced: [{slot: 'Belt', name: 'Cheap Belt', chaos: 40}], unknown: [{slot: 'Amulet', name: 'Mystery', reason: 'No quote'}],
  excluded: {categories: []}, divineChaos: 327, source: 'poe.ninja', updated: 1,
  uniqueBudget: {budgetChaos: 1635, budgetDivine: 5, spentChaos: 203.5, remainingChaos: 1431.5, withinBudget: true,
    uniques: [{slot: 'Belt', name: 'Cheap Belt', chaos: 40, countedChaos: 40, assumed: false},
              {slot: 'Amulet', name: 'Mystery', chaos: null, countedChaos: 163.5, assumed: true}]}});
const budgetText = texts(priceBudget);
assert.match(budgetText, /Unique budget: 1,635 chaos \(5 divine\)/);
assert.match(budgetText, /spent 203.5/);
assert.match(budgetText, /remaining 1,431.5/);
assert.match(budgetText, /Amulet: Mystery � no quote, assumed 163.5 chaos/);
assert.doesNotMatch(budgetText, /OVER BUDGET/);

// 6. Readiness and gaps come from the authoritative report.
const gapBuild = {qualityReport: {...report, status: 'experimental', completeness: 'incomplete', encounter_readiness: 'not_ready',
  gaps: ['main link has 4 of 6 gems', 'chaos resistance is -52%'], repairs: ['complete the six-link']}, loadout, quote: {schema: 2}};
const gapText = texts(sandbox.gapsCard(gapBuild, sandbox.normalizeBuild(gapBuild)));
assert.match(gapText, /Completeness: incomplete/);
assert.match(gapText, /Encounter readiness: not_ready/);
assert.match(gapText, /main link has 4 of 6 gems/);
assert.match(gapText, /complete the six-link/);
assert.match(texts(sandbox.gapsCard({}, sandbox.normalizeBuild({}))), /Saved before the combined readiness report/);

// 7. Progression stages show unique names, groups, jewels and stage-specific unique prices.
const stage = {title: 'Mapping - Level 75', level: 75, passives: 90, passiveBudget: 90, ascendancyPoints: 6,
  gems: ['Winter Orb', 'Arcane Surge'], gemLevels: {'Winter Orb': 19, 'Arcane Surge': 19},
  stats: {Life: 3000, EnergyShield: 2000}, instructions: ['Mapping assumes a six-linked body armour'],
  gear: [{slot: 'Body Armour', name: "Kaom's Heart", base: 'Glorious Plate', rarity: 'unique', isUnique: true},
    {slot: 'Helmet', name: 'Witchcraft Helmet', base: 'Hubris Circlet', rarity: 'rare'}],
  skillGroups: [{index: 1, slot: 'Body Armour', gems: [{name: 'Winter Orb', level: 19}, {name: 'Arcane Surge', level: 19}]}],
  socketedGemCount: 2, jewels: [{node: '61', name: 'Jewel A', base: 'Cobalt Jewel'}],
  priceCoverage: {schema: 2, uniqueCount: 1, uniqueSubtotalChaos: 40, coverageStatus: 'All 1 unique items quoted', unknown: [], priced: []}};
const progression = sandbox.progressionCard({progression: [stage]});
const progressText = texts(progression);
assert.match(progressText, /Body Armour: Kaom's Heart \(Glorious Plate\)/);
assert.match(progressText, /Helmet: Hubris Circlet/);
assert.doesNotMatch(progressText, /Witchcraft Helmet/);
assert.match(progressText, /2 socketed gems in 1 group/);
assert.match(progressText, /node 61: Jewel A/);
assert.match(progressText, /40 chaos unique subtotal/);

// 7b. Readiness card shows the Pantheon choice, chaos repair and the packages placed on spare sockets.
const readinessBuild = {qualityReport: {legality: 'pass', mechanics: 'pass', completeness: 'complete', encounter_readiness: 'ready',
    gaps: [], repairs: [], status: 'validated'}, loadout: {}, quote: {schema: 2},
  recipe: {constraints: {pantheon: {major: 'TheBrineKing', minor: 'Shakari'},
      pantheonSelection: {majorBy: 'rule', minorBy: 'PoB effective hit pool'},
      chaosRepair: {before: -36, after: 12, floor: 0}},
    skillPlanSummary: {omissions: [], fill: [{gems: ['Phase Run', 'Faster Casting'], evidence: 'role', function: 'second movement skill'}]}}};
const readinessText = texts(sandbox.gapsCard(readinessBuild, sandbox.normalizeBuild(readinessBuild)));
assert.match(readinessText, /Pantheon: Soul of Brine King \(major\) \+ Soul of Shakari \(minor\)/);
assert.match(readinessText, /Chaos resistance repair: -36% . 12% \(floor 0%\)/);
assert.match(readinessText, /Phase Run \+ Faster Casting \(role-justified\): second movement skill/);

// 8. app.js renders every card from the source with the DOM render path syntactically intact.
assert.match(source, /gapsCard\(build, normal\)/);
console.log('Agent 2 UI regressions passed');
