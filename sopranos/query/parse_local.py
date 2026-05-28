"""Rule-based query parser. Zero LLM calls, zero tokens.

Covers most common query shapes: characters from the roster, location keywords,
time-of-day / mood / violence / activity keywords, group-size phrases, and
"without X" exclusions. Anything it can't match becomes part of `semantic_query`
and falls through to the embedding-based re-rank.

For exotic queries the LLM parser still has the edge; the trade-off is
zero cost vs. fewer correctly inferred hard filters.
"""
from __future__ import annotations

import re

from sopranos.db.models import QueryFilter
from sopranos.roster import load_roster

# --- keyword maps --------------------------------------------------------

# Multi-word keys are checked as substrings; single-word keys with word-boundary regex.
LOCATION_KEYWORDS: dict[str, str] = {
    "restaurant": "restaurant_or_food_business",
    "diner": "restaurant_or_food_business",
    "satriale": "restaurant_or_food_business",
    "vesuvio": "restaurant_or_food_business",
    "nuovo vesuvio": "restaurant_or_food_business",
    "pork store": "restaurant_or_food_business",
    "dinner": "restaurant_or_food_business",
    "bar": "bar_or_club",
    "club": "bar_or_club",
    "bing": "bar_or_club",
    "bada bing": "bar_or_club",
    "strip club": "bar_or_club",
    "lounge": "bar_or_club",
    "car": "vehicle",
    "suv": "vehicle",
    "escalade": "vehicle",
    "vehicle": "vehicle",
    "driving": "vehicle",
    "boat": "vehicle",
    "home": "home_interior",
    "house": "home_interior",
    "kitchen": "home_interior",
    "living room": "home_interior",
    "bedroom": "home_interior",
    "dining room": "home_interior",
    "backyard": "home_exterior",
    "driveway": "home_exterior",
    "pool": "home_exterior",
    "office": "office_or_business",
    "construction site": "office_or_business",
    "warehouse": "warehouse_or_industrial",
    "basement": "warehouse_or_industrial",
    "industrial": "warehouse_or_industrial",
    "therapy": "medical_or_therapy",
    "psychiatrist": "medical_or_therapy",
    "melfi's office": "medical_or_therapy",
    "doctor": "medical_or_therapy",
    "hospital": "medical_or_therapy",
    "clinic": "medical_or_therapy",
    "church": "religious",
    "funeral": "religious",
    "cemetery": "religious",
    "school": "school",
    "college": "school",
    "street": "street_or_outdoor",
    "park": "street_or_outdoor",
    "outdoor": "street_or_outdoor",
    "outside": "street_or_outdoor",
    "sidewalk": "street_or_outdoor",
    "store": "store_or_shop",
    "shop": "store_or_shop",
    "police": "law_enforcement",
    "fbi": "law_enforcement",
    "precinct": "law_enforcement",
    "courthouse": "law_enforcement",
    "hotel": "hotel_or_lodging",
    "motel": "hotel_or_lodging",
}

INTERIOR_EXTERIOR_KEYWORDS: dict[str, str] = {
    "indoor": "interior", "indoors": "interior", "inside": "interior",
    "outdoor": "exterior", "outdoors": "exterior", "outside": "exterior",
}

TIME_KEYWORDS: dict[str, str] = {
    "night": "night",
    "evening": "night",
    "midnight": "night",
    "morning": "day",
    "afternoon": "day",
    "noon": "day",
    "daytime": "day",
    "daylight": "day",
    "dawn": "dawn_dusk",
    "dusk": "dawn_dusk",
    "sunset": "dawn_dusk",
    "sunrise": "dawn_dusk",
    "twilight": "dawn_dusk",
}

MOOD_KEYWORDS: dict[str, str] = {
    "tense": "tense",
    "tension": "tense",
    "violent": "violent",
    "comedic": "comedic",
    "comedy": "comedic",
    "funny": "comedic",
    "humorous": "comedic",
    "intimate": "intimate",
    "romantic": "intimate",
    "sex": "intimate",
    "sad": "melancholy",
    "melancholy": "melancholy",
    "depressed": "melancholy",
    "grieving": "melancholy",
    "celebrating": "celebratory",
    "celebration": "celebratory",
    "celebratory": "celebratory",
    "party": "celebratory",
    "anxious": "anxious",
    "nervous": "anxious",
    "angry": "angry",
    "rage": "angry",
    "warm": "warm",
    "tender": "warm",
}

