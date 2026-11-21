"""Helpers for the well QC notebooks (the standard template well_qc.ipynb and investigation notebooks).

Read only, like the fluid and geo QC: a notebook loads the payload the well pipeline wrote (gaps, the
measured history as it came in, history.csv next to it) and the model as stored in the database
(read-only): wells, trajectories, completions, events. When the version names a geo version, it also
loads that grid (layer depths, PORO/PERMX from their registered files, contacts, zone surfaces) and places
each perforation in it the way the deck builder does, so it checks what Flow will see.
Every check returns a finding {area, check, status, detail}; display helpers and the summary are shared.
"""
from __future__ import annotations

import html
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ..core.files import sha256_of
from ..validators._util import read_array
from .qc_lib import LAYOUT, finding, overall, show_findings, show_list, write_summary  # noqa: F401  (re-exported)

AREAS = ("wells", "trajectories", "completions", "history", "controls")
RATES = ("oil_rate", "water_rate", "gas_rate", "inj_rate")
MEASURED = {"oil_rate": "oil", "water_rate": "water", "gas_rate": "gas"}     # model rate -> measured volume column
HEAD_WARN = 50.0            # m between the wells table head and the first survey station
DLS_WARN = 10.0             # dogleg severity, degrees per 30 m
CUM_WARN, CUM_FAIL = 0.02, 0.10   # model vs measured cumulative volume (relative)
SPIKE = 5.0                 # a month above SPIKE x the well's median rate
LOW_PERM = 1.0              # mD: a perforated cell this tight barely flows
PRED_JUMP = 2.0             # prediction target above PRED_JUMP x the last history rate
STEP = 0.5                  # m MD between samples when placing perforations (as the deck builder)


