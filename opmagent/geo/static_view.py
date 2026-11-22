"""Static view of a geo model: a deck holding only the static model, made into EGRID + INIT by an OPM Flow dry run,
so ResInsight shows the grid and its properties as OPM computes them (active cells, pore volumes, depths,
transmissibilities) - before any simulation, for a draft as well as an approved version.

OPM Flow won't load a deck without PROPS and SOLUTION, so the deck carries small placeholder tables (one dead oil,
water, a two-point SWOF, an EQUIL far below the grid). They change no static array; the deck says so in its header and
is never registered as a deck (decks need four approved models). With a res version on the same geo version, its
region arrays (SATNUM, EQLNUM, FIPNUM) and SWATINIT are included too.

    <file_store>/qc/geo_<model>_v<N>/static/STATIC_<MODEL>_V<N>.DATA  (+ .EGRID, .INIT, run.log)
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from ..core.errors import INVALID_INPUT, ToolError
from ..deck.render import _num, _region_count, grid_section
from ..notebooks.runner import qc_dir

RES_ARRAYS = ("SATNUM", "EQLNUM", "FIPNUM", "SWATINIT")
# RUNSPEC keywords that size what a passed-through GRID keyword needs (MULTREGT regions, faults); the rest of a deck's
# RUNSPEC passthrough (tracers, end-point scaling, VFP dimensions) asks for PROPS or SCHEDULE data a static view lacks
GRID_RUNSPEC = {"GRIDOPTS", "REGDIMS", "FAULTDIM", "NSTACK"}
PLACEHOLDER = "-- placeholder for OPM to load the deck: static view only, not for simulation"


def _paths(con, rows: list[dict]) -> list[dict]:
    out = []
    for p in rows:
        p = dict(p)
        if p.get("file_id"):
            r = con.execute("SELECT path FROM files WHERE id = ?", (p["file_id"],)).fetchone()
            p["path"] = r["path"] if r else None
        else:
            p["path"] = None
        out.append(p)
    return out


def render_static(geo: dict, res: dict | None, file_paths: dict[int, str], title: str,
                  passthrough: dict | None = None) -> str:
    """The static deck text: RUNSPEC, the geo model's GRID section, placeholder PROPS, the region arrays, a
    placeholder EQUIL, one report step. passthrough: the source deck's passthrough of a geo model rebuilt from a deck
    (its GRID and EDIT keywords, e.g. MULTREGT, change transmissibilities as in the real deck)."""
    pt = passthrough or {}
    gh, gt = geo["header"], geo["tables"]
    nx, ny, nz = gh["nx"], gh["ny"], gh["nz"]
    ncell = nx * ny * nz
    props = {p["keyword"]: p for p in gt["geo_properties"]}
    if res is not None:
        props.update({p["keyword"]: p for p in res["tables"].get("res_properties", []) if p["keyword"] in RES_ARRAYS})
    view = {"tables": {"geo_properties": list(props.values())}}
    npvt = _region_count(view, "PVTNUM")
    nsat = _region_count(view, "SATNUM")
    neql = _region_count(view, "EQLNUM")
    L: list[str] = []
    a = L.append
    a(f"-- Static view of geo {gh['model_name']} v{gh['version']}" +
      (f" with the region arrays of res {res['header']['model_name']} v{res['header']['version']}" if res else ""))
    a("-- Made by opmagent for ResInsight (OPM Flow dry run: EGRID + INIT). Not a simulation deck.")
    a("RUNSPEC\n")
    a(f"TITLE\n{title}\n")
    a(f"DIMENS\n {nx} {ny} {nz} /\n")
    a("OIL\nWATER\n")
    a(f"{gh['unit_system']}\n")
    a("START\n 1 'JAN' 2000 /\n")
    a(f"TABDIMS\n {nsat} {npvt} 2 2 /\n")
    a(f"EQLDIMS\n {neql} /\n")
    for item in pt.get("RUNSPEC", []):
        if item["keyword"] in GRID_RUNSPEC:
            a(item["raw_text"].rstrip() + "\n")
    a("GRID\n")
    L += grid_section(gh, gt, file_paths)
    for item in pt.get("GRID", []):
        a(item["raw_text"].rstrip() + "\n")
    if pt.get("EDIT"):
        a("EDIT\n")
        for item in pt["EDIT"]:
            a(item["raw_text"].rstrip() + "\n")
    a("PROPS\n")
    a(PLACEHOLDER)
    a("PVDO")
    for _ in range(npvt):
        a(" 100 1.1 1.0\n 400 1.0 1.0 /")
    a("")
    a("PVTW")
    for _ in range(npvt):
        a(" 200 1.0 4.5e-5 0.5 0 /")
    a("")
    a("DENSITY")
    for _ in range(npvt):
        a(" 800 1000 1 /")
    a("")
    a("ROCK\n 200 0 /\n")
    a("SWOF")
    for _ in range(nsat):
        a(" 0 0 1 0\n 1 1 0 0 /")
    a("")
    if "SWATINIT" in props:
        p = props["SWATINIT"]
        a(f"SWATINIT\n {ncell}*{_num(p['const_value'])} /\n" if p["const_value"] is not None
          else f"INCLUDE\n '{p['path']}' /\n")
    regs = [kw for kw in ("SATNUM", "PVTNUM", "EQLNUM", "FIPNUM") if kw in props]
    if regs:
        a("REGIONS\n")
        for kw in regs:
            p = props[kw]
            a(f"{kw}\n {ncell}*{int(p['const_value'])} /\n" if p["const_value"] is not None
              else f"INCLUDE\n '{p['path']}' /\n")
    a("SOLUTION\n")
    a(PLACEHOLDER)
    a("EQUIL")
    for _ in range(neql):
        a(" 0 200 1e5 0 0 0 /")
    a("")
    a("SCHEDULE\n")
    a("TSTEP\n 1 /\n")
    a("END")
    return "\n".join(L) + "\n"


def build_static_view(cfg, con, geo_version_id: int, *, res_version_id: int | None = None,
                      timeout: int = 600) -> dict:
    """Render the static deck of a geo version and run OPM Flow's dry run on it. Returns the deck, EGRID and INIT
    paths and the arrays INIT holds. Read only for the model: writes files in its QC folder, registers nothing."""
    from ..core import models
    geo = models.get_model(con, "geo", geo_version_id)
    gh = geo["header"]
    res = None
    if res_version_id is not None:
        res = models.get_model(con, "res", res_version_id)
        if res["header"]["geo_version_id"] != geo_version_id:
            raise ToolError(INVALID_INPUT, f"res version {res_version_id} is defined on geo version "
                                           f"{res['header']['geo_version_id']}, not {geo_version_id}")
        res["tables"]["res_properties"] = _paths(con, res["tables"].get("res_properties", []))
    geo["tables"]["geo_properties"] = _paths(con, geo["tables"]["geo_properties"])
    file_paths = {}
    if gh.get("grid_file_id"):
        r = con.execute("SELECT path FROM files WHERE id = ?", (gh["grid_file_id"],)).fetchone()
        file_paths[gh["grid_file_id"]] = r["path"]
    exe = shutil.which(cfg.flow_bin)
    if not exe:
        raise ToolError(INVALID_INPUT, f"the static view needs the OPM Flow binary ([paths] flow_bin = {cfg.flow_bin} "
                                       "is not on PATH)")
    out = qc_dir(cfg.file_store, gh["model_name"], gh["version"], kind="geo") / "static"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", f"STATIC_{gh['model_name']}_V{gh['version']}").upper()
    deck = out / f"{stem}.DATA"
    deck.write_text(render_static(geo, res, file_paths, f"static view {gh['model_name']} v{gh['version']}",
                                  _source_passthrough(con, gh)))
    log = out / "run.log"
    with open(log, "w") as fh:
        r = subprocess.run([exe, str(deck), "--enable-dry-run=true", f"--output-dir={out}"], stdout=fh,
                           stderr=subprocess.STDOUT, cwd=out, timeout=timeout)
    egrid, init = out / f"{stem}.EGRID", out / f"{stem}.INIT"
    if r.returncode != 0 or not egrid.is_file():
        tail = log.read_text().strip().splitlines()[-8:]
        raise ToolError("STATIC_VIEW_FAILED", "OPM Flow could not load the static deck: " + " | ".join(tail),
                        {"deck": str(deck), "log": str(log)})
    import json
    (out / "view.json").write_text(json.dumps({"geo_version_id": geo_version_id, "res_version_id": res_version_id}))
    arrays = []
    try:
        from opm.io.ecl import EclFile
        skip = {"INTEHEAD", "LOGIHEAD", "DOUBHEAD", "TABDIMS", "TAB"}
        arrays = [x[0] for x in EclFile(str(init)).arrays if x[0] not in skip]
    except Exception:  # noqa: BLE001  the list is informative only
        pass
    active = next((ln.split(":")[1].split("/")[0].strip() for ln in log.read_text().splitlines()
                   if "Total number of active cells" in ln), None)
    return {"ok": True, "deck": str(deck), "egrid": str(egrid), "init": str(init), "folder": str(out),
            "arrays": arrays, "active_cells": int(active) if active and active.isdigit() else None,
            "res_version_id": res_version_id,
            "spoken_summary": f"Static view of geo {gh['model_name']} v{gh['version']} is ready: grid {gh['nx']} by "
                              f"{gh['ny']} by {gh['nz']}" + (f", {active} active cells" if active else "") +
                              f". Open it in ResInsight with: opmagent results open --geo {geo_version_id}."}


def _source_passthrough(con, gh: dict) -> dict:
    """The passthrough of the deck a geo version was rebuilt from, through its study (data_source.study)."""
    spec = gh.get("spec_json") or {}
    if isinstance(spec, str):
        import json
        spec = json.loads(spec or "{}")
    study = (spec.get("data_source") or {}).get("study")
    if not study:
        return {}
    from ..data import deck_source
    try:
        if not deck_source.has_deck(con, study):
            return {}
        return deck_source.deck_info(con, study)["spec"].get("passthrough") or {}
    except ToolError:
        return {}


def latest_res_on(con, geo_version_id: int) -> int | None:
    """The res version to show with a geo version: the approved one on it if any, else the latest."""
    r = con.execute("SELECT id FROM res_model_versions WHERE geo_version_id = ? "
                    "ORDER BY (status = 'approved') DESC, id DESC LIMIT 1", (geo_version_id,)).fetchone()
    return r["id"] if r else None


def static_case(cfg, con, geo_version_id: int) -> Path | None:
    """The EGRID of a geo version's static view, if it has been made."""
    from ..core import models
    h = models.header(con, "geo", geo_version_id)
    folder = qc_dir(cfg.file_store, h["model_name"], h["version"], kind="geo") / "static"
    return next(iter(sorted(folder.glob("*.EGRID"))), None) if folder.is_dir() else None


