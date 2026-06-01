// SopranosDB static front-end. Runs FTS5 keyword search + structured-facet
// filtering entirely in the browser. The read-only SQLite DB (~23 MB, served
// gzip-compressed to ~7.7 MB) is downloaded ONCE and queried in memory — so
// every query after the initial load is instant, with zero network. No app
// server, no LLM, no embeddings.
//
// Engine: the official SQLite WASM build (@sqlite.org/sqlite-wasm), bundled by
// Vite (the .wasm is emitted as a hashed asset).

import sqlite3InitModule from "@sqlite.org/sqlite-wasm";
import "./style.css";

let CFG = {};        // config.json (dbUrl, keyframeBase)
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const statusEl = $("#status");
const resultsEl = $("#results");
const lightbox = $("#lightbox");
const lightboxImg = $("#lightbox-img");

let sdb = null;      // in-memory sqlite3.oo1.DB
let OPTIONS = {};    // filters.json
let LAST_ROWS = [];  // current top-N-by-relevance result set, for instant client re-sort

// ---------- helpers ----------

function esc(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
const pad = (n) => String(n).padStart(2, "0");
const epCode = (season, episode) => `S${pad(season)}E${pad(episode)}`;
const hhmmss = (s) => {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return `${pad(h)}:${pad(m)}:${pad(sec)}`;
};

function keyframeUrls(json, season, episode) {
  let arr = [];
  try { arr = JSON.parse(json || "[]"); } catch { arr = []; }
  const code = epCode(season, episode);
  return arr.map((p) => `${CFG.keyframeBase}/${code}/keyframes/${String(p).split("/").pop()}`);
}

// Common English words carry no signal but blow up the FTS scan (they match
// nearly every scene), so drop them from the MATCH. Mirrors search.py.
const STOPWORDS = new Set(
  ("a an and are as at be been but by for from had has have he her his in into is it its " +
   "of on or that the their them they this to was were what when which who will with you")
    .split(" "),
);

// Free text -> FTS5 MATCH expression: drop stopwords, quote each remaining token
// (so punctuation can't break the query), OR-join for recall; bm25() ranks.
function ftsMatch(text) {
  let toks = (text || "").match(/[A-Za-z0-9']+/g) || [];
  const content = toks.filter((t) => !STOPWORDS.has(t.toLowerCase()));
  toks = content.length ? content : toks; // all-stopword query: fall back to as-typed
  if (!toks.length) return null;
  return toks.map((t) => `"${t}"`).join(" OR ");
}

function q(sql, params = []) {
  // In-memory query; returns an array of {column: value} row objects.
  return sdb.selectObjects(sql, params);
}

// ---------- support / donate config ----------
//
// The site is fully static — there's no payment backend. Each method either
// links out to a hosted service (Ko-fi, PayPal, GitHub Sponsors…) or just shows
// a handle/address to copy and a deep-link into the relevant app (Cash App,
// Bitcoin/ETH). Nothing here touches a third party until the visitor clicks.
//
// >>> TO GO LIVE: replace the REPLACE_ME / empty values below with your real
// handles. Any method still left as a placeholder (or blank) is automatically
// hidden, so a half-filled config can never ship a broken button. Delete the
// methods you don't want; reorder freely (display order follows this list).
//
// config.json may also carry a "support" array; if present and non-empty it
// OVERRIDES this list, so production handles can be re-pointed without a JS
// rebuild (same convention as dbUrl / keyframeBase).
const DEFAULT_SUPPORT = [
  { kind: "cashapp", label: "Cash App", handle: "$IMMUZ" },
  { kind: "crypto", label: "Bitcoin", symbol: "BTC", scheme: "bitcoin",
    address: "32VL1vWyAR7GnTYANdg5SpeSYrKjQpFRGU" },
  // --- More options: fill in a real value (replace REPLACE_ME) to switch any of
  // these on. Anything left as a placeholder or blank is hidden automatically. ---
  { kind: "link", label: "Ko-fi", url: "https://ko-fi.com/REPLACE_ME",
    blurb: "Drop a one-off tip — no fees on Ko-fi, no account needed.", cta: "Tip on Ko-fi" },
  { kind: "link", label: "PayPal", url: "https://paypal.me/REPLACE_ME",
    blurb: "Old reliable — one-time or recurring.", cta: "Pay with PayPal" },
  { kind: "link", label: "GitHub Sponsors", url: "https://github.com/sponsors/REPLACE_ME",
    blurb: "Back ongoing development with a monthly sponsorship.", cta: "Sponsor on GitHub" },
  { kind: "link", label: "Buy Me a Coffee", url: "https://www.buymeacoffee.com/REPLACE_ME",
    blurb: "Caffeinate the next batch of scene tagging.", cta: "Buy a coffee" },
];

// A method is "live" only when its value is filled in and not still a placeholder.
const SUPPORT_PLACEHOLDER = /REPLACE_ME/i;
const supportValue = (m) =>
  m.kind === "link" ? m.url : m.kind === "cashapp" ? m.handle : m.address;
function supportIsLive(m) {
  const v = (supportValue(m) || "").trim();
  return Boolean(v) && !SUPPORT_PLACEHOLDER.test(v);
}
function supportMethods() {
  const list = Array.isArray(CFG.support) && CFG.support.length ? CFG.support : DEFAULT_SUPPORT;
  return list.filter(supportIsLive);
}

// ---------- query building (mirrors search.py:_build_filter_sql) ----------

function buildFacetSql(f) {
  const where = [], params = [];
  if (f.location_types?.length) {
    where.push(`s.location_type IN (${f.location_types.map(() => "?").join(",")})`);
    params.push(...f.location_types);
  }
  if (f.location_interior_exterior) { where.push("s.location_interior_exterior = ?"); params.push(f.location_interior_exterior); }
  if (f.time_of_day) { where.push("s.time_of_day = ?"); params.push(f.time_of_day); }
  if (f.mood) { where.push("s.mood = ?"); params.push(f.mood); }
  if (f.violence_level) { where.push("s.violence_level = ?"); params.push(f.violence_level); }
  if (f.min_group_size != null) { where.push("s.group_size_total >= ?"); params.push(f.min_group_size); }
  if (f.max_group_size != null) { where.push("s.group_size_total <= ?"); params.push(f.max_group_size); }
  for (const ch of f.required_characters || []) {
    where.push("EXISTS (SELECT 1 FROM scene_characters sc WHERE sc.scene_id=s.id AND sc.character_name=? AND sc.uncertain=0)");
    params.push(ch);
  }
  for (const ch of f.excluded_characters || []) {
    where.push("NOT EXISTS (SELECT 1 FROM scene_characters sc WHERE sc.scene_id=s.id AND sc.character_name=?)");
    params.push(ch);
  }
  for (const [kind, vals] of [["activity", f.activities], ["topic", f.topics]]) {
    for (const v of vals || []) {
      where.push("EXISTS (SELECT 1 FROM scene_tags st WHERE st.scene_id=s.id AND st.tag_type=? AND st.tag_value=?)");
      params.push(kind, v.toLowerCase().trim());
    }
  }
  return [where.length ? where.join(" AND ") : "1=1", params];
}

const SCENE_COLS = `s.id, s.scene_index, s.start_s, s.end_s, s.summary, s.location_name,
  s.location_type, s.time_of_day, s.mood, s.violence_level, s.group_size_total,
  s.dialogue_highlight, s.transcript_text, s.keyframes_json, s.view_count,
  e.season, e.episode, e.title`;

async function attachCharacters(rows) {
  if (!rows.length) return;
  const ids = rows.map((r) => r.id);
  const ph = ids.map(() => "?").join(",");
  const cr = await q(
    `SELECT scene_id, character_name FROM scene_characters WHERE uncertain=0 AND scene_id IN (${ph})`,
    ids,
  );
  const byId = {};
  for (const r of cr) (byId[r.scene_id] ||= []).push(r.character_name);
  for (const r of rows) r.characters = byId[r.id] || [];
}

// Chronological (air) order — the DB's default ranking when there's no keyword
// query, and the tiebreak for the client-side secondary sorts.
const CHRONO = "e.season, e.episode, s.scene_index";

// The DB always returns the top-N by RELEVANCE: bm25 when there's a keyword
// query, chronological when the box is empty (relevance has nothing to rank by).
// The user-chosen sort (newest / oldest / popularity) is NOT pushed into SQL —
// otherwise "newest" would rank the whole corpus by date and ignore the search.
// Instead we fetch the top-N relevant rows here and reorder them with sortRows().
async function runSearch(text, f, top) {
  const [where, params] = buildFacetSql(f);
  const match = ftsMatch(text);
  let sql, args;
  if (match) {
    // `rank` is FTS5's reserved special column — never alias to it. Drive off the
    // FTS table so MATCH/bm25 bind cleanly across SQLite versions.
    sql = `SELECT ${SCENE_COLS}, bm25(scenes_fts) AS bm25_score
           FROM scenes_fts
           JOIN scenes s ON s.id = scenes_fts.rowid
           JOIN episodes e ON e.id = s.episode_id
           WHERE scenes_fts MATCH ? AND ${where}
           ORDER BY bm25_score, ${CHRONO} LIMIT ?`;
    args = [match, ...params, top];
  } else {
    sql = `SELECT ${SCENE_COLS}, 0.0 AS bm25_score
           FROM scenes s JOIN episodes e ON e.id = s.episode_id
           WHERE ${where}
           ORDER BY ${CHRONO} LIMIT ?`;
    args = [...params, top];
  }
  const rows = await q(sql, args);
  await attachCharacters(rows);
  return rows;
}

// Reorder an already-fetched (top-N-by-relevance) result set on the client, so
// switching sort is instant and never drops a relevant result for an irrelevant
// older/newer one. "relevance" keeps the server's bm25 order untouched.
function sortRows(rows, sort) {
  const chrono = (a, b) =>
    a.season - b.season || a.episode - b.episode || a.scene_index - b.scene_index;
  const out = rows.slice();
  switch (sort) {
    case "newest":     out.sort((a, b) => -chrono(a, b)); break;
    case "oldest":     out.sort(chrono); break;
    case "popularity": out.sort((a, b) => (b.view_count || 0) - (a.view_count || 0) || chrono(a, b)); break;
    // "relevance" (default): leave as-is — already in bm25 order from the DB.
  }
  return out;
}

// ---------- rendering ----------

function renderScene(h) {
  const code = epCode(h.season, h.episode);
  const chars = (h.characters || []).map(
    (c) => `<a href="#/character/${encodeURIComponent(c)}">${esc(c)}</a>`
  ).join(", ") || "—";
  const loc = h.location_name || "—";
  const locType = h.location_type
    ? ` <a href="#/location/${encodeURIComponent(h.location_type)}">(${esc(h.location_type)})</a>` : "";
  const dialogue = h.dialogue_highlight
    ? `<div class="dialogue">${esc(h.dialogue_highlight)}</div>` : "";
  const transcript = h.transcript_text
    ? `<details><summary>Transcript</summary><pre>${esc(h.transcript_text)}</pre></details>` : "";
  const keyframes = keyframeUrls(h.keyframes_json, h.season, h.episode)
    .map((url) => `<img src="${esc(url)}" data-full="${esc(url)}" loading="lazy" decoding="async" alt="" />`).join("");
  return `
    <div class="result">
      <div class="result-head">
        <div>
          <a class="ep" href="#/episode/${code}">${code}</a> ${esc(h.title)}
          &nbsp;<a class="sim" href="#/scene/${h.id}" title="permalink">#${h.scene_index}</a>
        </div>
        <div class="ts">${hhmmss(h.start_s)} – ${hhmmss(h.end_s)}</div>
      </div>
      <div class="meta"><strong>Location:</strong> ${esc(loc)}${locType} &nbsp; <strong>Characters:</strong> ${chars}</div>
      <div class="summary">${esc(h.summary || "")}</div>
      ${dialogue}
      <div class="keyframes">${keyframes}</div>
      ${transcript}
    </div>`;
}

function renderScenes(rows, headingHtml = "") {
  if (!rows.length) {
    resultsEl.innerHTML = headingHtml + `<div class="empty">No scenes match that. Fuhgeddaboudit — try fewer filters.</div>`;
    return;
  }
  resultsEl.innerHTML = headingHtml + rows.map(renderScene).join("");
  bindKeyframes();
}

function bindKeyframes() {
  $$(".keyframes img").forEach((img) => {
    img.addEventListener("click", () => {
      lightboxImg.src = img.dataset.full;
      lightbox.classList.add("show");
    });
  });
}

// ---------- filter UI ----------
//
// Filters are held in a single in-memory state object (FILTERS), NOT read out of
// the DOM. Multi-value facets use a click-to-add chip control (pick from a dropdown,
// it becomes a removable pill) instead of a native <select multiple>, which needs
// Ctrl/Cmd-click and silently drops selections. An "active filters" bar mirrors the
// whole state so it can be cleared one pill at a time.

// The facet vocab is stored snake_case (e.g. "restaurant_or_food_business"); show it
// as readable prose. A few values read better with an explicit override.
const LABEL_OVERRIDES = {
  dawn_dusk: "Dawn / dusk",
  location_interior_exterior: "Inside / outside",
};
function humanize(v) {
  if (v == null || v === "") return "";
  if (LABEL_OVERRIDES[v]) return LABEL_OVERRIDES[v];
  const s = String(v).replace(/_or_/g, " / ").replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// Grouped for scannability; the headings double as a bit of in-world flavor.
const FILTER_GROUPS = [
  ["The Family", [
    ["characters", "Must include", "multi", "characters"],
    ["excluded_characters", "Leave out", "multi", "characters"],
    ["__group__", "People in scene", "range", null],
  ]],
  ["The Neighborhood", [
    ["location_types", "Location", "multi", "location_types"],
    ["location_interior_exterior", "Inside / outside", "single", "interior_exterior"],
    ["time_of_day", "Time of day", "single", "times_of_day"],
  ]],
  ["The Vibe", [
    ["mood", "Mood", "single", "moods"],
    ["violence_level", "Violence", "single", "violence_levels"],
    ["activities", "What's happening", "multi", "activities"],
    ["topics", "What's it about", "multi", "topics"],
  ]],
];

// name -> human label, for the active-filter pills (the range pair is special-cased).
const FILTER_LABELS = Object.fromEntries(
  FILTER_GROUPS.flatMap(([, specs]) => specs.map(([name, label]) => [name, label])),
);

// Live filter state. Multi -> array of values; single -> string; number -> int.
let FILTERS = {};

function fieldControl(name, label, kind, optKey) {
  if (kind === "range") {
    return `<div class="f-field f-range">
      <span class="f-label">${esc(label)}</span>
      <div class="f-range-row">
        <input type="number" class="f-num" data-field="min_group_size" placeholder="min" min="1" max="50" />
        <span class="f-to">to</span>
        <input type="number" class="f-num" data-field="max_group_size" placeholder="max" min="1" max="50" />
      </div>
    </div>`;
  }
  const opts = (OPTIONS[optKey] || []).map((v) => `<option value="${esc(v)}">${esc(humanize(v))}</option>`).join("");
  if (kind === "multi") {
    return `<div class="f-field f-multi" data-field="${name}">
      <span class="f-label">${esc(label)}</span>
      <select class="f-add" data-field="${name}" aria-label="Add ${esc(label.toLowerCase())}">
        <option value="">+ add…</option>${opts}
      </select>
      <div class="f-chips" data-chips="${name}"></div>
    </div>`;
  }
  return `<div class="f-field" data-field="${name}">
    <span class="f-label">${esc(label)}</span>
    <select class="f-single" data-field="${name}"><option value="">— any —</option>${opts}</select>
  </div>`;
}

function buildFilterUI() {
  const grid = $("#filter-grid");
  grid.innerHTML = FILTER_GROUPS.map(([title, specs]) => `
    <section class="f-group">
      <div class="f-group-title">${esc(title)}</div>
      <div class="f-group-fields">${specs.map((s) => fieldControl(...s)).join("")}</div>
    </section>`).join("");

  // Add-to-list dropdowns: selecting an option appends a chip, then resets.
  grid.addEventListener("change", (e) => {
    const el = e.target;
    const name = el.dataset.field;
    if (!name) return;
    if (el.classList.contains("f-add")) {
      const v = el.value;
      el.value = "";
      if (!v) return;
      const arr = (FILTERS[name] ||= []);
      if (!arr.includes(v)) arr.push(v);
    } else if (el.classList.contains("f-single")) {
      if (el.value) FILTERS[name] = el.value; else delete FILTERS[name];
    } else if (el.classList.contains("f-num")) {
      const n = parseInt(el.value, 10);
      if (Number.isFinite(n)) FILTERS[name] = n; else delete FILTERS[name];
    }
    syncFilterUI();
  });

  $("#filter-clear").addEventListener("click", clearFilters);
  syncFilterUI();
}

function removeFilterValue(name, value) {
  if (Array.isArray(FILTERS[name])) {
    FILTERS[name] = FILTERS[name].filter((v) => v !== value);
    if (!FILTERS[name].length) delete FILTERS[name];
  } else {
    delete FILTERS[name];
  }
  syncFilterUI();
}

function clearFilters() {
  FILTERS = {};
  $$("#filter-grid select, #filter-grid input").forEach((el) => (el.value = ""));
  syncFilterUI();
}

function chipHtml(name, value, text) {
  return `<button type="button" class="chip" data-rm-field="${esc(name)}" data-rm-val="${esc(value)}">
    ${esc(text)}<span class="chip-x" aria-hidden="true">×</span></button>`;
}

// Reflect FILTERS into the DOM: per-field chips, the single/number controls,
// the active-filters bar, the count badge.
function syncFilterUI() {
  // Per-field chip rows (multi only).
  $$("#filter-grid [data-chips]").forEach((box) => {
    const name = box.dataset.chips;
    box.innerHTML = (FILTERS[name] || []).map((v) => chipHtml(name, v, humanize(v))).join("");
  });
  // Keep single-selects / number inputs in step with the state.
  $$("#filter-grid .f-single").forEach((el) => (el.value = FILTERS[el.dataset.field] || ""));
  $$("#filter-grid .f-num").forEach((el) => (el.value = FILTERS[el.dataset.field] ?? ""));

  // Wire chip removal (delegated once is simpler, but rebuilding innerHTML drops
  // listeners — so (re)bind on the live nodes here and in the active bar).
  $$(".chip").forEach((c) =>
    c.onclick = () => removeFilterValue(c.dataset.rmField, c.dataset.rmVal));

  renderActiveBar();
  const n = facetCount();
  $("#filter-count").textContent = n ? `${n} active` : "";
  if (n > 0) $("#filters").open = true;
}

function renderActiveBar() {
  const bar = $("#active-bar");
  if (!bar) return;
  const pills = [];
  for (const [name, val] of Object.entries(FILTERS)) {
    const label = FILTER_LABELS[name] || (name === "min_group_size" || name === "max_group_size" ? "People in scene" : name);
    if (Array.isArray(val)) {
      for (const v of val) pills.push(chipHtml(name, v, `${label}: ${humanize(v)}`));
    } else if (name === "min_group_size") {
      pills.push(chipHtml(name, val, `≥ ${val} people`));
    } else if (name === "max_group_size") {
      pills.push(chipHtml(name, val, `≤ ${val} people`));
    } else {
      pills.push(chipHtml(name, val, `${label}: ${humanize(val)}`));
    }
  }
  if (!pills.length) { bar.innerHTML = ""; bar.classList.remove("show"); return; }
  bar.classList.add("show");
  bar.innerHTML = `<span class="active-label">Filtering by</span>${pills.join("")}` +
    `<button type="button" class="active-clear" id="active-clear">Clear all</button>`;
  $$("#active-bar .chip").forEach((c) =>
    c.onclick = () => removeFilterValue(c.dataset.rmField, c.dataset.rmVal));
  $("#active-clear").onclick = clearFilters;
}

function currentFacets() {
  return {
    required_characters: FILTERS.characters || [],
    excluded_characters: FILTERS.excluded_characters || [],
    location_types: FILTERS.location_types || [],
    activities: FILTERS.activities || [],
    topics: FILTERS.topics || [],
    time_of_day: FILTERS.time_of_day || null,
    location_interior_exterior: FILTERS.location_interior_exterior || null,
    mood: FILTERS.mood || null,
    violence_level: FILTERS.violence_level || null,
    min_group_size: FILTERS.min_group_size ?? null,
    max_group_size: FILTERS.max_group_size ?? null,
  };
}

function facetCount() {
  const f = currentFacets();
  let n = 0;
  for (const v of Object.values(f)) {
    if (Array.isArray(v)) n += v.length ? 1 : 0;
    else if (v != null) n += 1;
  }
  return n;
}

// ---------- search action ----------

async function doSearch() {
  const text = $("#q").value.trim();
  const top = Math.max(1, Math.min(parseInt($("#top").value, 10) || 25, 200));
  const sort = $("#sort").value;
  const f = currentFacets();
  if (!text && facetCount() === 0) {
    LAST_ROWS = [];
    statusEl.textContent = "Type some keywords, or pick a filter or two.";
    resultsEl.innerHTML = `<div class="empty">Search 8,082 indexed scenes by keyword, or work the filters — by family, neighborhood, or vibe.</div>`;
    return;
  }
  $("#go").disabled = true;
  statusEl.textContent = "Searching…";
  resultsEl.innerHTML = "";
  try {
    const t0 = performance.now();
    const rows = await runSearch(text, f, top);
    const ms = Math.round(performance.now() - t0);
    LAST_ROWS = rows;
    statusEl.textContent = `${rows.length} result${rows.length === 1 ? "" : "s"} in ${ms} ms.`;
    renderScenes(sortRows(rows, sort));
  } catch (err) {
    statusEl.textContent = `Error: ${err.message || err}`;
  } finally {
    $("#go").disabled = false;
  }
}

// ---------- browse views ----------

async function viewEpisodes() {
  statusEl.textContent = "Episodes";
  const eps = await q("SELECT season, episode, title FROM episodes ORDER BY season, episode");
  const bySeason = {};
  for (const e of eps) (bySeason[e.season] ||= []).push(e);
  let html = "";
  for (const season of Object.keys(bySeason).sort((a, b) => a - b)) {
    html += `<div class="season-head">Season ${season}</div><div class="browse-grid">`;
    html += bySeason[season].map((e) => {
      const code = epCode(e.season, e.episode);
      return `<a class="browse-card" href="#/episode/${code}"><div class="ep">${code}</div><div class="sub">${esc(e.title)}</div></a>`;
    }).join("");
    html += `</div>`;
  }
  resultsEl.innerHTML = html;
}

async function viewCharacters() {
  statusEl.textContent = "Characters";
  const counts = await q(
    "SELECT character_name, COUNT(*) AS c FROM scene_characters WHERE uncertain=0 GROUP BY character_name"
  );
  const byName = {};
  for (const r of counts) byName[r.character_name] = r.c;
  const names = (OPTIONS.characters || []).slice().sort();
  resultsEl.innerHTML = `<div class="browse-grid">` + names.map((n) => {
    const c = byName[n] || 0;
    return `<a class="browse-card" href="#/character/${encodeURIComponent(n)}"><div>${esc(n)}</div><div class="sub">${c} scene${c === 1 ? "" : "s"}</div></a>`;
  }).join("") + `</div>`;
}

async function viewEpisode(code) {
  const m = /^S(\d{2})E(\d{2})$/.exec(code || "");
  if (!m) { resultsEl.innerHTML = `<div class="empty">Bad episode code.</div>`; return; }
  const season = parseInt(m[1], 10), episode = parseInt(m[2], 10);
  statusEl.textContent = `Episode ${code}`;
  const rows = await q(
    `SELECT ${SCENE_COLS}, 0.0 AS bm25_score FROM scenes s JOIN episodes e ON e.id=s.episode_id
     WHERE e.season=? AND e.episode=? ORDER BY s.scene_index`, [season, episode]);
  await attachCharacters(rows);
  const title = rows[0] ? esc(rows[0].title) : "";
  renderScenes(rows, `<div class="season-head">${code} — ${title} · ${rows.length} scenes</div>`);
}

async function viewCharacter(name) {
  statusEl.textContent = `Character: ${name}`;
  const rows = await q(
    `SELECT ${SCENE_COLS}, 0.0 AS bm25_score FROM scenes s JOIN episodes e ON e.id=s.episode_id
     WHERE EXISTS (SELECT 1 FROM scene_characters sc WHERE sc.scene_id=s.id AND sc.character_name=? AND sc.uncertain=0)
     ORDER BY e.season, e.episode, s.scene_index LIMIT 400`, [name]);
  await attachCharacters(rows);
  renderScenes(rows, `<div class="season-head">${esc(name)} · ${rows.length} scenes${rows.length === 400 ? " (showing first 400)" : ""}</div>`);
}

async function viewLocation(type) {
  statusEl.textContent = `Location: ${type}`;
  const rows = await q(
    `SELECT ${SCENE_COLS}, 0.0 AS bm25_score FROM scenes s JOIN episodes e ON e.id=s.episode_id
     WHERE s.location_type=? ORDER BY e.season, e.episode, s.scene_index LIMIT 400`, [type]);
  await attachCharacters(rows);
  renderScenes(rows, `<div class="season-head">${esc(type)} · ${rows.length} scenes${rows.length === 400 ? " (showing first 400)" : ""}</div>`);
}

async function viewScene(id) {
  statusEl.textContent = `Scene ${id}`;
  const rows = await q(
    `SELECT ${SCENE_COLS}, 0.0 AS bm25_score FROM scenes s JOIN episodes e ON e.id=s.episode_id WHERE s.id=?`,
    [parseInt(id, 10)]);
  await attachCharacters(rows);
  renderScenes(rows, "");
}

// ---------- support / donate view ----------

function supportCardHtml(m) {
  const label = `<div class="support-card-label">${esc(m.label)}${
    m.symbol ? ` <span class="support-sym">${esc(m.symbol)}</span>` : ""}</div>`;
  const blurb = m.blurb ? `<p class="support-card-blurb">${esc(m.blurb)}</p>` : "";

  if (m.kind === "link") {
    const cta = m.cta || `Open ${m.label}`;
    return `<div class="support-card">${label}${blurb}
      <a class="btn-link" href="${esc(m.url)}" target="_blank" rel="noopener noreferrer">${esc(cta)} ↗</a>
    </div>`;
  }

  if (m.kind === "cashapp") {
    const tag = m.handle.replace(/^\$/, "");
    const url = `https://cash.app/$${encodeURIComponent(tag)}`;
    return `<div class="support-card">${label}${blurb}
      <div class="support-handle">
        <code>$${esc(tag)}</code>
        <button type="button" class="copy-btn" data-copy="$${esc(tag)}">Copy</button>
      </div>
      <a class="btn-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open in Cash App ↗</a>
    </div>`;
  }

  // crypto: show the full address (mono, wraps), copy button, and a wallet URI.
  const uri = m.scheme ? `${m.scheme}:${m.address}` : m.address;
  return `<div class="support-card">${label}${blurb}
    <div class="support-handle support-addr">
      <code title="${esc(m.address)}">${esc(m.address)}</code>
      <button type="button" class="copy-btn" data-copy="${esc(m.address)}">Copy</button>
    </div>
    <a class="btn-link ghost" href="${esc(uri)}">Open in wallet</a>
  </div>`;
}

function viewSupport() {
  statusEl.textContent = "Support SopranosDB";
  const methods = supportMethods();
  const body = methods.length
    ? `<div class="support-methods">${methods.map(supportCardHtml).join("")}</div>`
    : `<div class="support-empty">Tip options are being set up — check back soon.</div>`;
  resultsEl.innerHTML = `
    <section class="support">
      <div class="support-hero">
        <h2>Enjoying the database? Show your appreciation.</h2>
      </div>
      ${body}
    </section>`;
  bindCopyButtons();
}

// Copy-to-clipboard for Cash App tags / crypto addresses, with a fallback for
// insecure contexts (file://, plain http) where navigator.clipboard is absent.
function bindCopyButtons() {
  $$(".copy-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const text = btn.dataset.copy || "";
      let ok = true;
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { ok = document.execCommand("copy"); } catch { ok = false; }
        ta.remove();
      }
      const prev = btn.dataset.label || (btn.dataset.label = btn.textContent);
      btn.textContent = ok ? "Copied!" : "Press ⌘C";
      btn.classList.add("copied");
      setTimeout(() => { btn.textContent = prev; btn.classList.remove("copied"); }, 1400);
    });
  });
}

