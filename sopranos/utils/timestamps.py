from __future__ import annotations


def seconds_to_hhmmss(s: float) -> str:
    s = max(0.0, float(s))
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}"


def hhmmss_to_seconds(t: str) -> float:
    parts = t.split(":")
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    h, m, s = parts
    return int(h) * 3600 + int(m) * 60 + float(s)
