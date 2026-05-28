from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sopranos.config import SEASON_DIRS

EPISODE_RE = re.compile(r"S(\d{2})E(\d{2})\s+(.+)\.mp4$")


@dataclass(frozen=True)
class EpisodeRef:
    season: int
    episode: int
    title: str
    path: Path

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"


def parse_episode_filename(path: Path) -> EpisodeRef | None:
    m = EPISODE_RE.search(path.name)
    if not m:
        return None
    return EpisodeRef(
        season=int(m.group(1)),
        episode=int(m.group(2)),
        title=m.group(3).strip(),
        path=path,
    )


def list_season_episodes(season: int) -> list[EpisodeRef]:
    season_dir = SEASON_DIRS[season]
    refs: list[EpisodeRef] = []
    for p in sorted(season_dir.glob("*.mp4")):
        ref = parse_episode_filename(p)
        if ref is not None:
            refs.append(ref)
    return refs


def find_episode(code: str) -> EpisodeRef:
    m = re.fullmatch(r"S(\d{2})E(\d{2})", code, re.IGNORECASE)
    if not m:
        raise ValueError(f"Episode code must look like S01E03, got: {code!r}")
    season = int(m.group(1))
    episode = int(m.group(2))
    for ref in list_season_episodes(season):
        if ref.episode == episode:
            return ref
    raise FileNotFoundError(f"No episode file found for {code}")
