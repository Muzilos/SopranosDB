from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from sopranos.config import (
    INTERIOR_EXTERIOR, LOCATION_TYPES, MOODS, TIMES_OF_DAY,
    VIOLENCE_LEVELS, VLM_MAX_CONCURRENT, VLM_MAX_TOKENS, VLM_MODEL,
)
from sopranos.db.models import SceneLabel
from sopranos.roster import CastEntry, roster_prompt_block
from sopranos.utils.hashing import hash_bytes


def _enum_line(name: str, values: list[str]) -> str:
    return f"- {name}: one of {values}"


def build_system_prompt(roster: list[CastEntry]) -> str:
    return f"""You analyze single narrative scenes from The Sopranos (HBO drama, 1999-2007).
You will be shown 2-3 keyframes from one scene plus a transcript excerpt of its dialogue,
and you must return a single JSON object describing the scene.

{roster_prompt_block(roster)}

NAMING RULES:
- When naming a character in `characters`, you MUST use the EXACT `canonical_name` from the roster above.
- If you see someone NOT in the roster, name them descriptively: "unknown_man_in_suit_1", "unknown_woman_at_bar", "unknown_child", etc. Do not invent canonical names.
- If you are LESS THAN 70% confident about a character ID, place them in `uncertain_characters` instead of `characters`. Use canonical_name there too if applicable.
- `group_size_total` counts ALL people visible (named + unknown + background). `background_people_count` counts only unnamed background people.

DECEASED-CHARACTER RULE (CRITICAL):
The user message contains the episode code (e.g. "S04E03"). Some roster entries are
marked `[DECEASED AFTER S03E01]` etc. If the scene's episode is LATER than that, the
character is DEAD and cannot be physically present in the scene. DO NOT use their name —
even if a face looks similar. Instead use an `unknown_*` descriptor. Exception: flashbacks
and dream sequences may include them; in that case ALSO add `flashback` or `dream_sequence`
to `tags` and keep the deceased character in `uncertain_characters` (not `characters`).

FLASHBACK / DREAM RULE:
- If the keyframe shows a younger version of a character (e.g. young Tony in a S03 flashback),
  do NOT label them as the present-day character. Use `unknown_young_man` or similar, and
  add `flashback` to `tags`.
- Kevin Finnerty dream sequences (S06E02-04): Tony Soprano in those dream scenes is still
  Tony Soprano (same actor). Don't invent "Kevin Finnerty" as a separate person.

TONY SOPRANO vs TONY BLUNDETTO (CRITICAL DISAMBIGUATION):
- Tony Soprano (James Gandolfini): HEAVY, broad-shouldered, bald/balding.
- Tony Blundetto (Steve Buscemi): SKINNY, gaunt face, bulging eyes, receding sandy hair.
- These two cousins are visually OPPOSITE. If you see a skinny man in a scene with a heavy
  man, the skinny one is Tony Blundetto and the heavy one is Tony Soprano. Do NOT default
  to Tony Soprano for both.

OUTPUT SCHEMA — respond with ONE valid JSON object, no prose, no markdown fences:
{{
  "summary": str (2-4 sentence plot summary, 60-400 chars),
  "location_name": str (specific name if known: "Satriale's Pork Store", "Soprano kitchen", "Dr. Melfi's office", "Bada Bing"; else generic),
  "location_type": str,
  "location_interior_exterior": str,
  "time_of_day": str,
  "mood": str,
  "violence_level": str,
  "characters": [str],
  "uncertain_characters": [str],
  "background_people_count": int,
  "group_size_total": int,
  "activities": [str] (lowercase verbs: "talking", "eating", "driving", "smoking", "arguing", "embracing"),
  "topics": [str] (1-3 word topics: "loyalty", "marriage troubles", "drug deal", "therapy", "family dinner"),
  "tags": [str] (short tags useful for retrieval: "mentor_scene", "phone_call", "establishing_shot", "italian_food"),
  "notable_objects": [str] (props: "handgun", "espresso_cup", "cigar", "wedding_ring"),
  "dialogue_highlight": str (most memorable line if any, else "")
}}

ENUM VALUES:
{_enum_line("location_type", LOCATION_TYPES)}
{_enum_line("location_interior_exterior", INTERIOR_EXTERIOR)}
{_enum_line("time_of_day", TIMES_OF_DAY)}
{_enum_line("mood", MOODS)}
{_enum_line("violence_level", VIOLENCE_LEVELS)}

If a transcript excerpt suggests a phone call with cross-cut locations, add "phone_call" to tags
and pick the location_type that best matches the keyframes you see.

DETAILED CONVENTIONS AND EXAMPLES (study these — they define the expected style).

LOCATION_NAME conventions:
- Use proper-noun names when the location is a recurring set: "Satriale's Pork Store",
  "Bada Bing", "Soprano kitchen", "Soprano backyard", "Dr. Melfi's office",
  "Vesuvio restaurant", "Junior Soprano's house", "Tony's Suburban", "Pizzaland",
  "Father Phil's rectory", "The Stugots" (Tony's boat).
- For generic places: "diner counter", "school parking lot", "hospital corridor",
  "highway shoulder", "warehouse interior", "strip-mall sidewalk".
- For unclear locations, describe what you see: "dimly lit basement", "wood-paneled bar".
- Never use vague labels like "interior", "outside", "location" alone.

TOPIC and TAG vocabulary you should re-use across scenes (lowercase, underscores):
- Topic examples: loyalty, betrayal, marriage_troubles, parenting, school_grades,
  business_deal, drug_deal, gambling_debt, racketeering, therapy_session, panic_attack,
  family_dinner, italian_heritage, catholic_guilt, infidelity, mob_politics,
  recruitment, violence_planning, money_laundering, funeral, wedding, hospital_visit.
- Tag examples: mentor_scene, intimate_argument, phone_call, dream_sequence,
  voiceover, establishing_shot, opening_titles, montage, party_scene, car_ride,
  cooking, smoking_scene, drinking_scene, italian_food, americana, suburban_life,
  meeting_at_back_table, code_talk.

ACTIVITY vocabulary (single lowercase verbs unless idiomatic):
- talking, eating, drinking, smoking, driving, walking, arguing, fighting,
  embracing, kissing, sleeping, praying, reading, watching_tv, cooking,
  shooting, beating, threatening, negotiating, on_phone, gambling, dancing.

GROUP SIZE GUIDANCE (this matters for queries like "Tony and 3-5 men"):
- `group_size_total` includes the named characters PLUS unknown people PLUS background.
- Be careful with shot-reverse-shot: if you see Tony in one frame and Christopher in
  another, both belong to the same scene → both count.
- If a wide shot shows 6 people at a table, group_size_total = 6 even if you only
  recognize 2 of them.

VIOLENCE_LEVEL guidance:
- none: no aggression at all.
- implied: discussion of past/future violence; weapons visible but unused.
- mild: shoving, slap, raised voice with physical posture.
- moderate: punching, restraining, blood drawn but not lethal.
- severe: shootings, killings, beatings causing serious injury.

WORKED EXAMPLE 1 — A dinner scene at Satriale's:
Input description: 3 keyframes showing Tony, Silvio, Paulie, Christopher and one
unknown man at a round table inside Satriale's. Plates of food, espresso cups,
a half-empty bottle of red wine. Dialogue transcript: "[Tony] So this thing with
the Triboro garbage contract, we gotta lean on Larry. [Silvio] He's been ducking
my calls for two weeks." Suggested output:
{{
  "summary": "Tony, Silvio, Paulie and Christopher meet at Satriale's over food and espresso to plan how to pressure Larry on the Triboro garbage contract. An unknown associate sits in.",
  "location_name": "Satriale's Pork Store",
  "location_type": "restaurant_or_food_business",
  "location_interior_exterior": "interior",
  "time_of_day": "day",
  "mood": "tense",
  "violence_level": "implied",
  "characters": ["Tony Soprano", "Silvio Dante", "Paulie Gualtieri", "Christopher Moltisanti"],
  "uncertain_characters": [],
  "background_people_count": 1,
  "group_size_total": 5,
  "activities": ["eating", "talking", "drinking"],
  "topics": ["business_deal", "racketeering", "intimidation"],
  "tags": ["meeting_at_back_table", "italian_food", "code_talk", "satriales"],
  "notable_objects": ["espresso_cup", "red_wine_bottle", "pork_sandwich"],
  "dialogue_highlight": "We gotta lean on Larry."
}}

WORKED EXAMPLE 2 — Therapy session:
Input description: 2 keyframes of Tony seated on Melfi's leather sofa, Melfi
in her chair taking notes. Tony looks frustrated, hand on forehead. Dialogue:
"[Tony] I'm sick of it. Every morning I wake up and there's this thing. [Melfi]
The depression you described last week. [Tony] It's not depression, I told you."
Suggested output:
{{
  "summary": "Tony complains to Dr. Melfi about persistent feelings he refuses to call depression. Melfi presses him gently; he deflects.",
  "location_name": "Dr. Melfi's office",
  "location_type": "medical_or_therapy",
  "location_interior_exterior": "interior",
  "time_of_day": "day",
  "mood": "tense",
  "violence_level": "none",
  "characters": ["Tony Soprano", "Dr. Jennifer Melfi"],
  "uncertain_characters": [],
  "background_people_count": 0,
  "group_size_total": 2,
  "activities": ["talking"],
  "topics": ["therapy_session", "depression", "denial"],
  "tags": ["mentor_scene", "introspection"],
  "notable_objects": ["leather_sofa", "notepad"],
  "dialogue_highlight": "It's not depression, I told you."
}}

WORKED EXAMPLE 3 — Brief violent moment:
Input description: 3 keyframes of Christopher pinning Brendan against a chain-link
fence at night in a parking lot. No dialogue heard in transcript; ambient.
Suggested output:
{{
  "summary": "Christopher confronts Brendan in a deserted parking lot at night, slamming him against a chain-link fence to deliver a warning.",
  "location_name": "parking lot",
  "location_type": "street_or_outdoor",
  "location_interior_exterior": "exterior",
  "time_of_day": "night",
  "mood": "violent",
  "violence_level": "moderate",
  "characters": ["Christopher Moltisanti", "Brendan Filone"],
  "uncertain_characters": [],
  "background_people_count": 0,
  "group_size_total": 2,
  "activities": ["threatening", "fighting"],
  "topics": ["intimidation", "drug_deal", "discipline"],
  "tags": ["confrontation", "night_scene"],
  "notable_objects": ["chain-link_fence"],
  "dialogue_highlight": ""
}}

FINAL CHECKLIST before responding:
1. Did you use ONLY canonical names from the roster for known characters?
2. Did you use one of the enumerated values for location_type, location_interior_exterior, time_of_day, mood, violence_level?
3. Did you set group_size_total to the FULL count of people visible across all frames?
4. Are activities, topics, and tags lowercase with underscores?
5. Did you respond with valid JSON only, no prose, no fences?
"""


