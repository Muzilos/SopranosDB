from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from sopranos.config import KEYFRAME_JPEG_QUALITY, KEYFRAMES_PER_SCENE


def _phash(frame: np.ndarray) -> int:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct = cv2.dct(small)
    block = dct[:8, :8]
    med = float(np.median(block.flatten()[1:]))
    bits = (block > med).flatten()
    h = 0
    for b in bits:
        h = (h << 1) | int(b)
    return h


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def extract_keyframes(
    mp4_path: Path,
    scene_start_s: float,
    scene_end_s: float,
    shot_starts_ends: list[tuple[float, float]],
    out_dir: Path,
    scene_idx: int,
) -> list[Path]:
    """Pick KEYFRAMES_PER_SCENE diverse frames and write JPEGs. Returns ordered paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(mp4_path))
    try:
        if not shot_starts_ends:
            # Even sampling
            duration = scene_end_s - scene_start_s
            candidates_t = [scene_start_s + duration * f for f in (0.25, 0.5, 0.75)]
        elif len(shot_starts_ends) == 1:
            s, e = shot_starts_ends[0]
            candidates_t = [s + (e - s) * f for f in (0.25, 0.5, 0.75)]
        elif len(shot_starts_ends) == 2:
            s1, e1 = shot_starts_ends[0]; s2, e2 = shot_starts_ends[1]
            candidates_t = [(s1 + e1) / 2.0, (s2 + e2) / 2.0,
                            ((s1 + e1) / 2.0 + (s2 + e2) / 2.0) / 2.0]
        else:
            candidates_t = [(s + e) / 2.0 for s, e in shot_starts_ends]

        frames: list[tuple[float, np.ndarray]] = []
        for t in candidates_t:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t * 1000.0))
            ok, frame = cap.read()
            if ok:
                frames.append((t, frame))
        if not frames:
            return []

        # Pick KEYFRAMES_PER_SCENE via greedy max-min Hamming on pHash
        n = min(KEYFRAMES_PER_SCENE, len(frames))
        hashes = [_phash(f) for _, f in frames]
        chosen_idx = [0]
        while len(chosen_idx) < n:
            best_i, best_dist = -1, -1
            for i in range(len(frames)):
                if i in chosen_idx:
                    continue
                d = min(_hamming(hashes[i], hashes[j]) for j in chosen_idx)
                if d > best_dist:
                    best_dist, best_i = d, i
            if best_i == -1:
                break
            chosen_idx.append(best_i)
        chosen_idx.sort(key=lambda i: frames[i][0])

        paths: list[Path] = []
        suffix = "abc"
        for k, ci in enumerate(chosen_idx):
            out = out_dir / f"scene_{scene_idx:03d}_{suffix[k]}.jpg"
            cv2.imwrite(str(out), frames[ci][1],
                        [cv2.IMWRITE_JPEG_QUALITY, KEYFRAME_JPEG_QUALITY])
            paths.append(out)
        return paths
    finally:
        cap.release()
