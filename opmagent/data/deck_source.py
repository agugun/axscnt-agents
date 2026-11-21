"""Study data from an existing deck: the reverse process.

The data manager reads a deck (`opmagent data from-deck <study> <deck>`, MCP `load_deck_data`) and stores what
each modeler needs to rebuild it exactly; the modelers build their versions from it the usual way
(`opmagent study build fluid|geo|well`), the deck builder renders the deck with the source deck's own schedule,
dates and passthrough keywords, and `opmagent study compare` runs the source deck and checks the study's run
reproduces it (static grid and properties, initial state, every summary vector).

    deck ──(OPM parser, opmagent.deck.importer)──►
      tables  -> data_deck_rows   PVT (PVTO, PVDG, ...), SCAL, ROCK, contacts/EQUIL, faults, wells, groups,
                                  completions (IJK), schedule events; one JSON record per row, numbers exact
      arrays  -> files            GRID.GRDECL and one include file per cell array, in <study>/data/deck/load-NNN/
                                  (a new folder per load: versions built from an earlier load keep their files)
      the rest-> data_deck        model headers, deck spec (dates, schedule mode, passthrough), coverage

The forward modelers' own methods (EOS characterisation, kriging, MD perforations) can't be inverted: a deck
holds their results, not their raw data. So the rebuild takes the deck's tables, grid and arrays as given; the
modelers' validators, QC reports and the approval gate apply as for any other version.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path

import numpy as np

from ..core import files as filesmod
from ..core import studies
from ..core.errors import INVALID_INPUT, NOT_FOUND, ToolError
from . import store

KINDS = ("fluid", "geo", "well")


def _json_default(v):
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, Path):
        return str(v)
    raise TypeError(f"not JSON serialisable: {type(v).__name__}")


def _dumps(v) -> str:
    return json.dumps(v, default=_json_default)


def _next_folder(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    nums = [int(p.name.split("-")[1]) for p in base.glob("load-[0-9]*") if p.name.split("-")[1].isdigit()]
    out = base / f"load-{max(nums, default=0) + 1:03d}"
    out.mkdir()
    return out


def load_deck(con, name: str, deck_path: str | Path, *, write: bool = True) -> dict:
    """Read a deck into the study's data (replacing what was loaded before). write=False reports only."""
    from ..deck.importer import ImportError_, import_deck
    st = studies._study(con, name)
    deck_path = Path(deck_path).expanduser()
    if not deck_path.is_absolute():
        cand = Path(st["folder"]) / "inbox" / deck_path
        deck_path = cand if cand.exists() else deck_path.resolve()
    if not deck_path.is_file():
        raise ToolError(NOT_FOUND, f"deck not found: {deck_path}")
    folder = _next_folder(Path(st["folder"]) / "data" / "deck") if write else Path(tempfile.mkdtemp())
    try:
        imp = import_deck(deck_path, folder)
    except ImportError_ as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise ToolError("IMPORT_UNSUPPORTED", str(e)) from e
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    from .load import STUDY_TABLES
    sid = st["id"]
    con.execute("BEGIN IMMEDIATE")
    try:
        for t in STUDY_TABLES:
            con.execute(f"DELETE FROM {t} WHERE study_id = ?", (sid,))
        src_fid = filesmod.register_file(con, deck_path, "deck", "source deck (study data)")
        sha = con.execute("SELECT sha256 FROM files WHERE id = ?", (src_fid,)).fetchone()[0]
        stt = deck_path.stat()
        con.execute("INSERT INTO data_files(study_id, file_id, format, category, path, original, sha256, size_bytes, "
                    "mtime, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sid, src_fid, "deck", "decks", str(deck_path), deck_path.name, sha, stt.st_size, stt.st_mtime,
                     _dumps({"coverage": imp["coverage"]["counts"], "grid": imp["grid"]})))
        rows = 0
        for kind in KINDS:
            for tbl, recs in imp[kind]["tables"].items():
                con.executemany("INSERT INTO data_deck_rows(study_id, kind, tbl, seq, row_json) VALUES (?,?,?,?,?)",
                                [(sid, kind, tbl, i, _dumps(r)) for i, r in enumerate(recs or [])])
                rows += len(recs or [])
        con.execute("INSERT INTO data_sources(study_id, file_id, path, label, dataset, rows) VALUES (?,?,?,?,?,?)",
                    (sid, src_fid, str(deck_path), deck_path.name, "deck", rows))
        # arrays and the grid stay files: catalogue each, and point the geo header and properties at them
        ids = {}
        for p in sorted({*imp["files"], *(x["file"] for x in imp["geo"]["props"] if "file" in x),
                         *([imp["geo"]["header"]["grid_file"]] if imp["geo"]["header"].get("grid_file") else [])}):
            p = Path(p)
            fid = filesmod.register_file(con, p, "grid" if p.name == "GRID.GRDECL" else "property")
            sha = con.execute("SELECT sha256 FROM files WHERE id = ?", (fid,)).fetchone()[0]
            s = p.stat()
            ids[str(p)] = con.execute(
                "INSERT INTO data_files(study_id, file_id, format, category, path, original, sha256, size_bytes, "
                "mtime) VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, fid, "include", "deck", str(p), f"{deck_path.name}: {p.name}", sha, s.st_size,
                 s.st_mtime)).lastrowid
        headers = {k: dict(imp[k]["header"]) for k in KINDS}
        grid_file = headers["geo"].pop("grid_file", None)
        if grid_file:
            headers["geo"]["grid_data_file_id"] = ids[str(Path(grid_file))]
        props = [{"keyword": x["keyword"], "data_file_id": ids[str(Path(x["file"]))]} if "file" in x else
                 {"keyword": x["keyword"], "const_value": x["const_value"]} for x in imp["geo"]["props"]]
        con.execute("INSERT INTO data_deck(study_id, source_path, source_file_id, folder, headers_json, spec_json, "
                    "props_json, coverage_json, grid_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, str(deck_path), src_fid, str(folder), _dumps(headers), _dumps(imp["spec"]), _dumps(props),
                     _dumps(imp["coverage"]), _dumps(imp["grid"])))
        cov = imp["coverage"]
        issues = []
        if cov["counts"].get("passthrough"):
            issues.append(("data", "warning", "passthrough",
                           f"{cov['counts']['passthrough']} keyword(s) have no model home and are carried verbatim "
                           f"by the deck builder"))
        if cov["schedule_mode"] == "verbatim":
            issues.append(("well", "warning", "schedule",
                           "the schedule is carried verbatim (" + "; ".join(cov["schedule_reasons"][:3]) + "): the "
                           "well model holds wells and completions, the deck keeps the original schedule text"))
        for arr, n in (cov.get("inexact_cells") or {}).items():
            issues.append(("geo", "warning", arr, f"{n} cell(s) of {arr} can't be written back bit-exactly"))
        spec = imp["spec"]
        sp = st["spec"]
        if sp.get("start_date") and sp["start_date"] != spec["start_date"]:
            issues.append(("data", "warning", "period", f"the study period comes from the deck: {spec['start_date']} "
                                                        f"to {spec['end_date']} (the study said {sp['start_date']})"))
        con.executemany("INSERT INTO data_issues(study_id, modeler, severity, item, message) VALUES (?,?,?,?,?)",
                        [(sid, *i) for i in issues])
        summary = store.inventory(con, sid)
    except BaseException:
        con.execute("ROLLBACK")
        if write:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    con.execute("COMMIT" if write else "ROLLBACK")
    if not write:
        shutil.rmtree(folder, ignore_errors=True)
    else:
        studies._event(con, sid, "data", "loaded", {"deck": str(deck_path), "rows": rows,
                                                    "coverage": imp["coverage"]["counts"]}, "data")
    summary["loaded"] = write
    summary["deck"] = {"source": str(deck_path), "grid": imp["grid"], "coverage": imp["coverage"]["counts"],
                       "schedule_mode": imp["coverage"]["schedule_mode"], "start_date": imp["spec"]["start_date"],
                       "end_date": imp["spec"]["end_date"], "rows": rows,
                       "tables": {k: {t: len(r or []) for t, r in imp[k]["tables"].items() if r} for k in KINDS}}
    g = imp["grid"]
    summary["spoken_summary"] = (
        f"Read {deck_path.name}: grid {g['nx']} by {g['ny']} by {g['nz']} ({g['nactive']} active), "
        f"{len(imp['well']['tables']['wells'])} wells, {rows} table rows; schedule "
        f"{imp['coverage']['schedule_mode']}." + ("" if write else " (check only: nothing was written)"))
    return summary