@dataclass
class VLMRequest:
    episode_code: str
    scene_index: int
    start_s: float
    end_s: float
    keyframe_paths: list[Path]
    transcript_text: str


@dataclass
class VLMResponse:
    request: VLMRequest
    label: SceneLabel
    raw_json: dict[str, Any]
    usage: dict[str, int]
    cached: bool


def _user_content(req: VLMRequest) -> list[dict]:
    blocks: list[dict] = []
    for p in req.keyframe_paths:
        b = p.read_bytes()
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(b).decode("ascii"),
            },
        })
    blocks.append({
        "type": "text",
        "text": (
            f"Episode: {req.episode_code}\n"
            f"Scene timestamps: {req.start_s:.1f}s to {req.end_s:.1f}s "
            f"(duration {req.end_s - req.start_s:.1f}s).\n\n"
            f"Dialogue transcript (may be empty for action-only scenes):\n"
            f"{req.transcript_text or '(no dialogue)'}\n\n"
            f"Respond with ONE valid JSON object matching the schema. No prose."
        ),
    })
    return blocks


def _cache_key(req: VLMRequest, system_prompt: str) -> str:
    img_bytes = b"".join(p.read_bytes() for p in req.keyframe_paths)
    user_text = (req.episode_code + str(req.start_s) + str(req.end_s)
                 + req.transcript_text)
    return hash_bytes(system_prompt.encode("utf-8"), img_bytes, user_text.encode("utf-8"))


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip("`").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in model response: {text[:200]!r}")
    return json.loads(text[start:end + 1])


