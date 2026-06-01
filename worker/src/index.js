// SopranosDB query API — Cloudflare Worker over a D1 (SQLite) database.
//
// Replaces the old "download the whole 22 MB DB and query it in the browser"
// design: the SQLite FTS5 + facet SQL now runs server-side on D1 and only small
// JSON result sets cross the wire. D1 is also the shared, writable store that
// makes the live "Popularity" view counts possible.
//
// Endpoints (all under /api, JSON responses, CORS-enabled for the site origin):
//   GET  /api/search?q=&top=&f=<json facets>   top-N scenes by bm25 relevance
//   GET  /api/episodes                          season → episode list
//   GET  /api/characters                        roster + per-character scene counts
//   GET  /api/episode/:code  (S01E02)           all scenes in an episode
//   GET  /api/character/:name                   scenes featuring a character
//   GET  /api/location/:type                    scenes at a location type
//   GET  /api/scene/:id                          one scene (permalink)
//   POST /api/view/:id                           record a view (deduped, anti-bot)
//
// Env bindings: DB (D1), ALLOWED_ORIGIN (site origin for CORS + view checks),
// VIEW_SECRET (secret salt for the per-visitor hash; set via `wrangler secret put`).

const SCENE_COLS = `s.id, s.scene_index, s.start_s, s.end_s, s.summary, s.location_name,
  s.location_type, s.time_of_day, s.mood, s.violence_level, s.group_size_total,
  s.dialogue_highlight, s.transcript_text, s.keyframes_json, s.view_count,
  e.season, e.episode, e.title`;

// ---------- FTS query building (mirrors the old client + search.py) ----------

const STOPWORDS = new Set(
  ("a an and are as at be been but by for from had has have he her his in into is it its " +
   "of on or that the their them they this to was were what when which who will with you")
    .split(" "),
);

// Free text -> FTS5 MATCH expression: drop stopwords, quote each token, OR-join.
function ftsMatch(text) {
  let toks = (text || "").match(/[A-Za-z0-9']+/g) || [];
  const content = toks.filter((t) => !STOPWORDS.has(t.toLowerCase()));
  toks = content.length ? content : toks;
  if (!toks.length) return null;
  return toks.map((t) => `"${t}"`).join(" OR ");
}

// Facet object -> [whereSql, params]. Identical shape to the old buildFacetSql.
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
      params.push(kind, String(v).toLowerCase().trim());
    }
  }
  return [where.length ? where.join(" AND ") : "1=1", params];
}

const CHRONO = "e.season, e.episode, s.scene_index";

// ---------- DB helpers ----------

async function rows(env, sql, params = []) {
  const stmt = params.length ? env.DB.prepare(sql).bind(...params) : env.DB.prepare(sql);
  const r = await stmt.all();
  return r.results || [];
}

// Attach a characters[] array to each scene row (one extra query, batched).
async function attachCharacters(env, list) {
  if (!list.length) return list;
  const ids = list.map((r) => r.id);
  const ph = ids.map(() => "?").join(",");
  const cr = await rows(env,
    `SELECT scene_id, character_name FROM scene_characters WHERE uncertain=0 AND scene_id IN (${ph})`, ids);
  const byId = {};
  for (const r of cr) (byId[r.scene_id] ||= []).push(r.character_name);
  for (const r of list) r.characters = byId[r.id] || [];
  return list;
}

const clampTop = (v) => Math.max(1, Math.min(parseInt(v, 10) || 25, 200));

// ---------- query endpoints ----------

async function search(env, url) {
  const text = url.searchParams.get("q") || "";
  const top = clampTop(url.searchParams.get("top"));
  let f = {};
  try { f = JSON.parse(url.searchParams.get("f") || "{}"); } catch { f = {}; }
  const [where, params] = buildFacetSql(f);
  const match = ftsMatch(text);
  let sql, args;
  if (match) {
    // top-N by relevance; the client applies the secondary sort (newest/popularity).
    sql = `SELECT ${SCENE_COLS}, bm25(scenes_fts) AS bm25_score
           FROM scenes_fts JOIN scenes s ON s.id = scenes_fts.rowid
           JOIN episodes e ON e.id = s.episode_id
           WHERE scenes_fts MATCH ? AND ${where}
           ORDER BY bm25_score, ${CHRONO} LIMIT ?`;
    args = [match, ...params, top];
  } else {
    sql = `SELECT ${SCENE_COLS}, 0.0 AS bm25_score
           FROM scenes s JOIN episodes e ON e.id = s.episode_id
           WHERE ${where} ORDER BY ${CHRONO} LIMIT ?`;
    args = [...params, top];
  }
  return attachCharacters(env, await rows(env, sql, args));
}

async function episodes(env) {
  return rows(env, "SELECT season, episode, title FROM episodes ORDER BY season, episode");
}

async function characters(env) {
  return rows(env,
    "SELECT character_name, COUNT(*) AS c FROM scene_characters WHERE uncertain=0 GROUP BY character_name");
}

async function episodeScenes(env, code) {
  const m = /^S(\d{2})E(\d{2})$/.exec(code || "");
  if (!m) return [];
  const list = await rows(env,
    `SELECT ${SCENE_COLS}, 0.0 AS bm25_score FROM scenes s JOIN episodes e ON e.id=s.episode_id
     WHERE e.season=? AND e.episode=? ORDER BY s.scene_index`, [parseInt(m[1], 10), parseInt(m[2], 10)]);
  return attachCharacters(env, list);
}