# ---------------------------------------------------------------- read back (the data layer)
def has_deck(con, study) -> bool:
    return con.execute("SELECT 1 FROM data_deck WHERE study_id = ?", (store.study_id(con, study),)).fetchone() \
        is not None


def deck_info(con, study) -> dict:
    sid = store.study_id(con, study)
    r = con.execute("SELECT * FROM data_deck WHERE study_id = ?", (sid,)).fetchone()
    if not r:
        raise ToolError(INVALID_INPUT, "this study's data didn't come from a deck")
    d = dict(r)
    for k in ("headers_json", "spec_json", "props_json", "coverage_json", "grid_json"):
        d[k[:-5]] = json.loads(d.pop(k)) if d[k] else None
    return d


def deck_tables(con, study, kind: str | None = None, table: str | None = None) -> dict:
    """{kind: {table: [rows]}} as the importer produced them (exact numbers)."""
    sid = store.study_id(con, study)
    q, args = "SELECT kind, tbl, row_json FROM data_deck_rows WHERE study_id = ?", [sid]
    for col, v in (("kind", kind), ("tbl", table)):
        if v:
            q, args = q + f" AND {col} = ?", args + [v]
    out: dict = {}
    for r in con.execute(q + " ORDER BY id", args):   # load order: the importer's table order (wells before completions)
        out.setdefault(r["kind"], {}).setdefault(r["tbl"], []).append(json.loads(r["row_json"]))
    return out


