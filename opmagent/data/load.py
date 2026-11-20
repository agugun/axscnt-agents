"""Data manager: file everything in a study's inbox, convert the tabular data into the study data tables, catalog
the rest, check the two together, and say what is missing.

    <study>/inbox/**   anything the engineer drops: lab report workbooks, LAS logs, tops and well tables (CSV or
                       Excel, any sheet), Volve production data, a geo.json grid spec, SEG-Y, surfaces, PDFs ...

           │  opmagent data load <study>
           ▼
    <study>/data/<category>/...   every recognised file as it came (logs/, seismic/, surfaces/, grids/,
                                  documents/, images/, tables/, pvt/, specs/): the originals other software opens
    data_files                    their catalog: format, path, checksum, well, metadata
    data_log_curves               each LAS curve: unit, depth range, samples, min / mean / max (samples stay in the file)
    data_sources                       every file (and sheet) the tables came from, with its row count
    data_pvt_samples / _composition / _tests      the lab report(s): fluid modeler
    data_tops                                     tops picks: geo modeler
    data_contacts, data_geo_spec                  contacts and the grid spec (engineer's choices): geo modeler
    data_wells, data_surveys, data_perforations,  well modeler (and geo, for the surveys)
    data_controls, data_production
    data_issues                        what is still missing or doubtful, per modeler

Files are recognised by their content, not their names. Column names are matched case-insensitively with common
variants (WELL / WELLNAME, EASTING / X, TVDSS / TVD, MD_TOP / TOP_MD ...). A load replaces the study's rows, so
it can be re-run whenever the inbox changes. The loaded data is then read back the way each modeler reads it
(opmagent.data.store), so 'ready' means the modeler will accept it. Nothing is invented: a missing item is
recorded as an issue for the data manager to chase, never filled in. Checks that need both the tables and the
files (opmagent.data.checks) run last.
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import pandas as pd

from ..core import files as filesmod
from ..core import studies
from . import formats

# canonical column -> accepted variants (lower case, spaces/dashes as underscores)
SYN = {
    "well_name": ["well_name", "well", "wellname", "wellbore", "well_bore", "uwi", "borehole"],
    "surface": ["surface", "horizon", "marker", "top_name", "formation", "pick"],
    "x": ["x", "easting", "utm_x", "x_coord", "xcoord", "east"],
    "y": ["y", "northing", "utm_y", "y_coord", "ycoord", "north"],
    "tvd": ["tvd", "tvdss", "tvd_m", "z", "depth_tvd"],
    "md": ["md", "measured_depth", "md_m", "depth_md", "depth"],
    "inc": ["inc", "incl", "inclination", "dev"],
    "azi": ["azi", "azim", "azimuth"],
    "type": ["type", "well_type", "purpose"],
    "phase": ["phase", "preferred_phase", "fluid"],
    "ref_depth": ["ref_depth", "datum", "datum_depth", "bhp_depth"],
    "date": ["date", "event_date", "start_date", "dateprd"],
    "top_md": ["top_md", "md_top", "perf_top", "top_perf", "from_md", "md_from"],
    "bottom_md": ["bottom_md", "md_bottom", "base_md", "perf_bottom", "bottom_perf", "to_md", "md_to"],
    "control": ["control", "mode", "control_mode"],
    "rate": ["rate", "target_rate", "target"],
    "bhp": ["bhp", "bhp_limit", "min_bhp", "max_bhp"],
    "oil": ["oil", "oil_vol", "oil_rate", "qo"], "water": ["water", "water_vol", "water_rate", "qw"],
    "gas": ["gas", "gas_vol", "gas_rate", "qg"], "water_inj": ["water_inj", "wi", "winj", "water_injected"],
}
ROLES = [   # checked in this order: the first role whose columns a table has wins
    ("tops", {"well_name", "surface", "x", "y", "tvd"}),
    ("perforations", {"well_name", "date", "top_md", "bottom_md"}),
    ("controls", {"well_name", "date", "control"}),
    ("surveys", {"well_name", "md"}),
    ("wells", {"well_name", "type", "phase"}),
    ("history", {"well_name", "date"}),
]
GEO_SPEC_KEYS = ("surfaces", "layers", "areal", "contacts")
CONTACT_KEYS = ("datum_depth", "datum_pressure", "owc_depth", "goc_depth")
POROSITY = {"PHIE", "PHIT", "PORO", "PHI", "NPHI_CORR"}
PERMEABILITY = {"PERM", "KLOGH", "KH", "PERMX"}
SHALE = {"VSH", "VCL", "VSHALE"}
# dataset -> (table, file kind, columns written)
DATASETS = {
    "tops": ("data_tops", "tops", ["well_name", "surface", "x", "y", "tvd"]),
    "wells": ("data_wells", "other_input",
              ["well_name", "well_type", "phase", "group_name", "x", "y", "ref_depth", "i", "j"]),
    "surveys": ("data_surveys", "deviation_survey", ["well_name", "md", "x", "y", "tvd", "inc", "azi", "x0", "y0"]),
    "perforations": ("data_perforations", "other_input",
                     ["well_name", "date", "top_md", "bottom_md", "state", "skin", "diameter"]),
    "controls": ("data_controls", "other_input", ["well_name", "date", "control", "rate", "bhp"]),
    "history": ("data_production", "production_data",
                ["well_name", "date", "oil", "water", "gas", "water_inj", "gas_inj", "bhp", "thp", "hours"]),
}
STUDY_TABLES = ["data_issues", "data_geo_spec", "data_contacts", "data_pvt_samples", "data_production",
                "data_controls", "data_perforations", "data_log_curves", "data_surveys", "data_tops", "data_wells",
                "data_sources", "data_files"]


def _norm(c) -> str:
    return str(c).strip().lower().replace(" ", "_").replace("-", "_")


def canonical(df: pd.DataFrame) -> pd.DataFrame:
    """Rename variant column names to the modelers' names; unknown columns are kept as they are."""
    rename, taken = {}, set()
    for col in df.columns:
        n = _norm(col)
        for canon, variants in SYN.items():
            if n in variants and canon not in taken:
                rename[col] = canon
                taken.add(canon)
                break
        else:
            rename[col] = n
    return df.rename(columns=rename)


