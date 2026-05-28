from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Large/generated data lives on /mnt/d to save WSL VHD space; code stays in REPO_ROOT.
DATA_ROOT = Path(os.environ.get("SOPRANOS_DATA_ROOT", "/mnt/d/SopranosDatabase"))


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip(); v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_dotenv(REPO_ROOT / ".env")
MEDIA_ROOT = Path("/mnt/d/MEDIA/SHOWS/The Sopranos S01-S06 (1999-)")
ARTIFACTS_DIR = DATA_ROOT / "artifacts"
DATA_DIR = REPO_ROOT / "data"             # source-controlled small data (cast roster)
DB_PATH = DATA_ROOT / "db" / "sopranos.db"
SCHEMA_PATH = REPO_ROOT / "sopranos" / "db" / "schema.sql"
ROSTER_PATH = DATA_DIR / "cast_roster.json"
LOG_PATH = DATA_ROOT / "logs" / "pipeline.log"

SEASON_DIRS = {
    1: MEDIA_ROOT / "The Sopranos S01 (360p re-blurip)",
    2: MEDIA_ROOT / "The Sopranos S02 (360p re-blurip)",
    3: MEDIA_ROOT / "The Sopranos S03 (360p re-blurip)",
    4: MEDIA_ROOT / "The Sopranos S04 (360p re-blurip)",
    5: MEDIA_ROOT / "The Sopranos S05 (360p re-blurip)",
    6: MEDIA_ROOT / "The Sopranos S06 (360p re-blurip)",
}

WHISPER_MODEL = "medium.en"  # large-v3 int8 OOMs on 4GB; medium.en fits and is great for clear TV English.
WHISPER_COMPUTE_TYPE = "int8"
WHISPER_DEVICE = "cuda"
WHISPER_LANGUAGE = "en"

SHOT_DETECTOR_THRESHOLD = 27.0

SCENE_MERGE_MAX_GAP_S = 0.5
SCENE_MERGE_MAX_BOUNDARY_SILENCE_S = 1.0      # was 2.0 — split when modest silence at cut
SCENE_MERGE_MIN_COLOR_SIM = 0.6
SCENE_MAX_DURATION_S = 180.0                  # was 240 — force-split anything over 3 min
SCENE_MIN_DURATION_S = 8.0
TITLE_SEQUENCE_SKIP_S = 90.0
SCENE_INTERNAL_SPLIT_MIN_SILENCE_S = 0.8      # for force-splitting long scenes; was 1.5

KEYFRAMES_PER_SCENE = 3
KEYFRAME_JPEG_QUALITY = 85

VLM_MODEL = "claude-haiku-4-5-20251001"
VLM_MAX_CONCURRENT = 8
VLM_MAX_TOKENS = 1024

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384

QUERY_DEFAULT_TOP_K = 10

LOCATION_TYPES = [
    "home_interior",
    "home_exterior",
    "restaurant_or_food_business",
    "bar_or_club",
    "office_or_business",
    "medical_or_therapy",
    "street_or_outdoor",
    "vehicle",
    "warehouse_or_industrial",
    "religious",
    "school",
    "store_or_shop",
    "law_enforcement",
    "hotel_or_lodging",
    "other",
]
INTERIOR_EXTERIOR = ["interior", "exterior", "mixed"]
TIMES_OF_DAY = ["day", "night", "dawn_dusk", "unclear"]
MOODS = [
    "tense", "violent", "comedic", "intimate", "melancholy",
    "celebratory", "neutral", "anxious", "angry", "warm",
]
VIOLENCE_LEVELS = ["none", "implied", "mild", "moderate", "severe"]