async function characterScenes(env, name) {
  const list = await rows(env,
    `SELECT ${SCENE_COLS}, 0.0 AS bm25_score FROM scenes s JOIN episodes e ON e.id=s.episode_id
     WHERE EXISTS (SELECT 1 FROM scene_characters sc WHERE sc.scene_id=s.id AND sc.character_name=? AND sc.uncertain=0)
     ORDER BY ${CHRONO} LIMIT 400`, [name]);
  return attachCharacters(env, list);
}

async function locationScenes(env, type) {
  const list = await rows(env,
    `SELECT ${SCENE_COLS}, 0.0 AS bm25_score FROM scenes s JOIN episodes e ON e.id=s.episode_id
     WHERE s.location_type=? ORDER BY ${CHRONO} LIMIT 400`, [type]);
  return attachCharacters(env, list);
}

async function oneScene(env, id) {
  const list = await rows(env,
    `SELECT ${SCENE_COLS}, 0.0 AS bm25_score FROM scenes s JOIN episodes e ON e.id=s.episode_id WHERE s.id=?`,
    [parseInt(id, 10)]);
  return attachCharacters(env, list);
}

// ---------- view counting (anti-bot) ----------

async function sha256(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Record a view, but only count it once per (scene, visitor, day). Several cheap
// gates keep automated traffic out: must be a same-origin POST with a real UA,
// the scene must exist, and the per-visitor/day dedupe caps inflation.
async function recordView(request, env, id) {
  const sceneId = parseInt(id, 10);
  if (!Number.isInteger(sceneId)) return { counted: false, reason: "bad id" };

  // Origin / fetch-metadata gates — drop cross-site and non-browser callers.
  const origin = request.headers.get("Origin") || "";
  if (env.ALLOWED_ORIGIN && origin && origin !== env.ALLOWED_ORIGIN) return { counted: false, reason: "origin" };
  const fetchSite = request.headers.get("Sec-Fetch-Site");
  if (fetchSite && !["same-origin", "same-site", "none"].includes(fetchSite)) return { counted: false, reason: "fetch-site" };
  const ua = request.headers.get("User-Agent") || "";
  if (ua.length < 8 || /\b(bot|crawl|spider|curl|wget|python-requests|headless)\b/i.test(ua)) return { counted: false, reason: "ua" };

  // Per-visitor/day identity — salted hash of IP+UA, never stored raw (privacy).
  const ip = request.headers.get("CF-Connecting-IP") || "0.0.0.0";
  const day = new Date().toISOString().slice(0, 10);
  const visitor = await sha256(`${ip}|${ua}|${day}|${env.VIEW_SECRET || "salt"}`);

  const ins = await env.DB
    .prepare("INSERT OR IGNORE INTO scene_views (scene_id, visitor, day) VALUES (?, ?, ?)")
    .bind(sceneId, visitor, day).run();
  if (ins.meta.changes > 0) {
    // First view from this visitor today — count it. (Guard the UPDATE on a real
    // scene id so a bogus id can't create phantom rows.)
    const upd = await env.DB
      .prepare("UPDATE scenes SET view_count = view_count + 1 WHERE id = ?").bind(sceneId).run();
    return { counted: upd.meta.changes > 0 };
  }
  return { counted: false, reason: "dedupe" };
}

// ---------- HTTP plumbing ----------

// Echo the request Origin for CORS when it's the configured site origin or a
// localhost dev preview; otherwise fall back to the configured origin. (This only
// governs which page may READ the API — view counting is gated separately, and
// stays restricted to ALLOWED_ORIGIN, so dev previews can't inflate real counts.)
function corsHeaders(origin, env) {
  let allow = env.ALLOWED_ORIGIN || "*";
  if (origin && (origin === env.ALLOWED_ORIGIN || /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin))) {
    allow = origin;
  }
  return {
    "Access-Control-Allow-Origin": allow,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin") || "";
    const json = (data, status = 200) => new Response(JSON.stringify(data), {
      status,
      headers: { "Content-Type": "application/json; charset=utf-8", ...corsHeaders(origin, env) },
    });
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(origin, env) });

    const path = url.pathname.replace(/\/+$/, "");
    const seg = path.split("/").filter(Boolean); // ["api", ...]
    const arg = seg[2] ? decodeURIComponent(seg.slice(2).join("/")) : "";

    try {
      if (request.method === "POST" && seg[1] === "view") {
        return json(await recordView(request, env, arg));
      }
      if (request.method !== "GET") return json({ error: "method not allowed" }, 405);

      switch (seg[1]) {
        case "search":     return json(await search(env, url));
        case "episodes":   return json(await episodes(env));
        case "characters": return json(await characters(env));
        case "episode":    return json(await episodeScenes(env, arg));
        case "character":  return json(await characterScenes(env, arg));
        case "location":   return json(await locationScenes(env, arg));
        case "scene":      return json(await oneScene(env, arg));
        default:           return json({ error: "not found" }, 404);
      }
    } catch (err) {
      return json({ error: String(err && err.message || err) }, 500);
    }
  },
};