def _qc_report(path: Path, kind: str, name: str, version: int, info: dict, tables: dict, warnings: list) -> Path:
    from ..reports.simple_report import _list, _page, _table
    g = info["grid"]
    rows = [[t, len(r)] for t, r in tables.items()]
    sections = [("Source", _list([f"deck: {info['source_path']}", f"grid {g['nx']} x {g['ny']} x {g['nz']}, "
                                  f"{g['nactive']} active", f"schedule: {info['coverage']['schedule_mode']}"])),
                ("Tables taken from the deck", _table(["table", "rows"], rows)),
                ("Validator warnings", _list(warnings))]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_page(f"{kind} model {name} v{version} (from deck)",
                          f"rebuilt from {html.escape(Path(info['source_path']).name)}", sections, [], False))
    return path


def build_model(cfg, con, study: str, kind: str, model_name: str, *, geo_version_id: int | None = None,
                description: str | None = None) -> dict:
    """A modeler's version from the study's deck data, through that modeler's own profile (origin 'built')."""
    from ..core.models import review_state
    from ..servers.data_server import DataService
    from ..servers.data_server import wrap as dwrap
    if kind not in KINDS:
        raise ToolError(INVALID_INPUT, f"kind must be one of {KINDS}")
    info = deck_info(con, study)
    tables = deck_tables(con, study, kind).get(kind, {})
    svc = DataService(kind, cfg)

    def D(n, **k):
        r = dwrap(svc, n)(**k)
        if not r.get("ok"):
            raise ToolError(r.get("code", "ERROR"), r.get("message", "failed"), r.get("details"))
        return r

    src_ids = [info["source_file_id"]]
    h = dict(info["headers"][kind])
    if kind == "geo":
        if h.get("grid_data_file_id"):
            p = store.verified_path(con, h.pop("grid_data_file_id"))
            h["grid_file_id"] = D("register_file", path=str(p), kind="grid")["file_id"]
        props = []
        for x in info["props"]:
            if "data_file_id" in x:
                p = store.verified_path(con, x["data_file_id"])
                props.append({"keyword": x["keyword"],
                              "file_id": D("register_file", path=str(p), kind="property")["file_id"]})
            else:
                props.append({"keyword": x["keyword"], "const_value": x["const_value"]})
        tables = {**tables, "geo_properties": props}
    if kind == "well":
        if geo_version_id is None:
            raise ToolError(INVALID_INPUT, "the deck's completions are on its grid: build the geo model first and "
                                           "give its version (the study does this when geo is done)")
        h["geo_version_id"] = geo_version_id
    assumptions = [f"taken as given from {Path(info['source_path']).name}: tables, grid and cell arrays unchanged "
                   f"(the reverse of a deck can't recover raw lab data, logs or tops)"]
    if kind == "well" and info["coverage"]["schedule_mode"] == "verbatim":
        assumptions.append("schedule carried verbatim by the deck builder: "
                           + "; ".join(info["coverage"]["schedule_reasons"][:3]))
    h.update(source_deck_file_id=info["source_file_id"], user_description=description,
             assumptions_json=assumptions,
             spec_json={"data_source": {"study": study, "deck": info["source_path"], "folder": info["folder"]}})
    v = D("create_version", kind=kind, model_name=model_name, header=h, source_file_ids=src_ids)
    vid = v["version_id"]
    for t, recs in tables.items():
        if recs:
            D("write_tables", kind=kind, version_id=vid, table=t, rows=recs)
    val = D("run_validator", kind=kind, version_id=vid)
    if not val["passed"]:
        return {"ok": False, "status": "draft", "version_id": vid, "validator": val,
                "spoken_summary": val["spoken_summary"] + " The draft is kept for fixing."}
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", model_name)
    rep = _qc_report(cfg.file_store / "qc" / f"{kind}_{slug}_v{v['version']}.html", kind, model_name, v["version"],
                     info, tables, [w["message"] if isinstance(w, dict) else str(w) for w in val["warnings"]])
    rid = D("register_file", path=str(rep), kind="qc_report")["file_id"]
    sub = D("submit_for_review", kind=kind, version_id=vid, qc_report_file_id=rid,
            metrics={"from_deck": Path(info["source_path"]).name, "tables": {t: len(r) for t, r in tables.items()}})
    return {"ok": True, "status": sub["header"]["status"], "version_id": vid, "version": v["version"],
            "qc_report": str(rep), "assumptions": assumptions,
            "spoken_summary": f"{kind.capitalize()} model {model_name} version {v['version']} {review_state(sub)}: "
                              f"rebuilt from {Path(info['source_path']).name}, "
                              f"{sum(len(r) for r in tables.values())} table rows."}


