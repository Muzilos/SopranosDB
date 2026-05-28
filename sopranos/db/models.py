from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from sopranos.config import (
    INTERIOR_EXTERIOR, LOCATION_TYPES, MOODS, TIMES_OF_DAY, VIOLENCE_LEVELS,
)

LocationType = Literal[tuple(LOCATION_TYPES)]  # type: ignore[valid-type]


class SceneLabel(BaseModel):
    """The structured output Haiku returns per scene. Validated on each response."""

    summary: str = Field(..., min_length=10, max_length=600)
    location_name: str = Field(..., max_length=120)
    location_type: str
    location_interior_exterior: str
    time_of_day: str
    mood: str
    violence_level: str
    characters: list[str] = Field(default_factory=list)
    uncertain_characters: list[str] = Field(default_factory=list)
    background_people_count: int = Field(0, ge=0, le=200)
    group_size_total: int = Field(0, ge=0, le=200)
    activities: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    notable_objects: list[str] = Field(default_factory=list)
    dialogue_highlight: str = Field("", max_length=400)

    @field_validator("location_type")
    @classmethod
    def _loc_type(cls, v: str) -> str:
        if v not in LOCATION_TYPES:
            raise ValueError(f"location_type must be one of {LOCATION_TYPES}, got {v!r}")
        return v

    @field_validator("location_interior_exterior")
    @classmethod
    def _int_ext(cls, v: str) -> str:
        if v not in INTERIOR_EXTERIOR:
            raise ValueError(f"location_interior_exterior must be one of {INTERIOR_EXTERIOR}")
        return v

    @field_validator("time_of_day")
    @classmethod
    def _tod(cls, v: str) -> str:
        if v not in TIMES_OF_DAY:
            raise ValueError(f"time_of_day must be one of {TIMES_OF_DAY}")
        return v

    @field_validator("mood")
    @classmethod
    def _mood(cls, v: str) -> str:
        if v not in MOODS:
            raise ValueError(f"mood must be one of {MOODS}")
        return v

    @field_validator("violence_level")
    @classmethod
    def _violence(cls, v: str) -> str:
        if v not in VIOLENCE_LEVELS:
            raise ValueError(f"violence_level must be one of {VIOLENCE_LEVELS}")
        return v


class QueryFilter(BaseModel):
    """Structured filter extracted from a user's natural-language query."""

    required_characters: list[str] = Field(default_factory=list)
    excluded_characters: list[str] = Field(default_factory=list)
    min_group_size: int | None = None
    max_group_size: int | None = None
    location_types: list[str] = Field(default_factory=list)
    location_interior_exterior: str | None = None
    time_of_day: str | None = None
    mood: str | None = None
    violence_level: str | None = None
    activities: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    objects: list[str] = Field(default_factory=list)
    semantic_query: str = ""
    excluded_terms: list[str] = Field(default_factory=list)
