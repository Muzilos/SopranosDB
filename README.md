# SopranosDatabase

A scene-indexed **fan database** for The Sopranos. Every episode is broken into
narrative scenes (~8,000 across all six seasons), each labeled with location,
characters, mood, activities, dialogue, and three keyframes. You browse and
search it like a classic pre-LLM fansite: a keyword box (full-text, ranked) plus
structured filters — characters, location, time of day, mood, violence, group
size, activities.

There are two halves:

1. **Ingest pipeline** (offline, GPU + Claude Haiku for labeling) — builds the
   SQLite database from the video files. Run rarely, on your own machine.
2. **Static site** (what gets deployed) — HTML/JS + a WASM SQLite engine that
   runs the search **in the browser** against a read-only `.db` served over HTTP
   range requests. No app server, no LLM, no ML model at request time, so it
   hosts for ~$0 (Cloudflare Pages + R2).

## Setup

```bash
sudo apt install -y ffmpeg            # only needed for ingest

python3 -m venv .venv
source .venv/bin/activate

pip install -e .                      # query CLI + static-site build + preview server
pip install -e ".[ingest]"            # ADD the heavy deps to (re)build the corpus
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env   # only used by ingest (scene labeling)
```

`sopranos/config.py` declares paths:
- Source video: `/mnt/d/MEDIA/SHOWS/The Sopranos S01-S06 (1999-)`
- Subtitles: `/mnt/d/MEDIA/SHOWS/The Sopranos S01-S06 (1999-)/Subtitles/`
- Generated data (`artifacts/`, `db/`, `logs/`): `/mnt/d/SopranosDatabase/` (override with `SOPRANOS_DATA_ROOT`)
- Code stays in repo root.

`.env` in the repo root is auto-loaded by `sopranos.config`.

## Usage

```bash
# --- Search (no LLM, no tokens: FTS5 keyword + rule-based facet matching) ---
sopranos query "Tony eating dinner at a restaurant"
sopranos query "..." --top 25 --play 1     # launch ffplay at result 1 (local only)
sopranos query "..." --debug               # show the parsed structured filter

# --- Build & preview the static site ---
python scripts/build_static_site.py        # -> dist/
sopranos serve                             # preview at http://127.0.0.1:8000

# --- Ingest (rebuild the corpus; needs the [ingest] extras) ---
sopranos ingest --episode S01E01           # one episode
sopranos ingest --season 1                 # full season
sopranos ingest --episode S01E01 --force-from label   # re-run a stage and downstream

# --- Misc ---
sopranos roster show
sopranos roster add "Mikey Palmice" --alias "Mickey" --description "..." --first-seen S01E03
sopranos stats                             # episode/scene counts + ingest API spend
sopranos qa sample --n 20                  # random scenes for manual review
```

## Static site

The deployed product is fully static. Search and browse run client-side:

