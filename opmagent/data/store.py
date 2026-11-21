"""Study data layer: the one way to read a study's data, whether it is a table in the database or a file.

The data manager (opmagent.data.load) converts tabular data into the data_* tables and files the rest as it came
in <study>/data/, with a catalog entry (data_files; LAS curves in data_log_curves). Readers here combine both:
a log is found in the catalog, its file is checked against the checksum it was loaded with (`verified_path`), and
read with its format's reader (opmagent.data.formats). A file changed or removed since the load is refused until
the data manager loads again, so a model is never built from data that no longer matches the catalog.

Each reader returns the data in the shape that modeler's code already takes, so a model built from a study is
built from exactly what the data manager loaded (and the source files are recorded with the version):

    fluid_input(con, study)  -> (fluid_input dict, gaps)          fluid modeler
    tops / logs / surveys / geo_spec(con, study)  -> DataFrames, spec dict   geo modeler
    well_inputs(con, study)  -> {wells, surveys, perforations, controls, history: DataFrame}   well modeler

and for the agents' own analysis: inventory (what is loaded, per dataset and well, with the open issues),
rows (one dataset, filtered by well), log_stats (per well and curve, from the catalog: samples, depth range,
min/mean/max), log_samples (from the files), files (the catalog), verify (every file against its checksum).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ..core.errors import INVALID_INPUT, NOT_FOUND, ToolError
from ..core.files import sha256_of
from . import formats

TABLES = {"wells": "data_wells", "tops": "data_tops", "surveys": "data_surveys", "perforations": "data_perforations",
          "controls": "data_controls", "history": "data_production", "sources": "data_sources",
          "issues": "data_issues", "log_curves": "data_log_curves", "pvt_samples": "data_pvt_samples",
          "files": "data_files"}
DATASETS = ["pvt", "tops", "logs", "geo_spec", "contacts", "wells", "surveys", "perforations", "controls", "history"]
FOR = {"fluid": ["pvt"], "geo": ["tops", "logs", "geo_spec", "surveys"], "res": ["contacts", "geo_spec"],
       "well": ["wells", "surveys", "perforations", "controls", "history"]}


def study_id(con, study) -> int:
    if isinstance(study, int):
        return study
    r = con.execute("SELECT id FROM studies WHERE name = ?", (study,)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"no study named {study}")
    return r["id"]


def _df(con, sql, args) -> pd.DataFrame:
    rows = con.execute(sql, args).fetchall()
    return pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame()


def _drop_empty(df: pd.DataFrame) -> pd.DataFrame:
    return df.dropna(axis=1, how="all") if not df.empty else df


def source_file_ids(con, study, datasets: list[str] | None = None) -> list[int]:
    """The registered inbox files the study's data came from (for a model version's sources)."""
    sid = study_id(con, study)
    q = "SELECT DISTINCT file_id FROM data_sources WHERE study_id = ? AND file_id IS NOT NULL"
    args: list = [sid]
    if datasets:
        q += f" AND dataset IN ({','.join('?' * len(datasets))})"
        args += datasets
    return [r[0] for r in con.execute(q + " ORDER BY file_id", args)]


def issues(con, study, modeler: str | None = None) -> list[dict]:
    sid = study_id(con, study)
    q, args = "SELECT modeler, severity, item, message FROM data_issues WHERE study_id = ?", [sid]
    if modeler:
        q, args = q + " AND modeler = ?", args + [modeler]
    return [dict(r) for r in con.execute(q + " ORDER BY id", args)]


def ready(con, study, modeler: str) -> bool:
    sid = study_id(con, study)
    loaded = con.execute("SELECT COUNT(*) FROM data_sources WHERE study_id = ?", (sid,)).fetchone()[0]
    return bool(loaded) and not any(i["severity"] in ("required", "decision") for i in issues(con, sid, modeler))


# ---------------------------------------------------------------- fluid
def fluid_input(con, study) -> tuple[dict, list[dict]]:
    """The selected lab sample as the fluid modeler's fluid_input, with the data manager's fluid issues as gaps."""
    from ..fluid.excel import COMPONENT_NAMES
    sid = study_id(con, study)
    s = con.execute("SELECT p.*, d.path FROM data_pvt_samples p LEFT JOIN data_sources d ON d.id = p.source_id "
                    "WHERE p.study_id = ? ORDER BY p.selected DESC, p.id LIMIT 1", (sid,)).fetchone()
    if not s:
        raise ToolError(INVALID_INPUT, "no lab report loaded for this study (data manager: opmagent data load)")
    comp, plus = {}, None
    for r in con.execute("SELECT * FROM data_pvt_composition WHERE sample_id = ?", (s["id"],)):
        if r["component"].endswith("+"):
            plus = {"name": r["component"].rstrip("+"), "mol_percent": r["mol_percent"], "mw_g_mol": r["mw_g_mol"],
                    "sg": r["sg"]}
        else:
            comp[COMPONENT_NAMES.get(r["component"].upper(), r["component"])] = r["mol_percent"]
    t = {k: [] for k in ("separator", "cce", "dl", "viscosity")}
    for r in con.execute("SELECT * FROM data_pvt_tests WHERE sample_id = ? ORDER BY test, stage, id", (s["id"],)):
        t[r["test"]].append(r)
    fi = {
        "sample_name": s["sample_name"], "temperature_c": s["temperature_c"], "psat_lab": s["psat_bar"],
        "unit_system": s["unit_system"] or "METRIC", "salinity_ppm": s["salinity_ppm"] or 0.0,
        "p_min": s["p_min_bar"], "p_max": s["p_max_bar"], "composition": comp, "plus_fraction": plus,
        "separator_stages": [{"pressure": r["pressure"], "temperature": r["temperature"]} for r in t["separator"]],
        "measured": {"measured_gor_sm3_sm3": s["gor_sm3_sm3"], "measured_bo_fb": s["bo_fb"], "measured_api": s["api"]},
        "cce": [{"pressure": r["pressure"], "relative_volume": r["relative_volume"]} for r in t["cce"]],
        "dl": [{"pressure": r["pressure"], "rs": r["rs"], "bo": r["bo"], "oil_density": r["oil_density"]}
               for r in t["dl"]],
        "viscosity": [{"pressure": r["pressure"], "mu_o": r["mu_o"]} for r in t["viscosity"]],
        "source_file": s["path"],
    }
    gaps = [{"severity": "required" if i["severity"] in ("required", "decision") else "recommended",
             "item": i["item"], "message": i["message"]} for i in issues(con, sid, "fluid")]
    return fi, gaps


# ---------------------------------------------------------------- geo
def tops(con, study) -> pd.DataFrame:
    return _df(con, "SELECT well_name, surface, x, y, tvd FROM data_tops WHERE study_id = ? "
                    "ORDER BY well_name, tvd", (study_id(con, study),))


def surveys(con, study) -> pd.DataFrame:
    return _drop_empty(_df(con, "SELECT well_name, md, x, y, tvd, inc, azi, x0, y0 FROM data_surveys "
                                "WHERE study_id = ? ORDER BY well_name, md", (study_id(con, study),)))


def survey_xyz(con, study) -> pd.DataFrame | None:
    """Surveys as x, y, tvd stations (inclination/azimuth surveys by minimum curvature from the wellhead)."""
    sv = surveys(con, study)
    if sv.empty:
        return None
    from ..well.ingest import surveys as to_xyz
    w = wells(con, study)
    heads = {r.well_name: (r.x or 0.0, r.y or 0.0) for r in w.itertuples()} if not w.empty and "x" in w else {}
    return pd.DataFrame(to_xyz(sv, heads))


# ---------------------------------------------------------------- files
def _check(con, f: dict) -> str:
    """'ok', 'changed' or 'missing' for a catalogued file. Size and mtime first; the checksum only if they moved."""
    p = Path(f["path"])
    if not p.exists():
        return "missing"
    st = p.stat()
    if st.st_size == f["size_bytes"] and f["mtime"] is not None and abs(st.st_mtime - f["mtime"]) < 1e-6:
        return "ok"
    if sha256_of(p) == f["sha256"]:
        con.execute("UPDATE data_files SET mtime = ? WHERE id = ?", (st.st_mtime, f["id"]))   # touched, same bytes
        return "ok"
    return "changed"


def verified_path(con, data_file_id: int) -> Path:
    """The catalogued file's path, after checking it is still the file that was loaded."""
    r = con.execute("SELECT * FROM data_files WHERE id = ?", (data_file_id,)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"no catalogued file {data_file_id}")
    f = dict(r)
    state = _check(con, f)
    if state != f["status"]:
        con.execute("UPDATE data_files SET status = ? WHERE id = ?", (state, f["id"]))
    if state != "ok":
        raise ToolError(INVALID_INPUT, f"{f['path']} has {'changed' if state == 'changed' else 'gone'} since it was "
                                       f"loaded: the data manager loads the study again (opmagent data load)",
                        {"data_file_id": f["id"], "status": state})
    return Path(f["path"])