def view_res(cfg, con, geo_version_id: int) -> int | None:
    """The res version a geo version's current static view includes (None: none, or no view)."""
    import json
    egrid = static_case(cfg, con, geo_version_id)
    meta = egrid.parent / "view.json" if egrid else None
    return json.loads(meta.read_text()).get("res_version_id") if meta and meta.is_file() else None


def current_case(cfg, con, geo_version_id: int) -> Path:
    """The static view to open: the existing one if it includes the res model now on that geo version, else made
    again (a res model built after the geo model brings its region arrays and SWATINIT)."""
    want = latest_res_on(con, geo_version_id)
    egrid = static_case(cfg, con, geo_version_id)
    if egrid is not None and view_res(cfg, con, geo_version_id) == want:
        return egrid
    return Path(build_static_view(cfg, con, geo_version_id, res_version_id=want)["egrid"])


def after_res_build(cfg, con, res_version_id: int) -> dict:
    """Remake the static view of the res model's geo version with its region arrays; never fails the res build."""
    try:
        r = con.execute("SELECT geo_version_id FROM res_model_versions WHERE id = ?", (res_version_id,)).fetchone()
        v = build_static_view(cfg, con, r["geo_version_id"], res_version_id=res_version_id)
        return {"static_view": v["egrid"], "static_view_say":
                f" The geo static view now shows its regions: opmagent results open --geo {r['geo_version_id']}."}
    except Exception as e:  # noqa: BLE001
        return {"static_view_error": getattr(e, "message", None) or f"{type(e).__name__}: {e}", "static_view_say": ""}


def after_build(cfg, con, geo_version_id: int) -> dict:
    """Make the static view right after a geo build; never fails the build (no Flow binary, or Flow refusing the
    deck, is reported instead). Returns keys to merge into the build's result."""
    try:
        v = build_static_view(cfg, con, geo_version_id)
        return {"static_view": v["egrid"],
                "static_view_say": f" Static view for ResInsight: opmagent results open --geo {geo_version_id}."}
    except ToolError as e:
        return {"static_view_error": e.message if hasattr(e, "message") else str(e), "static_view_say": ""}
    except Exception as e:  # noqa: BLE001
        return {"static_view_error": f"{type(e).__name__}: {e}", "static_view_say": ""}
