"""File registry: every input and output file is recorded with its sha256."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from .errors import INVALID_INPUT, NOT_FOUND, ToolError

KINDS = {
    "lab_report", "well_log", "tops", "surface", "core", "scal", "deviation_survey", "production_data",
    "well_test", "other_input", "grid", "property", "vfp", "qc_report", "include", "deck",
}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def register_file(con: sqlite3.Connection, path: str | Path, kind: str, note: str | None = None) -> int:
    """Record a file (idempotent on path + content). Returns its file id."""
    if kind not in KINDS:
        raise ToolError(INVALID_INPUT, f"unknown file kind '{kind}'", {"allowed": sorted(KINDS)})
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise ToolError(NOT_FOUND, f"file not found: {p}")
    digest = sha256_of(p)
    row = con.execute("SELECT id FROM files WHERE path = ? AND sha256 = ?", (str(p), digest)).fetchone()
    if row:
        return row["id"]
    cur = con.execute(
        "INSERT INTO files(path, sha256, kind, size_bytes, note) VALUES (?,?,?,?,?)",
        (str(p), digest, kind, p.stat().st_size, note),
    )
    return cur.lastrowid


def get_file(con: sqlite3.Connection, file_id: int) -> dict:
    row = con.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        raise ToolError(NOT_FOUND, f"file {file_id} not found")
    return dict(row)


def find_in_inbox(inbox: Path, query: str) -> list[str]:
    """Spoken-name file lookup: case-insensitive match of all query words in the file name."""
    words = [w for w in query.lower().replace("_", " ").replace("-", " ").split() if w]
    if not inbox.is_dir():
        return []
    hits = []
    for p in sorted(inbox.rglob("*")):
        if p.is_file():
            name = p.name.lower().replace("_", " ").replace("-", " ")
            if all(w in name for w in words):
                hits.append(str(p))
    return hits
