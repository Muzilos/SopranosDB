// SopranosDB front-end. Search + browse run against a Cloudflare Worker backed by
// a D1 (SQLite) database: FTS5 keyword search, facet SQL, and the live "Popularity"
// view counts all execute server-side, and the browser just renders the small JSON
// responses. (The old design downloaded the whole ~22 MB DB and queried it in
// memory via sqlite-wasm; that's gone — purgeLegacyClientData cleans up after it.)

import "./style.css";

let CFG = {};        // config.json (apiBase, keyframeBase)
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const statusEl = $("#status");
const resultsEl = $("#results");
const lightbox = $("#lightbox");
const lightboxImg = $("#lightbox-img");

let OPTIONS = {};    // filters.json
let LAST_ROWS = [];  // current top-N-by-relevance result set, for instant client re-sort

// ---------- helpers ----------

function esc(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
// Render an FTS snippet safely: escape the transcript text, then restore the
// literal <mark>…</mark> wrappers the Worker added around the matched words.
function markSnippet(snip) {
  return esc(snip).replace(/&lt;mark&gt;/g, "<mark>").replace(/&lt;\/mark&gt;/g, "</mark>");
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

// All query SQL (FTS5/bm25, facet filtering, stopwords, character attachment) now
// runs in the Worker; the client just calls the API and renders the JSON it gets.
async function api(path, params) {
  const base = (CFG.apiBase || "").replace(/\/$/, "");
  const url = new URL(base + path);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v != null && v !== "") url.searchParams.set(k, String(v));
    }
  }
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`API ${resp.status}`);
  return resp.json();
}

