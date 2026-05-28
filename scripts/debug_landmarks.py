"""Print landmark matches for one episode to inspect what's happening."""
import sys
sys.path.insert(0, "/root/SopranosDatabase/scripts")
from audit_srt_offsets import (
    parse_srt, load_asr_words, pick_landmarks, fuzzy_match_landmark, srt_path_for
)
from pathlib import Path

key = sys.argv[1] if len(sys.argv) > 1 else "S01E01"
season = int(key[1:3]); episode = int(key[4:6])
srt = srt_path_for(season, episode)
asr = load_asr_words(Path(f"/mnt/d/SopranosDatabase/artifacts/{key}/transcript.json"))
cues = parse_srt(srt)
lms = pick_landmarks(cues, target=20)
print(f"{key}: cues={len(cues)}, asr_words={len(asr)}, landmarks={len(lms)}")
for srt_t, words, raw in lms:
    m = fuzzy_match_landmark(words, asr)
    if m is None:
        print(f"  srt={srt_t:7.2f}  asr=None  off=None  conf=--  | {' '.join(words[:8])}")
    else:
        asr_t, conf = m
        offs = asr_t - srt_t
        print(f"  srt={srt_t:7.2f}  asr={asr_t:7.2f}  off={offs:+7.2f}  conf={conf:.2f}  | {' '.join(words[:8])}")
