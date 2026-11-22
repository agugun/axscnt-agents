"""Workspace upkeep: a consistent backup of the database and config, and cleaning files that are no longer needed.

backup: the database copied with SQLite's online backup (consistent while agents write), the config and a manifest,
        in one .tar.gz under ~/.local/share/opmagent/backups; --files adds the file store (without the views,
        which are remade when opened).
clean:  what can go, by category, listed first (a dry run) and removed only with yes=True:
        runs     the output folders of failed runs (the run rows and their logs' note stay in the database)
        imports  older import folders none of whose models are approved (the newest import of each name is kept)
        views    initialisation views and geo static views: remade when opened (opmagent results open)
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path

from . import __version__
from .core import db as dbmod

CATEGORIES = ("runs", "imports", "views")
KINDS = ("fluid", "geo", "res", "well")


def backups_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opmagent" / "backups"


def size_of(p: Path) -> int:
    if p.is_symlink() or p.is_file():
        return p.lstat().st_size
    return sum(f.lstat().st_size for f in p.rglob("*") if f.is_file() or f.is_symlink()) if p.is_dir() else 0


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def usage(cfg) -> list[dict]:
    """Size of each top folder of the file store, largest first."""
    fs = Path(cfg.file_store)
    rows = [{"folder": p.name, "bytes": size_of(p)} for p in sorted(fs.iterdir())] if fs.is_dir() else []
    return sorted(rows, key=lambda r: -r["bytes"])


# ------------------------------------------------------------------ backup
def _views(fs: Path) -> list[Path]:
    return [fs / "init_views", *sorted((fs / "qc").glob("geo_*_v*/static"))]


def backup(cfg, *, config_path: Path | None, out: Path | None = None, files: bool = False) -> dict:
    """One archive: opm.db (a consistent copy), opmagent.toml, manifest.json, and with files=True the file store."""
    db, fs = Path(cfg.database), Path(cfg.file_store)
    if not db.is_file():
        raise FileNotFoundError(f"no database at {db}")
    ws = config_path.parent if config_path else db.parent
    out = Path(out).expanduser() if out else backups_dir()
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = out / f"{ws.name}-{stamp}.tar.gz"
    tmp = out / f".{archive.name}.db"
    try:
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        dst = sqlite3.connect(tmp)
        with dst:
            src.backup(dst)
        counts = {}
        for t in ("studies", "decks", "runs", "files", "approvals",
                  *(f"{k}_model_versions" for k in KINDS)):
            try:
                counts[t] = dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                pass
        src.close()
        dst.close()
        manifest = {"opmagent": __version__, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "workspace": str(ws), "config": str(config_path) if config_path else None,
                    "database": str(db), "file_store": str(fs), "with_files": files, "rows": counts}
        skip = {"files/" + str(v.relative_to(fs)) for v in _views(fs)}   # views: remade when opened
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(tmp, arcname="opm.db")
            if config_path and config_path.is_file():
                tar.add(config_path.resolve(), arcname="opmagent.toml")
            data = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mtime = len(data), int(time.time())
            tar.addfile(info, io.BytesIO(data))
            if files and fs.is_dir():
                tar.add(fs, arcname="files", filter=lambda ti: None if ti.name in skip else ti)
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "archive": str(archive), "bytes": archive.stat().st_size, "manifest": manifest}


# ------------------------------------------------------------------ clean
def _approved(con) -> dict[str, set[int]]:
    return {k: {r[0] for r in con.execute(f"SELECT id FROM {k}_model_versions WHERE status='approved'")}
            for k in KINDS}


def plan(cfg, categories=CATEGORIES) -> list[dict]:
    """What clean would remove: [{category, path, bytes, why}]. Reads only."""
    fs = Path(cfg.file_store)
    items: list[dict] = []
    con = dbmod.connect(cfg.database)
    try:
        if "runs" in categories:
            for r in con.execute("SELECT id, out_dir FROM runs WHERE status='failed' AND out_dir != ''"):
                p = Path(r["out_dir"])
                if p.is_dir() and _inside(p, fs):
                    items.append({"category": "runs", "path": p, "run_id": r["id"],
                                  "why": f"run {r['id']} failed"})
        if "imports" in categories and (fs / "imports").is_dir():
            approved = _approved(con)
            for name in sorted(d for d in (fs / "imports").iterdir() if d.is_dir()):
                imps = sorted(name.glob("import-[0-9]*"))
                for imp in imps[:-1]:                           # the newest import of each name is kept
                    meta = imp / "import.json"
                    if not meta.is_file():
                        continue                                # unknown content: left alone
                    v = json.loads(meta.read_text()).get("versions", {})
                    if any(v.get(k) in approved[k] for k in KINDS):
                        continue                                # a model of it is approved: kept
                    items.append({"category": "imports", "path": imp,
                                  "why": f"older import of {name.name}, none of its models approved"})
        if "views" in categories:
            for v in _views(fs):
                for p in (sorted(v.iterdir()) if v.name == "init_views" and v.is_dir() else [v] if v.is_dir() else []):
                    items.append({"category": "views", "path": p, "why": "remade when opened"})
    finally:
        con.close()
    for it in items:
        it["bytes"] = size_of(it["path"])
    return items


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def clean(cfg, items: list[dict]) -> dict:
    """Remove what plan() listed. Only paths inside the file store; failed runs get a note."""
    fs = Path(cfg.file_store)
    removed, freed = [], 0
    con = dbmod.connect(cfg.database)
    try:
        for it in items:
            p = Path(it["path"])
            if not _inside(p, fs) or p.resolve() == fs.resolve() or not p.exists():
                continue
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
            removed.append(str(p))
            freed += it.get("bytes", 0)
            if it["category"] == "runs":
                with dbmod.transaction(con):
                    con.execute("UPDATE runs SET note = trim(coalesce(note || '; ', '') || ?) WHERE id = ?",
                                ("files removed by opmagent clean", it["run_id"]))
    finally:
        con.close()
    return {"ok": True, "removed": removed, "bytes": freed}
