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
    const utilities = Object.entries(stage.gemLevels).filter(([name]) => !stage.gems.includes(name));
    if (utilities.length) content.append(el("p", "muted", utilities.map(([name, level]) => `${name} (${level})`).join(" · ")));
    content.append(el("p", "muted", `PoB life + ES: ${fmt((stage.stats.Life || 0) + (stage.stats.EnergyShield || 0))} · Fire / Cold / Lightning: ${["Fire", "Cold", "Lightning"].map(name => `${fmt(stage.stats[name + "Resist"])}%`).join(" / ")}`));
    for (const instruction of stage.instructions) content.append(el("p", "muted", instruction));
    const gear = el("ul", "unknown-list stage-gear");
    for (const item of stage.gear) gear.append(el("li", "", `${item.slot}: ${item.base}`));
    content.append(el("h4", "", "Equipment targets"), gear);
  };
  select.onchange = update; update();
  card.append(el("h3", "", "Campaign to endgame"),
    el("p", "muted", "Matching stages are included in the PoB tree, skill, equipment and configuration dropdowns. Each act is an end-of-act checkpoint. Numbers beside gems are gem levels. The PoB Notes tab contains the leveling and upgrade instructions."),
    label, select, content);
  return card;
}
function render(build) {
  localStorage.setItem("witchcraft-last-build", build.id);
  const root = $("result");
  root.replaceChildren();
  root.hidden = false;
  const heading = el("div", "result-heading"), title = el("div");
  const resultTitle = el("h2", "", `${build.mainSkill} ${build.ascendancy}`);
  resultTitle.id = "result-title";
  title.append(el("p", "eyebrow", "GENERATED BUILD"), resultTitle,
    el("p", "muted", `Level ${build.level} Witch · ${build.league} · tree ${build.treeVersion.replace("_", ".")} · PoB ${build.pobVersion}`));
  heading.append(title, el("span", "validation-pill", "PoB validated"));
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
    share.append(el("p", "", build.shareError || "The validated build was saved locally. Retry publication when pobb.in is available."));
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

  const stats = el("div", "stats-grid"), output = build.stats || {};
  stats.append(metric("POB DPS", fmt(Math.max(...["FullDPS", "FullDotDPS", "CombinedDPS", "TotalDPS", "TotalDotDPS"].map(key => output[key] || 0)))),
    metric("LIFE + ES", fmt((output.Life || 0) + (output.EnergyShield || 0))),
    metric("ELEMENTAL RES", ["Fire", "Cold", "Lightning"].map(k => `${fmt(output[k + "Resist"])}%`).join(" / ")),
    metric("CHAOS RES", `${fmt(output.ChaosResist)}%`));
  root.append(stats);
  const progression = progressionCard(build);
  if (progression) root.append(progression);

  const columns = el("div", "result-columns"), price = el("section", "detail-card");
  const subtotalLabel = build.quote.priced.length
    ? `${fmt(build.quote.pricedSubtotalChaos)} chaos priced subtotal`
    : "No equipped items have a reliable quote";
  const priceSource = `${build.quote.source} · updated ${new Date(build.quote.updated * 1000).toLocaleString()}`;
  const priceDetail = build.quote.priced.length
    ? `≈ ${fmt(build.quote.pricedSubtotalDivine)} divine at ${fmt(build.quote.divineChaos)} chaos/divine · ${priceSource}`
    : `Live rate: ${fmt(build.quote.divineChaos)} chaos/divine · ${priceSource}`;
  price.append(el("h3", "", "Price coverage"), el("p", "price-total", subtotalLabel),
    el("p", "muted", priceDetail),
    el("p", "budget-status", `Budget: ${build.quote.budgetStatus}. ${build.quote.unknown.length} slots have no reliable quote.`));
  if (build.quote.priced.length) {
    const list = el("ul", "unknown-list");
    for (const item of build.quote.priced) list.append(el("li", "", `${item.slot}: ${item.name} · ≈ ${fmt(item.chaos)} chaos (${item.kind})`));
    price.append(el("h4", "", "Priced equipment"), list);
  }
  if (build.quote.unknown.length) {
    const list = el("ul", "unknown-list");
    for (const item of build.quote.unknown) list.append(el("li", "", `${item.slot}: ${item.name}`));
    price.append(el("h4", "", "Unpriced equipment"), list);
  }
  const plan = el("section", "detail-card");
  plan.append(el("h3", "", "Build plan"),
    el("p", "muted", `Prompt: ${build.prompt || "Previously generated build"}`),
    el("p", "muted", build.modelUsed ? `Planned by ${build.modelUsed}. ${build.modelIntent || ""}` : ""),
    el("p", "", `${build.treeNodes} allocated tree nodes · ${build.ascendancyPoints} ascendancy points`),
    el("p", "", `Main link: ${build.gems.join(" · ")}`),
    el("p", "muted", build.recipe.generation === "from-game-data"
      ? `${build.recipe.treeChange}. ${Object.keys(build.recipe.masteries || {}).length} mastery effects selected. Equipment generated from installed item definitions.`
      : `Tree adjustment: ${build.recipe.treeChange}. Support: ${build.recipe.changedMainLinks.join(", ")}. ${build.recipe.changedSlots.length ? `Changed gear: ${build.recipe.changedSlots.join(", ")}.` : "Source gear kept after validation."}`),
    el("p", "muted", build.recipe.modelReason || "PoB calculates the resulting export."));
  if (build.recipe.levelChange) plan.append(el("p", "muted", build.recipe.levelChange));
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
      if (job.status === "share_failed") showError("The build is validated and saved, but pobb.in publication failed. Use Retry sharing in the result.");
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
