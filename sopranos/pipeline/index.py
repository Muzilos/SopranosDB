from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from sopranos.db.connection import connect
from sopranos.db.models import SceneLabel
from sopranos.pipeline.embed import embed_texts
from sopranos.roster import load_roster
from sopranos.utils.episode_paths import EpisodeRef


def upsert_episode(conn: sqlite3.Connection, ref: EpisodeRef, duration_s: float, fps: float) -> int:
    cur = conn.execute(
        "INSERT INTO episodes(season, episode, title, file_path, duration_s, fps, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(season, episode) DO UPDATE SET "
        "title=excluded.title, file_path=excluded.file_path, duration_s=excluded.duration_s, "
        "fps=excluded.fps, processed_at=excluded.processed_at "
        "RETURNING id",
        (ref.season, ref.episode, ref.title, str(ref.path), duration_s, fps,
         datetime.now(timezone.utc).isoformat()),
    )
    return cur.fetchone()[0]


def sync_characters(conn: sqlite3.Connection) -> None:
    roster = load_roster()
    for c in roster:
        conn.execute(
            "INSERT INTO characters(canonical_name, aliases_json, description, first_seen) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(canonical_name) DO UPDATE SET "
            "aliases_json=excluded.aliases_json, description=excluded.description, first_seen=excluded.first_seen",
            (c.canonical_name, json.dumps(c.aliases), c.description, c.first_seen),
        )


def replace_episode_artifacts(
    conn: sqlite3.Connection,
    episode_id: int,
    shots: list[dict],
    scenes: list[dict],
    labels_by_scene_idx: dict[int, SceneLabel],
    raw_by_scene_idx: dict[int, dict],
    keyframes_by_scene_idx: dict[int, list[str]],
    transcript_by_scene_idx: dict[int, str],
) -> list[int]:
    conn.execute("DELETE FROM shots WHERE episode_id = ?", (episode_id,))
    conn.execute("DELETE FROM scenes WHERE episode_id = ?", (episode_id,))

    for s in shots:
        conn.execute(
            "INSERT INTO shots(episode_id, shot_index, start_s, end_s) VALUES (?, ?, ?, ?)",
            (episode_id, s["shot_index"], s["start_s"], s["end_s"]),
        )

    scene_ids: list[int] = []
    embed_inputs: list[str] = []

    for sc in scenes:
        idx = sc["scene_index"]
        label = labels_by_scene_idx.get(idx)
        raw = raw_by_scene_idx.get(idx, {})
        keyframes = keyframes_by_scene_idx.get(idx, [])
        transcript_text = transcript_by_scene_idx.get(idx, "")

        cur = conn.execute(
            "INSERT INTO scenes("
            "episode_id, scene_index, start_s, end_s, duration_s, shot_count, "
            "summary, location_name, location_type, location_interior_exterior, "
            "time_of_day, mood, violence_level, group_size_total, background_people_count, "
            "dialogue_highlight, transcript_text, keyframes_json, raw_vlm_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                episode_id, idx, sc["start_s"], sc["end_s"], sc["duration_s"],
                len(sc["shot_indices"]),
                label.summary if label else None,
                label.location_name if label else None,
                label.location_type if label else None,
                label.location_interior_exterior if label else None,
                label.time_of_day if label else None,
                label.mood if label else None,
                label.violence_level if label else None,
                label.group_size_total if label else None,
                label.background_people_count if label else None,
                label.dialogue_highlight if label else None,
                transcript_text,
                json.dumps(keyframes),
                json.dumps(raw),
            ),
        )
        sid = cur.fetchone()[0]
        scene_ids.append(sid)

        if label:
            for name in label.characters:
                conn.execute(
                    "INSERT OR IGNORE INTO scene_characters(scene_id, character_name, uncertain) "
                    "VALUES (?, ?, 0)", (sid, name),
                )
            for name in label.uncertain_characters:
                conn.execute(
                    "INSERT OR IGNORE INTO scene_characters(scene_id, character_name, uncertain) "
                    "VALUES (?, ?, 1)", (sid, name),
                )
            for tag_type, values in (
                ("activity", label.activities),
                ("topic", label.topics),
                ("tag", label.tags),
                ("object", label.notable_objects),
            ):
                for v in values:
                    if v:
                        conn.execute(
                            "INSERT OR IGNORE INTO scene_tags(scene_id, tag_type, tag_value) "
                            "VALUES (?, ?, ?)", (sid, tag_type, v.lower().strip()),
                        )

        embed_inputs.append(_embed_input_for(label, transcript_text))

    # Compute embeddings + write to scenes_vec
    if scene_ids:
        vecs = embed_texts(embed_inputs)
        conn.execute(
            "DELETE FROM scenes_vec WHERE scene_id IN ("
            f"{','.join('?' * len(scene_ids))})", tuple(scene_ids),
        )
        for sid, vec in zip(scene_ids, vecs):
            conn.execute(
                "INSERT INTO scenes_vec(scene_id, embedding) VALUES (?, ?)",
                (sid, vec.astype(np.float32).tobytes()),
            )
    return scene_ids


def _embed_input_for(label: SceneLabel | None, transcript_text: str) -> str:
    if label is None:
        return transcript_text or ""
    bits = [
        label.summary,
        label.location_name,
        label.dialogue_highlight,
        " ".join(label.activities),
        " ".join(label.topics),
        " ".join(label.tags),
        " ".join(label.notable_objects),
        " ".join(label.characters),
    ]
    return " | ".join(b for b in bits if b)


def log_usage(conn: sqlite3.Connection, kind: str, model: str, usage: dict, scene_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO api_usage(ts, kind, model, input_tokens, cache_creation_tokens, "
        "cache_read_tokens, output_tokens, scene_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            kind, model,
            int(usage.get("input_tokens", 0)),
            int(usage.get("cache_creation_input_tokens", 0)),
            int(usage.get("cache_read_input_tokens", 0)),
            int(usage.get("output_tokens", 0)),
            scene_id,
        ),
    )
