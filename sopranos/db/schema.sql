-- SopranosDatabase schema. Loaded once via sopranos.db.connection.init_db.
-- sqlite-vec extension must be loaded before creating scenes_vec.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    season INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    title TEXT NOT NULL,
    file_path TEXT NOT NULL,
    duration_s REAL NOT NULL,
    fps REAL NOT NULL,
    processed_at TEXT,
    UNIQUE(season, episode)
);

CREATE TABLE IF NOT EXISTS shots (
    id INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    shot_index INTEGER NOT NULL,
    start_s REAL NOT NULL,
    end_s REAL NOT NULL,
    UNIQUE(episode_id, shot_index)
);
CREATE INDEX IF NOT EXISTS idx_shots_episode ON shots(episode_id);

CREATE TABLE IF NOT EXISTS scenes (
    id INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    scene_index INTEGER NOT NULL,
    start_s REAL NOT NULL,
    end_s REAL NOT NULL,
    duration_s REAL NOT NULL,
    shot_count INTEGER NOT NULL,
    summary TEXT,
    location_name TEXT,
    location_type TEXT,
    location_interior_exterior TEXT,
    time_of_day TEXT,
    mood TEXT,
    violence_level TEXT,
    group_size_total INTEGER,
    background_people_count INTEGER,
    dialogue_highlight TEXT,
    transcript_text TEXT,
    keyframes_json TEXT,
    raw_vlm_json TEXT,
    UNIQUE(episode_id, scene_index)
);
CREATE INDEX IF NOT EXISTS idx_scenes_episode ON scenes(episode_id);
CREATE INDEX IF NOT EXISTS idx_scenes_location_type ON scenes(location_type);
CREATE INDEX IF NOT EXISTS idx_scenes_group_size ON scenes(group_size_total);

CREATE TABLE IF NOT EXISTS scene_characters (
    scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
    character_name TEXT NOT NULL,
    uncertain INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scene_id, character_name)
);
CREATE INDEX IF NOT EXISTS idx_sc_character ON scene_characters(character_name);

CREATE TABLE IF NOT EXISTS scene_tags (
    scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
    tag_type TEXT NOT NULL,            -- 'activity' | 'topic' | 'tag' | 'object'
    tag_value TEXT NOT NULL,
    PRIMARY KEY (scene_id, tag_type, tag_value)
);
CREATE INDEX IF NOT EXISTS idx_st_value ON scene_tags(tag_value);
CREATE INDEX IF NOT EXISTS idx_st_type_value ON scene_tags(tag_type, tag_value);

CREATE TABLE IF NOT EXISTS characters (
    canonical_name TEXT PRIMARY KEY,
    aliases_json TEXT NOT NULL,
    description TEXT,
    first_seen TEXT
);

CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,                -- 'label' | 'query_filter'
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL,
    scene_id INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS scenes_fts USING fts5(
    summary, location_name, transcript_text, dialogue_highlight,
    content='scenes', content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS scenes_ai AFTER INSERT ON scenes BEGIN
    INSERT INTO scenes_fts(rowid, summary, location_name, transcript_text, dialogue_highlight)
    VALUES (new.id, new.summary, new.location_name, new.transcript_text, new.dialogue_highlight);
END;
CREATE TRIGGER IF NOT EXISTS scenes_ad AFTER DELETE ON scenes BEGIN
    INSERT INTO scenes_fts(scenes_fts, rowid, summary, location_name, transcript_text, dialogue_highlight)
    VALUES('delete', old.id, old.summary, old.location_name, old.transcript_text, old.dialogue_highlight);
END;
CREATE TRIGGER IF NOT EXISTS scenes_au AFTER UPDATE ON scenes BEGIN
    INSERT INTO scenes_fts(scenes_fts, rowid, summary, location_name, transcript_text, dialogue_highlight)
    VALUES('delete', old.id, old.summary, old.location_name, old.transcript_text, old.dialogue_highlight);
    INSERT INTO scenes_fts(rowid, summary, location_name, transcript_text, dialogue_highlight)
    VALUES (new.id, new.summary, new.location_name, new.transcript_text, new.dialogue_highlight);
END;
