"""Helpers for the res QC notebooks (the standard template res_qc.ipynb and investigation notebooks).

Read only, like the other QC notebooks. A notebook loads the payload the res pipeline wrote (spec, assumptions,
validator warnings, the static view's files) and the model as stored in the database (read-only): SWOF, SGOF,
rock, contacts, aquifers and their connections, region arrays. Grid quantities come from the geo version's static
view, made by OPM Flow with this res version's regions (its INIT: PORV, DEPTH, SATNUM, EQLNUM, FIPNUM per cell), so
the checks are on what Flow will see, on any grid. Without the static view the grid checks are n/a.
Every check returns a finding {area, check, status, detail}; display helpers and the summary are shared.
"""
from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .qc_lib import LAYOUT, finding, overall, show_findings, show_list, write_summary  # noqa: F401  (re-exported)

AREAS = ("scal", "regions", "contacts", "rock", "aquifers")
MOBILE_WARN = 0.2            # 1 - Swc - Sorw below this: little oil can move
KRO_TOL = 1e-6               # kro at connate water, SWOF against SGOF
CR_RANGE = {"METRIC": (1e-6, 1e-3), "FIELD": (7e-8, 7e-5)}     # rock compressibility, 1/bar or 1/psi
AQ_RATIO_WARN = 1.0          # Fetkovich water volume below this x the reservoir pore volume: a small aquifer


