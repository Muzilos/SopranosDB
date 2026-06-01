-- D1 schema for SopranosDB — read-mostly corpus + live view counts.
--
-- Differences from sopranos/db/schema.sql (the ingest schema):
--   * Only the columns the served UI needs (drops raw_vlm_json, shots, api_usage,
--     episode file_path/fps/processed_at, scenes_vec).
--   * Adds scenes.view_count (the live "Popularity" signal) and scene_views
--     (per-visitor/day dedupe so a view can only be counted once — anti-bot).
--   * scenes_fts has NO sync triggers. The corpus never changes at runtime (the
--     only write is UPDATE scenes.view_count, which isn't indexed), so we populate
--     the external-content index ONCE with 'rebuild' after load instead of paying
--     trigger overhead on every view increment.

PRAGMA foreign_keys = OFF;

CREATE TABLE episodes (
    id       INTEGER PRIMARY KEY,
    season   INTEGER NOT NULL,
    episode  INTEGER NOT NULL,
    title    TEXT NOT NULL
);

CREATE TABLE scenes (
    id                          INTEGER PRIMARY KEY,
    episode_id                  INTEGER NOT NULL,
    scene_index                 INTEGER NOT NULL,
    start_s                     REAL,
    end_s                       REAL,
    duration_s                  REAL,
    shot_count                  INTEGER,
    summary                     TEXT,
    location_name               TEXT,
    location_type               TEXT,
    location_interior_exterior  TEXT,
    time_of_day                 TEXT,
    mood                        TEXT,
    violence_level              TEXT,
    group_size_total            INTEGER,
    background_people_count     INTEGER,
    dialogue_highlight          TEXT,
    transcript_text             TEXT,
    keyframes_json              TEXT,
    view_count                  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_scenes_episode       ON scenes(episode_id);
CREATE INDEX idx_scenes_location_type ON scenes(location_type);
CREATE INDEX idx_scenes_group_size    ON scenes(group_size_total);

CREATE TABLE scene_characters (
    scene_id        INTEGER NOT NULL,
    character_name  TEXT NOT NULL,
    uncertain       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scene_id, character_name)
);
CREATE INDEX idx_sc_character ON scene_characters(character_name);

CREATE TABLE scene_tags (
    scene_id   INTEGER NOT NULL,
    tag_type   TEXT NOT NULL,
    tag_value  TEXT NOT NULL,
    PRIMARY KEY (scene_id, tag_type, tag_value)
);
CREATE INDEX idx_st_type_value ON scene_tags(tag_type, tag_value);

CREATE TABLE characters (
    canonical_name  TEXT PRIMARY KEY,
    aliases_json    TEXT NOT NULL,
    description     TEXT,
    first_seen      TEXT
);

CREATE VIRTUAL TABLE scenes_fts USING fts5(
    summary, location_name, transcript_text, dialogue_highlight,
    content='scenes', content_rowid='id',
    tokenize='porter unicode61'
);

-- One row == one counted view. (scene_id, visitor, day) UNIQUE means a given
-- visitor can bump a given scene at most once per day; view_count only increments
-- when a fresh row actually inserts.
CREATE TABLE scene_views (
    scene_id  INTEGER NOT NULL,
    visitor   TEXT NOT NULL,
    day       TEXT NOT NULL,
    PRIMARY KEY (scene_id, visitor, day)
);
