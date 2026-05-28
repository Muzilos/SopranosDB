from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import sqlite_vec
except ImportError:
    # Only the offline ingest pipeline needs the vector table; the query path
    # (FTS5 + facets) and the static-site build run fine without it.
    sqlite_vec = None

from sopranos.config import DB_PATH, EMBED_DIM, SCHEMA_PATH


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if sqlite_vec is not None:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    conn = _connect(db_path)
    try:
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
        if sqlite_vec is not None:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS scenes_vec USING vec0("
                f"scene_id INTEGER PRIMARY KEY, embedding FLOAT[{EMBED_DIM}])"
            )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def connect(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
