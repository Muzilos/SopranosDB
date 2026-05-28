from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float
    fps: float
    audio_sample_rate: int | None


def ffprobe(path: str) -> ProbeResult:
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    out = subprocess.check_output(cmd, text=True)
    info = json.loads(out)
    duration = float(info["format"]["duration"])
    fps = 0.0
    sr: int | None = None
    for s in info["streams"]:
        if s["codec_type"] == "video" and fps == 0.0:
            r = s.get("avg_frame_rate", "0/1")
            num, den = r.split("/")
            if int(den) != 0:
                fps = int(num) / int(den)
        elif s["codec_type"] == "audio" and sr is None:
            sr = int(s.get("sample_rate", 0)) or None
    return ProbeResult(duration_s=duration, fps=fps, audio_sample_rate=sr)
