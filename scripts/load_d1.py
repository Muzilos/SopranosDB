#!/usr/bin/env python3
"""Generate a D1-loadable SQL file from the trimmed ``dist/site.db``.

The static build (``build_static_site.py``) still produces ``dist/site.db`` — we
just no longer ship it to browsers. Instead this script flattens its base tables
into INSERT statements on top of ``worker/schema_d1.sql`` and rebuilds the FTS5
index, producing one ``.sql`` file to import into Cloudflare D1:

    python scripts/build_static_site.py --skip-install      # makes dist/site.db
    python scripts/load_d1.py                                # -> dist/d1_load.sql
    cd worker && npx wrangler d1 import sopranosdb --remote --file ../dist/d1_load.sql

The generated file DROPs and recreates the corpus tables (idempotent reload) but
leaves ``scene_views`` intact (CREATE IF NOT EXISTS) so live view-dedupe history
survives a corpus refresh. ``scenes.view_count`` is carried over from site.db, so
a refresh preserves accumulated popularity (bake counts in first via
``build_static_site.py --view-counts``).
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_D1 = REPO_ROOT / "worker" / "schema_d1.sql"

# (table, column-list, rows-per-INSERT). scenes get a small chunk because
# transcript_text can be a few KB each and D1 caps per-statement size.
TABLES = [
    ("episodes", ["id", "season", "episode", "title"], 200),
    ("scenes", [
        "id", "episode_id", "scene_index", "start_s", "end_s", "duration_s", "shot_count",
        "summary", "location_name", "location_type", "location_interior_exterior",
        "time_of_day", "mood", "violence_level", "group_size_total", "background_people_count",
        "dialogue_highlight", "transcript_text", "keyframes_json", "labels", "view_count",
    ], 20),
    ("scene_characters", ["scene_id", "character_name", "uncertain"], 200),
    ("scene_tags", ["scene_id", "tag_type", "tag_value"], 200),
    ("characters", ["canonical_name", "aliases_json", "description", "first_seen"], 200),
]

# Dropped before reload, in FK-safe order (fts references scenes as content).
DROP_ORDER = ["scenes_fts", "scene_tags", "scene_characters", "scenes", "episodes", "characters"]


def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def emit(conn: sqlite3.Connection, out) -> None:
    # Idempotent reload: drop corpus tables (keep scene_views' live data).
    out.write("PRAGMA foreign_keys = OFF;\n")
    for t in DROP_ORDER:
        # Virtual tables (scenes_fts) are dropped with plain DROP TABLE too —
        # there is no DROP VIRTUAL TABLE in SQLite.
        out.write(f"DROP TABLE IF EXISTS {t};\n")

    # Schema (scene_views made IF NOT EXISTS so a reload preserves dedupe history).
    schema = SCHEMA_D1.read_text().replace(
        "CREATE TABLE scene_views", "CREATE TABLE IF NOT EXISTS scene_views")
    out.write("\n" + schema + "\n")

    total = 0
    for table, cols, chunk in TABLES:
        collist = ", ".join(cols)
        cur = conn.execute(f"SELECT {collist} FROM {table}")
        batch, n = [], 0
        for row in cur:
            batch.append("(" + ",".join(lit(v) for v in row) + ")")
            if len(batch) >= chunk:
                out.write(f"INSERT INTO {table} ({collist}) VALUES\n" + ",\n".join(batch) + ";\n")
                n += len(batch); batch = []
        if batch:
            out.write(f"INSERT INTO {table} ({collist}) VALUES\n" + ",\n".join(batch) + ";\n")
            n += len(batch)
        print(f"  {table}: {n} rows")
        total += n

    # Populate the external-content FTS index in one shot (no per-row triggers).
    out.write("INSERT INTO scenes_fts(scenes_fts) VALUES('rebuild');\n")
    print(f"  + FTS5 rebuild. total {total} data rows.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate D1 load SQL from dist/site.db.")
    ap.add_argument("--db", default=str(REPO_ROOT / "dist" / "site.db"), help="trimmed source DB")
    ap.add_argument("--out", default=str(REPO_ROOT / "dist" / "d1_load.sql"), help="output SQL file")
    args = ap.parse_args()

    src = Path(args.db)
    if not src.is_file():
        raise SystemExit(f"{src} not found — run build_static_site.py first.")
    out_path = Path(args.out)
    conn = sqlite3.connect(src)
    try:
        with out_path.open("w") as out:
            emit(conn, out)
    finally:
        conn.close()
    mb = out_path.stat().st_size / 1e6
    print(f"Wrote {out_path} ({mb:.1f} MB)")


if __name__ == "__main__":
    main()