# Stronger words override weaker ones; check in priority order.
VIOLENCE_KEYWORDS: list[tuple[str, str]] = [
    ("murder", "severe"),
    ("kills", "severe"),
    ("killing", "severe"),
    ("execution", "severe"),
    ("shoots", "severe"),
    ("shooting", "severe"),
    ("strangle", "severe"),
    ("strangling", "severe"),
    ("stabbing", "severe"),
    ("violent", "severe"),
    ("brutal", "severe"),
    ("beating", "moderate"),
    ("beats", "moderate"),
    ("punching", "moderate"),
    ("fighting", "moderate"),
    ("fistfight", "moderate"),
    ("slap", "mild"),
    ("shove", "mild"),
]

# English phrase -> canonical DB tag value (lower-snake-case).
ACTIVITY_KEYWORDS: dict[str, str] = {
    "talking": "talking",
    "talk ": "talking",
    "speaking": "talking",
    "speak ": "talking",
    "conversation": "talking",
    "arguing": "arguing",
    "argue": "arguing",
    "argument": "arguing",
    "eating": "eating",
    "eat ": "eating",
    "dining": "eating",
    "drinking": "drinking",
    "drink ": "drinking",
    "phone": "on_phone",
    "calling": "on_phone",
    "phone call": "on_phone",
    "driving": "driving",
    "drive ": "driving",
    "walking": "walking",
    "walk ": "walking",
    "sitting": "sitting",
    "sit ": "sitting",
    "standing": "standing",
    "stand ": "standing",
    "watching tv": "watching_tv",
    "watching television": "watching_tv",
    "watching": "watching",
    "watch ": "watching",
    "threatening": "threatening",
    "threaten": "threatening",
    "listening": "listening",
    "listen": "listening",
    "embracing": "embracing",
    "embrace": "embracing",
    "hugging": "embracing",
    "kissing": "embracing",
    "reading": "reading",
    "read ": "reading",
    "negotiating": "negotiating",
    "negotiate": "negotiating",
    "negotiation": "negotiating",
    "confronting": "confronting",
    "confront": "confronting",
    "confrontation": "confronting",
    "greeting": "greeting",
    "greet": "greeting",
    "discussing": "discussing",
    "discuss": "discussing",
    "sleeping": "sleeping",
    "sleep ": "sleeping",
    "fighting": "fighting",
    "fight ": "fighting",
    "cooking": "cooking",
    "cook ": "cooking",
    "praying": "praying",
    "pray ": "praying",
    "shooting": "shooting",
    "shoot ": "shooting",
    "running": "running",
    "run ": "running",
    "laughing": "laughing",
    "laugh ": "laughing",
    "crying": "crying",
    "cry ": "crying",
}

NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _word_to_int(s: str) -> int:
    key = s.lower()
    if key in NUMBER_WORDS:
        return NUMBER_WORDS[key]
    return int(s)


_NUM = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"


# --- main entry point ----------------------------------------------------

