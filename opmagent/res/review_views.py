"""Res review: view specs, anchors and what they point at (the review workbench, phase 4; see opmagent/reviews.py).

Views (all from the res QC library's data, the model as stored):
  kr               SWOF and SGOF curves per SATNUM (satnum: one, or all); base_version_id overlays the base's dashed
  pc               water-oil capillary pressure per SATNUM; base_version_id overlays the base's
  regions          a region or grid keyword on one layer (kw, layer), on cell indices
  contacts         each EQLNUM's cell depths with its datum, OWC and GOC; base_version_id draws the base's contacts too
  contact_section  the cells of one row or column at their depths, coloured by a keyword, with the contacts
  aquifers         the aquifer connections on a plan of the grid

Anchors: {"satnum": 2, "curve": "krw", "s": 0.45}, {"eqlnum": 1, "contact": "OWC"}, {"cell": [i, j, k], "kw": ...},
{"aquifer": 1}, {"finding": ...}, {} (general).

The grid (depths, pore volumes, regions) is the static view's INIT when the version has one; else it is made from the
geo version (cell-centre depths, PORV = bulk x PORO x NTG) with this version's region arrays.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from ..core import models
from ..core.errors import INVALID_INPUT, ToolError
from ..notebooks import res_qc_lib as rq
from ..notebooks import runner

FIGS = ("kr", "pc", "regions", "contacts", "contact_section", "aquifers")
ANCHOR_KEYS = {"satnum", "curve", "s", "eqlnum", "contact", "cell", "kw", "aquifer", "layer", "finding", "text"}
GRID_KWS = ("SATNUM", "EQLNUM", "FIPNUM", "ROCKNUM", "PVTNUM", "PORV", "DEPTH", "PORO", "PERMX", "NTG", "SWATINIT")
CURVES = {"krw": ("swof", "sw"), "krow": ("swof", "sw"), "pcow": ("swof", "sw"), "krg": ("sgof", "sg"),
          "krog": ("sgof", "sg"), "pcog": ("sgof", "sg")}


# ---------------------------------------------------------------- loading
def _grid_from_geo(cfg, con, geo_version_id: int, res_props: list[dict], dims) -> dict | None:
    """Depths and pore volumes from the geo version (geo-modeler grids), with this version's region arrays."""
    from ..geo import review_views as gv
    try:
        g = gv.load_version(cfg, con, geo_version_id)
    except ToolError:
        return None
    ld, a, arr = g.get("layer_depths"), g.get("areal"), g["arrays"]
    if ld is None or not a or "PORO" not in arr:
        return None
    nx, ny, nz = dims
    dz = np.diff(ld, axis=0)
    out = {"DEPTH": 0.5 * (ld[1:] + ld[:-1]), "DZ": dz, "PORO": arr["PORO"],
           "PORV": a["dx"] * a["dy"] * dz * arr["PORO"] * arr.get("NTG", 1.0), "active": dz > 1e-6, "from": "geo"}
    for kw in ("NTG", "PERMX"):
        if kw in arr:
            out[kw] = arr[kw]
    out["n_active"] = int(out["active"].sum())
    out.update(_region_arrays(res_props, dims))
    return out


def _region_arrays(res_props: list[dict], dims) -> dict:
    from pathlib import Path

    from ..validators._util import read_array
    nx, ny, nz = dims
    out = {}
    for p in res_props:
        if p["keyword"] not in rq.__dict__.get("REGION_KWS", ("SATNUM", "EQLNUM", "FIPNUM", "ROCKNUM", "PVTNUM")):
            continue
        if p.get("const_value") is not None:
            out[p["keyword"]] = np.full((nz, ny, nx), float(p["const_value"]))
        elif p.get("path") and Path(p["path"]).is_file():
            v = np.asarray(read_array(p["path"]), dtype=float)
            if v.size == nx * ny * nz:
                out[p["keyword"]] = v.reshape(nz, ny, nx)
    return out