# ---------------------------------------------------------------- loading
def _ro(db_path):
    con = sqlite3.connect(f"file:{Path(db_path).expanduser()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _rows(con, sql, *a):
    return [dict(r) for r in con.execute(sql, a)]


def load_grid(init_path: str | Path | None, egrid_path: str | Path | None, dims: tuple[int, int, int]) -> dict | None:
    """The static view's INIT as global (nz, ny, nx) arrays (NaN / 0 where inactive), or None."""
    if not init_path or not Path(init_path).is_file():
        return None
    from opm.io.ecl import EclFile
    nx, ny, nz = dims
    n = nx * ny * nz
    f = EclFile(str(init_path))
    names = {a[0] for a in f.arrays}
    porv = np.asarray(f["PORV"], dtype=float)
    if porv.size != n:
        return None
    active = porv > 0
    if egrid_path and Path(egrid_path).is_file():
        g = EclFile(str(egrid_path))
        if "ACTNUM" in {a[0] for a in g.arrays}:
            active = np.asarray(g["ACTNUM"]) > 0
    na = int(active.sum())
    out = {"active": active.reshape(nz, ny, nx), "PORV": porv.reshape(nz, ny, nx), "n_active": na}
    for kw in ("DEPTH", "DZ", "PORO", "NTG", "PERMX", "SATNUM", "EQLNUM", "FIPNUM", "PVTNUM", "SWATINIT"):
        if kw in names:
            v = np.asarray(f[kw], dtype=float)
            full = np.full(n, np.nan)
            if v.size == na:
                full[active] = v
            elif v.size == n:
                full = v
            else:
                continue
            out[kw] = full.reshape(nz, ny, nx)
    return out


def load(payload_path: str | Path, db_path: str | Path | None = None, version_id: int | None = None) -> dict:
    payload_path = Path(payload_path)
    data = json.loads(payload_path.read_text())
    db_path = db_path or data.get("db_path")
    vid = int(version_id or data.get("version_id"))
    con = _ro(db_path)
    try:
        h = _rows(con, "SELECT * FROM res_model_versions WHERE id = ?", vid)[0]
        t = {k: _rows(con, f"SELECT * FROM {k} WHERE version_id = ? ORDER BY {o}", vid)
             for k, o in (("swof", "satnum, sw"), ("sgof", "satnum, sg"), ("rock", "region"), ("contacts", "eqlnum"),
                          ("aquifers", "aquifer_id"), ("aquifer_connections", "aquifer_id, id"),
                          ("res_properties", "keyword"))}
        g = _rows(con, "SELECT g.*, m.name AS model_name FROM geo_model_versions g JOIN geo_models m "
                       "ON m.id = g.geo_model_id WHERE g.id = ?", h["geo_version_id"])[0]
    finally:
        con.close()
    dims = (g["nx"], g["ny"], g["nz"])
    sv = data.get("static_view") or {}
    data.update(header=h, tables=t, tables_from_db=True, geo=g, dims=dims, unit_system=h["unit_system"],
                swof=pd.DataFrame(t["swof"]), sgof=pd.DataFrame(t["sgof"]),
                grid=load_grid(sv.get("init"), sv.get("egrid"), dims))
    return data


def header(data: dict):
    from IPython.display import HTML
    g, gr = data["geo"], data["grid"]
    nsat = len({r["satnum"] for r in data["tables"]["swof"]})
    grid = (f"{gr['n_active']:,} active cells (the static view, as Flow sees it)" if gr
            else "no static view: grid checks are n/a")
    return HTML(f"<h1>Res model QC: {html.escape(data['model_name'])} version {data['version']}</h1>"
                f"<p>Built {data.get('created_at', '')} &middot; on geo {html.escape(g['model_name'])} v{g['version']} "
                f"({g['nx']} x {g['ny']} x {g['nz']}) &middot; {nsat} saturation region(s), "
                f"{len(data['tables']['contacts'])} equilibration region(s), {len(data['tables']['aquifers'])} "
                f"aquifer(s) &middot; {grid} &middot; {data['unit_system']}</p>")


# ---------------------------------------------------------------- SCAL
def end_points(data: dict) -> pd.DataFrame:
    """Per SATNUM: Swc, Sorw (1 - Sw where krow reaches 0), krw there, Sgc, Sorg, kro at Swc from both tables."""
    rows = []
    sw, sg = data["swof"], data["sgof"]
    for s in sorted(set(sw["satnum"]) if len(sw) else []):
        w = sw[sw["satnum"] == s].sort_values("sw")
        g = sg[sg["satnum"] == s].sort_values("sg") if len(sg) else sg
        swc = float(w["sw"].iloc[0])
        zero = w[w["krow"] <= 0]
        sw_or = float(zero["sw"].iloc[0]) if len(zero) else float(w["sw"].iloc[-1])
        krw_or = float(np.interp(sw_or, w["sw"], w["krw"]))
        r = {"satnum": int(s), "swc": swc, "sorw": 1 - sw_or, "krw_at_sorw": krw_or,
             "kro_swof": float(w["krow"].iloc[0]), "mobile_oil": sw_or - swc,
             "pc_max": float(w["pcow"].max()), "pc_at_max_sw": float(w["pcow"].iloc[-1])}
        if len(g):
            mob = g[g["krg"] > 0]
            r.update(sgc=float(mob["sg"].iloc[0]) if len(mob) else None,
                     sg_max=float(g["sg"].iloc[-1]), kro_sgof=float(g["krog"].iloc[0]),
                     sorg=1 - swc - float(g[g["krog"] <= 0]["sg"].iloc[0]) if (g["krog"] <= 0).any() else None)
        rows.append(r)
    return pd.DataFrame(rows)


def scal_checks(data: dict) -> list[dict]:
    out = []
    ep = end_points(data)
    if not len(ep):
        return [finding("scal", "SWOF present", "fail", "no SWOF rows")]
    for r in ep.to_dict("records"):
        s = f"SATNUM {r['satnum']}"
        mob = r["mobile_oil"]
        out.append(finding("scal", f"{s}: mobile oil window", "warn" if mob < MOBILE_WARN else "pass",
                           f"1 - Swc - Sorw = {mob:.3f} (Swc {r['swc']:.3f}, Sorw {r['sorw']:.3f})"))
        if "kro_sgof" in r and r.get("kro_sgof") is not None:
            d = abs(r["kro_swof"] - r["kro_sgof"])
            out.append(finding("scal", f"{s}: kro at connate water, SWOF and SGOF", "pass" if d <= KRO_TOL else "warn",
                               f"SWOF krow {r['kro_swof']:.4f}, SGOF krog {r['kro_sgof']:.4f}"))
            room = 1 - r["swc"]
            out.append(finding("scal", f"{s}: SGOF ends within 1 - Swc", "pass" if r["sg_max"] <= room + 1e-6 else "warn",
                               f"last Sg {r['sg_max']:.3f}, 1 - Swc {room:.3f}"))
        w = data["swof"][data["swof"]["satnum"] == r["satnum"]].sort_values("sw")
        pc_ok = bool(np.all(np.diff(w["pcow"].to_numpy()) <= 1e-12))
        out.append(finding("scal", f"{s}: Pcow does not rise with Sw", "pass" if pc_ok else "warn",
                           f"Pcow {w['pcow'].iloc[0]:g} to {w['pcow'].iloc[-1]:g}"))
    return out


# ---------------------------------------------------------------- regions, contacts, rock
def region_volumes(data: dict, kw: str) -> pd.DataFrame:
    gr = data["grid"]
    if not gr or kw not in gr:
        return pd.DataFrame()
    reg, pv = gr[kw][gr["active"]], gr["PORV"][gr["active"]]
    df = pd.DataFrame({kw: reg.astype(int), "pore_volume": pv})
    return df.groupby(kw).agg(cells=("pore_volume", "size"), pore_volume=("pore_volume", "sum")).reset_index()


def region_checks(data: dict) -> list[dict]:
    out = []
    gr = data["grid"]
    nsat = len({r["satnum"] for r in data["tables"]["swof"]})
    neql = len(data["tables"]["contacts"])
    if not gr:
        return [finding("regions", "region arrays as Flow sees them", "n/a", "no static view")]
    for kw, have in (("SATNUM", nsat), ("EQLNUM", neql)):
        if kw not in gr:
            out.append(finding("regions", f"{kw} in the static view", "pass", "one region (default 1)"))
            continue
        used = sorted(int(v) for v in np.unique(gr[kw][gr["active"]]))
        missing = [v for v in used if v > have]
        unused = [v for v in range(1, have + 1) if v not in used]
        out.append(finding("regions", f"{kw} values have tables", "fail" if missing else "pass",
                           f"values {used}, tables 1..{have}" + (f"; no table for {missing}" if missing else "")))
        if unused:
            out.append(finding("regions", f"every {kw} table is used", "warn", f"not used by any active cell: {unused}"))
    if "FIPNUM" in gr:
        v = region_volumes(data, "FIPNUM")
        empty = v[v["pore_volume"] <= 0]
        out.append(finding("regions", "FIPNUM regions hold pore volume", "warn" if len(empty) else "pass",
                           f"{len(v)} region(s)" + (f"; empty: {empty['FIPNUM'].tolist()}" if len(empty) else "")))
    return out


def contact_checks(data: dict) -> list[dict]:
    out = []
    gr = data["grid"]
    for c in data["tables"]["contacts"]:
        e = f"EQLNUM {c['eqlnum']}"
        owc, goc = c["owc_depth"], c["goc_depth"]
        if owc is not None and goc is not None:
            out.append(finding("contacts", f"{e}: GOC above OWC", "pass" if goc <= owc else "fail",
                               f"GOC {goc:g}, OWC {owc:g}"))
        if not gr or "DEPTH" not in gr:
            out.append(finding("contacts", f"{e}: contacts against the grid", "n/a", "no static view"))
            continue
        cells = gr["active"] & ((gr["EQLNUM"] == c["eqlnum"]) if "EQLNUM" in gr else True)
        d = gr["DEPTH"][cells]
        if not d.size:
            out.append(finding("contacts", f"{e}: cells", "warn", "no active cells in this region"))
            continue
        half = gr["DZ"][cells] / 2 if "DZ" in gr else np.zeros_like(d)    # cell faces, not centres
        top, base = float(np.nanmin(d - half)), float(np.nanmax(d + half))
        dd = c["datum_depth"]
        out.append(finding("contacts", f"{e}: datum within the region's depth range",
                           "pass" if top - 1e-6 <= dd <= base + 1e-6 else "warn",
                           f"datum {dd:g}, cells {top:.1f}-{base:.1f}"))
        for name, z in (("OWC", owc), ("GOC", goc)):
            if z is None:
                continue
            where = "above the region" if z < top else "below the region" if z > base else "inside the region"
            out.append(finding("contacts", f"{e}: {name} {z:g}", "pass", f"{where} ({top:.1f}-{base:.1f})"))
    return out


def rock_checks(data: dict) -> list[dict]:
    lo, hi = CR_RANGE.get(data["unit_system"], CR_RANGE["METRIC"])
    unit = "1/psi" if data["unit_system"] == "FIELD" else "1/bar"
    return [finding("rock", f"region {r['region']}: compressibility in the usual range",
                    "pass" if lo <= r["cr"] <= hi else "warn", f"cr {r['cr']:g} {unit} (usual {lo:g}-{hi:g})")
            for r in data["tables"]["rock"]]


# ---------------------------------------------------------------- aquifers
def _conn_cells(c: dict) -> list[tuple[int, int, int]]:
    return [(k - 1, j - 1, i - 1) for k in range(c["k1"], c["k2"] + 1) for j in range(c["j1"], c["j2"] + 1)
            for i in range(c["i1"], c["i2"] + 1)]


def aquifer_table(data: dict) -> pd.DataFrame:
    """Per aquifer: connected cells, active ones, the aquifer's water volume and its ratio to the reservoir's."""
    gr = data["grid"]
    res_pv = float(gr["PORV"][gr["active"]].sum()) if gr else None
    rows = []
    for a in data["tables"]["aquifers"]:
        cells = [x for c in data["tables"]["aquifer_connections"] if c["aquifer_id"] == a["aquifer_id"]
                 for x in _conn_cells(c)]
        act = sum(bool(gr["active"][x]) for x in cells) if gr else None
        if a["aq_type"] == "FETKOVICH":
            vol = a["water_volume"]
        else:   # the Carter-Tracy aquifer's pore volume out to ten inner radii, as a scale for the reader
            vol = np.pi * ((10 * a["inner_radius"]) ** 2 - a["inner_radius"] ** 2) * a["thickness"] * a["porosity"] \
                * (a["angle_deg"] or 360) / 360
        rows.append({"aquifer": a["aquifer_id"], "type": a["aq_type"].lower().replace("_", "-"),
                     "connected_cells": len(cells), "active_cells": act, "water_volume": vol,
                     "volume_ratio": (vol / res_pv) if res_pv else None})
    return pd.DataFrame(rows)


def aquifer_checks(data: dict) -> list[dict]:
    if not data["tables"]["aquifers"]:
        return [finding("aquifers", "analytical aquifers", "n/a", "none in this model")]
    out = []
    for r in aquifer_table(data).to_dict("records"):
        a = f"aquifer {r['aquifer']}"
        if r["active_cells"] is None:
            out.append(finding("aquifers", f"{a}: connected cells are active", "n/a", "no static view"))
        else:
            st = "fail" if r["active_cells"] == 0 else "warn" if r["active_cells"] < r["connected_cells"] else "pass"
            out.append(finding("aquifers", f"{a}: connected cells are active", st,
                               f"{r['active_cells']} of {r['connected_cells']} active (Flow skips inactive ones)"))
        if r["volume_ratio"] is not None:
            label = "water volume" if r["type"] == "fetkovich" else "pore volume to 10 inner radii"
            st = "warn" if r["type"] == "fetkovich" and r["volume_ratio"] < AQ_RATIO_WARN else "pass"
            out.append(finding("aquifers", f"{a}: size against the reservoir", st,
                               f"{label} {r['water_volume']:.3g}, {r['volume_ratio']:.2f} x the reservoir pore volume"))
    return out


def run_all_checks(data: dict) -> list[dict]:
    out = []
    for fn in (scal_checks, region_checks, contact_checks, rock_checks, aquifer_checks):
        try:
            out += fn(data)
        except Exception as e:  # noqa: BLE001  a check that can't run is a finding, not a crash
            out.append(finding(fn.__name__.replace("_checks", ""), "check could run", "warn", f"{type(e).__name__}: {e}"))
    return out


# ---------------------------------------------------------------- plots
def fig_kr(data: dict):
    sw, sg = data["swof"], data["sgof"]
    fig = make_subplots(rows=1, cols=2, subplot_titles=("SWOF: krw, krow against Sw", "SGOF: krg, krog against Sg"))
    for s in sorted(set(sw["satnum"])):
        w = sw[sw["satnum"] == s]
        fig.add_trace(go.Scatter(x=w["sw"], y=w["krw"], name=f"krw {s}", mode="lines+markers"), 1, 1)
        fig.add_trace(go.Scatter(x=w["sw"], y=w["krow"], name=f"krow {s}", mode="lines+markers",
                                 line=dict(dash="dash")), 1, 1)
        if len(sg):
            g = sg[sg["satnum"] == s]
            fig.add_trace(go.Scatter(x=g["sg"], y=g["krg"], name=f"krg {s}", mode="lines+markers"), 1, 2)
            fig.add_trace(go.Scatter(x=g["sg"], y=g["krog"], name=f"krog {s}", mode="lines+markers",
                                     line=dict(dash="dash")), 1, 2)
    fig.update_xaxes(range=[0, 1])
    fig.update_yaxes(range=[0, 1.02], title_text="kr")
    fig.update_layout(**{**LAYOUT, "height": 420})
    return fig


def fig_pc(data: dict):
    sw = data["swof"]
    if not len(sw) or not (sw["pcow"].abs() > 0).any():
        return None
    unit = "psi" if data["unit_system"] == "FIELD" else "bar"
    fig = go.Figure()
    for s in sorted(set(sw["satnum"])):
        w = sw[sw["satnum"] == s]
        fig.add_trace(go.Scatter(x=w["sw"], y=w["pcow"], name=f"SATNUM {s}", mode="lines+markers"))
    fig.update_layout(title=f"Water-oil capillary pressure ({unit})", xaxis_title="Sw", yaxis_title=f"Pcow ({unit})",
                      **{**LAYOUT, "height": 380})
    return fig


def fig_regions(data: dict, kw: str, layer: int = 0):
    gr = data["grid"]
    if not gr or kw not in gr:
        return None
    z = np.where(gr["active"][layer], gr[kw][layer], np.nan)
    fig = go.Figure(go.Heatmap(z=z, colorscale="Viridis", colorbar=dict(title=kw), hovertemplate="i %{x} j %{y}: %{z}"))
    fig.update_layout(title=f"{kw}, layer {layer + 1}", xaxis_title="i", yaxis_title="j", **{**LAYOUT, "height": 420})
    fig.update_yaxes(autorange="reversed", scaleanchor="x")
    return fig


def fig_contacts(data: dict):
    """Per EQLNUM: the cells' depth range against the datum, OWC and GOC."""
    gr = data["grid"]
    if not gr or "DEPTH" not in gr:
        return None
    fig = go.Figure()
    for c in data["tables"]["contacts"]:
        cells = gr["active"] & ((gr["EQLNUM"] == c["eqlnum"]) if "EQLNUM" in gr else True)
        d = gr["DEPTH"][cells]
        if not d.size:
            continue
        x = f"EQLNUM {c['eqlnum']}"
        fig.add_trace(go.Box(y=d, name=x, boxpoints=False, marker_color="#888", showlegend=False))
        for name, z, colour in (("datum", c["datum_depth"], "#333"), ("OWC", c["owc_depth"], "#1f77b4"),
                                ("GOC", c["goc_depth"], "#d62728")):
            if z is not None:
                fig.add_trace(go.Scatter(x=[x], y=[z], mode="markers+text", text=[name], textposition="middle right",
                                         marker=dict(symbol="line-ew-open", size=40, color=colour), showlegend=False))
    fig.update_yaxes(autorange="reversed", title_text="depth")
    fig.update_layout(title="Cell depths per equilibration region, with the datum and contacts", **{**LAYOUT, "height": 420})
    return fig


def fig_aquifers(data: dict):
    """Plan view of the connected cells (their i, j), over the top active layer's pore volume."""
    gr = data["grid"]
    conns = data["tables"]["aquifer_connections"]
    if not conns:
        return None
    nx, ny, _ = data["dims"]
    fig = go.Figure()
    if gr:
        fig.add_trace(go.Heatmap(z=np.where(gr["active"][0], gr["PORV"][0], np.nan), colorscale="Greys",
                                 showscale=False, hoverinfo="skip"))
    for a in data["tables"]["aquifers"]:
        cells = [x for c in conns if c["aquifer_id"] == a["aquifer_id"] for x in _conn_cells(c)]
        fig.add_trace(go.Scatter(x=[x[2] for x in cells], y=[x[1] for x in cells], mode="markers",
                                 name=f"aquifer {a['aquifer_id']} ({a['aq_type'].lower()})", marker=dict(size=9)))
    fig.update_layout(title="Aquifer connections (plan view, i and j from 0)", xaxis_title="i", yaxis_title="j",
                      **{**LAYOUT, "height": 420})
    fig.update_yaxes(autorange="reversed", scaleanchor="x", range=[ny, -1])
    fig.update_xaxes(range=[-1, nx])
    return fig