def _role(df: pd.DataFrame) -> str | None:
    cols = set(df.columns)
    if {"dateprd", "npd_well_bore_name"} <= {_norm(c) for c in df.columns}:
        return "volve"
    for role, need in ROLES:
        if need <= cols:
            if role == "surveys" and not ({"x", "y", "tvd"} <= cols or {"inc", "azi"} <= cols):
                continue
            if role == "history" and not cols & {"oil", "water", "gas", "water_inj"}:
                continue
            return role
    return None


def _tables(path: Path) -> list[tuple[str, pd.DataFrame]]:
    """(label, raw table) for a CSV or every sheet of a workbook."""
    if path.suffix.lower() == ".csv":
        return [(path.name, pd.read_csv(path))]
    sheets = pd.read_excel(path, sheet_name=None)
    return [(f"{path.name}[{s}]", df) for s, df in sheets.items() if not df.empty]


def _lab_report(path: Path):
    """(fluid_input, gaps) if the workbook is a lab report in the template format, else None."""
    from ..fluid.excel import read_lab_report
    try:
        fi, gaps = read_lab_report(path)
    except Exception:  # noqa: BLE001  (not a workbook the fluid reader understands)
        return None
    if any("missing sheets" in g["message"] for g in gaps):
        return None
    return fi, gaps


def survey_inbox(inbox: Path) -> dict:
    """Classify every file in the inbox (recursively). Nothing is written."""
    found = {"lab_reports": [], "las": [], "tables": {}, "volve": [], "geo_spec": [], "decks": [], "files": [],
             "other": []}
    for p in sorted(x for x in Path(inbox).rglob("*") if x.is_file()):
        suf = p.suffix.lower()
        fmt = formats.format_of(p)
        if suf == ".las":
            found["las"].append(p)
        elif fmt:
            found["files"].append((p, fmt))
        elif suf == ".json":
            try:
                spec = json.loads(p.read_text())
            except ValueError:
                spec = None
            (found["geo_spec"] if isinstance(spec, dict) and "surfaces" in spec else found["other"]).append(p)
        elif suf == ".data":
            found["decks"].append(p)
        elif suf in (".xlsx", ".xlsm", ".xls", ".csv"):
            if suf != ".csv":
                lab = _lab_report(p)
                if lab is not None:
                    found["lab_reports"].append((p, *lab))
                    continue
            try:
                tables = _tables(p)
            except Exception as e:  # noqa: BLE001
                found["other"].append(p)
                found.setdefault("unreadable", []).append(f"{p.name}: {e}")
                continue
            hit = False
            for label, raw in tables:
                if _role(raw) == "volve":
                    found["volve"].append(p)
                    hit = True
                    break
                role = _role(canonical(raw))
                if role:
                    found["tables"].setdefault(role, []).append((p, label, canonical(raw)))
                    hit = True
            if not hit:
                found["other"].append(p)
        else:
            found["other"].append(p)
    return found


