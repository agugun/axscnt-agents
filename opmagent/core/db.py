"""SQLite access shared by all servers."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path


def schema_sql() -> str:
    return resources.files("opmagent.core").joinpath("schema.sql").read_text()


def connect(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)  # autocommit; callers serialize access
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'decks'").fetchone():
        migrate(con)
    return con


MIGRATIONS = [
    # (name, SQL) applied in order if the object is missing; keeps older databases current
    ("runs", """CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY, deck_id INTEGER NOT NULL REFERENCES decks(id),
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed','stored')),
        engine TEXT, out_dir TEXT NOT NULL, pid INTEGER, returncode INTEGER, log_path TEXT, smspec_path TEXT,
        note TEXT, started_at TEXT, finished_at TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS results (run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        vector TEXT NOT NULL, time_days REAL NOT NULL, value REAL NOT NULL, PRIMARY KEY (run_id, vector, time_days));
        CREATE INDEX IF NOT EXISTS idx_runs_deck ON runs(deck_id);"""),
]


def migrate(con: sqlite3.Connection) -> list[str]:
    applied = []
    for name, sql in MIGRATIONS:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone():
            con.executescript(sql)
            applied.append(name)
    return applied


def init_db(path: str | Path) -> None:
    path = Path(path)
    if path.exists() and path.stat().st_size > 0:
        raise FileExistsError(f"{path} already exists; refusing to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(path)
    con.executescript(schema_sql())
    con.close()


@contextmanager
def transaction(con: sqlite3.Connection):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except Exception:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")
