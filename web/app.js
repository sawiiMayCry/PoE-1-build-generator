const $ = id => document.getElementById(id);
const form = $("generator-form");
let busy = false, ready = false;
const fmt = value => Number(value || 0).toLocaleString(undefined, {maximumFractionDigits: 1});
function el(tag, className, value) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (value !== undefined) result.textContent = String(value);
  return result;
}
function showError(message) { $("error").textContent = message || ""; $("error").hidden = !message; }
function loading(value, stage) {
  busy = value;
  $("generate-button").disabled = value || !ready;
  $("progress").hidden = !value;
  $("progress-text").textContent = stage || "";
}
async function api(url, options) {
  const response = await fetch(url, options);
  const type = response.headers.get("content-type") || "";
  const data = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(data?.error || `Request failed (${response.status})`);
  return data;
}
const post = (url, body) => api(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
function metric(label, value) {
  const block = el("div", "metric");
  block.append(el("span", "metric-label", label), el("strong", "", value));
  return block;
}
const SLOT_ORDER = ["Body Armour", "Helmet", "Gloves", "Boots", "Weapon 1", "Weapon 2", "Amulet", "Ring 1", "Ring 2", "Belt"];
const slotRank = slot => { const index = SLOT_ORDER.indexOf(slot); return index < 0 ? 99 : index; };
const plural = (count, one, many) => `${fmt(count)} ${count === 1 ? one : (many || one + "s")}`;
const isRareOrderLegacy = row => /Rare\/magic|modifier-aware/i.test(String(row?.reason || ""));

/* Older saved results lack some fields. Normalize once so every card can say
   plainly what is current and what predates the report. */
function normalizeBuild(build) {
  const report = build.qualityReport || null;
  const legacyNotes = [];
  if (!report) legacyNotes.push("Saved before the combined readiness report; the label below is derived from older fields and may be stricter than the original badge.");
  if (!build.loadout) legacyNotes.push("Saved before per-slot skill reports; only the main link is available. Regenerate to see every socket group.");
  if (!(build.quote && build.quote.schema >= 2)) legacyNotes.push("Saved with an earlier price schema; unique-only coverage is inferred from the old unpriced-slot list.");
  const gaps = report ? [...(report.gaps || [])] : [
    ...((build.completeness && build.completeness.gaps) || []),
    ...(((build.encounterReadiness && build.encounterReadiness.gaps) || []).filter(gap => !((build.completeness && build.completeness.gaps) || []).includes(gap)))];
  let status = report ? report.status : (build.qualityStatus || "unassessed");
  const dimensions = report ? {legality: report.legality, mechanics: report.mechanics,
    completeness: report.completeness, readiness: report.encounter_readiness} : {};
  // Never show "validated" over known gaps or a failed dimension.
  if (status === "validated") {
    const bad = report ? (report.legality !== "pass" || report.mechanics !== "pass" ||
      report.completeness !== "complete" || report.encounter_readiness !== "ready") : false;
    const legacyGaps = !report && (gaps.length || (build.completeness && build.completeness.status === "gaps") ||
      (build.mechanicChecks || []).some(check => !check.passed));
    if (bad || legacyGaps) status = "experimental";
  }
  return {gaps, status, dimensions, legacyNotes, repairs: report ? (report.repairs || []) : []};
}
function qualityLabel(status) {
  return ({validated: "Recipe validated", experimental: "Experimental recipe", failed: "Failed quality gate",
    unassessed: "Quality unassessed"})[status] || "Quality unassessed";
}
function priceSummary(quote) {
  const q = quote || {};
  const current = q.schema >= 2;
  const unknown = q.unknown || [];
  const rareLegacy = current ? [] : unknown.filter(isRareOrderLegacy);
  const uniquesUnknown = current ? unknown : unknown.filter(row => !isRareOrderLegacy(row));
  const priced = q.priced || [];
  const count = current ? (q.uniqueCount ?? priced.length + uniquesUnknown.length) : priced.length + uniquesUnknown.length;
  const excluded = current ? ((q.excluded && q.excluded.categories) || []) :
    (rareLegacy.length ? [{category: "rare equipment", count: rareLegacy.length, slots: rareLegacy.map(row => row.slot)}] : []);
  return {current, priced, unknown: uniquesUnknown, count, excluded,
    subtotal: q.uniqueSubtotalChaos ?? q.pricedSubtotalChaos ?? 0,
    divine: q.uniqueSubtotalDivine ?? q.pricedSubtotalDivine,
    noUniques: count === 0,
    status: current ? q.coverageStatus : (count === 0 ? "No unique items equipped" :
      `${priced.length} of ${count} unique items quoted`),
    budget: q.budgetStatus, policy: q.policy && q.policy.text};
}
function gemChip(gem) {
  const chip = el("span", `gem-chip ${gem.kind || "active"}${gem.enabled === false ? " disabled" : ""}`,
    `${gem.name}${gem.level ? " " + gem.level : ""}${gem.quality ? " q" + gem.quality : ""}`);
  chip.title = `${gem.kind === "support" ? "Support" : "Active"} gem${gem.enabled === false ? " (disabled)" : ""}`;
  return chip;
}
function groupBlock(group) {
  const block = el("div", `skill-group${group.isMain ? " main" : ""}`);
  const gems = group.gems || [];
  const head = el("div", "skill-group-head");
  head.append(el("strong", "", group.isMain ? `Main · ${group.mainActive || gems[0]?.name || ""}` :
    (group.label || group.mainActive || "Group")),
    el("span", "muted", gems.length > 1 ? `${gems.length}-link` : "1 gem"));
  const chips = el("div", "gem-chips");
  for (const gem of gems) chips.append(gemChip(gem));
  block.append(head, chips);
  return block;
}
function slotCard(slot, info, groups) {
  const card = el("div", "slot-card");
  const item = info.item || {};
  const title = el("div", "slot-title");
  title.append(el("span", "slot-name", slot),
    el("strong", info.isUnique ? "unique-name" : "", item.name && info.isUnique ? item.name : (item.base || "")),
    el("span", "muted", info.isUnique && item.base ? item.base : ""));
  const runs = (info.linkedRuns || []).length ? (info.linkedRuns || []).map(size => `${size}L`).join(" · ") : "no sockets";
  card.append(title, el("p", "muted socket-line",
    `${info.used}/${info.sockets} sockets used · ${runs}${info.spare ? ` · ${info.spare} spare` : ""}`));
  for (const group of groups) card.append(groupBlock(group));
  return card;
}
function loadoutCard(build) {
  const card = el("section", "detail-card loadout-card");
  card.append(el("h3", "", "Skills and links"));
  const loadout = build.loadout;
  if (!loadout) {
    card.append(el("p", "muted", `Main link: ${(build.gems || []).join(" · ")}`),
      el("p", "muted", "This result predates per-slot skill reports. Only the main link was saved; regenerate to see every socket group, spare sockets and jewels."));
    return card;
  }
  const counts = loadout.counts || {};
  card.append(el("p", "loadout-counts",
    `${plural(counts.socketedGems || 0, "socketed gem")} in ${plural(counts.socketedGroups || 0, "group")} · ` +
    `${plural(counts.supportedGroups || 0, "supported group")} · ` +
    `${plural(counts.itemGrantedSkills || 0, "item-granted skill")} (not counted as gems)`));
  const byIndex = Object.fromEntries((loadout.groups || []).map(group => [group.index, group]));
  const order = Object.entries(loadout.slots || {}).sort((a, b) => slotRank(a[0]) - slotRank(b[0]));
  const grid = el("div", "slot-grid");
  for (const [slot, info] of order) grid.append(slotCard(slot, info, (info.groups || []).map(index => byIndex[index]).filter(Boolean)));
  card.append(grid);
  if ((loadout.itemGranted || []).length) {
    const list = el("ul", "unknown-list");
    for (const entry of loadout.itemGranted) list.append(el("li", "", `${entry.source || "item"}: ${(entry.skills || []).filter(Boolean).join(", ") || "granted skill"}`));
    card.append(el("h4", "", "Item-granted skills (not socketed gems)"), list);
  }
  return card;
}
function jewelsCard(build) {
  const jewels = build.loadout?.jewels;
  const card = el("section", "detail-card jewels-card");
  card.append(el("h3", "", "Jewels"));
  if (!jewels) { card.append(el("p", "muted", "Jewel details were not saved with this older result.")); return card; }
  if (!jewels.length) { card.append(el("p", "muted", "No jewels are equipped in passive sockets.")); return card; }
  const list = el("ul", "jewel-list");
  for (const jewel of jewels) {
    const row = el("li", "");
    row.append(el("strong", jewel.isUnique ? "unique-name" : "", jewel.name), el("span", "muted", ` ${jewel.base} · passive socket ${jewel.node}`));
    if ((jewel.lines || []).length) row.append(el("div", "muted jewel-mods", jewel.lines.slice(0, 4).join(" · ")));
    list.append(row);
  }
  card.append(list);
  return card;
}
function gapsCard(build, normal) {
  const card = el("section", "detail-card gaps-card");
  card.append(el("h3", "", "Readiness"));
  const report = build.qualityReport;
  if (report) {
    const chips = el("div", "dimension-chips");
    for (const [name, value] of Object.entries({Legality: report.legality, Mechanics: report.mechanics,
      Completeness: report.completeness, "Encounter readiness": report.encounter_readiness})) {
      const ok = ["pass", "complete", "ready"].includes(value);
      chips.append(el("span", `dimension-chip ${ok ? "ok" : "gap"}`, `${name}: ${value || "unknown"}`));
    }
    card.append(chips);
  }
  if (normal.gaps.length) {
    const list = el("ul", "quality-warnings");
    for (const gap of normal.gaps) list.append(el("li", "", gap));
    card.append(el("h4", "", "Gaps"), list);
  } else card.append(el("p", "muted", "No completeness or readiness gaps were reported."));
  if (normal.repairs.length) {
    const list = el("ul", "selection-reasons");
    for (const repair of normal.repairs) list.append(el("li", "", repair));
    card.append(el("h4", "", "Recommended repairs"), list);
  }
  const summary = build.recipe?.skillPlanSummary;
  if (summary && (summary.omissions || []).length) {
    const list = el("ul", "selection-reasons");
    for (const omission of summary.omissions) list.append(el("li", "", `${omission.package}: ${omission.reason}`));
    card.append(el("h4", "", "Skill packages not used"), list);
  }
  const repair = build.recipe?.manaRepair || build.recipe?.constraints?.manaRepair;
  if (repair) {
    card.append(el("p", "muted", `Mana repair (${repair.applied ? "applied" : "not applied"}): ` +
      (repair.accepted || []).map(entry => entry.node).join(", ") +
      `${(repair.accepted || []).length ? " · " : ""}mana margin ${repair.marginBefore} → ${repair.marginAfter}`));
  }
  const constraints = build.recipe?.constraints || {};
  const godName = id => String(id || "").replace(/^TheBrineKing$/, "Brine King").replace(/([a-z])([A-Z])/g, "$1 $2");
  if (constraints.pantheon && (constraints.pantheon.major || constraints.pantheon.minor)) {
    const selection = constraints.pantheonSelection || {};
    card.append(el("p", "muted pantheon-line", `Pantheon: Soul of ${godName(constraints.pantheon.major)} (major) + ` +
      `Soul of ${godName(constraints.pantheon.minor)} (minor)` +
      (selection.majorBy ? ` · chosen by ${selection.majorBy}${selection.minorBy !== selection.majorBy ? " / " + selection.minorBy : ""}` : "")));
  }
  const chaos = constraints.chaosRepair;
  if (chaos) {
    card.append(el("p", "muted chaos-line", `Chaos resistance repair: ${chaos.before}% → ${chaos.after}% (floor ${chaos.floor}%)` +
      (chaos.reason ? ` · ${chaos.reason}` : "")));
  }
  const fills = (summary?.fill || []);
  if (fills.length) {
    const list = el("ul", "selection-reasons");
    for (const entry of fills) list.append(el("li", "", `${entry.gems.join(" + ")} (${entry.evidence === "stat" ? "PoB-measured" : "role-justified"}): ${entry.function}`));
    card.append(el("h4", "", "Packages placed on spare sockets"), list);
  }
  for (const note of normal.legacyNotes) card.append(el("p", "legacy-note", note));
  return card;
}
function priceCard(build, source) {
  const quote = source || build.quote || {};
  const price = priceSummary(quote);
  const card = el("section", "detail-card price-card");
  card.append(el("h3", "", "Unique item prices"));
  card.append(el("p", "price-total", price.noUniques ? "No unique items equipped" :
    `${fmt(price.subtotal)} chaos unique subtotal`));
  const sourceText = quote.source ? `${quote.source}${quote.updated ? " · updated " + new Date(quote.updated * 1000).toLocaleString() : ""}` : "";
  card.append(el("p", "muted", price.noUniques ? `Live rate: ${fmt(quote.divineChaos)} chaos/divine · ${sourceText}` :
    `${price.status}${price.divine ? ` · ≈ ${fmt(price.divine)} divine at ${fmt(quote.divineChaos)} chaos/divine` : ""} · ${sourceText}`));
  card.append(el("p", "budget-status", `Budget: ${price.budget || "unknown"}.`));
  const unique = quote.uniqueBudget;
  if (unique && unique.budgetChaos != null) {
    card.append(el("p", "budget-status unique-budget",
      `Unique budget: ${fmt(unique.budgetChaos)} chaos${unique.budgetDivine != null ? ` (${fmt(unique.budgetDivine)} divine)` : ""} � ` +
      `spent ${fmt(unique.spentChaos)} � remaining ${fmt(unique.remainingChaos)}` +
      (unique.withinBudget ? "" : " � OVER BUDGET")));
    const rows = (unique.uniques || []);
    if (rows.length) {
      const list = el("ul", "unknown-list unique-budget-list");
      for (const row of rows) list.append(el("li", "", `${row.slot}: ${row.name} � ${row.assumed ? "no quote, assumed " : ""}${fmt(row.countedChaos)} chaos${row.requested ? " � requested" : ""}`));
      card.append(el("h4", "", "Unique budget accounting"), list);
    }
  }
  if (price.policy) card.append(el("p", "budget-status muted", price.policy));
  card.append(el("p", "muted price-scope", price.current && quote.excludedNote ? quote.excludedNote :
    "Only unique items, unique jewels and unique flasks are priced. Rare items, gems and link/socket crafting are not priced and are not missing quotes."));
  if (price.priced.length) {
    const list = el("ul", "unknown-list");
    for (const item of price.priced) list.append(el("li", "", `${item.slot}: ${item.name}${item.variant ? " (" + item.variant + ")" : ""} · ≈ ${fmt(item.chaos)} chaos${item.links ? " · " + item.links + "L quote" : ""}${item.confidence && item.confidence !== "unknown" ? " · " + item.confidence + " confidence" : ""}`));
    card.append(el("h4", "", "Quoted uniques"), list);
  }
  if (price.unknown.length) {
    const list = el("ul", "unknown-list");
    for (const item of price.unknown) list.append(el("li", "", `${item.slot}: ${item.name} — ${item.detail || item.reason || "no reliable quote"}`));
    card.append(el("h4", "", "Unique items without a quote"), list);
  }
  if (price.excluded.length) {
    card.append(el("p", "muted", "Not priced by design: " + price.excluded.map(entry => `${entry.count} ${entry.category}`).join(", ") + "."));
  }
  return card;
}
function stageGearLine(item) {
  return item.isUnique || item.rarity === "unique" ? `${item.slot}: ${item.name} (${item.base})` : `${item.slot}: ${item.base}`;
}
function progressionCard(build) {
  if (!build.progression?.length) return null;
  const card = el("section", "detail-card progression-card");
  const label = el("label", "", "Progression stage");
  label.htmlFor = "progression-stage";
  const select = el("select"); select.id = "progression-stage";
  const content = el("div", "stage-content");
  for (const [index, stage] of build.progression.entries()) select.add(new Option(stage.title, String(index)));
  select.value = String(build.progression.length - 1);
  const update = () => {
    const stage = build.progression[Number(select.value)];
    content.replaceChildren();
    content.append(el("p", "muted", `Level ${stage.level} · ${stage.passives}/${stage.passiveBudget} paid passives · ${stage.ascendancyPoints} ascendancy points`),
      el("h4", "", "Main skill and links"),
      el("p", "", stage.gems.map(name => `${name} (${stage.gemLevels[name]})`).join(" → ")));
    if (stage.skillGroups?.length) {
      content.append(el("p", "loadout-counts", `${plural(stage.socketedGemCount || 0, "socketed gem")} in ${plural(stage.skillGroups.length, "group")}` +
        `${stage.supportedGroupCount ? ` · ${plural(stage.supportedGroupCount, "supported group")}` : ""}`));
      const groups = el("div", "stage-groups");
      for (const group of stage.skillGroups) {
        const row = el("p", "muted stage-group", `${group.slot || "?"}: ` + group.gems.map(gem => `${gem.name} ${gem.level ?? ""}`.trim()).join(" + "));
        groups.append(row);
      }
      content.append(groups);
    } else {
      const utilities = Object.entries(stage.gemLevels || {}).filter(([name]) => !stage.gems.includes(name));
      if (utilities.length) content.append(el("p", "muted", utilities.map(([name, level]) => `${name} (${level})`).join(" · ")));
    }
    content.append(el("p", "muted", `PoB life: ${fmt(stage.stats.Life || 0)} · energy shield: ${fmt(stage.stats.EnergyShield || 0)} · Fire / Cold / Lightning: ${["Fire", "Cold", "Lightning"].map(name => `${fmt(stage.stats[name + "Resist"])}%`).join(" / ")}`));
    for (const instruction of stage.instructions) content.append(el("p", instruction.startsWith("WARNING") ? "stage-warning" : "muted", instruction));
    const gear = el("ul", "unknown-list stage-gear");
    for (const item of stage.gear) gear.append(el("li", "", stageGearLine(item)));
    content.append(el("h4", "", "Equipment targets"), gear);
    if (stage.jewels?.length) {
      const jewels = el("ul", "unknown-list");
      for (const jewel of stage.jewels) jewels.append(el("li", "", `node ${jewel.node}: ${jewel.name} (${jewel.base})`));
      content.append(el("h4", "", "Jewels"), jewels);
    }
    if (stage.priceCoverage) {
      const price = priceSummary(stage.priceCoverage);
      content.append(el("h4", "", "Unique prices at this stage"),
        el("p", "muted", price.noUniques ? "No unique items equipped at this stage." :
          `${fmt(price.subtotal)} chaos unique subtotal · ${price.status}`));
      for (const item of price.unknown) content.append(el("p", "muted", `${item.slot}: ${item.name} — ${item.detail || item.reason}`));
    }
  };
  select.onchange = update; update();
  card.append(el("h3", "", "Campaign to endgame"),
    el("p", "muted", "Matching stages are included in the PoB tree, skill, equipment and configuration dropdowns. Each act is an end-of-act checkpoint. Numbers beside gems are gem levels. The PoB Notes tab contains the leveling and upgrade instructions."),
    label, select, content);
  return card;
}
function defenseMetrics(build) {
  const output = build.stats || {};
  const model = build.recipe?.defenseModel || build.recipe?.constraints?.defenseModel;
  const modelLabel = ({ci: "Chaos Inoculation", hybrid: "Life + energy shield", life: "Life", low_life: "Low life"})[model] || null;
  return {output, modelLabel};
}
function render(build) {
  localStorage.setItem("witchcraft-last-build", build.id);
  build.recipe = build.recipe || {};
  build.validation = build.validation || [];
  const root = $("result");
  root.replaceChildren();
  root.hidden = false;
  const normal = normalizeBuild(build);
  const heading = el("div", "result-heading"), title = el("div");
  const resultTitle = el("h2", "", `${build.mainSkill} ${build.ascendancy}`);
  resultTitle.id = "result-title";
  title.append(el("p", "eyebrow", "GENERATED BUILD"), resultTitle,
    el("p", "muted", `Level ${build.level} Witch · ${build.league} · tree ${build.treeVersion.replace("_", ".")} · PoB ${build.pobVersion}`));
  heading.append(title, el("span", `validation-pill quality-${normal.status}`, qualityLabel(normal.status)));
  root.append(heading);

  const share = el("div", build.shareStatus === "published" ? "share-card published" : "share-card pending");
  share.append(el("span", "share-kicker", build.shareStatus === "published" ? "YOUR PUBLIC POB" : "SHARING NEEDS A RETRY"));
  if (build.shareUrl) {
    const link = el("a", "share-link", build.shareUrl + " ↗");
    link.href = build.shareUrl; link.target = "_blank"; link.rel = "noopener noreferrer";
    share.append(link);
    const copy = el("button", "small-button", "Copy link");
    copy.type = "button";
    copy.onclick = async () => { await navigator.clipboard.writeText(build.shareUrl); copy.textContent = "Copied"; };
    share.append(copy);
  } else {
    share.append(el("p", "", build.shareError || "The build passed PoB checks and was saved locally. Retry publication when pobb.in is available."));
    const retry = el("button", "small-button", "Retry sharing");
    retry.type = "button";
    retry.onclick = async () => {
      retry.disabled = true; showError(""); loading(true, "Recalculating the saved export and publishing…");
      try { render(await post("/api/share", {id: build.id})); }
      catch (cause) {
        showError(cause.message);
        try { render(await api(`/api/build?id=${encodeURIComponent(build.id)}`)); } catch {}
      } finally { loading(false); }
    };
    share.append(retry);
  }
  const code = el("a", "code-link", "Get the PoB import code");
  code.href = `/api/export?id=${encodeURIComponent(build.id)}`; code.target = "_blank";
  share.append(code);
  root.append(share);

  const stats = el("div", "stats-grid"), {output, modelLabel} = defenseMetrics(build);
  stats.append(metric("POB DPS", fmt(Math.max(...["FullDPS", "FullDotDPS", "CombinedDPS", "TotalDPS", "TotalDotDPS"].map(key => output[key] || 0)))),
    metric("LIFE", fmt(output.Life || 0)),
    metric("ENERGY SHIELD", fmt(output.EnergyShield || 0)),
    metric("EFFECTIVE HIT POOL", output.TotalEHP ? fmt(output.TotalEHP) : "n/a"),
    metric("ELEMENTAL RES", ["Fire", "Cold", "Lightning"].map(k => `${fmt(output[k + "Resist"])}%`).join(" / ")),
    metric("CHAOS RES", `${fmt(output.ChaosResist)}%`),
    metric("DEFENSE MODEL", modelLabel || "Life + energy shield"),
    metric("UNSPENT POINTS", build.qualityReport?.counts?.unspentPoints ?? build.completeness?.unspentPassivePoints ?? "n/a"));
  root.append(stats);
  root.append(gapsCard(build, normal));
  const loadoutRow = el("div", "result-columns loadout-row");
  loadoutRow.append(loadoutCard(build), jewelsCard(build));
  root.append(loadoutRow);
  const progression = progressionCard(build);
  if (progression) root.append(progression);

  const columns = el("div", "result-columns"), price = priceCard(build);
  const plan = el("section", "detail-card");
  plan.append(el("h3", "", "Build plan"),
    el("p", "muted", `Prompt: ${build.prompt || "Previously generated build"}`),
    el("p", "muted", build.modelUsed ? `Planned by ${build.modelUsed}. ${build.modelIntent || ""}` : ""),
    el("p", "", `${build.treeNodes} allocated tree nodes · ${build.ascendancyPoints} ascendancy points`),
    el("p", "", `Main link: ${(build.gems || []).join(" · ")}`),
    el("p", "muted", build.recipe.generation === "from-game-data"
      ? `${build.recipe.treeChange}. ${Object.keys(build.recipe.masteries || {}).length} mastery effects selected. Equipment generated from installed item definitions.`
      : `Tree adjustment: ${build.recipe.treeChange}. Support: ${(build.recipe.changedMainLinks || []).join(", ")}. ${(build.recipe.changedSlots || []).length ? `Changed gear: ${build.recipe.changedSlots.join(", ")}.` : "Source gear kept after validation."}`),
    el("p", "muted", build.recipe.modelReason || "PoB calculates the resulting export."));
  if (build.recipe.levelChange) plan.append(el("p", "muted", build.recipe.levelChange));
  if (build.recipe.mechanics) {
    const mechanics = build.recipe.mechanics;
    plan.append(el("p", "muted", `Mechanics profile: ${mechanics.name || mechanics.profile || "generic"} · ${mechanics.damageSource || "unknown source"} · ${mechanics.hitOrAilment || "unclassified"}`));
  }
  if (build.mechanicChecks?.length) {
    const mechanicDetails = el("details", "checks"), mechanicList = el("ul");
    mechanicDetails.append(el("summary", "", `Mechanic checks · ${build.mechanicChecks.filter(check => check.passed).length}/${build.mechanicChecks.length} passed`));
    for (const check of build.mechanicChecks) mechanicList.append(el("li", "", `${check.passed ? "✓" : "✗"} ${check.name}: ${check.reason}`));
    mechanicDetails.append(mechanicList); plan.append(mechanicDetails);
  }
  if (build.qualityWarnings?.length) {
    const warnings = el("ul", "quality-warnings");
    for (const warning of build.qualityWarnings) warnings.append(el("li", "", warning));
    plan.append(el("h4", "", "Quality notes"), warnings);
  }
  const reasons = Object.entries(build.recipe.selectionReasons || {});
  if (reasons.length) {
    const reasonList = el("ul", "selection-reasons");
    for (const [kind, reason] of reasons) {
      const values = Array.isArray(reason) ? reason : [reason];
      for (const value of values) if (value) reasonList.append(el("li", "", `${kind}: ${value}`));
    }
    plan.append(el("h4", "", "Selection reasons"), reasonList);
  }
  const checks = el("details", "checks"), list = el("ul");
  checks.append(el("summary", "", `Validation checks · ${build.validation.filter(v => v.passed).length}/${build.validation.length} passed`));
  for (const check of build.validation) list.append(el("li", "", `${check.passed ? "✓" : "✗"} ${check.name}: ${check.reason}`));
  checks.append(list); plan.append(checks);
  columns.append(price, plan); root.append(columns);
  root.scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "start"});
}
async function poll(jobId) {
  for (;;) {
    await new Promise(resolve => setTimeout(resolve, 900));
    const job = await api(`/api/job?id=${encodeURIComponent(jobId)}`);
    $("progress-text").textContent = job.stage;
    if (job.status === "failed") throw new Error(job.error || "Generation failed");
    if (["complete", "share_failed"].includes(job.status)) {
      render(job.result);
      if (job.status === "share_failed") showError("The build passed PoB checks and was saved, but pobb.in publication failed. Use Retry sharing in the result.");
      return;
    }
  }
}
form.addEventListener("submit", async event => {
  event.preventDefault();
  if (busy) return;
  showError("");
  const prompt = $("prompt").value.trim();
  if (prompt.length < 12) { showError("Describe the build you want in at least 12 characters."); return; }
  $("result").hidden = true;
  loading(true, "Sending prompt to the local generator…");
  try {
    const {jobId} = await post("/api/generate", {prompt, model: $("model").value});
    await poll(jobId);
  } catch (cause) { showError(cause.message); }
  finally { loading(false); }
});
async function boot() {
  try {
    const data = await api("/api/status");
    const models = data.ollama?.models || [];
    $("model").replaceChildren(...models.map(name => new Option(name, name)));
    if (models.includes(data.ollama?.defaultModel)) $("model").value = data.ollama.defaultModel;
    ready = Boolean(models.length && data.pob.detected && data.game);
    $("generate-button").disabled = !ready;
    $("system-status").textContent = data.game
      ? `Ollama ${models.length ? `${models.length} models ready` : "unavailable"} · PoB ${data.pob.detected ? "ready" : "missing"} · ${data.game.league} · tree ${data.game.treeVersion.replace("_", ".")}`
      : `Setup needed: ${data.gameError || data.pob.message}`;
    if (!models.length || !data.pob.detected || !data.game) $("system-status").classList.add("warning");
  } catch (cause) { $("system-status").textContent = `Local backend unavailable: ${cause.message}`; $("system-status").classList.add("warning"); }
  const previous = localStorage.getItem("witchcraft-last-build");
  if (previous) {
    try { render(await api(`/api/build?id=${encodeURIComponent(previous)}`)); }
    catch { localStorage.removeItem("witchcraft-last-build"); }
  }
}
boot();