def files(con, study, category: str | None = None, well: str | None = None) -> list[dict]:
    """The study's file catalog (format, category, path, original inbox name, well, size, status, metadata)."""
    sid = study_id(con, study)
    q, args = ("SELECT id, format, category, path, original, well_name, size_bytes, status, meta_json "
               "FROM data_files WHERE study_id = ?"), [sid]
    for col, v in (("category", category), ("well_name", well)):
        if v:
            q, args = q + f" AND {col} = ?", args + [v]
    out = []
    for r in con.execute(q + " ORDER BY category, original", args):
        d = dict(r)
        d["meta"] = json.loads(d.pop("meta_json")) if d["meta_json"] else None
        out.append(d)
    return out


def verify(con, study) -> dict:
    """Check every catalogued file against what was loaded; changed or missing files need a reload."""
    sid = study_id(con, study)
    bad = []
    for r in con.execute("SELECT * FROM data_files WHERE study_id = ?", (sid,)).fetchall():
        state = _check(con, dict(r))
        if state != r["status"]:
            con.execute("UPDATE data_files SET status = ? WHERE id = ?", (state, r["id"]))
        if state != "ok":
            bad.append({"path": r["path"], "original": r["original"], "status": state})
    n = con.execute("SELECT COUNT(*) FROM data_files WHERE study_id = ?", (sid,)).fetchone()[0]
    return {"files": n, "not_ok": bad,
            "spoken_summary": (f"All {n} files are as loaded." if not bad else
                               f"{len(bad)} of {n} files changed or gone since the load: "
                               + ", ".join(b["original"] for b in bad[:5]) + ". Load the study again.")}


