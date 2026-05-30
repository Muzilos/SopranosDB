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
  s.dialogue_highlight, s.transcript_text, s.keyframes_json,
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
           ORDER BY bm25_score LIMIT ?`;
    args = [match, ...params, top];
  } else {
    sql = `SELECT ${SCENE_COLS}, 0.0 AS bm25_score
           FROM scenes s JOIN episodes e ON e.id = s.episode_id
           WHERE ${where}
           ORDER BY e.season, e.episode, s.scene_index LIMIT ?`;
    args = [...params, top];
  }
  const rows = await q(sql, args);
  await attachCharacters(rows);
  return rows;
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
    .map((url) => `<img src="${esc(url)}" data-full="${esc(url)}" loading="lazy" alt="" />`).join("");
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
    resultsEl.innerHTML = headingHtml + `<div class="empty">No scenes found.</div>`;
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

const FILTER_SPECS = [
  ["characters", "Required characters", "multi", "characters"],
  ["excluded_characters", "Excluded characters", "multi", "characters"],
  ["location_types", "Location type", "multi", "location_types"],
  ["activities", "Activities", "multi", "activities"],
  ["topics", "Topics", "multi", "topics"],
  ["time_of_day", "Time of day", "single", "times_of_day"],
  ["location_interior_exterior", "Interior/Exterior", "single", "interior_exterior"],
  ["mood", "Mood", "single", "moods"],
  ["violence_level", "Violence level", "single", "violence_levels"],
  ["min_group_size", "Min group size", "number", null],
  ["max_group_size", "Max group size", "number", null],
];

function buildFilterUI() {
  const grid = $("#filter-grid");
  grid.innerHTML = FILTER_SPECS.map(([name, label, kind, optKey]) => {
    if (kind === "number") {
      return `<label>${label}<input type="number" min="1" max="50" data-filter="${name}" /></label>`;
    }
    const opts = (OPTIONS[optKey] || []).map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
    const multi = kind === "multi" ? "multiple" : "";
    const empty = kind === "single" ? `<option value="">— any —</option>` : "";
    return `<label>${label}<select data-filter="${name}" ${multi}>${empty}${opts}</select></label>`;
  }).join("");

  grid.addEventListener("change", updateFilterCount);
  $("#filter-clear").addEventListener("click", () => {
    $$("#filter-grid [data-filter]").forEach((el) => {
      if (el.tagName === "SELECT" && el.multiple) [...el.options].forEach((o) => (o.selected = false));
      else el.value = "";
    });
    updateFilterCount();
  });
}

function readFilters() {
  const out = {};
  $$("#filter-grid [data-filter]").forEach((el) => {
    const name = el.dataset.filter;
    if (el.tagName === "SELECT" && el.multiple) {
      const vals = [...el.selectedOptions].map((o) => o.value).filter(Boolean);
      if (vals.length) out[name] = vals;
    } else if (el.value) {
      out[name] = el.value;
    }
  });
  return out;
}

function currentFacets() {
  const raw = readFilters();
  return {
    required_characters: raw.characters || [],
    excluded_characters: raw.excluded_characters || [],
    location_types: raw.location_types || [],
    activities: raw.activities || [],
    topics: raw.topics || [],
    time_of_day: raw.time_of_day || null,
    location_interior_exterior: raw.location_interior_exterior || null,
    mood: raw.mood || null,
    violence_level: raw.violence_level || null,
    min_group_size: raw.min_group_size ? parseInt(raw.min_group_size, 10) : null,
    max_group_size: raw.max_group_size ? parseInt(raw.max_group_size, 10) : null,
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

function updateFilterCount() {
  const n = facetCount();
  $("#filter-count").textContent = n ? `(${n} active)` : "";
  if (n > 0) $("#filters").open = true;
}

// ---------- search action ----------

async function doSearch() {
  const text = $("#q").value.trim();
  const top = Math.max(1, Math.min(parseInt($("#top").value, 10) || 25, 200));
  const f = currentFacets();
  if (!text && facetCount() === 0) {
    statusEl.textContent = "Enter keywords or pick at least one filter.";
    resultsEl.innerHTML = `<div class="empty">Search ~8,000 indexed scenes by keyword, or browse by episode / character / filter.</div>`;
    return;
  }
  $("#go").disabled = true;
  statusEl.textContent = "Searching…";
  resultsEl.innerHTML = "";
  try {
    const t0 = performance.now();
    const rows = await runSearch(text, f, top);
    const ms = Math.round(performance.now() - t0);
    statusEl.textContent = `${rows.length} result${rows.length === 1 ? "" : "s"} in ${ms} ms.`;
    renderScenes(rows);
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

// ---------- router ----------

function setActiveNav(route) {
  const map = { "": "#/", "episodes": "#/episodes", "characters": "#/characters" };
  const target = map[route] || "";
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === target));
}

async function route() {
  if (!sdb) return;
  const h = location.hash.replace(/^#\/?/, "");
  const [r, ...rest] = h.split("/");
  const arg = rest.map(decodeURIComponent).join("/");
  setActiveNav(r);
  const searchVisible = r === "";
  $("#search-form").style.display = searchVisible ? "" : "none";
  $("#filters").style.display = searchVisible ? "" : "none";
  try {
    switch (r) {
      case "": return doSearch();
      case "episodes": return viewEpisodes();
      case "episode": return viewEpisode(arg);
      case "characters": return viewCharacters();
      case "character": return viewCharacter(arg);
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
  // One-time download of the whole DB (gzip-encoded on the wire, ~7.7 MB), then
  // deserialize into an in-memory database. Every subsequent query is local.
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
  const bytes = new Uint8Array(received);
  let off = 0;
  for (const c of chunks) { bytes.set(c, off); off += c.length; }

  const p = sqlite3.wasm.alloc(bytes.length);
  sqlite3.wasm.heap8u().set(bytes, p);
  const db = new sqlite3.oo1.DB();
  const rc = sqlite3.capi.sqlite3_deserialize(
    db.pointer, "main", p, bytes.length, bytes.length,
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

  try {
    const sqlite3 = await sqlite3InitModule();
    sdb = await loadDatabase(sqlite3);
  } catch (err) {
    statusEl.textContent = "Failed to load the database. Check that it is reachable (and CORS-enabled if cross-origin). " + (err.message || err);
    return;
  }

  $("#search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (location.hash.replace(/^#\/?/, "").split("/")[0] === "") doSearch();
    else location.hash = "#/";
  });
  lightbox.addEventListener("click", () => lightbox.classList.remove("show"));
  window.addEventListener("hashchange", route);

  statusEl.textContent = "Search ~8,000 indexed scenes by keyword, or browse by episode / character / filter.";
  route();
}

init();
