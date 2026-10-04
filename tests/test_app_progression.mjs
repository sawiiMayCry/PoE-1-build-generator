import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
new vm.Script(source);
class Element {
  constructor(tag, className, value) { this.tag = tag; this.className = className; this.textContent = value; this.children = []; }
  append(...children) { this.children.push(...children); }
  add(child) { this.append(child); }
  replaceChildren(...children) { this.children = children; }
}
const sandbox = {el: (...args) => new Element(...args), fmt: String, Option: function(text, value) { this.text = text; this.value = value; }};
vm.createContext(sandbox);
vm.runInContext(source.slice(source.indexOf('const SLOT_ORDER'), source.indexOf('function defenseMetrics(')), sandbox);
const stages = [
  {title: 'Act 1 - Level 12', level: 12, passives: 13, passiveBudget: 13, ascendancyPoints: 0, gems: ['Summon Skeletons'],
   gemLevels: {'Summon Skeletons': 1}, stats: {Life: 400, EnergyShield: 50}, instructions: ['Use normal Skeletons'], gear: [{slot: 'Helmet', base: 'Vine Circlet'}]},
  {title: 'Endgame - Level 90', level: 90, passives: 113, passiveBudget: 113, ascendancyPoints: 8, gems: ['Vaal Summon Skeletons'],
   gemLevels: {'Vaal Summon Skeletons': 20}, stats: {Life: 4000, EnergyShield: 2000}, instructions: [], gear: [{slot: 'Helmet', base: 'Hubris Circlet'}]}
];
const card = sandbox.progressionCard({progression: stages});
const select = card.children.find(child => child.tag === 'select');
const content = card.children.find(child => child.className === 'stage-content');
const texts = node => [node.textContent || '', ...node.children.flatMap(texts)].join(' ');
assert.equal(select.value, '1');
assert.match(texts(content), /Vaal Summon Skeletons/);
assert.match(texts(content), /Hubris Circlet/);
select.value = '0'; select.onchange();
assert.match(texts(content), /13\/13 paid passives/);
assert.match(texts(content), /Vine Circlet/);
assert.doesNotMatch(texts(content), /Hubris Circlet/);
assert.equal(sandbox.progressionCard({}), null);
console.log('Progression selector and app.js syntax checks passed');
