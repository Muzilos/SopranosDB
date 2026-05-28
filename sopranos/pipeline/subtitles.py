"""Parse SRT files into the same transcript.json shape that faster-whisper produces.

A per-episode time offset (from data/srt_offsets.json) is added to every cue so
that the resulting timestamps line up with the actual spoken audio in the video.

Pseudo-words are emitted by spreading each cue's words evenly across its time
span. This preserves the word-level granularity that the shot-to-scene merger
relies on to detect silence gaps between cues.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from sopranos.config import DATA_DIR, MEDIA_ROOT


SRT_ROOT = MEDIA_ROOT / "Subtitles"
OFFSETS_PATH = DATA_DIR / "srt_offsets.json"

_TIMECODE_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)
_TAGS_RE = re.compile(r"<[^>]+>")
_FILE_RE = re.compile(r"(\d+)x(\d+)\s*-\s*(.+?)\.en\.srt$", re.IGNORECASE)


@dataclass
class Cue:
    start: float
    end: float
    text: str


def _tc_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def find_srt(season: int, episode: int) -> Path | None:
    season_dir = SRT_ROOT / f"The_Sopranos - season {season}.en"
    if not season_dir.is_dir():
        return None
    target_code = f"{season}x{episode:02d}"
    for p in season_dir.iterdir():
        if target_code.lower() in p.name.lower() and p.suffix.lower() == ".srt":
            return p
    return None


def parse_srt(srt_path: Path) -> list[Cue]:
    """Lenient SRT parser tolerant of BOMs, blank-line noise, and missing indices."""
    raw = srt_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8", errors="replace")
    cues: list[Cue] = []
    for block in re.split(r"\r?\n\r?\n+", text.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        tc_line = None
        body_start = 0
        for i, ln in enumerate(lines):
            if _TIMECODE_RE.search(ln):
                tc_line = ln
                body_start = i + 1
                break
        if tc_line is None:
            continue
        m = _TIMECODE_RE.search(tc_line)
        if not m:
            continue
        start = _tc_to_seconds(*m.group(1, 2, 3, 4))
        end = _tc_to_seconds(*m.group(5, 6, 7, 8))
        body = " ".join(lines[body_start:])
        body = _TAGS_RE.sub("", body).strip()
        if body:
            cues.append(Cue(start=start, end=end, text=body))
    return cues


@lru_cache(maxsize=1)
def _load_offsets() -> dict:
    if not OFFSETS_PATH.exists():
        return {}
    return json.loads(OFFSETS_PATH.read_text())


def offset_for(season: int, episode: int) -> float:
    """Return the recommended shift in seconds to add to SRT timestamps."""
    data = _load_offsets()
    code = f"S{season:02d}E{episode:02d}"
    episodes = data.get("episodes", {})
    e = episodes.get(code, {})
    if "recommended_shift_s" in e:
        return float(e["recommended_shift_s"])
    # Per-season median fallback
    season_shifts = [
        v["recommended_shift_s"]
        for k, v in episodes.items()
        if k.startswith(f"S{season:02d}") and "recommended_shift_s" in v
    ]
    if season_shifts:
        return float(statistics.median(season_shifts))
    # Overall fallback
    all_shifts = [v["recommended_shift_s"] for v in episodes.values() if "recommended_shift_s" in v]
    return float(statistics.median(all_shifts)) if all_shifts else 0.0


def _cue_to_pseudo_words(cue: Cue, offset: float) -> list[dict]:
    """Spread the cue's words evenly across its (shifted) time span."""
    raw_words = re.findall(r"\S+", cue.text)
    if not raw_words:
        return []
    a = cue.start + offset
    b = cue.end + offset
    span = max(b - a, 0.001)
    step = span / len(raw_words)
    out: list[dict] = []
    for i, w in enumerate(raw_words):
        ws = a + i * step
        we = ws + step
        out.append({"start": ws, "end": we, "text": " " + w if i > 0 else w})
    return out


def srt_to_transcript_payload(srt_path: Path, offset_s: float) -> dict:
    """Produce a payload shaped like faster-whisper's output:
    {"language": "en", "duration": <last_word_end>, "segments": [...]}.
    """
    cues = parse_srt(srt_path)
    segments: list[dict] = []
    for c in cues:
        words = _cue_to_pseudo_words(c, offset_s)
        if not words:
            continue
        segments.append({
            "start": c.start + offset_s,
            "end": c.end + offset_s,
            "text": c.text,
            "words": words,
        })
    duration = segments[-1]["end"] if segments else 0.0
    return {"language": "en", "duration": duration, "segments": segments}
