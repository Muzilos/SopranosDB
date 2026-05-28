from __future__ import annotations

import json
from pathlib import Path

from scenedetect import ContentDetector, SceneManager, open_video

from sopranos.config import SHOT_DETECTOR_THRESHOLD


def detect_shots(mp4_path: Path, out_json: Path) -> list[dict]:
    video = open_video(str(mp4_path))
    sm = SceneManager()
    sm.add_detector(ContentDetector(threshold=SHOT_DETECTOR_THRESHOLD))
    sm.detect_scenes(video=video, show_progress=False)
    raw = sm.get_scene_list()
    shots = [
        {"shot_index": i, "start_s": s.get_seconds(), "end_s": e.get_seconds()}
        for i, (s, e) in enumerate(raw)
    ]
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(shots))
    return shots