# ---------------------------------------------------------------- loading
def _ro(db_path):
    con = sqlite3.connect(f"file:{Path(db_path).expanduser()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _rows(con, sql, *a):
    return [dict(r) for r in con.execute(sql, a)]


def load(payload_path: str | Path, db_path: str | Path | None = None, version_id: int | None = None) -> dict:
    payload_path = Path(payload_path)
    data = json.loads(payload_path.read_text())
    db_path = db_path or data.get("db_path")
    vid = int(version_id or data.get("version_id"))
    con = _ro(db_path)
    try:
        h = _rows(con, "SELECT * FROM well_model_versions WHERE id = ?", vid)[0]
        t = {k: _rows(con, f"SELECT * FROM {k} WHERE version_id = ? ORDER BY {o}", vid)
             for k, o in (("wells", "well_name"), ("well_groups", "group_name"), ("well_trajectory", "well_name, md"),
                          ("well_completions", "well_name, event_date, id"), ("well_events", "well_name, event_date, mode"))}
        data["geo"] = load_geo(con, h["geo_version_id"]) if h["geo_version_id"] else None
    finally:
        con.close()
    data.update(header=h, tables=t, tables_from_db=True,
                wells=pd.DataFrame(t["wells"]), trajectory=pd.DataFrame(t["well_trajectory"]),
                completions=pd.DataFrame(t["well_completions"]), events=pd.DataFrame(t["well_events"]))
    hp = payload_path.parent / (data.get("history_file") or "history.csv")
    data["measured"] = pd.read_csv(hp, parse_dates=["date"]) if hp.is_file() else pd.DataFrame()
    return data


def load_geo(con, gid: int) -> dict | None:
    """The geo version the wells are placed in: header, areal layout, layer depths, PORO/PERMX, contacts."""
    r = _rows(con, "SELECT g.*, m.name AS model_name FROM geo_model_versions g JOIN geo_models m ON m.id = g.geo_model_id "
                   "WHERE g.id = ?", gid)
    if not r:
        return None
    h = r[0]
    spec = json.loads(h["spec_json"] or "{}")
    nx, ny, nz = h["nx"], h["ny"], h["nz"]
    geo = {"version_id": gid, "model_name": h["model_name"], "version": h["version"], "nx": nx, "ny": ny, "nz": nz,
           "areal": spec.get("areal"), "layer_depths": None, "arrays": {}, "changed": [],
           "contacts": [], "res": None, "surfaces": None}
    # contacts belong to the res model on this grid: the approved one if there is one, else the latest
    res = _rows(con, "SELECT r.id, r.version, r.status, m.name AS model_name FROM res_model_versions r "
                     "JOIN res_models m ON m.id = r.res_model_id WHERE r.geo_version_id = ? "
                     "ORDER BY (r.status = 'approved') DESC, r.id DESC LIMIT 1", gid)
    if res:
        geo["res"] = res[0]
        geo["contacts"] = _rows(con, "SELECT * FROM contacts WHERE version_id = ? ORDER BY eqlnum", res[0]["id"])
    if h["grid_type"] == "cartesian" and h["dx"] and h["dy"] and h["dz"] and h["tops"] is not None:
        geo["areal"] = geo["areal"] or {"x0": 0.0, "y0": 0.0, "dx": h["dx"], "dy": h["dy"]}
        z = h["tops"] + h["dz"] * np.arange(nz + 1)
        geo["layer_depths"] = np.broadcast_to(z[:, None, None], (nz + 1, ny, nx)).copy()
    elif spec.get("layer_depths_file") and Path(spec["layer_depths_file"]).is_file():
        geo["layer_depths"] = np.load(spec["layer_depths_file"])
    gs = spec.get("geo_spec") or {}
    if gs.get("surfaces") and gs.get("layers"):
        geo["surfaces"] = dict(zip(gs["surfaces"], np.concatenate([[0], np.cumsum(gs["layers"])]).astype(int).tolist()))
    for p in _rows(con, "SELECT g.keyword, g.const_value, f.path, f.sha256 FROM geo_properties g LEFT JOIN files f "
                        "ON f.id = g.file_id WHERE g.version_id = ? AND g.keyword IN ('PORO', 'PERMX', 'NTG')", gid):
        if p["path"] is None:
            geo["arrays"][p["keyword"]] = np.full((nz, ny, nx), float(p["const_value"]))
        elif Path(p["path"]).is_file() and sha256_of(p["path"]) == p["sha256"]:
            v = np.asarray(read_array(p["path"]), dtype=float)
            if v.size == nx * ny * nz:
                geo["arrays"][p["keyword"]] = v.reshape((nz, ny, nx))
        else:
            geo["changed"].append(p["keyword"])
    return geo


def header(data: dict):
    from IPython.display import HTML
    h, g = data["header"], data["geo"]
    w = data["wells"]
    n = {t: int((w["well_type"] == t).sum()) for t in ("PRODUCER", "INJECTOR")} if len(w) else {}
    geo = (f"placed in geo {html.escape(g['model_name'])} v{g['version']} ({g['nx']} x {g['ny']} x {g['nz']})" if g
           else "no geo version: grid checks are skipped")
    return HTML(f"<h1>Well model QC: {html.escape(data['model_name'])} version {data['version']}</h1>"
                f"<p>Built {data.get('created_at', '')} &middot; {n.get('PRODUCER', 0)} producers, {n.get('INJECTOR', 0)} "
                f"injectors &middot; start {h['start_date']} &middot; {geo}</p>")


# ---------------------------------------------------------------- geometry
def _interp(pts: pd.DataFrame, md: np.ndarray):
    return (np.interp(md, pts["md"], pts["x"]), np.interp(md, pts["md"], pts["y"]), np.interp(md, pts["md"], pts["tvd"]))


def place(data: dict) -> pd.DataFrame:
    """Every MD perforation sampled every STEP m and placed in the geo grid (i, j, k 1-based; k = 0 outside
    the grid's layers, i = 0 outside its areal extent): one row per completion and cell, with the length."""
    g, tr, comp = data["geo"], data["trajectory"], data["completions"]
    cols = ["completion_id", "well_name", "event_date", "state", "i", "j", "k", "length_m", "tvd_top", "tvd_base"]
    if g is None or g["layer_depths"] is None or not g["areal"] or comp.empty or tr.empty:
        return pd.DataFrame(columns=cols)
    a, ld = g["areal"], g["layer_depths"]
    rows = []
    for c in comp.itertuples():
        if pd.isna(c.top_md):
            rows.append({"completion_id": c.id, "well_name": c.well_name, "event_date": c.event_date, "state": c.state,
                         "i": int(c.i), "j": int(c.j), "k": int(c.k1), "length_m": np.nan, "tvd_top": np.nan, "tvd_base": np.nan})
            continue
        pts = tr[tr["well_name"] == c.well_name]
        if pts.empty:
            continue
        n = max(2, int((c.bottom_md - c.top_md) / STEP) + 1)
        md = np.linspace(c.top_md, c.bottom_md, n)
        x, y, z = _interp(pts, md)
        i = ((x - a["x0"]) // a["dx"]).astype(int) + 1
        j = ((y - a["y0"]) // a["dy"]).astype(int) + 1
        inside = (i >= 1) & (i <= g["nx"]) & (j >= 1) & (j <= g["ny"])
        k = np.zeros(n, dtype=int)
        for s in np.flatnonzero(inside):
            col = ld[:, j[s] - 1, i[s] - 1]
            kk = int(np.searchsorted(col, z[s], side="right"))      # layer k spans col[k-1] <= z < col[k]
            k[s] = kk if 1 <= kk <= g["nz"] else 0
        i[~inside], j[~inside] = 0, 0
        seg = (c.bottom_md - c.top_md) / (n - 1) if n > 1 else 0.0
        key = defaultdict(list)
        for s in range(n):
            key[(i[s], j[s], k[s])].append(s)
        for (ii, jj, kk), ss in key.items():
            rows.append({"completion_id": c.id, "well_name": c.well_name, "event_date": c.event_date, "state": c.state,
                         "i": int(ii), "j": int(jj), "k": int(kk), "length_m": seg * len(ss) if n > 1 else 0.0,
                         "tvd_top": float(z[ss].min()), "tvd_base": float(z[ss].max())})
    df = pd.DataFrame(rows, columns=cols)
    arr = g["arrays"]
    for kw in ("PORO", "PERMX", "NTG"):
        if kw in arr:
            df[kw] = [arr[kw][r.k - 1, r.j - 1, r.i - 1] if r.k > 0 and r.i > 0 else np.nan for r in df.itertuples()]
    return df


def dogleg(pts: pd.DataFrame) -> np.ndarray:
    """Dogleg severity (deg / 30 m) between consecutive survey segments, from x, y, tvd."""
    v = np.diff(pts[["x", "y", "tvd"]].to_numpy(float), axis=0)
    dmd = np.diff(pts["md"].to_numpy(float))
    n = np.linalg.norm(v, axis=1)
    ok = n > 1e-9
    u = v[ok] / n[ok, None]
    if len(u) < 2:
        return np.array([])
    ang = np.degrees(np.arccos(np.clip((u[1:] * u[:-1]).sum(1), -1, 1)))
    return ang / np.maximum(dmd[ok][1:], 1e-9) * 30.0


# ---------------------------------------------------------------- checks
def well_checks(data: dict) -> list[dict]:
    area, out = "wells", []
    w, tr, g = data["wells"], data["trajectory"], data["geo"]
    if w.empty:
        return [finding(area, "Wells", "fail", "no wells")]
    far = []
    for r in w.itertuples():
        pts = tr[tr["well_name"] == r.well_name]
        if len(pts) and pd.notna(r.head_x) and pd.notna(r.head_y):
            d = float(np.hypot(pts["x"].iloc[0] - r.head_x, pts["y"].iloc[0] - r.head_y))
            if d > HEAD_WARN:
                far.append(f"{r.well_name} {d:.0f} m")
    out.append(finding(area, "Well heads agree with the surveys", "warn" if far else "pass",
                       f"first survey station far from the wells table head: {', '.join(far)}" if far else
                       f"within {HEAD_WARN:g} m for every well with both"))
    if g and g["areal"]:
        a = g["areal"]
        outside = []
        for r in w.itertuples():
            pts = tr[tr["well_name"] == r.well_name]
            x, y = (pts["x"].iloc[-1], pts["y"].iloc[-1]) if len(pts) else (r.head_x, r.head_y)
            if pd.isna(x):
                continue
            if not (a["x0"] <= x < a["x0"] + g["nx"] * a["dx"] and a["y0"] <= y < a["y0"] + g["ny"] * a["dy"]):
                outside.append(r.well_name)
        out.append(finding(area, "Wells inside the geo grid", "fail" if outside else "pass",
                           f"outside the areal grid at total depth: {outside}" if outside else f"all {len(w)} wells"))
    inj = w[w["well_type"] == "INJECTOR"]
    bad = inj[~inj["preferred_phase"].isin(["WATER", "GAS"])]["well_name"].tolist()
    if bad:
        out.append(finding(area, "Injectors inject water or gas", "fail", f"{bad}"))
    return out


def trajectory_checks(data: dict) -> list[dict]:
    area, out = "trajectories", []
    w, tr = data["wells"], data["trajectory"]
    missing = sorted(set(w["well_name"]) - set(tr["well_name"])) if len(tr) else w["well_name"].tolist()
    out.append(finding(area, "Every well has a trajectory", "fail" if missing else "pass",
                       f"no trajectory: {missing}" if missing else f"{tr['well_name'].nunique()} wells"))
    if tr.empty:
        return out
    longer, worst = [], (0.0, "")
    for name, pts in tr.groupby("well_name"):
        pts = pts.sort_values("md")
        v = np.diff(pts[["x", "y", "tvd"]].to_numpy(float), axis=0)
        chord, dmd = np.linalg.norm(v, axis=1), np.diff(pts["md"].to_numpy(float))
        if (chord > dmd * 1.001 + 0.01).any():
            longer.append(name)
        d = dogleg(pts)
        if len(d) and d.max() > worst[0]:
            worst = (float(d.max()), name)
    out.append(finding(area, "Station spacing fits the measured depth", "fail" if longer else "pass",
                       f"the straight distance between stations exceeds the MD step in {longer}: x, y, TVD or MD is "
                       "wrong" if longer else "every step's 3D distance is within its MD step"))
    out.append(finding(area, f"Dogleg severity within {DLS_WARN:g} deg/30 m", "pass" if worst[0] <= DLS_WARN else "warn",
                       f"largest {worst[0]:.1f} deg/30 m ({worst[1]})" if worst[1] else "straight wells"))
    return out


def completion_checks(data: dict) -> list[dict]:
    area, out = "completions", []
    comp, ev, g = data["completions"], data["events"], data["geo"]
    if comp.empty:
        return [finding(area, "Completions", "fail", "no completions")]
    # an OPEN control needs an open connection by then, or Flow shuts the well
    late = []
    if len(ev):
        for (w, ), e in ev[ev["status"] == "OPEN"].groupby(["well_name"]):
            first = e["event_date"].min()
            c = comp[(comp["well_name"] == w) & (comp["state"] == "OPEN")]
            if c.empty or c["event_date"].min() > first:
                late.append(f"{w} (opens {first}, first open perforation {c['event_date'].min() if len(c) else 'never'})")
    out.append(finding(area, "Perforations open before the well flows", "fail" if late else "pass",
                       "; ".join(late) if late else "every flowing well has an open perforation by its first open control"))
    over = []
    for (w, d), c in comp.dropna(subset=["top_md"]).groupby(["well_name", "event_date"]):
        c = c.sort_values("top_md")
        if (c["top_md"].to_numpy()[1:] < c["bottom_md"].to_numpy()[:-1]).any():
            over.append(f"{w} {d}")
    if over:
        out.append(finding(area, "Perforation records don't overlap", "warn", f"overlapping intervals: {over}"))
    if g is None:
        out.append(finding(area, "Perforations placed in the grid", "n/a", "the well model names no geo version"))
        return out
    p = place(data)
    if p.empty:
        out.append(finding(area, "Perforations placed in the grid", "n/a", "the geo version has no layer depths or areal layout"))
        return out
    md = p.dropna(subset=["length_m"])
    tot = md.groupby("well_name")["length_m"].sum()
    outside = md[(md["k"] == 0) | (md["i"] == 0)].groupby("well_name")["length_m"].sum()
    frac = (outside / tot).dropna()
    none_in = [w for w in tot.index if frac.get(w, 0) >= 0.999]
    st = "fail" if none_in else "warn" if (frac > 0.01).any() else "pass"
    out.append(finding(area, "Perforations inside the grid's layers", st,
                       (f"no perforated length inside the grid: {none_in}; " if none_in else "")
                       + (", ".join(f"{w} {v:.0%} outside" for w, v in frac.items() if v > 0.01) or
                          f"{md['length_m'].sum():.0f} m perforated, all inside")))
    if "PERMX" in p:
        tight = p[(p["k"] > 0) & (p["PERMX"] < LOW_PERM)]
        out.append(finding(area, f"Perforated cells have PERMX above {LOW_PERM:g} mD", "warn" if len(tight) else "pass",
                           f"{len(tight)} perforated cell(s) below: {sorted(set(tight['well_name']))}" if len(tight) else
                           f"lowest {p.loc[p['k'] > 0, 'PERMX'].min():.3g} mD"))
    c = g["contacts"][0] if g["contacts"] else {}
    if not c:
        out.append(finding(area, "Producers against the contacts", "n/a",
                           "no res model on this geo version yet: contacts unknown"))
    wt = data["wells"].set_index("well_name")["well_type"]
    for key, label, typ in (("owc_depth", "below the OWC", "PRODUCER"), ("goc_depth", "above the GOC", "PRODUCER")):
        if c.get(key) is None:
            continue
        pr = p[p["well_name"].map(wt) == typ]
        hit = pr[pr["tvd_base"] > c[key]] if key == "owc_depth" else pr[pr["tvd_top"] < c[key]]
        hit = hit[hit["k"] > 0]
        src = f" (res {g['res']['model_name']} v{g['res']['version']}, {g['res']['status']})"
        out.append(finding(area, f"Producers not perforated {label}", "warn" if len(hit) else "pass",
                           (f"{sorted(set(hit['well_name']))} perforated {label} ({c[key]:g} m): early "
                            f"{'water' if key == 'owc_depth' else 'gas'}" if len(hit) else
                            f"{key.split('_')[0].upper()} {c[key]:g} m") + src))
    return out


def model_volumes(data: dict) -> pd.DataFrame:
    """Per well and month: the volume the deck applies (rate x days until the next event) next to the measured
    volume of the same month."""
    ev = data["events"]
    if ev.empty:
        return pd.DataFrame()
    h = ev[ev["mode"] == "HISTORY"].copy()
    if h.empty:
        return pd.DataFrame()
    h["date"] = pd.to_datetime(h["event_date"])
    rows = []
    for w, e in h.sort_values("date").groupby("well_name"):
        nxt = pd.to_datetime(ev[(ev["well_name"] == w)]["event_date"]).sort_values().tolist()
        for r in e.itertuples():
            later = [d for d in nxt if d > r.date]
            end = later[0] if later else r.date + pd.offsets.MonthBegin(1)
            days = (end - r.date).days
            row = {"well_name": w, "date": r.date, "days": days, "status": r.status}
            for k in RATES:
                v = getattr(r, k)
                row[k.replace("_rate", "_model")] = (v * days) if (r.status == "OPEN" and pd.notna(v)) else 0.0
            rows.append(row)
    df = pd.DataFrame(rows)
    m = data["measured"]
    if len(m):
        m = m.copy()
        m["month"] = m["date"].dt.to_period("M").dt.start_time
        agg = m.groupby(["well_name", "month"])[[c for c in ("oil", "water", "gas", "water_inj", "gas_inj") if c in m]].sum()
        agg = agg.reset_index().rename(columns={"month": "date"})
        agg["well_name"] = agg["well_name"].astype(str)
        df = df.merge(agg, on=["well_name", "date"], how="left")
    return df


def history_checks(data: dict) -> list[dict]:
    area, out = "history", []
    ev = data["events"]
    h = ev[ev["mode"] == "HISTORY"] if len(ev) else ev
    if h.empty:
        return [finding(area, "History", "n/a", "no history in this model")]
    v = model_volumes(data)
    wt = data["wells"].set_index("well_name")["well_type"]
    if len(data["measured"]) and len(v):
        msgs, worst = [], 0.0
        for w, g in v.groupby("well_name"):
            inj = wt.get(w) == "INJECTOR"
            pairs = [("inj_model", "water_inj" if "water_inj" in g else "gas_inj")] if inj else \
                    [(f"{k.replace('_rate', '')}_model", MEASURED[k]) for k in ("oil_rate", "water_rate", "gas_rate")]
            for mk, ck in pairs:
                if ck not in g or g[ck].fillna(0).sum() <= 0:
                    continue
                meas, mod = float(g[ck].fillna(0).sum()), float(g[mk].sum())
                rel = (mod - meas) / meas
                worst = max(worst, abs(rel))
                if abs(rel) > CUM_WARN:
                    msgs.append(f"{w} {ck} {rel:+.1%}")
        st = "pass" if worst <= CUM_WARN else "warn" if worst <= CUM_FAIL else "fail"
        out.append(finding(area, "Volumes the deck applies match the measured volumes", st,
                           ("; ".join(msgs[:8]) + (" ..." if len(msgs) > 8 else "") + ". WCONHIST applies each month's "
                            "rate (volume / producing days) for the whole month, so downtime adds volume; "
                            "WEFAC or rates over calendar days would remove it") if msgs else
                           f"largest difference {worst:.1%} per well and phase (limits {CUM_WARN:.0%} warn, {CUM_FAIL:.0%} fail)"))
    gaps = []
    for w, e in h.groupby("well_name"):
        months = pd.to_datetime(e["event_date"]).dt.to_period("M")
        full = pd.period_range(months.min(), months.max(), freq="M")
        miss = len(full) - months.nunique()
        if miss:
            gaps.append(f"{w} {miss}")
    out.append(finding(area, "No missing months inside each well's history", "warn" if gaps else "pass",
                       f"months without a row (the previous rate carries on through them): {', '.join(gaps)}" if gaps
                       else "every well's history is continuous"))
    spikes = []
    op = h[h["status"] == "OPEN"]
    for w, e in op.groupby("well_name"):
        for k in RATES:
            s = e[k].dropna()
            s = s[s > 0]
            if len(s) >= 6 and s.max() > SPIKE * s.median():
                spikes.append(f"{w} {k.replace('_rate', '')} {s.max() / s.median():.0f}x median")
    out.append(finding(area, f"No monthly rate above {SPIKE:g}x the well's median", "warn" if spikes else "pass",
                       ", ".join(spikes[:8]) if spikes else "no spikes"))
    prod = op[op["well_name"].map(wt) == "PRODUCER"]
    if len(prod):
        liq = prod[["oil_rate", "water_rate"]].fillna(0).sum(1)
        wct = (prod["water_rate"].fillna(0) / liq.where(liq > 0)).dropna()
        out.append(finding(area, "Producers' history has rates", "pass" if len(wct) else "fail",
                           f"water cut {wct.min():.0%}-{wct.max():.0%} over {prod['well_name'].nunique()} producers"
                           if len(wct) else "no oil or water rates"))
    no_bhp = sorted(set(op["well_name"]) - set(op.dropna(subset=["bhp"])["well_name"]))
    out.append(finding(area, "History has bottom-hole pressures", "warn" if no_bhp else "pass",
                       f"no BHP for {no_bhp}: nothing to compare the run's pressures with" if no_bhp else
                       f"{op['bhp'].notna().mean():.0%} of open months"))
    return out


def control_checks(data: dict) -> list[dict]:
    area, out = "controls", []
    ev, wt = data["events"], data["wells"].set_index("well_name")["well_type"]
    if ev.empty:
        return [finding(area, "Controls", "warn", "no events: the wells never open")]
    start = data["header"]["start_date"]
    early = ev[ev["event_date"] < start] if start else ev.iloc[0:0]
    out.append(finding(area, "Events on or after the start date", "fail" if len(early) else "pass",
                       f"{len(early)} event(s) before {start}" if len(early) else f"start {start}"))
    pred, hist = ev[ev["mode"] == "PREDICTION"], ev[ev["mode"] == "HISTORY"]
    if len(pred) and len(hist):
        overlap, jump = [], []
        for w, p in pred.groupby("well_name"):
            hw = hist[hist["well_name"] == w]
            if hw.empty:
                continue
            if p["event_date"].min() <= hw["event_date"].max():
                overlap.append(w)
            last = hw[hw["status"] == "OPEN"].sort_values("event_date").tail(3)
            first = p.sort_values("event_date").iloc[0]
            key = "inj_rate" if wt.get(w) == "INJECTOR" else {"ORAT": "oil_rate", "WRAT": "water_rate",
                                                                 "GRAT": "gas_rate"}.get(first["control"])
            ref = last[key].median() if key and len(last) else None     # median: one spike doesn't hide a jump
            if key and pd.notna(first.get(key)) and ref and ref > 0 and first[key] > PRED_JUMP * ref:
                jump.append(f"{w} {first[key]:.0f} vs {ref:.0f}")
        out.append(finding(area, "Prediction starts after the history", "warn" if overlap else "pass",
                           f"prediction controls before the last history month: {overlap}" if overlap else
                           "no overlap"))
        out.append(finding(area, f"Prediction targets within {PRED_JUMP:g}x the last history rates", "warn" if jump else "pass",
                           f"{', '.join(jump)} (target vs median of the last 3 open months)" if jump else "no jumps"))
    idle = sorted(set(data["wells"]["well_name"]) - set(ev[ev["status"] == "OPEN"]["well_name"]))
    out.append(finding(area, "Every well opens at some time", "warn" if idle else "pass",
                       f"never open: {idle}" if idle else f"all {len(data['wells'])} wells"))
    return out


def run_all_checks(data: dict) -> list[dict]:
    out = []
    g = data["geo"]
    if g and g["changed"]:
        out.append(finding("wells", "Geo arrays are the registered files", "fail", f"changed since registration: {g['changed']}"))
    for fn in (well_checks, trajectory_checks, completion_checks, history_checks, control_checks):
        try:
            out += fn(data)
        except Exception as e:  # noqa: BLE001  a check that can't run is a finding, not a crash
            out.append(finding(fn.__name__.replace("_checks", ""), "check could run", "warn", f"{type(e).__name__}: {e}"))
    return out


# ---------------------------------------------------------------- plots
COLOURS = {"PRODUCER": "#d62728", "INJECTOR": "#1f77b4"}


def fig_plan(data: dict):
    """Plan view: trajectories over the top layer's porosity, perforated parts thick."""
    g, tr, w = data["geo"], data["trajectory"], data["wells"].set_index("well_name")
    fig = go.Figure()
    if g and g["areal"] and "PORO" in g["arrays"]:
        a = g["areal"]
        xs, ys = a["x0"] + a["dx"] * (np.arange(g["nx"]) + 0.5), a["y0"] + a["dy"] * (np.arange(g["ny"]) + 0.5)
        fig.add_trace(go.Heatmap(x=xs, y=ys, z=g["arrays"]["PORO"][0], colorscale="Greys", reversescale=True, opacity=0.5,
                                 colorbar=dict(title="PORO L1"), hovertemplate="PORO %{z:.3f}<extra></extra>"))
    for name, pts in tr.groupby("well_name"):
        col = COLOURS.get(w["well_type"].get(name), "#333")
        fig.add_trace(go.Scatter(x=pts["x"], y=pts["y"], mode="lines", line=dict(color=col, width=1.5), showlegend=False,
                                 hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=[pts["x"].iloc[0]], y=[pts["y"].iloc[0]], mode="markers+text", text=[name],
                                 textposition="top center", marker=dict(color=col, size=9), showlegend=False))
        for c in data["completions"][data["completions"]["well_name"] == name].dropna(subset=["top_md"]).itertuples():
            md = np.linspace(c.top_md, c.bottom_md, 20)
            x, y, _ = _interp(pts, md)
            fig.add_trace(go.Scatter(x=x, y=y, mode="lines", line=dict(color=col, width=6), showlegend=False,
                                     hovertext=f"{name} {c.top_md:g}-{c.bottom_md:g} m MD {c.state}", hoverinfo="text"))
    fig.update_yaxes(scaleanchor="x", scaleratio=1)
    fig.update_layout(**{**LAYOUT, "height": 520}, title="Plan view: well paths (thick: perforated; red producers, blue injectors)")
    return fig


def fig_section(data: dict, well: str):
    """TVD against measured depth along the well, with the perforations and the geo zones at the well's cells."""
    tr = data["trajectory"]
    pts = tr[tr["well_name"] == well].sort_values("md")
    disp = np.concatenate([[0], np.cumsum(np.hypot(np.diff(pts["x"]), np.diff(pts["y"])))])
    fig = go.Figure(go.Scatter(x=disp, y=pts["tvd"], mode="lines+markers", name="path", line=dict(color="#555"),
                               customdata=pts["md"], hovertemplate="MD %{customdata:.0f} m<br>TVD %{y:.1f} m<extra></extra>"))
    for c in data["completions"][data["completions"]["well_name"] == well].dropna(subset=["top_md"]).itertuples():
        md = np.linspace(c.top_md, c.bottom_md, 20)
        x, y, z = _interp(pts, md)
        dd = np.interp(md, pts["md"], disp)
        fig.add_trace(go.Scatter(x=dd, y=z, mode="lines", line=dict(width=8, color="#2ca02c" if c.state == "OPEN" else "#999"),
                                 name=f"perf {c.event_date}", hovertext=f"{c.top_md:g}-{c.bottom_md:g} m MD", hoverinfo="text"))
    g = data["geo"]
    if g and g["layer_depths"] is not None and g["areal"]:
        a = g["areal"]
        i = np.clip(((pts["x"] - a["x0"]) // a["dx"]).astype(int), 0, g["nx"] - 1)
        j = np.clip(((pts["y"] - a["y0"]) // a["dy"]).astype(int), 0, g["ny"] - 1)
        for name, k in (g["surfaces"] or {"top": 0, "base": g["nz"]}).items():
            fig.add_trace(go.Scatter(x=disp, y=g["layer_depths"][k, j, i], mode="lines", line=dict(color="black", dash="dot", width=1),
                                     name=name))
        for c in g["contacts"][:1]:
            for key, col in (("owc_depth", "#1f77b4"), ("goc_depth", "#d62728")):
                if c[key] is not None:
                    fig.add_hline(y=c[key], line=dict(color=col, dash="dash"), annotation_text=key.split("_")[0].upper())
        zr = g["layer_depths"][:, j, i]
        fig.update_yaxes(range=[float(zr.max()) + 20, float(zr.min()) - 20])
    else:
        fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(title="horizontal displacement from the head (m)")
    fig.update_yaxes(title="TVD (m)")
    fig.update_layout(**{**LAYOUT, "height": 380}, title=f"Well {well}: path through the reservoir")
    return fig


def fig_perforated_cells(data: dict):
    p = place(data)
    p = p[p["k"] > 0]
    if p.empty or "PERMX" not in p:
        return None
    fig = go.Figure(go.Scatter(x=p["well_name"], y=p["k"], mode="markers", marker=dict(
        size=np.clip(p["length_m"].fillna(1), 2, 40) / 2 + 6, color=np.log10(p["PERMX"]), colorscale="Viridis",
        colorbar=dict(title="log10 PERMX"), line=dict(width=1, color="black")),
        customdata=np.stack([p["i"], p["j"], p["length_m"].fillna(0), p["PORO"] if "PORO" in p else p["k"] * 0, p["PERMX"]], 1),
        hovertemplate="%{x} cell (%{customdata[0]}, %{customdata[1]}, %{y})<br>%{customdata[2]:.1f} m perforated"
                      "<br>PORO %{customdata[3]:.3f}, PERMX %{customdata[4]:.1f} mD<extra></extra>"))
    fig.update_yaxes(autorange="reversed", title="layer K", dtick=1)
    fig.update_layout(**{**LAYOUT, "height": 380}, title="Perforated cells (size: perforated length; colour: PERMX)")
    return fig


def fig_history(data: dict, well: str):
    """One well's monthly rates as the deck applies them, with the measured monthly averages."""
    ev = data["events"]
    e = ev[(ev["well_name"] == well)].sort_values("event_date")
    inj = data["wells"].set_index("well_name")["well_type"].get(well) == "INJECTOR"
    keys = ["inj_rate"] if inj else ["oil_rate", "water_rate", "gas_rate"]
    fig = make_subplots(rows=1, cols=len(keys) + 1, subplot_titles=[k.replace("_", " ") for k in keys] + ["BHP"])
    m = data["measured"]
    m = m[m["well_name"].astype(str) == well] if len(m) else m
    for c, k in enumerate(keys + ["bhp"], start=1):
        y = e[k].where(e["status"] == "OPEN", 0 if k != "bhp" else np.nan)
        fig.add_trace(go.Scatter(x=e["event_date"], y=y, mode="lines", line=dict(shape="hv", color="#1f77b4"),
                                 name="model", showlegend=c == 1), 1, c)
        col = "bhp" if k == "bhp" else ("water_inj" if inj else MEASURED[k])
        if len(m) and col in m and m[col].notna().any():
            mm = m.set_index("date")[col].resample("MS").mean() if k == "bhp" else \
                m.set_index("date")[col].resample("MS").sum() / m.set_index("date")[col].resample("MS").size().clip(lower=1)
            fig.add_trace(go.Scatter(x=mm.index, y=mm.values, mode="markers", marker=dict(size=4, color="#d62728"),
                                     name="measured (calendar-day average)", showlegend=c == 1), 1, c)
    fig.update_layout(**{**LAYOUT, "height": 320}, title=f"Well {well}: rates the deck applies vs measured")
    return fig


def fig_cumulative(data: dict):
    v = model_volumes(data)
    if v.empty:
        return None
    fig = make_subplots(rows=1, cols=2, subplot_titles=("Field oil (sm3)", "Field water injected (sm3)"))
    for c, (mk, ck) in enumerate((("oil_model", "oil"), ("inj_model", "water_inj")), start=1):
        s = v.groupby("date")[[mk] + ([ck] if ck in v else [])].sum().sort_index().cumsum()
        fig.add_trace(go.Scatter(x=s.index, y=s[mk], name="model (deck)", line=dict(color="#1f77b4"), showlegend=c == 1), 1, c)
        if ck in s:
            fig.add_trace(go.Scatter(x=s.index, y=s[ck], name="measured", line=dict(color="#d62728", dash="dot"),
                                     showlegend=c == 1), 1, c)
    fig.update_layout(**LAYOUT, title="Cumulative volumes: what the deck applies vs what was measured")
    return fig


def fig_schedule(data: dict):
    """Per well: open (green) and shut (grey) periods, history and prediction, and perforation dates."""
    ev = data["events"]
    if ev.empty:
        return None
    fig = go.Figure()
    names = sorted(ev["well_name"].unique())
    end = (pd.to_datetime(ev["event_date"]).max() + pd.offsets.MonthBegin(1)).date().isoformat()
    for w in names:
        e = ev[ev["well_name"] == w].sort_values("event_date")
        dates = e["event_date"].tolist() + [end]
        for n, r in enumerate(e.itertuples()):
            col = ("#2ca02c" if r.mode == "HISTORY" else "#98df8a") if r.status == "OPEN" else "#ccc"
            fig.add_trace(go.Scatter(x=[r.event_date, dates[n + 1]], y=[w, w], mode="lines", line=dict(color=col, width=12),
                                     showlegend=False, hovertext=f"{w} {r.event_date} {r.mode} {r.status} {r.control or ''}",
                                     hoverinfo="text"))
    c = data["completions"]
    if len(c):
        fig.add_trace(go.Scatter(x=c["event_date"], y=c["well_name"], mode="markers", name="perforation",
                                 marker=dict(symbol="line-ns-open", size=18, color="black", line=dict(width=2))))
    fig.update_yaxes(categoryorder="array", categoryarray=names[::-1])
    fig.update_layout(**{**LAYOUT, "height": max(260, 30 * len(names) + 120)},
                      title="Schedule: open in history (dark green) and prediction (light), shut (grey), perforations (|)")
    return fig