def parse_query_local(text: str) -> tuple[QueryFilter, dict]:
    """Rule-based parser. Returns (QueryFilter, fake usage dict with 0 tokens)."""
    t = text.lower()
    qf = QueryFilter(semantic_query=text)

    # Characters: scan roster aliases as whole-word matches.
    roster = load_roster()
    # Build (alias_lower, canonical) pairs, longest alias first to prefer
    # "Tony Soprano" over "Tony" when both would match.
    alias_pairs: list[tuple[str, str]] = []
    for entry in roster:
        for name in [entry.canonical_name] + entry.aliases:
            alias_pairs.append((name.lower(), entry.canonical_name))
    alias_pairs.sort(key=lambda p: -len(p[0]))

    matched_chars: list[str] = []
    matched_spans: list[tuple[int, int]] = []  # consumed spans, prevent overlapping matches
    for alias_lower, canonical in alias_pairs:
        if canonical in matched_chars:
            continue
        # Word-boundary match — but \b doesn't work well with apostrophes,
        # so use lookarounds for non-word chars.
        for m in re.finditer(rf"(?<![\w']){re.escape(alias_lower)}(?![\w])", t):
            span = (m.start(), m.end())
            if any(s <= span[0] < e or s < span[1] <= e for s, e in matched_spans):
                continue
            matched_chars.append(canonical)
            matched_spans.append(span)
            break

    # Exclusions: "without X", "no X", "not X" — find the alias inside the next few words.
    excluded: list[str] = []
    excl_pattern = re.compile(r"\b(?:without|excluding|no(?:t)?)\s+([\w'\s.]+?)(?=\s+(?:and|or|in|at|on|with|but)\b|[,.!?]|$)")
    for m in excl_pattern.finditer(t):
        target = m.group(1).strip()
        for alias_lower, canonical in alias_pairs:
            if canonical in excluded:
                continue
            if re.search(rf"(?<![\w']){re.escape(alias_lower)}(?![\w])", target):
                excluded.append(canonical)
                # Demote from required if it was matched there.
                if canonical in matched_chars:
                    matched_chars.remove(canonical)
                break
    qf.required_characters = matched_chars
    qf.excluded_characters = excluded

    # Locations
    seen_loc: list[str] = []
    for kw, loc_type in LOCATION_KEYWORDS.items():
        if " " in kw:
            present = kw in t
        else:
            present = re.search(rf"\b{re.escape(kw)}\b", t) is not None
        if present and loc_type not in seen_loc:
            seen_loc.append(loc_type)
    qf.location_types = seen_loc

    # Interior/exterior
    for kw, val in INTERIOR_EXTERIOR_KEYWORDS.items():
        if re.search(rf"\b{kw}\b", t):
            qf.location_interior_exterior = val
            break

    # Time of day
    for kw, tod in TIME_KEYWORDS.items():
        if re.search(rf"\b{kw}\b", t):
            qf.time_of_day = tod
            break

    # Violence (priority-ordered)
    for kw, vlevel in VIOLENCE_KEYWORDS:
        if re.search(rf"\b{kw}\b", t):
            qf.violence_level = vlevel
            break

    # Mood — but skip if "violent" matched (already encoded as violence_level).
    if not (qf.violence_level == "severe" and re.search(r"\bviolent\b", t)):
        for kw, mood in MOOD_KEYWORDS.items():
            if re.search(rf"\b{kw}\b", t):
                qf.mood = mood
                break

    # Activities
    activities: list[str] = []
    for kw, canonical in ACTIVITY_KEYWORDS.items():
        present = kw in t if " " in kw or kw.endswith(" ") else re.search(rf"\b{re.escape(kw.strip())}\b", t) is not None
        if present and canonical not in activities:
            activities.append(canonical)
    qf.activities = activities

    # Group size
    # "alone" / "by himself"
    if re.search(r"\balone\b|\bby (?:him|her|them)sel(?:f|ves)\b", t):
        qf.min_group_size = 1
        qf.max_group_size = 1

    # "N or more men/people/guys/others"
    m = re.search(rf"\b({_NUM})\s+or\s+more\b", t)
    if m and qf.min_group_size is None:
        n = _word_to_int(m.group(1))
        # "Tony and three or more men" -> Tony + 3 others = 4+
        qf.min_group_size = n + len(qf.required_characters)

    # "N to M" / "N or M"  -> range
    m = re.search(rf"\b({_NUM})\s+(?:to|or)\s+({_NUM})\b", t)
    if m and qf.min_group_size is None:
        a, b = _word_to_int(m.group(1)), _word_to_int(m.group(2))
        if a <= b:
            qf.min_group_size = a + len(qf.required_characters)
            qf.max_group_size = b + len(qf.required_characters)

    # "just N" / "only N" / "exactly N"  (e.g. "just the two of them")
    m = re.search(rf"\b(?:just|only|exactly)\s+(?:the\s+)?({_NUM})\b", t)
    if m and qf.min_group_size is None:
        n = _word_to_int(m.group(1))
        qf.min_group_size = n
        qf.max_group_size = n

    qf.semantic_query = text
    return qf, {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
