from __future__ import annotations

import shutil
import subprocess

from sopranos.query.search import SearchHit


def play(hit: SearchHit) -> None:
    if not shutil.which("ffplay"):
        print("ffplay not found in PATH; install ffmpeg.")
        return
    duration = max(1.0, hit.end_s - hit.start_s)
    cmd = [
        "ffplay", "-autoexit", "-ss", f"{hit.start_s:.3f}", "-t", f"{duration:.3f}",
        "-window_title", f"S{hit.season:02d}E{hit.episode:02d} scene {hit.scene_index}",
        hit.file_path,
    ]
    subprocess.Popen(cmd)