# ---------------------------------------------------------------- logs (LAS files, through the catalog)
def _las_files(con, study, well: str | None = None) -> list[dict]:
    sid = study_id(con, study)
    q, args = "SELECT id, well_name FROM data_files WHERE study_id = ? AND format = 'las'", [sid]
    if well:
        q, args = q + " AND well_name = ?", args + [well]
    return [dict(r) for r in con.execute(q + " ORDER BY id", args)]


def log_table(con, study, well: str) -> pd.DataFrame:
    """One well's logs as a wide table: depth (the LAS index) then a column per curve, read from its LAS file(s)
    after the integrity check. Several files for a well are merged on depth; where they overlap, the later wins."""
    out = None
    for f in _las_files(con, study, well):
        df = formats.read("las", verified_path(con, f["id"]))
        df = df.rename(columns={df.columns[0]: "depth"}).set_index("depth")
        out = df if out is None else df.combine_first(out)
    if out is None:
        return pd.DataFrame(columns=["depth"])
    return out.sort_index().reset_index()


def log_wells(con, study) -> list[str]:
    return sorted({f["well_name"] for f in _las_files(con, study)})


def logs(con, study, curves: dict | None = None) -> pd.DataFrame:
    """Rows well_name, tvd, phi, perm, vsh as the geo modeler upscales them (curves: the spec's curve choices)."""
    from ..geo.build import clean_logs, curve_map, pick_curves
    cm, sv = curve_map(curves), survey_xyz(con, study)
    return clean_logs([pick_curves(log_table(con, study, w), w, sv, cm) for w in log_wells(con, study)])


def res_spec(con, study) -> dict | None:
    """The res modeler's spec from the study data: contacts, and the SCAL, rock and region choices the engineer put in
    the geo spec (geo.json 'scal', 'rock', 'regions'). None when no contacts are loaded."""
    sid = study_id(con, study)
    c = con.execute("SELECT datum_depth, datum_pressure, owc_depth, goc_depth FROM data_contacts WHERE study_id = ?",
                    (sid,)).fetchone()
    if not c:
        return None
    g = con.execute("SELECT spec_json FROM data_geo_spec WHERE study_id = ?", (sid,)).fetchone()
    g = json.loads(g[0]) if g else {}
    return {"contacts": {k: c[k] for k in c.keys() if c[k] is not None},
            **{k: g[k] for k in ("scal", "rock", "regions") if k in g}}


