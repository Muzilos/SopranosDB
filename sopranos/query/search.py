from __future__ import annotations

import re

from dataclasses import dataclass

from sopranos.db.connection import connect
from sopranos.db.models import QueryFilter


@dataclass
class SearchHit:
    scene_id: int
    season: int
    episode: int
    title: str
    scene_index: int
    start_s: float
    end_s: float
    summary: str | None
    location_name: str | None
    characters: list[str]
    similarity: float  # relevance: -bm25 for keyword search (higher = better), 0.0 for pure-facet browse
    file_path: str
    reasons: list[str]


def _build_filter_sql(qf: QueryFilter) -> tuple[str, list]:
    where: list[str] = []
    params: list = []

    if qf.location_types:
        ph = ",".join("?" * len(qf.location_types))
        where.append(f"s.location_type IN ({ph})")
        params.extend(qf.location_types)
    if qf.location_interior_exterior:
        where.append("s.location_interior_exterior = ?")
        params.append(qf.location_interior_exterior)
    if qf.time_of_day:
        where.append("s.time_of_day = ?")
        params.append(qf.time_of_day)
    if qf.mood:
        where.append("s.mood = ?")
        params.append(qf.mood)
    if qf.violence_level:
        where.append("s.violence_level = ?")
        params.append(qf.violence_level)
    if qf.min_group_size is not None:
        where.append("s.group_size_total >= ?"); params.append(qf.min_group_size)
    if qf.max_group_size is not None:
        where.append("s.group_size_total <= ?"); params.append(qf.max_group_size)
    for ch in qf.required_characters:
        where.append(
            "EXISTS (SELECT 1 FROM scene_characters sc WHERE sc.scene_id = s.id "
            "AND sc.character_name = ? AND sc.uncertain = 0)"
        )
        params.append(ch)
    for ch in qf.excluded_characters:
        where.append(
            "NOT EXISTS (SELECT 1 FROM scene_characters sc WHERE sc.scene_id = s.id "
            "AND sc.character_name = ?)"
        )
        params.append(ch)
    for kind, values in (
        ("activity", qf.activities),
        ("topic", qf.topics),
        ("tag", qf.tags),
        ("object", qf.objects),
    ):
        for v in values:
            where.append(
                "EXISTS (SELECT 1 FROM scene_tags st WHERE st.scene_id = s.id "
                "AND st.tag_type = ? AND st.tag_value = ?)"
            )
            params.extend([kind, v.lower().strip()])

    return (" AND ".join(where) if where else "1=1"), params


# Order in which filters are dropped when the strict query returns no hits.
# Earlier = less essential. We NEVER drop required/excluded characters — those
# are usually the most explicit part of the user's intent.
_RELAX_ORDER = [
    "objects", "tags", "topics", "activities",
    "mood", "violence_level", "time_of_day", "location_interior_exterior",
    "min_group_size", "max_group_size",
    "location_types",
]


def _qf_drop(qf: QueryFilter, field: str) -> QueryFilter:
    data = qf.model_dump()
    default = [] if isinstance(data[field], list) else None
    data[field] = default
    return QueryFilter(**data)


# Common English words carry no signal but blow up the FTS scan (they match
# nearly every scene), so drop them from the MATCH. Mirrors STOPWORDS in main.js.
_STOPWORDS = frozenset(
    "a an and are as at be been but by for from had has have he her his in into is it its "
    "of on or that the their them they this to was were what when which who will with you".split()
)


def _fts_match_expr(text: str) -> str | None:
    """Turn free text into an FTS5 MATCH expression. Stopwords are dropped, each
    remaining token is quoted (so punctuation/apostrophes can't break the query)
    and OR-joined for recall; bm25() then ranks by how well each scene matches."""
    toks = [t for t in re.findall(r"[A-Za-z0-9']+", text or "") if t]
    content = [t for t in toks if t.lower() not in _STOPWORDS]
    toks = content or toks  # all-stopword query: fall back to as-typed
    if not toks:
        return None
    return " OR ".join(f'"{t}"' for t in toks)


