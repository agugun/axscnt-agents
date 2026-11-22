"""Study data from an existing deck: the reverse process.

The data manager reads a deck (`opmagent data from-deck <study> <deck>`, MCP `load_deck_data`) and stores what
each modeler needs to rebuild it exactly; the modelers build their versions from it the usual way
(`opmagent study build fluid|geo|res|well`), the deck builder renders the deck with the source deck's own schedule,
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

KINDS = ("fluid", "geo", "res", "well")
RES_PROPS = {"SATNUM", "EQLNUM", "FIPNUM", "ROCKNUM", "SWATINIT"}   # arrays of the res model; the rest are geo's


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
        all_props = imp["geo"]["props"] + imp["res"]["props"]
        for p in sorted({*imp["files"], *(x["file"] for x in all_props if "file" in x),
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
                 {"keyword": x["keyword"], "const_value": x["const_value"]} for x in all_props]
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
            issues.append(("res" if arr in RES_PROPS else "geo", "warning", arr,
                           f"{n} cell(s) of {arr} can't be written back bit-exactly"))
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


def copy_input(con, name: str, source_study: str, *, write: bool = True) -> dict:
    """Give study `name` the input another study read from a deck (scenario 2 of testing/: rebuild from scenario
    1's input). Tables and the deck spec are copied row for row; the grid and array files are copied into
    <name>/data/deck/load-NNN/ after checking each is still the file the source study loaded. The deck itself is
    not read again."""
    if not has_deck(con, source_study):
        raise ToolError(INVALID_INPUT, f"study {source_study} has no input read from a deck; for raw data, copy its "
                                       f"inbox and run `opmagent data load`")
    st = studies._study(con, name)
    sid, src_sid = st["id"], store.study_id(con, source_study)
    if sid == src_sid:
        raise ToolError(INVALID_INPUT, "the source study is this study")
    info = deck_info(con, source_study)
    files = [dict(r) for r in con.execute("SELECT * FROM data_files WHERE study_id = ? ORDER BY id", (src_sid,))]
    # every file the input refers to must still be the one that was loaded
    paths = {f["id"]: (store.verified_path(con, f["id"]) if f["category"] == "deck" else Path(f["path"]))
             for f in files}
    folder = _next_folder(Path(st["folder"]) / "data" / "deck") if write else Path(tempfile.mkdtemp())
    from .load import STUDY_TABLES
    con.execute("BEGIN IMMEDIATE")
    try:
        for t in STUDY_TABLES:
            con.execute(f"DELETE FROM {t} WHERE study_id = ?", (sid,))
        ids, copied = {}, 0
        for f in files:
            p = paths[f["id"]]
            if f["category"] == "deck":            # the grid and arrays: this study's own copy
                dst = folder / p.name
                shutil.copy2(p, dst)
                if filesmod.sha256_of(dst) != f["sha256"]:
                    raise ToolError("INTEGRITY", f"copy of {p.name} doesn't match the loaded file")
                fid = filesmod.register_file(con, dst, "grid" if p.name == "GRID.GRDECL" else "property")
                s_ = dst.stat()
                vals = (fid, str(dst), f["original"], s_.st_size, s_.st_mtime)
                copied += 1
            else:                                  # the source deck: the same file, referred to again
                vals = (f["file_id"], f["path"], f["original"], f["size_bytes"], f["mtime"])
            ids[f["id"]] = con.execute(
                "INSERT INTO data_files(study_id, file_id, format, category, path, original, sha256, size_bytes, "
                "mtime, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, vals[0], f["format"], f["category"], vals[1], vals[2], f["sha256"], vals[3], vals[4],
                 f["meta_json"])).lastrowid
        n = con.execute("INSERT INTO data_deck_rows(study_id, kind, tbl, seq, row_json) SELECT ?, kind, tbl, seq, "
                        "row_json FROM data_deck_rows WHERE study_id = ? ORDER BY id", (sid, src_sid)).rowcount
        con.execute("INSERT INTO data_sources(study_id, file_id, path, label, dataset, rows) SELECT ?, file_id, path, "
                    "label, dataset, rows FROM data_sources WHERE study_id = ?", (sid, src_sid))
        con.execute("INSERT INTO data_issues(study_id, modeler, severity, item, message) SELECT ?, modeler, severity, "
                    "item, message FROM data_issues WHERE study_id = ?", (sid, src_sid))
        headers = info["headers"]
        if headers["geo"].get("grid_data_file_id"):
            headers["geo"]["grid_data_file_id"] = ids[headers["geo"]["grid_data_file_id"]]
        props = [{**x, "data_file_id": ids[x["data_file_id"]]} if "data_file_id" in x else x for x in info["props"]]
        con.execute("INSERT INTO data_deck(study_id, source_path, source_file_id, folder, headers_json, spec_json, "
                    "props_json, coverage_json, grid_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, info["source_path"], info["source_file_id"], str(folder), _dumps(headers),
                     _dumps(info["spec"]), _dumps(props), _dumps(info["coverage"]), _dumps(info["grid"])))
        summary = store.inventory(con, sid)
    except BaseException:
        con.execute("ROLLBACK")
        shutil.rmtree(folder, ignore_errors=True)
        raise
    con.execute("COMMIT" if write else "ROLLBACK")
    if not write:
        shutil.rmtree(folder, ignore_errors=True)
    else:
        studies._event(con, sid, "data", "loaded", {"from_study": source_study, "rows": n, "files": copied}, "data")
    g = info["grid"]
    summary.update(loaded=write, from_study=source_study, rows=n, files=copied, folder=str(folder),
                   spoken_summary=f"Took study {source_study}'s input ({Path(info['source_path']).name}): {n} table "
                                  f"rows and {copied} grid and array files, all checked; grid {g['nx']} by "
                                  f"{g['ny']} by {g['nz']}." + ("" if write else " (check only: nothing was written)"))
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


def _arrays(con, info: dict, only: set | None = None) -> dict:
    """The study's cell arrays as numbers (from the checked files; constants filled), for the model views."""
    from ..reports.deck_model_report import read_grdecl_array
    g = info["grid"]
    n = g["nx"] * g["ny"] * g["nz"]
    out = {}
    for x in info["props"]:
        if only and x["keyword"] not in only:
            continue
        try:
            out[x["keyword"]] = (read_grdecl_array(store.verified_path(con, x["data_file_id"]), n)
                                 if "data_file_id" in x else np.full(n, float(x["const_value"])))
        except (ValueError, OSError):
            continue   # a view only: an array it can't read is left out of the plots
    return out


def _qc_report(con, path: Path, kind: str, name: str, version: int, info: dict, tables: dict, warnings: list,
               assumptions: list) -> Path:
    from ..reports.deck_model_report import model_report
    g = info["grid"]
    arrays = ({k: v for k, v in _arrays(con, info).items() if k not in RES_PROPS} if kind == "geo" else
              _arrays(con, info, RES_PROPS) if kind == "res" else None)
    poro_top = None
    if kind == "well":
        a = _arrays(con, info, {"PORO"}).get("PORO")
        if a is not None and a.size == g["nx"] * g["ny"] * g["nz"]:
            poro_top = a.reshape(g["nz"], g["ny"], g["nx"])[0]
    return model_report(path, kind=kind, name=name, version=version, source=info["source_path"], tables=tables,
                        header=info["headers"][kind], grid=g, coverage=info["coverage"], warnings=warnings,
                        assumptions=assumptions, arrays=arrays, poro_top=poro_top)


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
            if x["keyword"] in RES_PROPS:
                continue
            if "data_file_id" in x:
                p = store.verified_path(con, x["data_file_id"])
                props.append({"keyword": x["keyword"],
                              "file_id": D("register_file", path=str(p), kind="property")["file_id"]})
            else:
                props.append({"keyword": x["keyword"], "const_value": x["const_value"]})
        tables = {**tables, "geo_properties": props}
    if kind == "res":
        if geo_version_id is None:
            raise ToolError(INVALID_INPUT, "the res model is defined on the deck's grid: build the geo model first and "
                                           "give its version (the study does this when geo is done)")
        h["geo_version_id"] = geo_version_id
        props = []
        for x in info["props"]:
            if x["keyword"] not in RES_PROPS:
                continue
            if "data_file_id" in x:
                p = store.verified_path(con, x["data_file_id"])
                props.append({"keyword": x["keyword"],
                              "file_id": D("register_file", path=str(p), kind="property")["file_id"]})
            else:
                props.append({"keyword": x["keyword"], "const_value": x["const_value"]})
        tables = {**tables, "res_properties": props}
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
    rep = _qc_report(con, cfg.file_store / "qc" / f"{kind}_{slug}_v{v['version']}.html", kind, model_name,
                     v["version"], info, tables, [w["message"] if isinstance(w, dict) else str(w)
                                                  for w in val["warnings"]], assumptions)
    rid = D("register_file", path=str(rep), kind="qc_report")["file_id"]
    sub = D("submit_for_review", kind=kind, version_id=vid, qc_report_file_id=rid,
            metrics={"from_deck": Path(info["source_path"]).name, "tables": {t: len(r) for t, r in tables.items()}})
    sv, say = {}, ""
    if kind in ("geo", "res"):
        from ..geo.static_view import after_build, after_res_build
        sv = after_build(cfg, con, vid) if kind == "geo" else after_res_build(cfg, con, vid)
        say = sv.pop("static_view_say")
    return {"ok": True, "status": sub["header"]["status"], "version_id": vid, "version": v["version"],
            "qc_report": str(rep), "assumptions": assumptions, **sv,
            "spoken_summary": f"{kind.capitalize()} model {model_name} version {v['version']} {review_state(sub)}: "
                              f"rebuilt from {Path(info['source_path']).name}, "
                              f"{sum(len(r) for r in tables.values())} table rows." + say}


def deck_spec(con, study: str, name: str, requirement: str, ids: dict) -> dict:
    """The deck builder's spec for a deck-sourced study: the source deck's dates, schedule and passthrough."""
    info = deck_info(con, study)
    return {**info["spec"], "name": name, "requirement": requirement, **ids}


def _study_deck_path(con, deck_id: int) -> Path:
    r = con.execute("SELECT f.path FROM deck_files df JOIN files f ON f.id = df.file_id "
                    "WHERE df.deck_id = ? AND df.role = 'DATA'", (deck_id,)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"deck {deck_id} or its DATA file not found")
    return Path(r["path"])


def compare_with_source(cfg, con, study: str, on_progress=None, deck_only: bool = False) -> dict:
    """Is the study's deck the source deck again? Two levels:

    1. deck    - both decks read by the OPM parser: grid, every table row, every cell array, the schedule
                 (opmagent.deck.deck_compare; no simulation)
    2. results - the source deck is run (once per load) and the study's run compared with it: static, initial
                 state, every summary vector (opmagent.deck.roundtrip). Skipped with deck_only."""
    from ..deck import roundtrip
    from ..deck.deck_compare import compare_decks
    from ..sim.runner import run_deck
    st = studies.status(con, study)
    info = deck_info(con, study)
    res = {t["task"]: t["result"] for t in st["tasks"]}
    src = Path(info["source_path"])
    deck_id = (res.get("deck") or {}).get("deck_id")
    run_id = (res.get("run") or {}).get("run_id")
    if run_id and not deck_id:
        r = con.execute("SELECT deck_id FROM runs WHERE id = ?", (run_id,)).fetchone()
        deck_id = r["deck_id"] if r else None
    if not deck_id:
        raise ToolError(INVALID_INPUT, f"study {study} has no deck yet (the deck task isn't done)")
    gen = _study_deck_path(con, int(deck_id))
    out = {"source_deck": str(src), "study_deck": str(gen), "deck": compare_decks(src, gen)}
    if not deck_only:
        if not run_id:
            raise ToolError(INVALID_INPUT, f"study {study} has no finished run yet (use deck_only to compare the "
                                           f"decks without running)")
        r = con.execute("SELECT out_dir FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not r:
            raise ToolError(NOT_FOUND, f"run {run_id} not found")
        ref = Path(info["folder"]) / "run_source"
        if not (ref / f"{src.stem}.SMSPEC").exists():
            cb = on_progress or (lambda _label: None)
            ran = run_deck(src, ref, cfg.flow_bin, on_progress=cb("source"))
            if not ran["ok"]:
                return {"ok": False, "stage": "run source", "run": ran, **out,
                        "spoken_summary": "The source deck failed to run; see its log."}
        out["comparison"] = roundtrip.compare(ref, Path(r["out_dir"]), src.stem, gen.stem, src, gen)
    d = out["deck"]
    c = out.get("comparison")
    ok = d["passed"] and (deck_only or c["passed"])
    name = "compare_with_source_deck.json" if deck_only else "compare_with_source.json"
    report = Path(st["study"]["folder"]) / ("deck" if deck_only else "runs") / name
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"passed": ok, **out}, indent=1, default=str))
    say = d["spoken_summary"]
    if c is not None:
        if c["passed"]:
            say += (f" The runs match too: static, initial state and {c['levels'][2]['vectors_compared']} summary "
                    f"vectors within tolerance.")
        else:
            last = c["levels"][-1]
            say += f" The runs differ at the {last.get('level')} level: " + (
                ", ".join(last.get("failed", [])[:5]) or last.get("error", "see the report")) + "."
    studies._event(con, st["study"]["id"], "deck" if deck_only else "run", "compared",
                   {"passed": ok, "deck_passed": d["passed"], "results_passed": None if c is None else c["passed"],
                    "report": str(report)}, "compare")
    return {"ok": ok, **out, "report": str(report), "spoken_summary": say}