def load_version(cfg, con, version_id: int) -> dict:
    h = models.header(con, "res", version_id)
    folder = runner.qc_dir(cfg.file_store, h["model_name"], h["version"], kind="res")
    payload = folder / "payload.json"
    if payload.is_file():
        data = rq.load(payload, cfg.database, version_id)
    else:                                       # a version from a deck: the tables, no static view of its own
        t = {k: [dict(r) for r in con.execute(f"SELECT * FROM {k} WHERE version_id = ? ORDER BY {o}", (version_id,))]
             for k, o in (("swof", "satnum, sw"), ("sgof", "satnum, sg"), ("rock", "region"), ("contacts", "eqlnum"),
                          ("aquifers", "aquifer_id"), ("aquifer_connections", "aquifer_id, id"),
                          ("res_properties", "keyword"))}
        g = dict(con.execute("SELECT g.*, m.name AS model_name FROM geo_model_versions g JOIN geo_models m "
                             "ON m.id = g.geo_model_id WHERE g.id = ?", (h["geo_version_id"],)).fetchone())
        data = {"model_name": h["model_name"], "version": h["version"], "version_id": version_id, "header": h,
                "tables": t, "geo": g, "dims": (g["nx"], g["ny"], g["nz"]), "unit_system": h["unit_system"],
                "swof": pd.DataFrame(t["swof"]), "sgof": pd.DataFrame(t["sgof"]), "grid": None, "db_only": True,
                "spec": (h.get("spec_json") or {}).get("res_spec") or {}}
    data["header"] = models.header(con, "res", version_id)
    data.setdefault("spec", (data["header"].get("spec_json") or {}).get("res_spec") or {})
    props = [dict(r) for r in con.execute("SELECT p.keyword, p.const_value, f.path FROM res_properties p LEFT JOIN "
                                          "files f ON f.id = p.file_id WHERE p.version_id = ?", (version_id,))]
    if data.get("grid") is None:
        data["grid"] = _grid_from_geo(cfg, con, data["header"]["geo_version_id"], props, data["dims"])
    s = folder / "qc_summary.json"
    data["findings"] = json.loads(s.read_text()).get("findings", []) if s.is_file() else []
    data["kind"] = "res"
    data["keywords"] = [k for k in GRID_KWS if data["grid"] and k in data["grid"]]
    return data


def name(data: dict) -> str:
    return data.get("label") or f"v{data['version']}"


def satnums(data: dict) -> list[int]:
    return sorted({int(s) for s in data["swof"]["satnum"]}) if len(data["swof"]) else []


def eqlnums(data: dict) -> list[int]:
    return [int(c["eqlnum"]) for c in data["tables"]["contacts"]]


def figs(data: dict) -> tuple[str, ...]:
    out = ["kr"]
    if len(data["swof"]) and (data["swof"]["pcow"].abs() > 0).any():
        out.append("pc")
    if data["grid"]:
        out += ["regions", "contacts", "contact_section"]
    if data["tables"]["aquifers"]:
        out.append("aquifers")
    return tuple(out)


def view_args(data: dict) -> dict:
    nx, ny, nz = data["dims"]
    return {"kr": [{"name": "satnum", "choices": ["all"] + satnums(data)}],
            "pc": [{"name": "satnum", "choices": ["all"] + satnums(data)}],
            "regions": [{"name": "kw", "choices": data["keywords"]}, {"name": "layer", "min": 1, "max": nz}],
            "contacts": [], "aquifers": [],
            "contact_section": [{"name": "kw", "choices": data["keywords"]}, {"name": "along", "choices": ["x", "y"]},
                                {"name": "index", "min": 1, "max": ny}]}


def meta(data: dict) -> dict:
    nx, ny, nz = data["dims"]
    return {"kind": "res", "model_name": data["model_name"], "version": data["version"],
            "version_id": data.get("version_id"), "preview_id": data.get("preview_id"), "label": name(data),
            "db_only": bool(data.get("db_only")), "views": list(figs(data)), "view_args": view_args(data),
            "status": data["header"]["status"], "grid": [nx, ny, nz], "geo": {"version_id": data["header"]["geo_version_id"],
                                                                               "model_name": data["geo"]["model_name"],
                                                                               "version": data["geo"]["version"]},
            "grid_from": (data["grid"] or {}).get("from", "static view") if data["grid"] else None,
            "satnums": satnums(data), "eqlnums": eqlnums(data), "keywords": data["keywords"], "wells": [], "zones": [],
            "findings": data["findings"], "assumptions": data["header"].get("assumptions_json") or []}