_SELECT_COLS = (
    "s.id, s.scene_index, s.start_s, s.end_s, s.summary, s.location_name, "
    "e.season, e.episode, e.title, e.file_path"
)


def _run(conn, qf: QueryFilter, match_expr: str | None, top_k: int):
    where, params = _build_filter_sql(qf)
    if match_expr:
        # `rank` is FTS5's reserved special column — alias the score to something
        # else and drive off the FTS table so MATCH/bm25 bind cleanly.
        sql = (
            f"SELECT {_SELECT_COLS}, bm25(scenes_fts) AS bm25_score "
            f"FROM scenes_fts "
            f"JOIN scenes s ON s.id = scenes_fts.rowid "
            f"JOIN episodes e ON e.id = s.episode_id "
            f"WHERE scenes_fts MATCH ? AND {where} "
            f"ORDER BY bm25_score LIMIT ?"
        )
        return conn.execute(sql, [match_expr, *params, top_k]).fetchall()
    sql = (
        f"SELECT {_SELECT_COLS}, 0.0 AS bm25_score "
        f"FROM scenes s JOIN episodes e ON e.id = s.episode_id "
        f"WHERE {where} "
        f"ORDER BY e.season, e.episode, s.scene_index LIMIT ?"
    )
    return conn.execute(sql, [*params, top_k]).fetchall()


def search(qf: QueryFilter, top_k: int = 10) -> tuple[list[SearchHit], list[str]]:
    """Keyword (FTS5 bm25) + structured-facet search. No LLM, no embeddings.

    Free text is matched against the `scenes_fts` index and ranked with bm25();
    structured facets become hard SQL filters. If the strict filter returns no
    rows, optional facets are dropped one at a time (least-essential first); as a
    last resort the keyword match itself is dropped (pure facet browse).

    Returns (hits, relaxed_fields). `relaxed_fields` lists what was dropped to
    find hits — empty if the strict filter matched.
    """
    relaxed: list[str] = []
    effective_qf = qf
    match_expr = _fts_match_expr(qf.semantic_query or "")

    with connect() as conn:
        rows = _run(conn, effective_qf, match_expr, top_k)

        # Progressive relaxation: drop one facet at a time, in priority order.
        for field in _RELAX_ORDER:
            if rows:
                break
            current = getattr(effective_qf, field)
            if (isinstance(current, list) and not current) or current is None:
                continue
            effective_qf = _qf_drop(effective_qf, field)
            relaxed.append(field)
            rows = _run(conn, effective_qf, match_expr, top_k)

        # Last resort: keep the facets we have but drop the keyword match.
        if not rows and match_expr:
            rows = _run(conn, effective_qf, None, top_k)
            if rows:
                relaxed.append("keyword_match")

        if not rows:
            return [], relaxed

        out: list[SearchHit] = []
        for r in rows:
            chars = [row["character_name"] for row in conn.execute(
                "SELECT character_name FROM scene_characters WHERE scene_id = ? AND uncertain = 0",
                (r["id"],),
            ).fetchall()]
            out.append(SearchHit(
                scene_id=r["id"], season=r["season"], episode=r["episode"], title=r["title"],
                scene_index=r["scene_index"], start_s=r["start_s"], end_s=r["end_s"],
                summary=r["summary"], location_name=r["location_name"],
                characters=chars, similarity=-float(r["bm25_score"]), file_path=r["file_path"],
                reasons=_reasons(effective_qf, r),
            ))
        return out, relaxed


def _reasons(qf: QueryFilter, row) -> list[str]:
    r: list[str] = []
    if qf.required_characters:
        r.append(f"chars={','.join(qf.required_characters)}")
    if qf.location_types:
        r.append(f"loc_type={','.join(qf.location_types)}")
    if qf.min_group_size or qf.max_group_size:
        r.append(f"group={qf.min_group_size}-{qf.max_group_size}")
    if qf.activities:
        r.append(f"activities={','.join(qf.activities)}")
    if qf.mood:
        r.append(f"mood={qf.mood}")
    return r
