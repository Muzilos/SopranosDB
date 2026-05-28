from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from sopranos.config import (
    SCENE_INTERNAL_SPLIT_MIN_SILENCE_S, SCENE_MAX_DURATION_S,
    SCENE_MERGE_MAX_BOUNDARY_SILENCE_S, SCENE_MERGE_MAX_GAP_S,
    SCENE_MERGE_MIN_COLOR_SIM, SCENE_MIN_DURATION_S, TITLE_SEQUENCE_SKIP_S,
)


@dataclass
class Scene:
    scene_index: int
    start_s: float
    end_s: float
    shot_indices: list[int]

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def _hsv_hist(frame: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1, 2], None, [3, 3, 3], [0, 180, 0, 256, 0, 256])
    h = cv2.normalize(h, h).flatten()
    return h.astype(np.float32)


def _read_frame(cap: cv2.VideoCapture, t_s: float) -> np.ndarray | None:
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t_s * 1000.0))
    ok, frame = cap.read()
    return frame if ok else None


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a)); nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _max_silence_between(words: list[dict], t0: float, t1: float) -> float:
    """Largest word-gap entirely within [t0, t1], counting edges as silence."""
    if t1 <= t0:
        return 0.0
    in_window = [w for w in words if w["end"] > t0 and w["start"] < t1]
    if not in_window:
        return t1 - t0
    biggest = max(in_window[0]["start"] - t0, t1 - in_window[-1]["end"])
    for prev, nxt in zip(in_window, in_window[1:]):
        biggest = max(biggest, nxt["start"] - prev["end"])
    return max(biggest, 0.0)


def merge_shots_to_scenes(
    mp4_path: Path,
    shots: list[dict],
    transcript: dict,
    is_first_episode_of_season: bool,
    out_json: Path,
) -> list[Scene]:
    skip_until = 0.0 if is_first_episode_of_season else TITLE_SEQUENCE_SKIP_S
    shots = [s for s in shots if s["end_s"] > skip_until]
    if not shots:
        out_json.write_text("[]")
        return []
    words: list[dict] = [w for seg in transcript.get("segments", []) for w in seg["words"]]

    cap = cv2.VideoCapture(str(mp4_path))
    try:
        # Pre-extract a representative frame per shot end and shot start.
        # We grab the last frame of each shot and first frame of next at the actual
        # boundary timestamps; histogram cache keyed by (shot_index, side).
        hist_cache: dict[tuple[int, str], np.ndarray] = {}

        def hist_at(shot_idx_in_list: int, side: str) -> np.ndarray | None:
            key = (shot_idx_in_list, side)
            if key in hist_cache:
                return hist_cache[key]
            shot = shots[shot_idx_in_list]
            t = max(shot["start_s"], shot["end_s"] - 0.1) if side == "end" else shot["start_s"] + 0.05
            frame = _read_frame(cap, t)
            if frame is None:
                hist_cache[key] = None
                return None
            h = _hsv_hist(frame)
            hist_cache[key] = h
            return h

        groups: list[list[int]] = [[0]]
        for i in range(1, len(shots)):
            prev = shots[i - 1]
            cur = shots[i]
            gap = cur["start_s"] - prev["end_s"]
            silence_window_start = max(prev["end_s"] - 0.75, prev["start_s"])
            silence_window_end = min(cur["start_s"] + 0.75, cur["end_s"])
            silence = _max_silence_between(words, silence_window_start, silence_window_end)
            ha = hist_at(i - 1, "end"); hb = hist_at(i, "start")
            color_sim = _cosine(ha, hb) if ha is not None and hb is not None else 0.0
            same_scene = (
                gap <= SCENE_MERGE_MAX_GAP_S
                and silence < SCENE_MERGE_MAX_BOUNDARY_SILENCE_S
                and color_sim >= SCENE_MERGE_MIN_COLOR_SIM
            )
            if same_scene:
                groups[-1].append(i)
            else:
                groups.append([i])

        # Build scenes from groups
        scenes: list[Scene] = []
        for g in groups:
            start = shots[g[0]]["start_s"]
            end = shots[g[-1]]["end_s"]
            scenes.append(Scene(
                scene_index=-1,
                start_s=start, end_s=end,
                shot_indices=[shots[i]["shot_index"] for i in g],
            ))

        scenes = _apply_min_max_duration(scenes, shots, words)
    finally:
        cap.release()

    for i, sc in enumerate(scenes):
        sc.scene_index = i

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps([
        {"scene_index": s.scene_index, "start_s": s.start_s, "end_s": s.end_s,
         "duration_s": s.duration_s, "shot_indices": s.shot_indices}
        for s in scenes
    ]))
    return scenes


def _apply_min_max_duration(
    scenes: list[Scene], shots: list[dict], words: list[dict],
) -> list[Scene]:
    # Merge short scenes into closest neighbor
    out: list[Scene] = []
    for sc in scenes:
        if sc.duration_s < SCENE_MIN_DURATION_S and out:
            prev = out[-1]
            prev.end_s = sc.end_s
            prev.shot_indices.extend(sc.shot_indices)
        elif sc.duration_s < SCENE_MIN_DURATION_S and not out:
            out.append(sc)
        else:
            out.append(sc)
    # Force-split very long scenes recursively at largest internal silence
    shot_by_idx = {s["shot_index"]: s for s in shots}

    def try_split(sc: Scene) -> list[Scene]:
        if sc.duration_s <= SCENE_MAX_DURATION_S:
            return [sc]
        split_t = _largest_internal_silence_split(sc, words)
        if (split_t is None
                or split_t - sc.start_s < SCENE_MIN_DURATION_S
                or sc.end_s - split_t < SCENE_MIN_DURATION_S):
            return [sc]
        left_shots = [si for si in sc.shot_indices if shot_by_idx[si]["start_s"] < split_t]
        right_shots = [si for si in sc.shot_indices if si not in left_shots]
        if not left_shots or not right_shots:
            return [sc]
        left = Scene(-1, sc.start_s, split_t, left_shots)
        right = Scene(-1, split_t, sc.end_s, right_shots)
        return try_split(left) + try_split(right)

    final: list[Scene] = []
    for sc in out:
        final.extend(try_split(sc))
    return final


def _largest_internal_silence_split(sc: Scene, words: list[dict]) -> float | None:
    in_window = [w for w in words if w["start"] >= sc.start_s and w["end"] <= sc.end_s]
    if len(in_window) < 2:
        return None
    best_gap = 0.0; best_t: float | None = None
    for prev, nxt in zip(in_window, in_window[1:]):
        gap = nxt["start"] - prev["end"]
        if gap > best_gap:
            best_gap = gap
            best_t = (prev["end"] + nxt["start"]) / 2.0
    return best_t if best_gap >= SCENE_INTERNAL_SPLIT_MIN_SILENCE_S else None


def transcript_text_for_scene(transcript: dict, start_s: float, end_s: float) -> str:
    out: list[str] = []
    for seg in transcript.get("segments", []):
        if seg["end"] < start_s or seg["start"] > end_s:
            continue
        out.append(seg["text"].strip())
    return " ".join(out).strip()