def geo_spec(con, study) -> dict | None:
    """The grid spec as the geo modeler takes it (with the contacts, used only for its volume summary)."""
    sid = study_id(con, study)
    r = con.execute("SELECT spec_json FROM data_geo_spec WHERE study_id = ?", (sid,)).fetchone()
    if not r:
        return None
    spec = json.loads(r[0])
    c = con.execute("SELECT datum_depth, datum_pressure, owc_depth, goc_depth FROM data_contacts WHERE study_id = ?",
                    (sid,)).fetchone()
    if c:
        spec["contacts"] = {k: c[k] for k in c.keys() if c[k] is not None}
    return spec


# ---------------------------------------------------------------- well
def wells(con, study) -> pd.DataFrame:
    return _drop_empty(_df(con, "SELECT well_name, well_type AS type, phase, group_name AS \"group\", x, y, "
                                "ref_depth, i, j FROM data_wells WHERE study_id = ? ORDER BY id",
                           (study_id(con, study),)))


def well_inputs(con, study) -> dict:
    """The well modeler's inputs (read_inputs) as DataFrames; datasets that weren't loaded are left out."""
    sid = study_id(con, study)
    out = {"wells": wells(con, sid), "surveys": surveys(con, sid),
           "perforations": _drop_empty(_df(con, "SELECT well_name, date, top_md, bottom_md, state, skin, diameter "
                                                "FROM data_perforations WHERE study_id = ? ORDER BY id", (sid,))),
           "controls": _drop_empty(_df(con, "SELECT well_name, date, control, rate, bhp FROM data_controls "
                                            "WHERE study_id = ? ORDER BY id", (sid,))),
           "history": _drop_empty(_df(con, "SELECT well_name, date, oil, water, gas, water_inj, gas_inj, bhp, thp, "
                                           "hours FROM data_production WHERE study_id = ? ORDER BY well_name, date",
                                      (sid,)))}
    out = {k: v for k, v in out.items() if not v.empty}
    out["history_format"] = "standard"
    return out


# ---------------------------------------------------------------- analysis
def inventory(con, study) -> dict:
    """What is loaded for each dataset (rows, wells, sources), the open issues and which modelers are ready."""
    sid = study_id(con, study)
    one = lambda q: con.execute(q, (sid,)).fetchone()[0]  # noqa: E731
    wl = lambda t: [r[0] for r in con.execute(f"SELECT DISTINCT well_name FROM {t} WHERE study_id = ? "  # noqa: E731
                                              f"ORDER BY well_name", (sid,))]
    ds = {
        "pvt": {"rows": one("SELECT COUNT(*) FROM data_pvt_samples WHERE study_id = ?"),
                "samples": [dict(r) for r in con.execute(
                    "SELECT id, sample_name, temperature_c, psat_bar, selected FROM data_pvt_samples "
                    "WHERE study_id = ?", (sid,))]},
        "tops": {"rows": one("SELECT COUNT(*) FROM data_tops WHERE study_id = ?"), "wells": wl("data_tops"),
                 "surfaces": [r[0] for r in con.execute("SELECT surface FROM data_tops WHERE study_id = ? "
                                                        "GROUP BY surface ORDER BY AVG(tvd)", (sid,))]},
        "logs": {"rows": one("SELECT COUNT(*) FROM data_files WHERE study_id = ? AND format = 'las'"),
                 "unit": "files", "wells": log_wells(con, sid),
                 "curves": sorted({r[0] for r in con.execute("SELECT mnemonic FROM data_log_curves WHERE study_id = ?",
                                                             (sid,))})},
        "geo_spec": {"rows": one("SELECT COUNT(*) FROM data_geo_spec WHERE study_id = ?")},
        "contacts": {"rows": one("SELECT COUNT(*) FROM data_contacts WHERE study_id = ?")},
    }
    for k in ("wells", "surveys", "perforations", "controls", "history"):
        ds[k] = {"rows": one(f"SELECT COUNT(*) FROM {TABLES[k]} WHERE study_id = ?"), "wells": wl(TABLES[k])}
    ds["files"] = {"rows": one("SELECT COUNT(*) FROM data_files WHERE study_id = ?"), "unit": "files",
                   "categories": {r[0]: r[1] for r in con.execute(
                       "SELECT category, COUNT(*) FROM data_files WHERE study_id = ? GROUP BY category", (sid,))},
                   "not_ok": one("SELECT COUNT(*) FROM data_files WHERE study_id = ? AND status != 'ok'")}
    dk = con.execute("SELECT source_path, coverage_json, grid_json FROM data_deck WHERE study_id = ?", (sid,)).fetchone()
    ds["deck"] = {"rows": one("SELECT COUNT(*) FROM data_deck_rows WHERE study_id = ?")}
    if dk:
        cov = json.loads(dk["coverage_json"] or "{}")
        ds["deck"].update(source=dk["source_path"], grid=json.loads(dk["grid_json"] or "{}"),
                          schedule_mode=cov.get("schedule_mode"), coverage=cov.get("counts"),
                          tables={r[0] + "." + r[1]: r[2] for r in con.execute(
                              "SELECT kind, tbl, COUNT(*) FROM data_deck_rows WHERE study_id = ? GROUP BY kind, tbl",
                              (sid,))})
    if ds["history"]["rows"]:
        ds["history"]["dates"] = list(con.execute("SELECT MIN(date), MAX(date) FROM data_production "
                                                  "WHERE study_id = ?", (sid,)).fetchone())
    for d in ds.values():
        d["sources"] = []
    for r in con.execute("SELECT dataset, label, rows FROM data_sources WHERE study_id = ? ORDER BY id", (sid,)):
        if r["dataset"] in ds:
            ds[r["dataset"]]["sources"].append(f"{r['label']} ({r['rows']} rows)" if r["rows"] is not None
                                               else r["label"])
    iss = issues(con, sid)
    rd = {m: ready(con, sid, m) for m in ("fluid", "geo", "res", "well")}
    todo = [f"{i['modeler']}: {i['message']}" for i in iss if i["severity"] in ("required", "decision")]
    say = ("Nothing is loaded yet." if not one("SELECT COUNT(*) FROM data_sources WHERE study_id = ?") else
           "All four modelers have what they need." if all(rd.values()) else
           f"{len(todo)} item(s) still needed. First: {todo[0]}")
    return {"datasets": ds, "issues": iss, "ready": rd, "all_ready": all(rd.values()), "spoken_summary": say}


