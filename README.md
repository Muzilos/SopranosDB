# SopranosDatabase

Scene-indexed query database for The Sopranos. Breaks every episode into narrative
scenes (~8,000 across all six seasons) and lets you find them with natural-language
queries like:

> sopranos query "scene where Tony and three to five other men are eating dinner at a restaurant"

## Setup

```bash
sudo apt install -y ffmpeg
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
```

`sopranos/config.py` declares paths:
- Source video: `/mnt/d/MEDIA/SHOWS/The Sopranos S01-S06 (1999-)`
- Subtitles: `/mnt/d/MEDIA/SHOWS/The Sopranos S01-S06 (1999-)/Subtitles/`
- Generated data (`artifacts/`, `db/`, `logs/`): `/mnt/d/SopranosDatabase/` (override with `SOPRANOS_DATA_ROOT`)
- Code stays in repo root.

`.env` in the repo root is auto-loaded by `sopranos.config`.

## Usage

```bash
sopranos ingest --episode S01E01                # one episode
sopranos ingest --season 1                      # full season
sopranos ingest --episode S01E01 --force-from label   # re-run a stage and downstream

sopranos query "Tony eating dinner with men at a restaurant"
sopranos query "..." --top 10 --play 1          # launch ffplay at result 1
sopranos query "..." --debug                    # show parsed structured filter

sopranos roster show
sopranos roster add "Mikey Palmice" --alias "Mickey" --description "..." --first-seen S01E03

sopranos stats                                  # episode/scene counts + API spend
sopranos qa sample --n 20                       # random scenes for manual review
```

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
│   ├── query/{parse.py, search.py, play.py}
│   └── utils/{episode_paths.py, timestamps.py, hashing.py}
├── data/
│   ├── cast_roster.json                       # ~45 canonical entries
│   └── srt_offsets.json                       # measured SRT timing offsets
├── scripts/{qa_episode.py, audit_srt_offsets.py, debug_landmarks.py}
└── .env                                       # ANTHROPIC_API_KEY

/mnt/d/SopranosDatabase/                       # generated data (large)
├── artifacts/S0XE0Y/
│   ├── transcript.json, shots.json, scenes.json, scene_labels.json
│   ├── keyframes/scene_NNN_{a,b,c}.jpg
│   └── vlm_cache/<content-hash>.json
├── db/sopranos.db                             # SQLite DB
└── logs/
    ├── pipeline.log
    ├── qa_done.txt                            # QA tracking
    └── qa_runs.jsonl                          # per-episode QA findings
```

## Hardware notes (WSL2 / GTX 1050 Ti / 4 GB VRAM)

The bundled PyTorch CUDA build doesn't support sm_61 (Pascal). So:
- **Embeddings (sentence-transformers): forced to CPU** in `pipeline/embed.py`.
- **ASR: `faster-whisper` is fine on CUDA** because CTranslate2 is independent of PyTorch.
- ASR model is `medium.en` int8 (`large-v3` int8 OOMs on 4GB; `medium.en` is plenty for clear TV English).

## Cost & wall time

For the full 86-episode corpus with SRT + tuned merger + full roster:

| Item | Value |
|---|---|
| Episodes | 86 |
| Scenes indexed | ~8,000 |
| Wall time | ~3-4 h (ASR is bypassed via SRT) |
| API spend | ~$36 (Haiku 4.5 with prompt caching: ~$0.0045/scene) |

The system prompt is ~6,500 tokens (above Haiku 4.5's 4,096-token cache minimum),
so cache_read covers ~85% of input tokens after the first call.

## Known limitations

- A handful of long unbroken-dialogue scenes (5-10 corpus-wide) still exceed 180s
  because there is no large internal silence to split at.
- Background extras and minor recurring characters frequently get generic
  `unknown_man_in_suit_N` labels.
- Deceased characters occasionally still surface in legitimate flashback/photo
  contexts — sometimes correctly tagged `flashback`, sometimes not.
- Embedding-based ranking is a fuzzy match: structured filter is the primary
  retrieval mechanism. If your query needs an enum you didn't say, add it
  explicitly (e.g. `"violent confrontation at night"` to filter on mood/time).