def _val(v):
    """A table cell as SQLite takes it: NaN -> NULL, timestamps -> ISO date, numpy -> Python."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    if hasattr(v, "item"):
        v = v.item()
        if isinstance(v, float) and math.isnan(v):
            return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def _dates(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s).dt.date.astype(str)


class _Loader:
    def __init__(self, con, study: dict, inbox: Path, write: bool):
        self.con, self.sid, self.inbox, self.write = con, study["id"], inbox, write
        self.store = Path(study["folder"]) / "data"
        self.issues: list[dict] = []
        self._filed: dict[Path, dict] = {}

    def filed(self, p: Path, category: str, kind: str, fmt: str, well: str | None = None,
              meta: dict | None = None) -> dict:
        """File the original under <study>/data/<category>/ (as it came) and catalog it; once per file.
        In a check only (write=False) nothing is copied: the inbox file is catalogued, and rolled back."""
        p = Path(p)
        if p in self._filed:
            return self._filed[p]
        dest = self.store / category / self.rel(p)
        if self.write:
            if not dest.exists() or filesmod.sha256_of(dest) != filesmod.sha256_of(p):
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
        else:
            dest = p
        fid = filesmod.register_file(self.con, dest, kind)
        st = dest.stat()
        sha = self.con.execute("SELECT sha256 FROM files WHERE id = ?", (fid,)).fetchone()[0]
        did = self.con.execute(
            "INSERT INTO data_files(study_id, file_id, format, category, path, original, well_name, sha256, "
            "size_bytes, mtime, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.sid, fid, fmt, category, str(dest), self.rel(p), well, sha, st.st_size, st.st_mtime,
             json.dumps(meta, default=str) if meta else None)).lastrowid
        self._filed[p] = {"data_file_id": did, "file_id": fid, "path": dest}
        return self._filed[p]

    def issue(self, modeler, severity, item, message):
        self.issues.append({"modeler": modeler, "severity": severity, "item": item, "message": message})

    def source(self, dataset: str, path: Path, label: str, kind: str, rows: int | None = None) -> int:
        """A dataset converted into tables, from a filed original (one per file and sheet)."""
        category, fmt = {"pvt": ("pvt", "lab_report"), "geo_spec": ("specs", "json")}.get(dataset, ("tables", "table"))
        f = self.filed(path, category, kind, fmt)
        return self.con.execute("INSERT INTO data_sources(study_id, file_id, path, label, dataset, rows) "
                                "VALUES (?,?,?,?,?,?)",
                                (self.sid, f["file_id"], str(f["path"]), label, dataset, rows)).lastrowid

    def rel(self, p: Path) -> str:
        try:
            return str(Path(p).relative_to(self.inbox))
        except ValueError:
            return str(p)

    def insert(self, table: str, cols: list[str], df: pd.DataFrame, source_id: int, replace: bool = False) -> int:
        df = df[[c for c in cols if c in df.columns]]
        names = ["study_id", "source_id", *df.columns]
        sql = f"INSERT {'OR REPLACE ' if replace else ''}INTO {table}({', '.join(names)}) VALUES ({', '.join('?' * len(names))})"
        rows = [(self.sid, source_id, *(_val(v) for v in r)) for r in df.itertuples(index=False, name=None)]
        self.con.executemany(sql, rows)
        return len(rows)


def _well_table(role: str, df: pd.DataFrame) -> pd.DataFrame:
    """A canonical table -> the columns of its data_* table."""
    df = df.copy()
    df["well_name"] = df["well_name"].astype(str).str.strip()
    if role == "wells":
        df = df.rename(columns={"type": "well_type", "group": "group_name"})
        for c in ("well_type", "phase"):
            df[c] = df[c].astype(str).str.strip().str.upper()
    if "date" in df.columns:
        df["date"] = _dates(df["date"])
    if role == "controls":
        df["control"] = df["control"].astype(str).str.strip().str.upper()
    return df


def load(con, name: str, *, write: bool = True, geo_spec: dict | None = None) -> dict:
    """Convert the study's inbox into its data tables and check them with the modelers' readers.
    write=False only reports (opmagent data check): everything is rolled back. Returns the summary."""
    st = studies._study(con, name)
    inbox = Path(st["folder"]) / "inbox"
    geo_spec = geo_spec if geo_spec is not None else st["spec"].get("geo")
    f = survey_inbox(inbox)
    L = _Loader(con, st, inbox, write)
    con.execute("BEGIN IMMEDIATE")
    try:
        for t in STUDY_TABLES:
            con.execute(f"DELETE FROM {t} WHERE study_id = ?", (st["id"],))
        _load_fluid(L, f)
        _load_geo(L, f, geo_spec)
        _load_well(L, f)
        _load_files(L, f)
        from .checks import run_checks
        for i in run_checks(con, st["id"]):
            L.issue(*i)
        for u in f.get("unreadable", []):
            L.issue("data", "warning", "unreadable", u)
        con.executemany("INSERT INTO data_issues(study_id, modeler, severity, item, message) VALUES (?,?,?,?,?)",
                        [(st["id"], i["modeler"], i["severity"], i["item"], i["message"]) for i in L.issues])
        from . import store
        summary = store.inventory(con, name)
        summary.update({"unrecognised": [L.rel(p) for p in f["other"]], "decks": [L.rel(p) for p in f["decks"]]})
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT" if write else "ROLLBACK")
    summary["loaded"] = write
    if write:
        studies._event(con, st["id"], "data", "loaded",
                       {k: v["rows"] for k, v in summary["datasets"].items() if v["rows"]}, "data")
    return summary


# ---------------------------------------------------------------- fluid
def _load_fluid(L: _Loader, f: dict) -> None:
    from ..fluid.excel import COMPONENT_NAMES
    lab = {v: k for k, v in COMPONENT_NAMES.items()}   # NeqSim name -> lab name
    if not f["lab_reports"]:
        L.issue("fluid", "required", "lab report",
                "no PVT lab report workbook in the template format (opmagent fluid template lab.xlsx); "
                "a PDF report must be typed into the template")
        return
    if len(f["lab_reports"]) > 1:
        L.issue("fluid", "decision", "lab report",
                f"{len(f['lab_reports'])} lab reports: {', '.join(p.name for p, *_ in f['lab_reports'])}; "
                f"using {f['lab_reports'][0][0].name} - say which one if not")
    for n, (p, fi, gaps) in enumerate(f["lab_reports"]):
        src = L.source("pvt", p, L.rel(p), "lab_report")
        m = fi.get("measured") or {}
        sid = L.con.execute(
            "INSERT INTO data_pvt_samples(study_id, source_id, sample_name, temperature_c, psat_bar, unit_system, "
            "salinity_ppm, p_min_bar, p_max_bar, gor_sm3_sm3, bo_fb, api, selected) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (L.sid, src, fi.get("sample_name"), fi.get("temperature_c"), fi.get("psat_lab"), fi.get("unit_system"),
             fi.get("salinity_ppm"), fi.get("p_min"), fi.get("p_max"), m.get("measured_gor_sm3_sm3"),
             m.get("measured_bo_fb"), m.get("measured_api"), int(n == 0))).lastrowid
        comp = [(sid, lab.get(c, c), mol, None, None) for c, mol in (fi.get("composition") or {}).items()]
        if fi.get("plus_fraction"):
            pf = fi["plus_fraction"]
            comp.append((sid, pf["name"] + "+", pf["mol_percent"], pf.get("mw_g_mol"), pf.get("sg")))
        L.con.executemany("INSERT INTO data_pvt_composition VALUES (?,?,?,?,?)", comp)
        tests = [(sid, "separator", i + 1, s["pressure"], s["temperature"], None, None, None, None, None)
                 for i, s in enumerate(fi.get("separator_stages") or [])]
        tests += [(sid, "cce", None, r["pressure"], None, r["relative_volume"], None, None, None, None)
                  for r in fi.get("cce") or []]
        tests += [(sid, "dl", None, r["pressure"], None, None, r["rs"], r["bo"], r["oil_density"], None)
                  for r in fi.get("dl") or []]
        tests += [(sid, "viscosity", None, r["pressure"], None, None, None, None, None, r["mu_o"])
                  for r in fi.get("viscosity") or []]
        L.con.executemany("INSERT INTO data_pvt_tests(sample_id, test, stage, pressure, temperature, "
                          "relative_volume, rs, bo, oil_density, mu_o) VALUES (?,?,?,?,?,?,?,?,?,?)", tests)
        L.con.execute("UPDATE data_sources SET rows = ? WHERE id = ?", (len(comp) + len(tests), src))
        if n == 0:
            for g in gaps:
                L.issue("fluid", "required" if g["severity"] == "required" else "warning", g["item"], g["message"])


# ---------------------------------------------------------------- geo
def _load_geo(L: _Loader, f: dict, geo_spec: dict | None) -> None:
    tops = f["tables"].get("tops", [])
    surfaces: set[str] = set()
    if not tops:
        L.issue("geo", "required", "tops", "no tops table (columns well_name, surface, x, y, tvd)")
    seen = {}
    for p, label, df in tops:
        df = df[["well_name", "surface", "x", "y", "tvd"]].copy()
        df["well_name"], df["surface"] = df["well_name"].astype(str).str.strip(), df["surface"].astype(str).str.strip()
        for key in zip(df["well_name"], df["surface"]):
            seen[key] = seen.get(key, 0) + 1
        src = L.source("tops", p, label, "tops", len(df))
        L.insert("data_tops", DATASETS["tops"][2], df, src, replace=True)   # a later pick replaces an earlier one
        surfaces |= set(df["surface"])
    dup = sum(n - 1 for n in seen.values() if n > 1)
    if dup:
        L.issue("geo", "warning", "tops", f"{dup} duplicate well/surface picks; the last one is kept")

    if not f["las"]:
        L.issue("geo", "required", "logs", "no LAS well logs (porosity, permeability, shale volume)")
    else:
        for p in f["las"]:
            try:
                meta = formats.las_inspect(p)
            except Exception as e:  # noqa: BLE001  (a broken LAS is reported, not loaded)
                L.issue("geo", "required", p.name, f"can't read this LAS file: {e}")
                continue
            _catalog_las(L, p, meta)
        _duplicate_curves(L)

    spec = geo_spec
    spec_src = None
    if spec is None and f["geo_spec"]:
        p = f["geo_spec"][0]
        spec = json.loads(p.read_text())
        spec_src = L.source("geo_spec", p, L.rel(p), "other_input", 1)
    if not spec:
        L.issue("geo", "required", "grid spec",
                "no geo spec (geo.json: surfaces in order, layers per zone, areal grid x0/y0/dx/dy/nx/ny, contacts "
                "datum_depth/datum_pressure/owc_depth): the engineer gives the grid and contacts")
        return
    miss = [k for k in GEO_SPEC_KEYS if k not in spec]
    if miss:
        L.issue("geo", "required", "grid spec", f"geo spec lacks {', '.join(miss)}")
    if tops and "surfaces" in spec:
        unknown = sorted(set(spec["surfaces"]) - surfaces)
        if unknown:
            L.issue("geo", "required", "grid spec", f"surfaces with no tops picks: {', '.join(unknown)}")
    c = spec.get("contacts") or {}
    if c:
        L.con.execute("INSERT INTO data_contacts(study_id, source_id, datum_depth, datum_pressure, owc_depth, "
                      "goc_depth) VALUES (?,?,?,?,?,?)", (L.sid, spec_src, *(c.get(k) for k in CONTACT_KEYS)))
        if c.get("datum_depth") is None or c.get("datum_pressure") is None:
            L.issue("geo", "required", "contacts", "contacts need datum_depth and datum_pressure")
    L.con.execute("INSERT INTO data_geo_spec(study_id, source_id, spec_json) VALUES (?,?,?)",
                  (L.sid, spec_src, json.dumps({k: v for k, v in spec.items() if k != "contacts"})))


def _catalog_las(L: _Loader, p: Path, meta: dict) -> None:
    """File the LAS as it came; the catalog gets its well, depth index and per-curve statistics."""
    well = meta["well"]
    f = L.filed(p, "logs", "well_log", "las", well,
                {"depth": meta["depth"], "header": meta["header"], "curves": [c["mnemonic"] for c in meta["curves"]]})
    L.con.execute("INSERT INTO data_sources(study_id, file_id, path, label, dataset, rows) VALUES (?,?,?,?,?,?)",
                  (L.sid, f["file_id"], str(f["path"]), L.rel(p), "logs", sum(c["samples"] for c in meta["curves"])))
    L.con.executemany(
        "INSERT OR REPLACE INTO data_log_curves(study_id, data_file_id, well_name, mnemonic, unit, description, "
        "samples, top, base, min, mean, max) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(L.sid, f["data_file_id"], well, c["mnemonic"], c["unit"], c["description"], c["samples"], c["top"],
          c["base"], c["min"], c["mean"], c["max"]) for c in meta["curves"]])
    mnems = {c["mnemonic"] for c in meta["curves"] if c["samples"]}
    if not mnems & POROSITY:
        L.issue("geo", "required", p.name, f"{well}: no porosity curve (PHIE, PHIT or PORO)")
    if not mnems & PERMEABILITY:
        L.issue("geo", "warning", p.name, f"{well}: no permeability curve; perm from a transform")
    if not mnems & SHALE:
        L.issue("geo", "warning", p.name, f"{well}: no shale volume curve; no net-to-gross cut-off")


def _duplicate_curves(L: _Loader) -> None:
    for r in L.con.execute("SELECT well_name, mnemonic, COUNT(*) AS n FROM data_log_curves WHERE study_id = ? "
                           "GROUP BY well_name, mnemonic HAVING n > 1", (L.sid,)):
        L.issue("geo", "warning", "logs", f"{r['well_name']}: curve {r['mnemonic']} is in {r['n']} files; "
                                          f"where they overlap the later file is used")


def _load_files(L: _Loader, f: dict) -> None:
    """Other file-native data (SEG-Y, surfaces, grids, documents, images): filed and catalogued, not converted."""
    for p, fmt in f["files"]:
        try:
            meta = fmt.inspect(p) if fmt.inspect else {}
        except Exception:  # noqa: BLE001
            meta = {}
        L.filed(p, fmt.category, fmt.file_kind, fmt.name, None, meta or None)


# ---------------------------------------------------------------- well
def _load_well(L: _Loader, f: dict) -> None:
    from ..well.ingest import volve_history
    names: dict[str, set] = {}
    for role in ("wells", "surveys", "perforations", "controls", "history"):
        table, kind, cols = DATASETS[role]
        for p, label, df in f["tables"].get(role, []):
            df = _well_table(role, df)
            src = L.source(role, p, label, kind, len(df))
            if role == "wells":
                dup = df[df["well_name"].isin(names.get("wells", set())) | df["well_name"].duplicated()]
                if len(dup):
                    L.issue("well", "warning", "wells", f"wells listed twice (the last is kept): "
                                                        f"{', '.join(sorted(set(dup['well_name']))[:8])}")
                    L.con.executemany("DELETE FROM data_wells WHERE study_id = ? AND well_name = ?",
                                      [(L.sid, w) for w in set(dup["well_name"])])
                    df = df.drop_duplicates("well_name", keep="last")
            L.insert(table, cols, df, src)
            names.setdefault(role, set()).update(df["well_name"])
    if f["volve"] and not f["tables"].get("history"):
        for p in f["volve"]:
            df = volve_history(p)
            df["date"] = _dates(df["date"])
            df["well_name"] = df["well_name"].astype(str).str.strip()
            src = L.source("history", p, f"{L.rel(p)} (Volve daily production)", "production_data", len(df))
            L.insert("data_production", DATASETS["history"][2], df, src)
            names.setdefault("history", set()).update(df["well_name"])
    if not names.get("wells"):
        L.issue("well", "required", "wells", "no wells table (columns well_name, type, phase, x, y)")
        return
    for role in ("surveys", "perforations", "controls"):
        extra = sorted(names.get(role, set()) - names["wells"])
        if extra:
            L.issue("well", "required", role, f"wells not in the wells table: {', '.join(extra[:8])}")
    from . import store
    from ..well.ingest import read_inputs
    try:   # the well modeler's own reader decides what is still missing
        r = read_inputs(store.well_inputs(L.con, L.sid))
        for g in r.get("gaps", []):
            L.issue("well", g["severity"] if g["severity"] in ("required", "warning") else "warning",
                    g["item"], g["message"])
    except (ValueError, KeyError) as e:
        L.issue("well", "required", "well tables", str(e))
