#!/usr/bin/env python3
"""Build the static SopranosDB site bundle into ``dist/``.

The deployed site is fully static: a Vite-built front-end + the official SQLite
WASM engine (``@sqlite.org/sqlite-wasm``). The read-only ``.db`` is downloaded
once (gzip-compressed) and queried in memory, so the FTS5 + facet SQL runs in
the browser with zero per-query network. No app server, no LLM, no ML model.

This script:
  1. builds the front-end with Vite (npm install + vite build) — emits the app
     bundle, the SQLite worker chunks, and the ~3.6 MB .wasm into dist/assets/;
  2. trims a read-only copy of the corpus DB into dist/site.db;
  3. writes filters.json (facet vocab) and config.json (db/keyframe URLs).

Outputs (under ``--out``, default ``dist/``):
    index.html, assets/*               Vite bundle (app + SQLite worker + .wasm)
    config.json                        runtime config (db url, keyframe base, page size)
    filters.json                       facet vocab (characters, locations, moods, ...)
    site.db                            trimmed read-only DB (base tables + FTS5)
    artifacts -> <ARTIFACTS_DIR>       symlink so `sopranos serve` can show keyframes locally

Local preview:
    python scripts/build_static_site.py
    sopranos serve            # http://127.0.0.1:8000

Production: re-run pointing the front-end at your hosts, then upload the subsets
the script prints (the Vite bundle to a static host; site.db + keyframes to
object storage):
    python scripts/build_static_site.py \
        --db-url https://media.example.com/site.db \
        --keyframe-base https://media.example.com
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

from sopranos.config import (
    ARTIFACTS_DIR, DB_PATH, INTERIOR_EXTERIOR, LOCATION_TYPES, MOODS, SCHEMA_PATH,
    TIMES_OF_DAY, VIOLENCE_LEVELS,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_SRC = REPO_ROOT / "sopranos" / "web" / "static_site"

# The DB is downloaded whole and queried in memory (no range requests), so page
# size only affects the on-disk file a little. 4096 is SQLite's default.
PAGE_SIZE = 4096

# Columns copied into the trimmed scenes table. raw_vlm_json is intentionally
# dropped (debug-only blob); everything FTS indexes or the UI shows is kept.
_SCENE_COLS = (
    "id", "episode_id", "scene_index", "start_s", "end_s", "duration_s", "shot_count",
    "summary", "location_name", "location_type", "location_interior_exterior",
    "time_of_day", "mood", "violence_level", "group_size_total", "background_people_count",
    "dialogue_highlight", "transcript_text", "keyframes_json",
)


def build_site_db(src: Path, dst: Path, view_counts: dict | None = None) -> None:
    """Create a compact, read-only-friendly copy: base tables + FTS5 only,
    no vector table / api_usage / shots / raw_vlm_json, journal_mode=DELETE,
    page_size 1024, VACUUMed."""
    if not src.is_file():
        raise SystemExit(f"source DB not found: {src} (build the corpus first)")
    dst.unlink(missing_ok=True)
    dst.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(dst, isolation_level=None)  # autocommit; VACUUM needs no open txn
    try:
        conn.execute(f"PRAGMA page_size = {PAGE_SIZE}")
        # schema.sql creates the base tables + FTS5 + triggers (no scenes_vec — that
        # lives in db.connection.init_db and we deliberately omit it here).
        conn.executescript(SCHEMA_PATH.read_text())
        conn.execute("DROP TABLE IF EXISTS shots")
        conn.execute("DROP TABLE IF EXISTS api_usage")
        # Per-scene engagement counter for the "Popularity" sort. The served DB is
        # read-only and static, so real view counts are collected out-of-band (the
        # view-tracking Worker) and merged in here at build time via --view-counts;
        # absent that, every scene starts at 0 (Popularity then ties to chrono order).
        conn.execute("ALTER TABLE scenes ADD COLUMN view_count INTEGER NOT NULL DEFAULT 0")

        conn.execute("ATTACH DATABASE ? AS src", (str(src),))
        conn.execute("BEGIN")
        conn.execute("INSERT INTO episodes SELECT * FROM src.episodes")
        cols = ", ".join(_SCENE_COLS)
        src_cols = ", ".join(f"s.{c}" for c in _SCENE_COLS)
        # `labels` concatenates each scene's characters + tag values (activities,
        # topics, tags, objects) into one text blob, so the FTS index makes the
        # structured labels keyword-searchable (bare "Paulie"/"espresso" find a scene
        # even when the word never appears in the summary/transcript). Built from the
        # source side tables here so the scenes_ai trigger indexes it as we insert.
        conn.execute(
            f"INSERT INTO scenes ({cols}, labels) "
            f"SELECT {src_cols}, "
            "TRIM("
            "  COALESCE((SELECT group_concat(character_name, ' ') "
            "            FROM src.scene_characters sc WHERE sc.scene_id = s.id), '')"
            "  || ' ' || "
            "  COALESCE((SELECT group_concat(tag_value, ' ') "
            "            FROM src.scene_tags st WHERE st.scene_id = s.id), '')"
            ") "
            "FROM src.scenes s"
        )
        conn.execute("INSERT INTO scene_characters SELECT * FROM src.scene_characters")
        conn.execute("INSERT INTO scene_tags SELECT * FROM src.scene_tags")
        conn.execute("INSERT INTO characters SELECT * FROM src.characters")
        if view_counts:
            conn.executemany(
                "UPDATE scenes SET view_count = ? WHERE id = ?",
                [(int(c), int(sid)) for sid, c in view_counts.items()],
            )
            print(f"  view counts: merged into {len(view_counts)} scenes")
        conn.execute("COMMIT")
        conn.execute("DETACH DATABASE src")

        conn.execute("PRAGMA journal_mode = DELETE")  # WAL can't be served read-only/static
        conn.execute("INSERT INTO scenes_fts(scenes_fts) VALUES('optimize')")
        conn.execute("VACUUM")
    finally:
        conn.close()

    size_mb = dst.stat().st_size / 1e6
    with sqlite3.connect(dst) as c:
        n_scenes = c.execute("SELECT COUNT(*) FROM scenes").fetchone()[0]
        n_eps = c.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    print(f"  site.db: {n_eps} episodes, {n_scenes} scenes, {size_mb:.1f} MB")


def write_filters_json(db: Path, dst: Path, top_tags: int) -> None:
    """Facet vocab for the UI dropdowns + roster (with aliases) for the
    single-box keyword→facet parser. Single source of truth = config + the DB."""
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        roster = [
            {"canonical_name": r["canonical_name"], "aliases": json.loads(r["aliases_json"] or "[]")}
            for r in conn.execute("SELECT canonical_name, aliases_json FROM characters ORDER BY canonical_name")
        ]
        top_acts = [r[0] for r in conn.execute(
            "SELECT tag_value FROM scene_tags WHERE tag_type='activity' "
            "GROUP BY tag_value ORDER BY COUNT(*) DESC LIMIT ?", (top_tags,))]
        top_topics = [r[0] for r in conn.execute(
            "SELECT tag_value FROM scene_tags WHERE tag_type='topic' "
            "GROUP BY tag_value ORDER BY COUNT(*) DESC LIMIT ?", (top_tags,))]

    payload = {
        "characters": [e["canonical_name"] for e in roster],
        "roster": roster,
        "location_types": LOCATION_TYPES,
        "interior_exterior": INTERIOR_EXTERIOR,
        "times_of_day": TIMES_OF_DAY,
        "moods": MOODS,
        "violence_levels": VIOLENCE_LEVELS,
        "activities": top_acts,
        "topics": top_topics,
    }
    dst.write_text(json.dumps(payload, indent=2))
    print(f"  filters.json: {len(roster)} characters, {len(top_acts)} activities, {len(top_topics)} topics")


def vite_build(out: Path, skip_install: bool) -> None:
    """Build the front-end with Vite and copy its output into ``out``.

    Vite bundles the app, the sqlite-wasm-http worker chunks, and the SQLite
    .wasm into static_site/dist/; we then mirror that into the bundle dir.
    """
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm not found — install Node.js (>=18) to build the front-end.")
    if not skip_install and not (STATIC_SRC / "node_modules").is_dir():
        print("  npm install …")
        subprocess.run([npm, "install"], cwd=STATIC_SRC, check=True)
    print("  vite build …")
    subprocess.run([npm, "run", "build"], cwd=STATIC_SRC, check=True)

    vite_out = STATIC_SRC / "dist"
    for item in vite_out.iterdir():
        dest = out / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)
    assets = list((out / "assets").glob("*")) if (out / "assets").is_dir() else []
    print(f"  front-end: index.html + {len(assets)} bundled assets (app, SQLite worker, .wasm)")


def write_config_json(out: Path, api_base: str, keyframe_base: str,
                      support: list | None = None) -> None:
    """Runtime config the front-end fetches at startup. Keeping the URLs out of
    the JS bundle means a redeploy can re-point the query API / keyframes without
    rebuilding.

    ``apiBase`` is the Cloudflare Worker that serves the search/browse/view API
    (backed by D1). ``support`` (optional) overrides the front-end's built-in
    donation-method list."""
    config = {
        "apiBase": api_base.rstrip("/"),
        "keyframeBase": keyframe_base.rstrip("/"),
    }
    if support:
        config["support"] = support
    (out / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    extra = f", support={len(support)} method(s)" if support else ""
    print(f"  config.json: apiBase={api_base}{extra}")


def link_keyframes(out: Path) -> None:
    link = out / "artifacts"
    if link.is_symlink() or link.exists():
        if link.is_symlink():
            link.unlink()
        else:
            return  # a real dir is already there; leave it
    if ARTIFACTS_DIR.is_dir():
        link.symlink_to(ARTIFACTS_DIR)
        print(f"  artifacts -> {ARTIFACTS_DIR} (local keyframe preview)")
    else:
        print(f"  (skipped keyframe symlink: {ARTIFACTS_DIR} not found)")


def print_deploy_notes(out: Path, api_base: str, keyframe_base: str) -> None:
    db = out / "site.db"
    print("\nDeploy (D1-backed; the browser no longer downloads site.db):")
    print(f"  1. Keyframes -> R2 (preserve <EP>/keyframes/<file> layout):")
    print(f"       python scripts/upload_keyframes_r2.py")
    print(f"  2. Load the corpus into D1 (regenerates the API's data):")
    print(f"       python scripts/load_d1.py")
    print(f"       cd worker && npx wrangler d1 execute sopranosdb --remote --file ../dist/d1_load.sql")
    print(f"  3. Deploy the query Worker (set ALLOWED_ORIGIN in worker/wrangler.toml):")
    print(f"       cd worker && npx wrangler deploy")
    print(f"  4. Static bundle -> Cloudflare Pages (site.db is NOT shipped — it's only")
    print(f"     the source for step 2):")
    print(f"       rm -f {db} {out / 'd1_load.sql'}; npx wrangler pages deploy {out} --project-name sopranosdb")
    print(f"  Front-end URLs come from config.json (no JS rebuild needed to re-point):")
    print(f"     apiBase={api_base!r}, keyframeBase={keyframe_base!r}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the static SopranosDB site bundle.")
    ap.add_argument("--out", default=str(REPO_ROOT / "dist"), help="output directory")
    ap.add_argument("--source-db", default=str(DB_PATH), help="source sopranos.db")
    ap.add_argument("--api-url", default="https://sopranosdb-api.smm321.workers.dev",
                    help="base URL of the D1-backed query Worker (written to config.json as apiBase)")
    ap.add_argument("--keyframe-base", default="artifacts",
                    help="base URL for keyframe images (default: local artifacts symlink)")
    ap.add_argument("--top-tags", type=int, default=40, help="how many top activities/topics to expose")
    ap.add_argument("--support-json", default=None,
                    help="path to a JSON file (a list of donation-method objects) to embed in "
                         "config.json as `support`, overriding the front-end's built-in defaults")
    ap.add_argument("--view-counts", default=None,
                    help="path to a JSON object mapping scene_id -> view_count, baked into "
                         "scenes.view_count to power the Popularity sort")
    ap.add_argument("--skip-install", action="store_true",
                    help="skip `npm install` (assume node_modules is current)")
    ap.add_argument("--no-link-keyframes", action="store_true",
                    help="don't symlink dist/artifacts -> ARTIFACTS_DIR")
    args = ap.parse_args()

    out = Path(args.out)
    if out.exists():
        # Clear stale output but keep the artifacts symlink (cheap to recreate, but
        # avoids rewalking) — simplest is a full clean.
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"Building static site -> {out}")

    support = None
    if args.support_json:
        support = json.loads(Path(args.support_json).read_text())
        if not isinstance(support, list):
            raise SystemExit(f"--support-json must contain a JSON list, got {type(support).__name__}")

    view_counts = None
    if args.view_counts:
        view_counts = json.loads(Path(args.view_counts).read_text())
        if not isinstance(view_counts, dict):
            raise SystemExit("--view-counts must contain a JSON object {scene_id: count}")

    vite_build(out, skip_install=args.skip_install)
    build_site_db(Path(args.source_db), out / "site.db", view_counts)
    write_filters_json(out / "site.db", out / "filters.json", args.top_tags)
    write_config_json(out, args.api_url, args.keyframe_base, support)
    if not args.no_link_keyframes:
        link_keyframes(out)
    print_deploy_notes(out, args.api_url, args.keyframe_base)
    print("\nPreview: sopranos serve   (then open http://127.0.0.1:8000)")


if __name__ == "__main__":
    main()