def deck_spec(con, study: str, name: str, requirement: str, ids: dict) -> dict:
    """The deck builder's spec for a deck-sourced study: the source deck's dates, schedule and passthrough."""
    info = deck_info(con, study)
    return {**info["spec"], "name": name, "requirement": requirement, **ids}


def compare_with_source(cfg, con, study: str, on_progress=None) -> dict:
    """Run the source deck (once per load) and compare the study's run with it: static, initial, dynamic."""
    from ..deck import roundtrip
    from ..sim.runner import run_deck
    st = studies.status(con, study)
    info = deck_info(con, study)
    res = {t["task"]: t["result"] for t in st["tasks"]}
    run_id = (res.get("run") or {}).get("run_id")
    if not run_id:
        raise ToolError(INVALID_INPUT, f"study {study} has no finished run yet")
    r = con.execute("SELECT r.out_dir, f.path FROM runs r JOIN decks d ON d.id = r.deck_id JOIN deck_files df "
                    "ON df.deck_id = d.id AND df.role = 'DATA' JOIN files f ON f.id = df.file_id WHERE r.id = ?",
                    (run_id,)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"run {run_id} or its deck not found")
    src = Path(info["source_path"])
    ref = Path(info["folder"]) / "run_source"
    if not (ref / f"{src.stem}.SMSPEC").exists():
        cb = on_progress or (lambda _label: None)
        out = run_deck(src, ref, cfg.flow_bin, on_progress=cb("source"))
        if not out["ok"]:
            return {"ok": False, "stage": "run source", "run": out,
                    "spoken_summary": "The source deck failed to run; see its log."}
    cmp = roundtrip.compare(ref, Path(r["out_dir"]), src.stem, Path(r["path"]).stem, src, Path(r["path"]))
    report = Path(st["study"]["folder"]) / "runs" / "compare_with_source.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(cmp, indent=1, default=str))
    if cmp["passed"]:
        say = (f"The study reproduces {src.name}: static, initial state and "
               f"{cmp['levels'][2]['vectors_compared']} summary vectors match within tolerance.")
    else:
        last = cmp["levels"][-1]
        say = f"The study differs from {src.name} at the {last.get('level')} level: " + (
            ", ".join(last.get("failed", [])[:5]) or last.get("error", "see the report")) + "."
    studies._event(con, st["study"]["id"], "run", "compared", {"passed": cmp["passed"], "report": str(report)},
                   "compare")
    return {"ok": cmp["passed"], "comparison": cmp, "report": str(report), "spoken_summary": say}