- **Engine:** [`sqlite-wasm-http`](https://github.com/mmomtchev/sqlite-wasm-http)
  — the **official SQLite WASM** build plus an HTTP-range VFS, bundled by Vite.
  The browser opens the hosted `site.db` and fetches **only the pages each query
  touches** (a few hundred KB), not the whole file. (We previously used
  `sql.js-httpvfs`, but its bundled SQLite is 3.35 and mishandled some FTS5
  queries; the official engine is current and maintained.)
- **Search:** SQLite **FTS5** over summaries / transcripts / dialogue, ranked
  with `bm25()`. Structured filters become hard SQL `WHERE` clauses (same logic
  as `sopranos/query/search.py`, ported to JS in `src/main.js`).
- **Browse:** hash-routed, shareable URLs — `#/episode/S01E01`,
  `#/character/Tony%20Soprano`, `#/location/<type>`, `#/scene/<id>`.
- **Results show keyframes + transcript + metadata only** — no video playback
  (no streaming cost, no copyright exposure).

The front-end is a small Vite app in `sopranos/web/static_site/`
(`index.html`, `src/main.js`, `src/style.css`). `python scripts/build_static_site.py`
runs `npm install` + `vite build`, then assembles `dist/`:

```
dist/
├── index.html                  Vite entry (references the hashed bundle)
├── assets/                      app bundle + SQLite worker chunks + sqlite3-*.wasm (~3.6 MB)
├── config.json                 runtime config (db url, keyframe base) — fetched at startup
├── filters.json                facet vocab (characters, locations, moods, ...)
├── site.db                     trimmed read-only DB (~23 MB)
└── artifacts -> <ARTIFACTS_DIR> symlink so `sopranos serve` shows keyframes locally
```

`site.db` is the production DB minus what the site doesn't need: the vector
table, `api_usage`, `shots`, and the `raw_vlm_json` blob are dropped; page size
is **1024** (smaller pages = less waste per range request, per the library's
recommendation) and `journal_mode=DELETE` (WAL can't be served read-only).
57 MB → ~23 MB.

Putting the URLs in `config.json` (not the JS bundle) means a redeploy can
re-point the DB/keyframes to a new host **without** an `npm`/Vite rebuild.

`sopranos serve` is a small **Range-capable** static server (the stdlib one
ignores `Range`); it mimics how a real static host behaves so the WASM engine
works locally. For a fully local preview, build with the defaults
(`--db-url site.db --keyframe-base artifacts`) so it reads the bundled `site.db`
and the `artifacts/` symlink instead of R2.

> No `SharedArrayBuffer` / COOP-COEP headers are required: the engine
> auto-falls back to a synchronous HTTP backend. (That also avoids having to add
> cross-origin-resource headers to every R2 keyframe.)

## Deploy to Cloudflare ($0 tier)

Hosting splits across two free Cloudflare services:

- **R2** (object storage, zero egress fees): `site.db` + the ~25k keyframe JPGs (~743 MB).
- **Pages** (static hosting): the Vite bundle — `index.html`, `assets/`,
  `config.json`, `filters.json`.

### 1. Credentials (add to `.env`)

See **[Granting Claude deploy access](#granting-claude-deploy-access)** below for
exactly which API-token permissions to create. The deploy uses:

```
CLOUDFLARE_API_TOKEN=...          # Pages deploy + R2 bucket management
CLOUDFLARE_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...              # R2 S3 keys, for fast bulk upload
R2_SECRET_ACCESS_KEY=...
R2_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com
R2_BUCKET=sopranosdb
R2_PUBLIC_BASE=https://pub-xxxxxxxx.r2.dev   # bucket public URL (after enabling public access)
```

### 2. One-time bucket setup

Create the bucket, enable its public `r2.dev` URL, and set CORS so the browser
may fetch `site.db` with `Range` cross-origin (keyframes load via `<img>` and
need no CORS). `wrangler` expects Cloudflare's `{rules: [...]}` schema (**not**
the S3 `[...]` array):

```bash
npx wrangler r2 bucket create "$R2_BUCKET"
npx wrangler r2 bucket dev-url enable "$R2_BUCKET"   # the pub-xxxx.r2.dev URL

cat > /tmp/r2-cors.json <<'JSON'
{ "rules": [ {
  "allowed": { "origins": ["*"], "methods": ["GET","HEAD"], "headers": ["range","content-type"] },
  "exposeHeaders": ["Content-Range","Content-Length","Accept-Ranges","ETag"],
  "maxAgeSeconds": 3600
} ] }
JSON
npx wrangler r2 bucket cors set "$R2_BUCKET" --file /tmp/r2-cors.json
```

### 3. Build with production URLs, upload, deploy

```bash
# Build so config.json points the front-end at R2 (db + keyframes):
python scripts/build_static_site.py \
    --db-url "$R2_PUBLIC_BASE/site.db" \
    --keyframe-base "$R2_PUBLIC_BASE"

# Upload the DB and the ~25k keyframes to R2:
npx wrangler r2 object put "$R2_BUCKET/site.db" --file dist/site.db \
    --content-type application/x-sqlite3 --remote
python scripts/upload_keyframes_r2.py          # parallel, resumable; skips existing

# Deploy the Vite bundle to Pages (drop site.db — it's served from R2):
rm dist/site.db
npx wrangler pages deploy dist --project-name sopranosdb --branch main --commit-dirty=true
```

> Keyframe URLs are `<R2_PUBLIC_BASE>/S0xE0y/keyframes/scene_NNN_x.jpg` — exactly
> the on-disk `artifacts/` layout, which is why `upload_keyframes_r2.py` maps 1:1.
> `upload_keyframes_r2.py` derives the R2 **S3** credentials from
> `CLOUDFLARE_API_TOKEN` (Access Key ID = token id, Secret = SHA-256 of the
> token), so a single Cloudflare token drives Pages, the bucket, and the upload —
> the `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` in `.env` are optional.

## Granting Claude deploy access

To let me deploy and manage the site, create the credentials below and add them
to `.env`. Scope them tightly; you can revoke/rotate after a deploy.

**A. Cloudflare API token** — *My Profile → API Tokens → Create Token → Custom token.*
Account-scoped permissions:

| Permission | Level | Access | Why |
|---|---|---|---|
| Workers R2 Storage | Account | **Edit** | create/manage the R2 bucket, objects, CORS, public URL |
| Cloudflare Pages | Account | **Edit** | create the Pages project and deploy |
| Account Settings | Account | **Read** | wrangler reads account info |

Set **Account Resources → Include → (your account)**. No Zone permissions needed
unless you later attach a custom domain (then add **Zone → DNS: Edit** for that zone).
→ `CLOUDFLARE_API_TOKEN`. The account ID is on the dashboard sidebar / R2 page →
`CLOUDFLARE_ACCOUNT_ID`.

**B. R2 S3 API token (optional)** — token A is sufficient on its own:
`upload_keyframes_r2.py` derives working S3 credentials from `CLOUDFLARE_API_TOKEN`
(Access Key ID = the token's id, Secret = SHA-256 of the token), and `wrangler`
handles the bucket, CORS, and the DB upload. If you'd rather use explicit,
separately-revocable S3 keys, create one at *R2 → "Manage R2 API Tokens"*
(**Object Read & Write**, scoped to the bucket) and set `R2_ACCESS_KEY_ID` /
`R2_SECRET_ACCESS_KEY` / `R2_ENDPOINT`.

`R2_ENDPOINT` is `https://<account_id>.r2.cloudflarestorage.com`; `R2_PUBLIC_BASE`
is the `pub-xxxx.r2.dev` URL shown after you enable public access on the bucket.
With token A in `.env`, tell me the bucket name and whether you want the `r2.dev`
URL or a custom domain, and I'll set CORS, run the upload, and deploy.

## Pipeline

| # | Stage | Tool | Output |
|---|-------|------|--------|
| 1 | probe | ffprobe | row in `episodes` |
| 2 | audio | ffmpeg (skipped if SRT available) | `audio.wav` (temp) |
| 3 | asr   | **SRT parser (preferred) → faster-whisper fallback** | `transcript.json` |
| 4 | shots | PySceneDetect ContentDetector | `shots.json` |
| 5 | scenes | heuristic merge: silence + color similarity + gap; 8s min / 180s max | `scenes.json` |
| 6 | keyframes | ffmpeg + OpenCV, 3 per scene via pHash diversity | `keyframes/scene_NNN_{a,b,c}.jpg` |
| 7 | label | Claude Haiku 4.5 vision, async (8-way), prompt-cached, per-scene content-hash cache | `scene_labels.json` |
| 8 | embed | sentence-transformers all-MiniLM-L6-v2 (CPU) | numpy array |
| 9 | index | SQLite + sqlite-vec | rows in `sopranos.db` |

Stages are resumable. Each writes a `_done_NN_<stage>` sentinel in `artifacts/<episode>/`.
Use `--force-from <stage>` to invalidate that stage and everything downstream.

> Embeddings (stages 8–9's vector table) are **not used by the deployed site** —
> the static build drops them and search ranks with FTS5 `bm25()`. They remain in
> the source DB for possible future use; ripping them out of ingest is optional.

### SRT vs Whisper

If a matching SRT exists at `Subtitles/The_Sopranos - season N.en/...`, the parser
uses it (with a per-episode timing offset from `data/srt_offsets.json`, generated
once via `scripts/audit_srt_offsets.py`). Otherwise falls back to faster-whisper
`medium.en` int8 on GPU. SRT is ~50× faster and more accurate on proper nouns.

To regenerate offsets after a new season is transcribed:

```bash
.venv/bin/python scripts/audit_srt_offsets.py
```

### Scene merger

A "narrative scene" is many shots sharing location/time. Adjacent shots merge into
the same scene when **all** hold at the boundary:
- gap ≤ 0.5s,
- silence around the cut < 1.0s (from transcript word timestamps),
- HSV color-histogram cosine similarity ≥ 0.6.

Then any merged scene >180s is recursively force-split at internal silence gaps ≥0.8s.
Scenes <8s merge into the nearest neighbor. First 90s skipped except for E01 of each
season (cold opens).

### Character ID

Done entirely by the VLM (Claude Haiku) reading the keyframes + scene transcript.
The system prompt embeds the full cast roster from `data/cast_roster.json`. Each
entry has `canonical_name`, `aliases`, a physical `description` (Steve Buscemi /
James Gandolfini level detail), `first_seen`, and optional `deceased_after`. The
prompt tells the VLM:
- Use canonical names only; `unknown_*` for others
- Don't name a character in episodes after their `deceased_after` (flashbacks OK if tagged)
- Tony Soprano (heavy, bald) vs Tony Blundetto (skinny, gaunt) explicit disambiguation
- Flashback/dream-sequence rule for young versions or alternate-reality scenes

## Storage layout

```
/root/SopranosDatabase/                        # code + small data
├── sopranos/                                  # main Python package
│   ├── cli.py, config.py, roster.py
│   ├── pipeline/{probe,audio,asr,subtitles,shots,scenes,keyframes,vlm,embed,index,orchestrator}.py
│   ├── db/{schema.sql, connection.py, models.py}
│   ├── query/{parse_local.py, search.py, play.py}   # FTS5 + rule-based facets (no LLM)
│   └── web/
│       ├── static_site/{index.html, app.js, style.css}   # the static front-end
│       └── range_server.py                    # Range-capable preview server
├── scripts/{build_static_site.py, qa_episode.py, audit_srt_offsets.py, debug_landmarks.py}
├── dist/                                       # built static bundle (generated)
└── .env                                        # ANTHROPIC_API_KEY (ingest) + CLOUDFLARE_*/R2_* (deploy)

/mnt/d/SopranosDatabase/                       # generated data (large)
├── artifacts/S0XE0Y/
│   ├── transcript.json, shots.json, scenes.json, scene_labels.json
│   ├── keyframes/scene_NNN_{a,b,c}.jpg
│   └── vlm_cache/<content-hash>.json
├── db/sopranos.db                             # SQLite DB (source of truth)
└── logs/
    ├── pipeline.log
    ├── qa_done.txt                            # QA tracking
    └── qa_runs.jsonl                          # per-episode QA findings
```

## Hardware notes (WSL2 / GTX 1050 Ti / 4 GB VRAM)

Only relevant to **ingest**. The bundled PyTorch CUDA build doesn't support sm_61
(Pascal). So:
- **Embeddings (sentence-transformers): forced to CPU** in `pipeline/embed.py`.
- **ASR: `faster-whisper` is fine on CUDA** because CTranslate2 is independent of PyTorch.
- ASR model is `medium.en` int8 (`large-v3` int8 OOMs on 4GB; `medium.en` is plenty for clear TV English).

## Cost & wall time

**Ingest** (one-time, full 86-episode corpus with SRT + tuned merger + full roster):

| Item | Value |
|---|---|
| Episodes | 86 |
| Scenes indexed | ~8,000 |
| Wall time | ~3-4 h (ASR is bypassed via SRT) |
| Ingest API spend | ~$36 (Haiku 4.5 with prompt caching: ~$0.0045/scene) |

**Serving:** ~$0. Search is client-side (no API calls, no tokens). Hosting fits
Cloudflare's free tier — Pages is free, R2 has a free storage tier with **zero
egress fees**, so the ~765 MB of `site.db` + keyframes serves for free.

## Known limitations

- A handful of long unbroken-dialogue scenes (5-10 corpus-wide) still exceed 180s
  because there is no large internal silence to split at.
- Background extras and minor recurring characters frequently get generic
  `unknown_man_in_suit_N` labels.
- Deceased characters occasionally still surface in legitimate flashback/photo
  contexts — sometimes correctly tagged `flashback`, sometimes not.
- Search is keyword (FTS5 `bm25()`) + exact structured filters. It matches the
  words in summaries/transcripts/dialogue, not fuzzy concepts — if a query needs a
  facet, set it explicitly (character, location, mood, activity, group size).