# ---------------------------------------------------------------- specs and anchors
def _int(v, what, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ToolError(INVALID_INPUT, f"view: {what} must be an integer") from None
    if not lo <= n <= hi:
        raise ToolError(INVALID_INPUT, f"view: {what} {n} is outside {lo}..{hi}")
    return n


def check_spec(spec: dict, data: dict, base: dict | None = None) -> dict:
    if not isinstance(spec, dict) or spec.get("fig") not in figs(data):
        raise ToolError(INVALID_INPUT, f"view: 'fig' must be one of {figs(data)} for this model")
    pid = data.get("preview_id")
    fig = spec["fig"]
    out = {"fig": fig, **({"preview_id": pid} if pid else {"version_id": data["version_id"]})}
    if not pid and "version_id" in spec and int(spec["version_id"]) != data["version_id"]:
        raise ToolError(INVALID_INPUT, f"view is of version {spec['version_id']}, not {data['version_id']}")
    known = {"fig", "version_id", "preview_id"} | {
        "kr": {"satnum", "base_version_id"}, "pc": {"satnum", "base_version_id"}, "regions": {"kw", "layer"},
        "contacts": {"base_version_id"}, "aquifers": set(), "contact_section": {"kw", "along", "index"}}[fig]
    extra = set(spec) - known
    if extra:
        raise ToolError(INVALID_INPUT, f"view {fig}: unknown argument(s) {sorted(extra)}")
    nx, ny, nz = data["dims"]
    if fig in ("kr", "pc") and spec.get("satnum") not in (None, "all"):
        s = _int(spec["satnum"], "satnum", 1, 10 ** 6)
        if s not in satnums(data):
            raise ToolError(INVALID_INPUT, f"view: no SATNUM {s} ({satnums(data)})")
        out["satnum"] = s
    if fig in ("regions", "contact_section"):
        kw = spec.get("kw") or ("SATNUM" if "SATNUM" in data["keywords"] else data["keywords"][0])
        if kw not in data["keywords"]:
            raise ToolError(INVALID_INPUT, f"view: keyword {kw!r} is not in this grid ({data['keywords']})")
        out["kw"] = kw
    if fig == "regions":
        out["layer"] = _int(spec.get("layer", 1), "layer", 1, nz)
    if fig == "contact_section":
        out["along"] = spec.get("along", "x")
        if out["along"] not in ("x", "y"):
            raise ToolError(INVALID_INPUT, "view: along must be 'x' or 'y'")
        out["index"] = _int(spec.get("index", (ny if out["along"] == "x" else nx) // 2 + 1), "index", 1,
                            ny if out["along"] == "x" else nx)
    if spec.get("base_version_id") is not None:
        if base is None or int(spec["base_version_id"]) != base["version_id"] or base["model_name"] != data["model_name"]:
            raise ToolError(INVALID_INPUT, "view: base_version_id must be another version of this model")
        out["base_version_id"] = base["version_id"]
    return out


def check_anchor(anchor: dict | None) -> dict:
    a = dict(anchor or {})
    extra = set(a) - ANCHOR_KEYS
    if extra:
        raise ToolError(INVALID_INPUT, f"anchor: unknown key(s) {sorted(extra)}")
    if "cell" in a and (not isinstance(a["cell"], (list, tuple)) or len(a["cell"]) != 3
                        or not all(isinstance(x, int) and x >= 1 for x in a["cell"])):
        raise ToolError(INVALID_INPUT, "anchor: cell must be [i, j, k], 1-based integers")
    if "curve" in a and a["curve"] not in CURVES:
        raise ToolError(INVALID_INPUT, f"anchor: curve must be one of {sorted(CURVES)}")
    return a


# ---------------------------------------------------------------- figures
def _curve_traces(fig, data, satnum, base: bool, kind: str):
    import plotly.graph_objects as go
    sw, sg = data["swof"], data["sgof"]
    tag = name(data)
    for s in ([satnum] if satnum else satnums(data)):
        w = sw[sw["satnum"] == s]
        g = sg[sg["satnum"] == s] if len(sg) else sg
        pairs = ((("sw", "krw", w, 1), ("sw", "krow", w, 1), ("sg", "krg", g, 2), ("sg", "krog", g, 2)) if kind == "kr"
                 else (("sw", "pcow", w, 1),))
        for x, y, df, col in pairs:
            if not len(df):
                continue
            style = dict(dash="dot", color="#999") if base else dict(dash="dash" if y in ("krow", "krog") else "solid")
            tr = go.Scatter(x=df[x], y=df[y], mode="lines" if base else "lines+markers", line=style,
                            name=f"{y} {s}" + (f" ({tag})" if base else ""),
                            meta={"role": "skip" if base else "curve", "satnum": int(s), "curve": y},
                            hovertemplate=f"{y} SATNUM {s}" + (f" {tag}" if base else "") + "<br>%{x:.3f}: %{y:.4g}<extra></extra>")
            if kind == "kr":
                fig.add_trace(tr, 1, col)
            else:
                fig.add_trace(tr)


def figure(spec: dict, data: dict, pins=(), base: dict | None = None):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    f = spec["fig"]
    L = rq.LAYOUT
    if f == "kr":
        fig = make_subplots(rows=1, cols=2, subplot_titles=("SWOF: krw, krow against Sw", "SGOF: krg, krog against Sg"))
        if base is not None:
            _curve_traces(fig, base, spec.get("satnum"), True, "kr")
        _curve_traces(fig, data, spec.get("satnum"), False, "kr")
        fig.update_xaxes(range=[0, 1])
        fig.update_yaxes(range=[0, 1.02], title_text="kr")
        fig.update_layout(**{**L, "height": 440}, title=f"Relative permeability, {name(data)}"
                          + (f" (dotted: {name(base)})" if base is not None else ""))
    elif f == "pc":
        fig = go.Figure()
        if base is not None:
            _curve_traces(fig, base, spec.get("satnum"), True, "pc")
        _curve_traces(fig, data, spec.get("satnum"), False, "pc")
        unit = "psi" if data["unit_system"] == "FIELD" else "bar"
        fig.update_layout(**{**L, "height": 400}, title=f"Water-oil capillary pressure ({unit}), {name(data)}",
                          xaxis_title="Sw", yaxis_title=f"Pcow ({unit})")
    elif f == "regions":
        gr, k = data["grid"], spec["layer"] - 1
        z = np.where(gr["active"][k], gr[spec["kw"]][k], np.nan)
        nx, ny, _ = data["dims"]
        fig = go.Figure(go.Heatmap(x=np.arange(1, nx + 1), y=np.arange(1, ny + 1), z=z, colorscale="Viridis",
                                   colorbar=dict(title=spec["kw"]), meta={"role": "layer"},
                                   hovertemplate="i %{x} j %{y}: %{z}<extra></extra>"))
        fig.update_layout(**{**L, "height": 500}, title=f"{spec['kw']}, layer {spec['layer']}", xaxis_title="i",
                          yaxis_title="j")
        fig.update_yaxes(scaleanchor="x")
        pts = [(p["anchor"]["cell"][0], p["anchor"]["cell"][1], p) for p in pins
               if "cell" in p["anchor"] and p["anchor"]["cell"][2] == spec["layer"]]
        if pts:
            fig.add_trace(_pin_trace(pts))
    elif f == "contacts":
        fig = rq.fig_contacts(data) or go.Figure()
        cs = data["tables"]["contacts"]
        for t in fig.data:
            n = int(str(t.name or t.x[0]).split()[-1]) if (t.name or (t.x is not None and len(t.x))) else None
            if t.type == "box":
                t.meta = {"role": "eql", "eqlnum": n}
            else:
                t.meta = {"role": "contact", "eqlnum": n, "contact": (t.text or [""])[0]}
        if base is not None:
            for c in base["tables"]["contacts"]:
                x = f"EQLNUM {c['eqlnum']}"
                for nm, z in (("OWC", c["owc_depth"]), ("GOC", c["goc_depth"]), ("datum", c["datum_depth"])):
                    if z is not None and not any(c2["eqlnum"] == c["eqlnum"] and c2.get(
                            {"OWC": "owc_depth", "GOC": "goc_depth", "datum": "datum_depth"}[nm]) == z for c2 in cs):
                        fig.add_trace(go.Scatter(x=[x], y=[z], mode="markers+text", text=[f"{nm} {name(base)}"],
                                                 textposition="middle left", showlegend=False, meta={"role": "skip"},
                                                 marker=dict(symbol="line-ew-open", size=40, color="#999")))
        fig.update_layout(title=f"Cell depths per EQLNUM with the datum and contacts, {name(data)}"
                          + (f" (grey: {name(base)}'s where they differ)" if base is not None else ""))
    elif f == "contact_section":
        fig = _contact_section(spec, data, pins)
    else:
        fig = rq.fig_aquifers(data) or go.Figure()
        for t in fig.data:
            t.meta = {"role": "aquifer", "aquifer": int(str(t.name).split()[1])} if t.type == "scatter" else {"role": "skip"}
    return fig


def _pin_trace(points):
    import plotly.graph_objects as go
    return go.Scatter(x=[p[0] for p in points], y=[p[1] for p in points], mode="markers+text",
                      text=[f"#{p[2]['id']}" for p in points], textposition="top right",
                      textfont=dict(color="#d62728", shadow="1px 1px 2px white"),
                      marker=dict(symbol="x-thin", size=14, color="#d62728", line=dict(width=3, color="#d62728")),
                      customdata=[p[2]["comment"][:80] for p in points], showlegend=False, meta={"role": "pins"},
                      hovertemplate="%{text}: %{customdata}<extra></extra>")


def _contact_section(spec, data, pins):
    """One row (along x) or column (along y): each active cell at its centre depth, coloured by kw; the contacts of
    each EQLNUM in the section as lines."""
    import plotly.graph_objects as go
    gr = data["grid"]
    idx = spec["index"] - 1
    if spec["along"] == "x":
        sl = np.s_[:, idx, :]
        pos = lambda kk, n: n + 1  # noqa: E731
    else:
        sl = np.s_[:, :, idx]
        pos = lambda kk, n: n + 1  # noqa: E731
    act, dep, val = gr["active"][sl], gr["DEPTH"][sl], gr[spec["kw"]][sl]
    kk, nn = np.nonzero(act)
    fig = go.Figure(go.Scatter(x=nn + 1, y=dep[kk, nn], mode="markers", marker=dict(
        symbol="square", size=10, color=val[kk, nn], colorscale="Viridis", colorbar=dict(title=spec["kw"])),
        customdata=np.stack([kk + 1, val[kk, nn]], 1), meta={"role": "section_cell"},
        hovertemplate=("i" if spec["along"] == "x" else "j") + " %{x}, layer %{customdata[0]}<br>depth %{y:.1f}<br>"
                      + spec["kw"] + " %{customdata[1]:.4g}<extra></extra>"))
    eql = gr["EQLNUM"][sl] if "EQLNUM" in gr else None
    for c in data["tables"]["contacts"]:
        if eql is not None and not (eql[act] == c["eqlnum"]).any():
            continue
        for nm, key, colour in (("OWC", "owc_depth", "#1f77b4"), ("GOC", "goc_depth", "#d62728")):
            if c.get(key) is not None:
                fig.add_hline(y=c[key], line=dict(color=colour, dash="dash"), annotation_text=f"{nm} (EQLNUM {c['eqlnum']})")
    pts = [(p["anchor"]["cell"][0] if spec["along"] == "x" else p["anchor"]["cell"][1],
            float(dep[p["anchor"]["cell"][2] - 1, (p["anchor"]["cell"][0] if spec["along"] == "x" else p["anchor"]["cell"][1]) - 1]), p)
           for p in pins if "cell" in p["anchor"]
           and p["anchor"]["cell"][1 if spec["along"] == "x" else 0] == spec["index"]]
    if pts:
        fig.add_trace(_pin_trace(pts))
    fig.update_yaxes(autorange="reversed", title="depth (m)")
    fig.update_xaxes(title="i" if spec["along"] == "x" else "j")
    fig.update_layout(**{**rq.LAYOUT, "height": 460}, title=f"{spec['kw']} along {spec['along']} "
                      f"({'row j' if spec['along'] == 'x' else 'column i'} {spec['index']}) at cell depths, with the contacts")
    return fig


def locate(spec: dict, data: dict, role, point: dict) -> dict:
    text = point.get("text")
    if role == "pins" and text:
        return {"comment": int(str(text).lstrip("#"))}
    if role == "curve":
        return {"satnum": int(point["satnum"]), "curve": point["curve"], "s": round(float(point.get("x", 0)), 4)}
    if role == "eql" and point.get("eqlnum"):
        return {"eqlnum": int(point["eqlnum"])}
    if role == "contact" and point.get("eqlnum"):
        return {"eqlnum": int(point["eqlnum"]), "contact": point.get("contact") or "OWC"}
    if role == "aquifer":
        return {"aquifer": int(point["aquifer"])}
    nx, ny, nz = data["dims"]
    if role == "layer":
        i, j = int(round(float(point["x"]))), int(round(float(point["y"])))
        return {"cell": [min(max(i, 1), nx), min(max(j, 1), ny), spec["layer"]], "kw": spec["kw"]}
    if role == "section_cell":
        n = int(round(float(point["x"])))
        k = int(point["customdata"][0]) if point.get("customdata") else 1
        cell = [n, spec["index"], k] if spec["along"] == "x" else [spec["index"], n, k]
        return {"cell": cell, "kw": spec["kw"]}
    return {}


# ---------------------------------------------------------------- what an anchor or view points at
def _r(v, n=4):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return v
    return None if not np.isfinite(v) else float(f"{v:.{n}g}")


def intervals(data: dict) -> dict:
    """Per EQLNUM: pore volume in the gas cap (above the GOC), the oil column (GOC to OWC) and the water (below)."""
    gr = data["grid"]
    out = {}
    if not gr or "DEPTH" not in gr:
        return out
    for c in data["tables"]["contacts"]:
        m = gr["active"] & ((gr["EQLNUM"] == c["eqlnum"]) if "EQLNUM" in gr else True)
        d, pv = gr["DEPTH"][m], np.nan_to_num(gr["PORV"][m])
        owc = c["owc_depth"] if c["owc_depth"] is not None else np.inf
        goc = c["goc_depth"] if c["goc_depth"] is not None else -np.inf
        out[int(c["eqlnum"])] = {"pv_gas": float(pv[d < goc].sum()), "pv_oil": float(pv[(d >= goc) & (d < owc)].sum()),
                                 "pv_water": float(pv[d >= owc].sum()), "cells": int(m.sum())}
    return out


def describe(anchor: dict, data: dict) -> dict:
    a, out = anchor or {}, {}
    from ..core.review import label
    out["label"] = label(a)
    if "satnum" in a:
        ep = rq.end_points(data)
        row = ep[ep["satnum"] == a["satnum"]]
        out["satnum"] = {k: _r(v) for k, v in (row.iloc[0].to_dict() if len(row) else {}).items()}
        if "curve" in a and "s" in a:
            tbl, x = CURVES[a["curve"]]
            df = data[tbl][data[tbl]["satnum"] == a["satnum"]].sort_values(x)
            if len(df):
                out["satnum"][f"{a['curve']}_at_{x}"] = _r(np.interp(a["s"], df[x], df[a["curve"]]))
    if "eqlnum" in a:
        c = next((c for c in data["tables"]["contacts"] if c["eqlnum"] == a["eqlnum"]), None)
        out["eqlnum"] = {**(c or {}), **{k: _r(v, 6) for k, v in intervals(data).get(a["eqlnum"], {}).items()}}
    if "cell" in a and data["grid"]:
        gr = data["grid"]
        i, j, k = (x - 1 for x in a["cell"])
        nx, ny, nz = data["dims"]
        if not (i < nx and j < ny and k < nz):
            raise ToolError(INVALID_INPUT, f"anchor cell {a['cell']} is outside the grid {nx}x{ny}x{nz}")
        vals = {kw: _r(gr[kw][k, j, i]) for kw in data["keywords"]}
        cell = {"ijk": a["cell"], "active": bool(gr["active"][k, j, i]), "values": vals}
        eq = int(vals.get("EQLNUM") or 1)
        c = next((c for c in data["tables"]["contacts"] if c["eqlnum"] == eq), None)
        if c and vals.get("DEPTH") is not None:
            d = vals["DEPTH"]
            cell["interval"] = ("gas cap" if c["goc_depth"] is not None and d < c["goc_depth"] else
                                "water" if c["owc_depth"] is not None and d >= c["owc_depth"] else "oil column")
            cell["contacts"] = {k: c[k] for k in ("eqlnum", "owc_depth", "goc_depth", "datum_depth")}
        out["cell"] = cell
    if "aquifer" in a:
        aq = next((x for x in data["tables"]["aquifers"] if x["aquifer_id"] == a["aquifer"]), None)
        out["aquifer"] = {**(aq or {}), "connections": sum(1 for c in data["tables"]["aquifer_connections"]
                                                           if c["aquifer_id"] == a["aquifer"])}
    if "finding" in a:
        out["finding"] = next((f for f in data["findings"] if f"{f['area']}: {f['check']}" == a["finding"]), None)
    return out


def summarize(spec: dict, data: dict, base: dict | None = None) -> dict:
    f = spec["fig"]
    out = {"view": spec}
    if f in ("kr", "pc"):
        ep = rq.end_points(data)
        out["shows"] = ("relative permeability (SWOF, SGOF)" if f == "kr" else "water-oil capillary pressure") + \
            (f" for SATNUM {spec['satnum']}" if spec.get("satnum") else " per SATNUM")
        out["end_points"] = [{k: _r(v) for k, v in r.items()} for r in ep.to_dict("records")]
        if base is not None:
            out["base_end_points"] = [{k: _r(v) for k, v in r.items()} for r in rq.end_points(base).to_dict("records")]
    elif f == "regions":
        gr = data["grid"]
        lay = gr[spec["kw"]][spec["layer"] - 1][gr["active"][spec["layer"] - 1]]
        out["shows"] = f"{spec['kw']} on layer {spec['layer']}"
        vals, counts = np.unique(np.round(lay, 6), return_counts=True)
        out["values"] = {str(_r(v)): int(n) for v, n in list(zip(vals, counts))[:20]}
    elif f in ("contacts", "contact_section"):
        out["shows"] = "cell depths against the datum and contacts per EQLNUM"
        out["contacts"] = data["tables"]["contacts"]
        out["pore_volumes"] = {k: {kk: _r(vv, 6) for kk, vv in v.items()} for k, v in intervals(data).items()}
        if base is not None:
            out["base_contacts"] = base["tables"]["contacts"]
    else:
        out["shows"] = "aquifer connections on a plan of the grid"
        out["aquifers"] = data["tables"]["aquifers"]
    return out


# ---------------------------------------------------------------- the version diff
def metrics(data: dict) -> dict:
    out = {}
    for e, v in intervals(data).items():
        for k in ("pv_gas", "pv_oil", "pv_water"):
            out[f"eqlnum.{e}.{k}"] = v[k]
    for c in data["tables"]["contacts"]:
        for k in ("datum_depth", "datum_pressure", "owc_depth", "goc_depth"):
            out[f"eqlnum.{c['eqlnum']}.{k}"] = c.get(k)
    ep = rq.end_points(data)
    for r in ep.to_dict("records"):
        for k in ("swc", "sorw", "krw_at_sorw", "mobile_oil", "kro_swof", "sgc", "sorg", "pc_max"):
            if k in r and r[k] is not None:
                out[f"satnum.{r['satnum']}.{k}"] = r[k]
    for r in data["tables"]["rock"]:
        out[f"rock.{r['region']}.cr"] = r["cr"]
    out["aquifers"] = len(data["tables"]["aquifers"])
    gr = data["grid"]
    for kw in ("SATNUM", "EQLNUM", "FIPNUM"):
        if gr and kw in gr:
            out[f"{kw}.regions"] = int(len(np.unique(gr[kw][gr["active"]])))
    return out


def comparable(new: dict, old: dict):
    return None


def diff_extra(new: dict, old: dict) -> dict:
    return {}
