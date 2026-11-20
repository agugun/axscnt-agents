"""SQLite access shared by all servers."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path


def schema_sql() -> str:
    return resources.files("opmagent.core").joinpath("schema.sql").read_text()


def connect(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), isolation_level=None)  # autocommit; explicit transactions below
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    return con


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