// Fire-and-forget view ping (powers the live Popularity counts). The Worker
// applies the anti-bot checks and dedupes per visitor/day; the client only pings
// once per scene per day (see VIEWED) so we don't hammer the endpoint.
function pingView(id) {
  const base = (CFG.apiBase || "").replace(/\/$/, "");
  try {
    fetch(`${base}/api/view/${encodeURIComponent(id)}`, { method: "POST", keepalive: true }).catch(() => {});
  } catch { /* ignore */ }
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
  { kind: "link", label: "Buy Me a Coffee", url: "https://buymeacoffee.com/mozzarell",
    cta: "Buy a coffee" },
  // --- More options: fill in a real value (replace REPLACE_ME) to switch any of
  // these on. Anything left as a placeholder or blank is hidden automatically. ---
  { kind: "link", label: "Ko-fi", url: "https://ko-fi.com/REPLACE_ME",
    blurb: "Drop a one-off tip — no fees on Ko-fi, no account needed.", cta: "Tip on Ko-fi" },
  { kind: "link", label: "PayPal", url: "https://paypal.me/REPLACE_ME",
    blurb: "Old reliable — one-time or recurring.", cta: "Pay with PayPal" },
  { kind: "link", label: "GitHub Sponsors", url: "https://github.com/sponsors/REPLACE_ME",
    blurb: "Back ongoing development with a monthly sponsorship.", cta: "Sponsor on GitHub" },
  { kind: "cashapp", label: "Cash App", handle: "$REPLACE_ME" },
  { kind: "crypto", label: "Bitcoin", symbol: "BTC", scheme: "bitcoin", address: "REPLACE_ME" },
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

// ---------- search ----------

// Ask the Worker for the top-N scenes by RELEVANCE (bm25 when there's a keyword
// query, chronological when the box is empty). The user-chosen sort
// (newest / oldest / popularity) is applied to that set on the client by sortRows
// — it must NOT change what the DB selects, or "newest" would just rank the whole
// corpus by date and ignore the search. Facets ride along as one JSON `f` param;
// the Worker turns them back into the same EXISTS/IN filter SQL as before.
async function runSearch(text, f, top) {
  return api("/api/search", { q: text, top, f: JSON.stringify(f) });
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

// Facet values that carry no signal — don't render them as badges.
const BADGE_SKIP = new Set(["neutral", "none", "unclear"]);

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
  // When the keyword hit landed in the transcript, the Worker returns a snippet with
  // the matching words <mark>ed — surface it so a half-remembered line jumps out
  // instead of hiding inside the collapsed transcript below.
  const snippet = h.transcript_snip && h.transcript_snip.includes("<mark>")
    ? `<div class="snippet"><span class="snippet-key">Matched dialogue</span> ${markSnippet(h.transcript_snip)}</div>` : "";
  const transcript = h.transcript_text
    ? `<details><summary>Transcript</summary><pre>${esc(h.transcript_text)}</pre></details>` : "";
  const keyframes = keyframeUrls(h.keyframes_json, h.season, h.episode)
    .map((url) => `<img src="${esc(url)}" data-full="${esc(url)}" loading="lazy" decoding="async" alt="" />`).join("");
  // Mood / time / violence badges — confirm a match at a glance, and click one to
  // refine the search by that facet. Skip the "nothing to see here" values so the
  // row stays signal, not noise.
  const badges = [["mood", h.mood], ["time_of_day", h.time_of_day], ["violence_level", h.violence_level]]
    .filter(([, v]) => v && !BADGE_SKIP.has(v))
    .map(([f, v]) => `<button type="button" class="badge-pill badge-${f}${f === "violence_level" ? " v-" + esc(v) : ""}" data-facet="${f}" data-val="${esc(v)}" title="Refine by ${esc(humanize(v))}">${esc(humanize(v))}</button>`)
    .join("");
  const badgeRow = badges ? `<div class="badges">${badges}</div>` : "";
  return `
    <div class="result" data-scene-id="${h.id}">
      <div class="result-head">
        <div>
          <a class="ep" href="#/episode/${code}">${code}</a> ${esc(h.title)}
          &nbsp;<a class="sim" href="#/scene/${h.id}" title="permalink">#${h.scene_index}</a>
        </div>
        <div class="ts">${hhmmss(h.start_s)} – ${hhmmss(h.end_s)}</div>
      </div>
      <div class="meta"><strong>Location:</strong> ${esc(loc)}${locType} &nbsp; <strong>Characters:</strong> ${chars}</div>
      ${badgeRow}
      <div class="summary">${esc(h.summary || "")}</div>
      ${dialogue}
      ${snippet}
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
  observeScenes();
}

function bindKeyframes() {
  $$(".keyframes img").forEach((img) => {
    img.addEventListener("click", () => {
      lightboxImg.src = img.dataset.full;
      lightbox.classList.add("show");
    });
  });
}

// ---------- view tracking (live Popularity signal) ----------
//
// A scene counts as "viewed" once it has been on screen (≥50% visible) for a
// short dwell — not on mere render/scroll-past, which keeps drive-by impressions
// and prefetchers out. We also ping each scene at most once per day per browser
// (VIEWED, day-scoped) so reloads don't spam the endpoint; the Worker does the
// authoritative per-visitor/day dedupe and anti-bot filtering server-side.
const VIEW_DWELL_MS = 2000;
const VIEWED = loadViewed();   // scene ids already pinged today (this browser)
let viewObserver = null;

function loadViewed() {
  try {
    const o = JSON.parse(localStorage.getItem("sdb_viewed") || "{}");
    const day = new Date().toISOString().slice(0, 10);
    return new Set(o.day === day ? o.ids : []);
  } catch { return new Set(); }
}
function rememberViewed(id) {
  VIEWED.add(String(id));
  try {
    const day = new Date().toISOString().slice(0, 10);
    localStorage.setItem("sdb_viewed", JSON.stringify({ day, ids: [...VIEWED].slice(-4000) }));
  } catch { /* storage full / blocked — fine, we just re-ping next load */ }
}

function setupViewTracking() {
  if (!("IntersectionObserver" in window)) return; // graceful no-op
  viewObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const el = e.target;
      if (e.isIntersecting) {
        if (el._viewTimer) continue;
        el._viewTimer = setTimeout(() => {
          const id = el.dataset.sceneId;
          if (id && !VIEWED.has(id)) { rememberViewed(id); pingView(id); }
          viewObserver.unobserve(el);
        }, VIEW_DWELL_MS);
      } else if (el._viewTimer) {
        clearTimeout(el._viewTimer); el._viewTimer = null; // left the viewport before dwell elapsed
      }
    }
  }, { threshold: 0.5 });
}

function observeScenes() {
  if (!viewObserver) return;
  $$(".result[data-scene-id]").forEach((el) => {
    if (!VIEWED.has(el.dataset.sceneId)) viewObserver.observe(el);
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

// ---------- query language (single-box DSL) ----------
//
// Pro users can drive the same facets the chip panel exposes straight from the
// search box: `char:Tony location:restaurant time:night "exact line"`. We parse the
// box into the SAME { q, facets } shape the Worker already understands — so the
// Worker API is unchanged — and merge it with whatever chips are set. The parse is
// echoed back in the "interpreted as" bar so the grammar is discoverable and the
// user can see (and trust) how their words were read.

// field alias -> facet key (the keys are exactly currentFacets()'s shape).
const FIELD_ALIASES = {
  character: "required_characters", char: "required_characters", who: "required_characters", cast: "required_characters",
  exclude: "excluded_characters", without: "excluded_characters",
  location: "location_types", loc: "location_types", place: "location_types", where: "location_types",
  inside: "location_interior_exterior", inout: "location_interior_exterior", interior: "location_interior_exterior",
  time: "time_of_day", when: "time_of_day",
  mood: "mood", vibe: "mood",
  violence: "violence_level",
  activity: "activities", doing: "activities",
  topic: "topics", about: "topics",
  people: "__group__", size: "__group__", crowd: "__group__",
};
// facet key -> filters.json vocab key, for enum / tag resolution.
const ENUM_VOCAB = {
  location_types: "location_types", location_interior_exterior: "interior_exterior",
  time_of_day: "times_of_day", mood: "moods", violence_level: "violence_levels",
};
const TAG_VOCAB = { activities: "activities", topics: "topics" };
const ARRAY_FACETS = new Set(["required_characters", "excluded_characters", "location_types", "activities", "topics"]);
const FACET_DISPLAY = {
  required_characters: "Character", excluded_characters: "Without", location_types: "Location",
  location_interior_exterior: "Inside / outside", time_of_day: "Time", mood: "Mood",
  violence_level: "Violence", activities: "Activity", topics: "Topic",
};
// Everyday words -> the enum value they mean, so `mood:funny` / `time:evening` just work.
const VALUE_SYNONYMS = {
  inside: "interior", indoors: "interior", outside: "exterior", outdoors: "exterior", both: "mixed",
  evening: "night", nighttime: "night", daytime: "day", morning: "day", afternoon: "day", dusk: "dawn_dusk", dawn: "dawn_dusk",
  funny: "comedic", comedy: "comedic", romantic: "intimate", sad: "melancholy", happy: "warm", scary: "tense",
  bloody: "severe", brutal: "severe", gory: "severe",
};

let CHAR_INDEX = null;
function charIndex() {
  if (CHAR_INDEX) return CHAR_INDEX;
  CHAR_INDEX = new Map();
  for (const e of OPTIONS.roster || []) {
    CHAR_INDEX.set(e.canonical_name.toLowerCase(), e.canonical_name);
    for (const a of e.aliases || []) CHAR_INDEX.set(String(a).toLowerCase(), e.canonical_name);
  }
  return CHAR_INDEX;
}

function emptyFacets() {
  return {
    required_characters: [], excluded_characters: [], location_types: [], activities: [], topics: [],
    time_of_day: null, location_interior_exterior: null, mood: null, violence_level: null,
    min_group_size: null, max_group_size: null,
  };
}

function resolveCharacter(raw) {
  const idx = charIndex();
  const norm = String(raw).toLowerCase().trim();
  if (!norm) return null;
  if (idx.has(norm)) return idx.get(norm);
  for (const [k, canon] of idx) if (k.includes(norm)) return canon; // loose substring fallback
  return null;
}

function resolveEnum(facetKey, raw) {
  const vocab = OPTIONS[ENUM_VOCAB[facetKey]] || [];
  let norm = String(raw).toLowerCase().trim();
  norm = VALUE_SYNONYMS[norm] || norm;
  if (!norm) return null;
  for (const v of vocab) if (v.toLowerCase() === norm) return v;
  for (const v of vocab) if (humanize(v).toLowerCase() === norm) return v;
  for (const v of vocab) {
    const lower = v.toLowerCase();
    if (lower.includes(norm) || lower.split(/_+/).some((w) => w.startsWith(norm))) return v;
  }
  return null;
}

function resolveTag(facetKey, raw) {
  const vocab = OPTIONS[TAG_VOCAB[facetKey]] || [];
  const norm = String(raw).toLowerCase().trim().replace(/\s+/g, "_");
  if (!norm) return null;
  for (const v of vocab) if (v === norm) return v;
  for (const v of vocab) if (v.includes(norm) || norm.includes(v)) return v;
  return norm; // not in the top-N vocab, but the Worker may still match an exact tag
}

function parseGroup(raw) {
  const s = String(raw).trim();
  let m;
  if ((m = s.match(/^(\d+)\s*-\s*(\d+)$/))) return { min_group_size: +m[1], max_group_size: +m[2] };
  if ((m = s.match(/^>=\s*(\d+)$/))) return { min_group_size: +m[1] };
  if ((m = s.match(/^>\s*(\d+)$/))) return { min_group_size: +m[1] + 1 };
  if ((m = s.match(/^<=\s*(\d+)$/))) return { max_group_size: +m[1] };
  if ((m = s.match(/^<\s*(\d+)$/))) return { max_group_size: +m[1] - 1 };
  if ((m = s.match(/^(\d+)\+$/))) return { min_group_size: +m[1] };
  if ((m = s.match(/^(\d+)$/))) return { min_group_size: +m[1] };
  return null;
}

// Split a field value into individual values: handles `(a AND b)`, commas, and
// "quoted multi-word" values (e.g. character names with spaces).
function splitValues(raw) {
  let s = String(raw).trim();
  if (s.startsWith("(") && s.endsWith(")")) s = s.slice(1, -1);
  const out = [], cur = [];
  const flush = () => { if (cur.length) { out.push(cur.join(" ")); cur.length = 0; } };
  const re = /"([^"]+)"|(,)|([^\s,]+)/g;
  let m;
  while ((m = re.exec(s))) {
    if (m[1] != null) { flush(); out.push(m[1]); }
    else if (m[2] != null) { flush(); }                 // comma separator
    else if (/^(AND|OR)$/i.test(m[3])) { flush(); }     // boolean separator (both widen the same facet)
    else cur.push(m[3]);
  }
  flush();
  return out.filter(Boolean);
}

function applyField(key, values, facets, interp, warnings) {
  if (key === "__group__") {
    for (const v of values) {
      const g = parseGroup(v);
      if (!g) { warnings.push(`people: "${v}" — try 2-5, >=4, or a number`); continue; }
      Object.assign(facets, g);
      if (g.min_group_size != null) interp.push({ label: "People", text: `≥ ${g.min_group_size}` });
      if (g.max_group_size != null) interp.push({ label: "People", text: `≤ ${g.max_group_size}` });
    }
    return;
  }
  for (const raw of values) {
    let resolved = null;
    if (key === "required_characters" || key === "excluded_characters") resolved = resolveCharacter(raw);
    else if (ENUM_VOCAB[key]) resolved = resolveEnum(key, raw);
    else if (TAG_VOCAB[key]) resolved = resolveTag(key, raw);
    if (!resolved) { warnings.push(`${FACET_DISPLAY[key] || key}: "${raw}" — no match`); continue; }
    if (ARRAY_FACETS.has(key)) { if (!facets[key].includes(resolved)) facets[key].push(resolved); }
    else facets[key] = resolved;
    interp.push({ label: FACET_DISPLAY[key] || key, text: humanize(resolved) });
  }
}

// Parse the search box into { q, facets, interp, warnings }. `q` is the leftover
// free text (bare words + "phrases"); qualifiers become facets. `usedDsl` is true
// when any qualifier/phrase was present, which is what gates the interpreted-as bar.
function parseQuery(input) {
  const facets = emptyFacets();
  const interp = [], warnings = [], bareWords = [], phrases = [];
  let usedDsl = false;
  // field:value | field:(group) | field:"phrase"  ||  "phrase"  ||  bare word
  const re = /(-)?([a-zA-Z_]+):(\([^)]*\)|"[^"]*"|[^\s]+)|"([^"]+)"|(\S+)/g;
  let m;
  while ((m = re.exec(input))) {
    const field = m[2] && FIELD_ALIASES[m[2].toLowerCase()];
    if (field) {
      usedDsl = true;
      const key = (m[1] && field === "required_characters") ? "excluded_characters" : field;
      applyField(key, splitValues(m[3]), facets, interp, warnings);
    } else if (m[4] != null) {       // standalone "quoted phrase" -> phrase search
      usedDsl = true;
      phrases.push(m[4]);
    } else {                          // bare word (or an unknown `word:` -> treat as text)
      bareWords.push(m[0]);
    }
  }
  const q = [...bareWords, ...phrases.map((p) => `"${p}"`)].join(" ").trim();
  if (usedDsl && bareWords.length) interp.unshift({ label: "Keywords", text: bareWords.join(" ") });
  for (const p of phrases) interp.push({ label: "Exact phrase", text: p });
  return { q, facets, interp, warnings, usedDsl };
}

// Union DSL facets onto the chip-panel facets. Arrays merge (deduped); single-value
// fields keep the chip value when both are set (the panel is the explicit source of truth).
function mergeFacets(base, dsl) {
  const out = emptyFacets();
  for (const k of Object.keys(out)) {
    if (Array.isArray(out[k])) {
      const seen = new Set();
      out[k] = [...(base[k] || []), ...(dsl[k] || [])].filter((v) => v != null && !seen.has(v) && seen.add(v));
    } else {
      out[k] = base[k] != null ? base[k] : dsl[k];
    }
  }
  return out;
}

function countFacets(f) {
  let n = 0;
  for (const v of Object.values(f)) {
    if (Array.isArray(v)) n += v.length ? 1 : 0;
    else if (v != null) n += 1;
  }
  return n;
}

// The "interpreted as" bar under the box — only shown once the user reaches for the
// DSL (a qualifier or a phrase) or when something didn't resolve, so plain keyword
// searches stay clutter-free.
function renderInterpBar(parsed) {
  const bar = $("#interp-bar");
  if (!bar) return;
  const chips = parsed.interp.map(
    (i) => `<span class="interp-chip"><span class="interp-key">${esc(i.label)}</span> ${esc(i.text)}</span>`);
  const warns = parsed.warnings.map((w) => `<span class="interp-chip interp-warn">⚠ ${esc(w)}</span>`);
  const all = [...chips, ...warns];
  if ((!parsed.usedDsl && !warns.length) || !all.length) { bar.innerHTML = ""; bar.classList.remove("show"); return; }
  bar.classList.add("show");
  bar.innerHTML = `<span class="interp-label">Reading</span>${all.join("")}`;
}

// ---------- search action ----------

async function doSearch() {
  const raw = $("#q").value.trim();
  const top = Math.max(1, Math.min(parseInt($("#top").value, 10) || 25, 200));
  const sort = $("#sort").value;
  const parsed = parseQuery(raw);
  const f = mergeFacets(currentFacets(), parsed.facets);
  renderInterpBar(parsed);
  if (!parsed.q && countFacets(f) === 0) {
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
    const rows = await runSearch(parsed.q, f, top);
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
  const eps = await api("/api/episodes");
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
  const counts = await api("/api/characters");
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
  statusEl.textContent = `Episode ${code}`;
  const rows = await api(`/api/episode/${code}`);
  const title = rows[0] ? esc(rows[0].title) : "";
  renderScenes(rows, `<div class="season-head">${code} — ${title} · ${rows.length} scenes</div>`);
}

async function viewCharacter(name) {
  statusEl.textContent = `Character: ${name}`;
  const rows = await api(`/api/character/${encodeURIComponent(name)}`);
  renderScenes(rows, `<div class="season-head">${esc(name)} · ${rows.length} scenes${rows.length === 400 ? " (showing first 400)" : ""}</div>`);
}

async function viewLocation(type) {
  statusEl.textContent = `Location: ${type}`;
  const rows = await api(`/api/location/${encodeURIComponent(type)}`);
  renderScenes(rows, `<div class="season-head">${esc(type)} · ${rows.length} scenes${rows.length === 400 ? " (showing first 400)" : ""}</div>`);
}

async function viewScene(id) {
  statusEl.textContent = `Scene ${id}`;
  const rows = await api(`/api/scene/${encodeURIComponent(id)}`);
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

  // crypto: show the full address (mono, wraps) with a copy button. We intentionally
  // DON'T render a `${scheme}:` wallet link — a raw bitcoin:/ethereum: href throws an
  // "unknown protocol" error in desktop browsers that have no wallet registered for it.
  // Copy-and-paste into any wallet works everywhere, so that's the only path we offer.
  return `<div class="support-card">${label}${blurb}
    <p class="support-card-note">Send ${esc(m.symbol || m.label)} to this address from any wallet:</p>
    <div class="support-handle support-addr">
      <code title="${esc(m.address)}">${esc(m.address)}</code>
      <button type="button" class="copy-btn" data-copy="${esc(m.address)}">Copy</button>
    </div>
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
        <h2>Help support this site</h2>
        <p class="support-lede">SopranosDB is a free, ad-free fan project. If it's useful to you,
        a small tip helps cover hosting and keeps the scene tagging going. Thanks for chipping in.</p>
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

// One-time purge of whatever the OLD (download-the-whole-DB) build may have left
// on a returning visitor's machine. The ~22 MB site.db and the SQLite WASM lived
// in the browser's HTTP cache, which scripts can't evict directly — but the new
// build never requests them again, so they're orphaned and the browser reclaims
// them on its own. What IS script-clearable we clear here: Cache Storage,
// IndexedDB (any OPFS/idb the engine created), and stray service workers. Guarded
// by a localStorage flag so it runs at most once per browser.
async function purgeLegacyClientData() {
  if (localStorage.getItem("sdb_purged_v2")) return;
  try {
    if (navigator.serviceWorker?.getRegistrations) {
      for (const reg of await navigator.serviceWorker.getRegistrations()) await reg.unregister();
    }
    if (window.caches?.keys) {
      for (const key of await caches.keys()) await caches.delete(key);
    }
    if (indexedDB?.databases) {
      for (const db of await indexedDB.databases()) if (db.name) indexedDB.deleteDatabase(db.name);
    }
  } catch { /* best-effort cleanup; never block startup on it */ }
  try { localStorage.setItem("sdb_purged_v2", "1"); } catch { /* ignore */ }
}

async function init() {
  // Returning visitors may still hold the old cached DB/WASM — sweep it once, now.
  purgeLegacyClientData();

  try {
    CFG = await (await fetch("config.json")).json();
    OPTIONS = await (await fetch("filters.json")).json();
  } catch (err) {
    statusEl.textContent = "Failed to load config.json / filters.json.";
    return;
  }
  if (!CFG.apiBase) {
    statusEl.textContent = "config.json is missing apiBase — the query API URL isn't set.";
    return;
  }
  buildFilterUI();
  setupViewTracking();

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
  // Click a result's mood/time/violence badge to refine the search by that facet.
  resultsEl.addEventListener("click", (e) => {
    const b = e.target.closest(".badge-pill[data-facet]");
    if (!b) return;
    FILTERS[b.dataset.facet] = b.dataset.val;   // single-value facets
    syncFilterUI();
    if (onSearchView()) doSearch();
    else location.hash = "#/";                   // jump to search, route() runs doSearch
  });
  lightbox.addEventListener("click", () => lightbox.classList.remove("show"));
  window.addEventListener("hashchange", route);

  statusEl.textContent = "Search 8,082 indexed scenes by keyword, or browse by episode / character / filter.";
  route();
}

init();
