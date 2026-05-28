"""Sample scenes from one or more episodes and dump rich evidence for a QA agent.

Usage:
    python scripts/qa_episode.py S02E05 S02E06 ...

For each episode it prints, per sampled scene:
- ASR-vs-SRT timing sanity: start/end of the scene and 2-3 transcript lines that fall in it
- Character list, location, summary
- Keyframe file paths (the agent can view them with Read)
- Adjacent shots' raw timestamps so you can eyeball boundary plausibility
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

from sopranos.config import ARTIFACTS_DIR
from sopranos.db.connection import connect
from sopranos.utils.timestamps import seconds_to_hhmmss


def dump_episode(code: str, n_scenes: int = 5) -> None:
    season = int(code[1:3]); episode = int(code[4:6])
    art_dir = ARTIFACTS_DIR / code
    transcript = json.loads((art_dir / "transcript.json").read_text())
    shots = json.loads((art_dir / "shots.json").read_text())
    scenes_raw = json.loads((art_dir / "scenes.json").read_text())
    shot_by_idx = {s["shot_index"]: s for s in shots}

    with connect() as conn:
        ep_row = conn.execute(
            "SELECT id, title FROM episodes WHERE season=? AND episode=?",
            (season, episode),
        ).fetchone()
        if not ep_row:
            print(f"!! {code}: not indexed in DB")
            return
        ep_id = ep_row["id"]
        scenes = conn.execute(
            "SELECT id, scene_index, start_s, end_s, duration_s, shot_count, "
            "summary, location_name, location_type, group_size_total, mood, "
            "transcript_text, keyframes_json "
            "FROM scenes WHERE episode_id=? ORDER BY scene_index", (ep_id,),
        ).fetchall()
        n = min(n_scenes, len(scenes))
        # Sample skipping the title sequence (first 3 scenes)
        candidates = scenes[3:] if len(scenes) > 5 else scenes
        sample = random.sample(list(candidates), min(n, len(candidates)))
        sample.sort(key=lambda r: r["scene_index"])

        print(f"\n========== {code} ({ep_row['title']}) ==========")
        print(f"total scenes: {len(scenes)}, transcript segments: {len(transcript['segments'])}, shots: {len(shots)}")
        for r in sample:
            chars = [row[0] for row in conn.execute(
                "SELECT character_name FROM scene_characters WHERE scene_id=? AND uncertain=0",
                (r["id"],),
            ).fetchall()]
            uncertain = [row[0] for row in conn.execute(
                "SELECT character_name FROM scene_characters WHERE scene_id=? AND uncertain=1",
                (r["id"],),
            ).fetchall()]
            scn_raw = next((s for s in scenes_raw if s["scene_index"] == r["scene_index"]), None)
            shot_idxs = scn_raw["shot_indices"] if scn_raw else []
            print(f"\n--- scene {r['scene_index']} ({seconds_to_hhmmss(r['start_s'])} -> {seconds_to_hhmmss(r['end_s'])}, {r['duration_s']:.1f}s, {r['shot_count']} shots) ---")
            print(f"  location: {r['location_name']!r}  type={r['location_type']}  mood={r['mood']}  group={r['group_size_total']}")
            print(f"  summary: {r['summary']}")
            print(f"  characters: {', '.join(chars)}" + (f"  uncertain={uncertain}" if uncertain else ""))
            print(f"  shots in scene: {len(shot_idxs)}  first/last shot: "
                  f"{shot_by_idx[shot_idxs[0]]['start_s']:.2f}/{shot_by_idx[shot_idxs[-1]]['end_s']:.2f}" if shot_idxs else "")
            print(f"  keyframes: {json.loads(r['keyframes_json'])}")
            # Transcript lines that fall inside the scene
            print(f"  transcript lines inside scene:")
            count = 0
            for seg in transcript["segments"]:
                if seg["end"] < r["start_s"] or seg["start"] > r["end_s"]:
                    continue
                print(f"    [{seg['start']:.1f}s] {seg['text']!r}")
                count += 1
                if count >= 6:
                    print(f"    ... (+{sum(1 for s in transcript['segments'] if s['end']>=r['start_s'] and s['start']<=r['end_s'])-6} more)")
                    break


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        print("Usage: qa_episode.py S02E05 [S02E06 ...]")
        sys.exit(2)
    random.seed(42)
    for code in argv[1:]:
        try:
            dump_episode(code)
        except Exception as e:
            print(f"!! {code}: {e}")


if __name__ == "__main__":
    main(sys.argv)