def rows(con, study, dataset: str, well: str | None = None, limit: int = 500) -> list[dict]:
    """Rows of one dataset (wells, tops, surveys, perforations, controls, history, sources, issues, log_curves,
    pvt_samples), optionally for one well."""
    if dataset not in TABLES:
        raise ToolError(INVALID_INPUT, f"dataset must be one of {sorted(TABLES)} (logs: log_stats / log_samples; "
                                       f"pvt: fluid data via pvt_samples and fluid_input)")
    sid = study_id(con, study)
    q, args = f"SELECT * FROM {TABLES[dataset]} WHERE study_id = ?", [sid]
    if well:
        q, args = q + " AND well_name = ?", args + [well]
    return [dict(r) for r in con.execute(q + " ORDER BY id LIMIT ?", (*args, int(limit)))]


def log_stats(con, study, well: str | None = None) -> list[dict]:
    """Per well and curve, from the catalog: unit, samples, depth range and min / mean / max, and the file."""
    sid = study_id(con, study)
    q = ("SELECT c.well_name, c.mnemonic, c.unit, c.description, c.samples, c.top, c.base, c.min, c.mean, c.max, "
         "f.original AS file FROM data_log_curves c JOIN data_files f ON f.id = c.data_file_id WHERE c.study_id = ?")
    args: list = [sid]
    if well:
        q, args = q + " AND c.well_name = ?", args + [well]
    return [dict(r) for r in con.execute(q + " ORDER BY c.well_name, c.mnemonic, f.id", args)]


def log_samples(con, study, well: str, curves: list[str] | None = None, top: float | None = None,
                base: float | None = None, step: int = 1) -> list[dict]:
    """One well's log samples (depth plus a value per curve) read from its LAS file(s), optionally a depth window
    and every n-th sample."""
    df = log_table(con, study, well)
    if curves:
        df = df[["depth", *[c.upper() for c in curves if c.upper() in df.columns]]]
    if top is not None:
        df = df[df["depth"] >= top]
    if base is not None:
        df = df[df["depth"] <= base]
    df = df.iloc[::max(1, int(step))]
    return json.loads(df.to_json(orient="records"))
