from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sopranos.config import ROSTER_PATH


@dataclass
class CastEntry:
    canonical_name: str
    aliases: list[str]
    description: str
    first_seen: str
    deceased_after: str = ""   # last episode in which this character appears alive


def load_roster(path: Path = ROSTER_PATH) -> list[CastEntry]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    return [
        CastEntry(
            canonical_name=v["canonical_name"],
            aliases=v.get("aliases", []),
            description=v.get("description", ""),
            first_seen=v.get("first_seen", ""),
            deceased_after=v.get("deceased_after", ""),
        )
        for v in raw.values()
    ]


def roster_prompt_block(roster: list[CastEntry]) -> str:
    lines = ["CANONICAL CAST ROSTER (use these exact `canonical_name` values):"]
    for c in roster:
        aliases = ", ".join(c.aliases) if c.aliases else "(none)"
        deceased = f"  [DECEASED AFTER {c.deceased_after} — do NOT use this name in later episodes]" if c.deceased_after else ""
        lines.append(
            f"- {c.canonical_name}: aliases={aliases}. {c.description} (first_seen={c.first_seen}){deceased}"
        )
    return "\n".join(lines)