// ---------- router ----------

function setActiveNav(route) {
  const map = { "": "#/", "episodes": "#/episodes", "characters": "#/characters", "support": "#/support" };
  const target = map[route] || "";
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === target));
}

async function route() {
  const h = location.hash.replace(/^#\/?/, "");
  const [r, ...rest] = h.split("/");
  const arg = rest.map(decodeURIComponent).join("/");
  setActiveNav(r);
  const searchVisible = r === "";
  $("#search-form").style.display = searchVisible ? "" : "none";
  $("#filters").style.display = searchVisible ? "" : "none";
  // Support/donate needs no database — render it immediately, even while the
  // DB is still downloading. Every other view queries sdb, so gate those.
  if (r === "support") {
    try { viewSupport(); } catch (err) { statusEl.textContent = `Error: ${err.message || err}`; }
    return;
  }
  if (!sdb) return;
  try {
    switch (r) {
      case "": return doSearch();
      case "episodes": return viewEpisodes();
      case "episode": return viewEpisode(arg);
      case "characters": return viewCharacters();
      case "character": return viewCharacter(arg);
      case "support": return viewSupport();
      case "location": return viewLocation(arg);
      case "scene": return viewScene(arg);
      default:
        resultsEl.innerHTML = `<div class="empty">Unknown page.</div>`;
    }
  } catch (err) {
    statusEl.textContent = `Error: ${err.message || err}`;
  }
}

// ---------- init ----------

async function loadDatabase(sqlite3) {
  // One-time download of the whole DB (compressed on the wire — br/gzip, ~7 MB —
  // and decompressed transparently by the browser), then deserialize into an
  // in-memory database. Every subsequent query is local; nothing is persisted to
  // disk (no OPFS / IndexedDB / localStorage), so the only client footprint is
  // this RAM copy plus whatever the browser keeps in its HTTP cache.
  const url = new URL(CFG.dbUrl, location.href).href;
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status} fetching the database`);

  // Stream so we can show download progress (Content-Length is the *compressed*
  // size; the body arrives already-decompressed, so report MB, not a percentage).
  const reader = resp.body.getReader();
  const chunks = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    statusEl.textContent = `Loading database… ${(received / 1e6).toFixed(1)} MB`;
  }

  // Copy the chunks straight into the WASM heap, releasing each one as we go,
  // instead of first assembling a separate contiguous Uint8Array. That avoids a
  // second full-size (~22 MB) JS buffer, cutting the peak load-time memory from
  // ~3× to ~2× the DB size. alloc() may grow (and detach) the heap, so take the
  // heap8u() view *after* allocating.
  const p = sqlite3.wasm.alloc(received);
  const heap = sqlite3.wasm.heap8u();
  let off = 0;
  for (let i = 0; i < chunks.length; i++) {
    heap.set(chunks[i], p + off);
    off += chunks[i].length;
    chunks[i] = null; // drop the reference so the GC can reclaim it now
  }
  chunks.length = 0;

  const db = new sqlite3.oo1.DB();
  const rc = sqlite3.capi.sqlite3_deserialize(
    db.pointer, "main", p, received, received,
    sqlite3.capi.SQLITE_DESERIALIZE_FREEONCLOSE,
  );
  if (rc) throw new Error(`sqlite3_deserialize failed (rc=${rc})`);
  return db;
}

async function init() {
  try {
    CFG = await (await fetch("config.json")).json();
    OPTIONS = await (await fetch("filters.json")).json();
  } catch (err) {
    statusEl.textContent = "Failed to load config.json / filters.json.";
    return;
  }
  buildFilterUI();

  // Wire navigation and render once up front so the Support page (which needs no
  // DB) and nav are usable immediately, while the database downloads in the
  // background. DB-backed views show the "Loading database…" status until ready.
  window.addEventListener("hashchange", route);
  route();

  try {
    const sqlite3 = await sqlite3InitModule();
    sdb = await loadDatabase(sqlite3);
  } catch (err) {
    statusEl.textContent = "Failed to load the database. Check that it is reachable (and CORS-enabled if cross-origin). " + (err.message || err);
    return;
  }

  const onSearchView = () => location.hash.replace(/^#\/?/, "").split("/")[0] === "";
  $("#search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (onSearchView()) doSearch();
    else location.hash = "#/";
  });
  // Changing the sort just reorders the current top-N on the client — no re-query.
  $("#sort").addEventListener("change", () => {
    if (!onSearchView()) return;
    if (LAST_ROWS.length) renderScenes(sortRows(LAST_ROWS, $("#sort").value));
    else if ($("#q").value.trim() || facetCount() > 0) doSearch();
  });
  // Changing the result count changes how many rows we fetch, so re-run the query.
  $("#top").addEventListener("change", () => {
    if (onSearchView() && ($("#q").value.trim() || facetCount() > 0)) doSearch();
  });
  lightbox.addEventListener("click", () => lightbox.classList.remove("show"));

  statusEl.textContent = "Search 8,082 indexed scenes by keyword, or browse by episode / character / filter.";
  route();
}

init();