async def _label_one(
    client: AsyncAnthropic,
    semaphore: asyncio.Semaphore,
    req: VLMRequest,
    system_prompt: str,
    cache_dir: Path,
) -> VLMResponse:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(req, system_prompt)
    cache_file = cache_dir / f"{key}.json"
    if cache_file.exists():
        payload = json.loads(cache_file.read_text())
        return VLMResponse(
            request=req,
            label=SceneLabel(**payload["label"]),
            raw_json=payload["label"],
            usage=payload.get("usage", {}),
            cached=True,
        )

    async with semaphore:
        last_error: Exception | None = None
        for attempt in range(2):
            user_content = _user_content(req)
            if attempt == 1 and last_error is not None:
                user_content[-1]["text"] += (
                    f"\n\nYour previous response failed validation: {last_error}. "
                    "Reply with corrected JSON only."
                )
            try:
                msg = await client.messages.create(
                    model=VLM_MODEL,
                    max_tokens=VLM_MAX_TOKENS,
                    system=[{
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": user_content}],
                )
                text = "".join(b.text for b in msg.content if b.type == "text")
                raw = _extract_json(text)
                label = SceneLabel(**raw)
                usage = {
                    "input_tokens": msg.usage.input_tokens,
                    "output_tokens": msg.usage.output_tokens,
                    "cache_creation_input_tokens": getattr(msg.usage, "cache_creation_input_tokens", 0) or 0,
                    "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
                }
                cache_file.write_text(json.dumps({"label": raw, "usage": usage}))
                return VLMResponse(request=req, label=label, raw_json=raw, usage=usage, cached=False)
            except (ValidationError, ValueError, json.JSONDecodeError) as e:
                last_error = e
        raise RuntimeError(f"VLM failed twice for {req.episode_code} scene {req.scene_index}: {last_error}")


async def label_scenes(
    requests: list[VLMRequest],
    roster: list[CastEntry],
    cache_dir: Path,
    on_progress=None,
) -> list[VLMResponse]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = AsyncAnthropic(api_key=api_key)
    system_prompt = build_system_prompt(roster)
    sem = asyncio.Semaphore(VLM_MAX_CONCURRENT)

    async def run(r):
        out = await _label_one(client, sem, r, system_prompt, cache_dir)
        if on_progress is not None:
            on_progress(out)
        return out

    return await asyncio.gather(*(run(r) for r in requests))
