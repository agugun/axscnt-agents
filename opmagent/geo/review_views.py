"""Geo review: view specs, anchors and what they point at (docs/design/geo-review.md, sections 1 and 2).

A **view spec** names a figure from a fixed registry and its arguments, e.g.
  {"fig": "layer_map", "version_id": 42, "kw": "PORO", "layer": 5}
The workbench draws it with the geo QC library (the same pictures as the QC notebook). A comment stores the
spec of the view it was made on, so the agent sees what the engineer saw. A spec is data, never code: only
registered figures with checked arguments can be named.

An **anchor** is what a comment is about, from a click on a view:
  {"well": "W2"}                      a well
  {"cell": [i, j, k], "kw": "PORO"}  a cell (1-based, as in the deck), with "well" if it is a well's column
  {"layer": 5} / {"zone": "Lower"}   a layer or a zone
  {"surface": "TopRes"}              a surface; with "well" for that well's top, "column": [i, j] for a place
  {"finding": "<area>: <check>"}     a QC finding
  {}                                 the model in general
It is stored in review_feedback.item as JSON {"anchor": ..., "view": ...}. An older free-text item reads as
{"text": item}.

Everything here reads the version as stored (geo_qc_lib.load): it never changes a model.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..core import models
from ..core.review import label, make_item, parse_item  # noqa: F401  (re-exported)
from ..core.errors import INVALID_INPUT, NOT_FOUND, ToolError
from ..notebooks import geo_qc_lib as gq
from ..notebooks import runner

FIGS = ("surfaces", "layer_map", "section", "well", "at_wells", "distributions", "poroperm")
ANCHOR_KEYS = {"well", "cell", "kw", "layer", "zone", "surface", "column", "finding", "text"}


# ---------------------------------------------------------------- loading
def qc_folder(cfg, con, version_id: int) -> Path:
    h = models.header(con, "geo", version_id)
    return runner.qc_dir(cfg.file_store, h["model_name"], h["version"], kind="geo")


def load_version(cfg, con, version_id: int) -> dict:
    """A geo version as the QC notebook reads it, plus its QC findings. Needs the QC payload (a version built from
    tops and logs); a version from a deck has none yet."""
    folder = qc_folder(cfg, con, version_id)
    payload = folder / "payload.json"
    if not payload.is_file():
        raise ToolError(NOT_FOUND, f"geo version {version_id} has no QC payload ({payload}); the review workbench "
                                   "needs a version built from tops and logs")
    data = gq.load(payload, cfg.database, version_id)
    s = folder / "qc_summary.json"
    data["findings"] = json.loads(s.read_text()).get("findings", []) if s.is_file() else []
    data["keywords"] = sorted(k for k, a in data["arrays"].items() if np.ndim(a) == 3)
    return data


def zones(data: dict) -> list[dict]:
    """Zone name (its top surface), and its layers, 1-based inclusive."""
    out, k = [], 1
    for s, n in zip(data["spec"]["surfaces"][:-1], data["spec"]["layers"]):
        out.append({"zone": s, "first": k, "last": k + n - 1})
        k += n
    return out


def zone_of(data: dict, layer: int) -> str | None:
    return next((z["zone"] for z in zones(data) if z["first"] <= layer <= z["last"]), None)


def meta(data: dict) -> dict:
    """What the workbench needs to offer choices: wells, surfaces, keywords, layers, zones, findings."""
    wc = gq.well_cells(data)
    return {"model_name": data["model_name"], "version": data["version"], "version_id": data["version_id"],
            "status": data["header"]["status"], "grid": [data["nx"], data["ny"], data["nz"]], "areal": data["areal"],
            "wells": [str(w) for w in wc["well_name"]], "surfaces": list(data["spec"]["surfaces"]),
            "keywords": data["keywords"], "zones": zones(data), "findings": data["findings"],
            "assumptions": data.get("assumptions", [])}


# ---------------------------------------------------------------- view specs
def _int(v, name, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ToolError(INVALID_INPUT, f"view: {name} must be an integer") from None
    if not lo <= n <= hi:
        raise ToolError(INVALID_INPUT, f"view: {name} {n} is outside {lo}..{hi}")
    return n


def check_spec(spec: dict, data: dict) -> dict:
    """The spec with its arguments checked against the model and the defaults filled in."""
    if not isinstance(spec, dict) or spec.get("fig") not in FIGS:
        raise ToolError(INVALID_INPUT, f"view: 'fig' must be one of {FIGS}")
    fig, out = spec["fig"], {"fig": spec["fig"], "version_id": data["version_id"]}
    if "version_id" in spec and int(spec["version_id"]) != data["version_id"]:
        raise ToolError(INVALID_INPUT, f"view is of version {spec['version_id']}, not {data['version_id']}")
    known = {"fig", "version_id"} | {"surfaces": set(), "layer_map": {"kw", "layer", "log"},
                                     "section": {"kw", "along", "well", "cell", "log"}, "well": {"well"},
                                     "at_wells": set(), "distributions": set(), "poroperm": set()}[fig]
    extra = set(spec) - known
    if extra:
        raise ToolError(INVALID_INPUT, f"view {fig}: unknown argument(s) {sorted(extra)}")
    wells = set(meta(data)["wells"])
    if fig in ("layer_map", "section"):
        kw = spec.get("kw", "PORO")
        if kw not in data["keywords"]:
            raise ToolError(INVALID_INPUT, f"view: keyword {kw!r} is not in this model ({data['keywords']})")
        out["kw"] = kw
        out["log"] = bool(spec.get("log", kw.startswith("PERM")))
    if fig == "layer_map":
        out["layer"] = _int(spec.get("layer", 1), "layer", 1, data["nz"])
    if fig == "section":
        out["along"] = spec.get("along", "x")
        if out["along"] not in ("x", "y"):
            raise ToolError(INVALID_INPUT, "view: along must be 'x' or 'y'")
        if spec.get("cell") is not None:
            c = spec["cell"]
            if not isinstance(c, (list, tuple)) or len(c) < 2:
                raise ToolError(INVALID_INPUT, "view: cell must be [i, j] (1-based)")
            out["cell"] = [_int(c[0], "cell i", 1, data["nx"]), _int(c[1], "cell j", 1, data["ny"])]
        else:
            w = spec.get("well") or (sorted(wells)[0] if wells else None)
            if w is not None and str(w) not in wells:
                raise ToolError(INVALID_INPUT, f"view: no well {w!r} in this model")
            out["well"] = None if w is None else str(w)
    if fig == "well":
        w = spec.get("well") or (sorted(wells)[0] if wells else None)
        if w is None or str(w) not in wells:
            raise ToolError(INVALID_INPUT, f"view: no well {w!r} in this model")
        out["well"] = str(w)
    return out


def check_anchor(anchor: dict | None) -> dict:
    a = dict(anchor or {})
    extra = set(a) - ANCHOR_KEYS
    if extra:
        raise ToolError(INVALID_INPUT, f"anchor: unknown key(s) {sorted(extra)}")
    if "cell" in a:
        c = a["cell"]
        if not isinstance(c, (list, tuple)) or len(c) != 3 or not all(isinstance(x, int) and x >= 1 for x in c):
            raise ToolError(INVALID_INPUT, "anchor: cell must be [i, j, k], 1-based integers")
    return a


# ---------------------------------------------------------------- figures
def _xy_to_ij(data, x, y):
    a = data["areal"]
    i = int(np.clip((float(x) - a["x0"]) // a["dx"], 0, data["nx"] - 1))
    j = int(np.clip((float(y) - a["y0"]) // a["dy"], 0, data["ny"] - 1))
    return i, j


def _well_at(data, i, j):
    wc = gq.well_cells(data)
    m = wc[(wc["i"] == i) & (wc["j"] == j)]
    return str(m["well_name"].iloc[0]) if len(m) else None


def _pins_layer_map(data, spec, pins):
    xs, ys = gq.cell_centres(data)
    wc = gq.well_cells(data).set_index("well_name")
    out = []
    for p in pins:
        a = p["anchor"]
        if "cell" in a and a["cell"][2] == spec["layer"]:
            out.append((xs[a["cell"][0] - 1], ys[a["cell"][1] - 1], p))
        elif "column" in a:
            out.append((xs[a["column"][0] - 1], ys[a["column"][1] - 1], p))
        elif "well" in a and "cell" not in a and str(a["well"]) in wc.index and a.get("layer") in (None, spec["layer"]):
            out.append((float(wc.loc[str(a["well"]), "x"]), float(wc.loc[str(a["well"]), "y"]), p))
    return out


def _pins_section(data, spec, i, j, pins):
    xs, ys = gq.cell_centres(data)
    ld = data["layer_depths"]
    out = []
    for p in pins:
        c = p["anchor"].get("cell")
        if not c:
            continue
        ci, cj, ck = c[0] - 1, c[1] - 1, c[2] - 1
        if (spec["along"] == "x" and cj == j) or (spec["along"] == "y" and ci == i):
            z = 0.5 * (ld[ck, cj, ci] + ld[ck + 1, cj, ci])
            out.append((xs[ci] if spec["along"] == "x" else ys[cj], float(z), p))
    return out


def _pin_trace(points):
    import plotly.graph_objects as go
    return go.Scatter(x=[p[0] for p in points], y=[p[1] for p in points], mode="markers+text",
                      text=[f"#{p[2]['id']}" for p in points], textposition="top right",
                      textfont=dict(color="#d62728", shadow="1px 1px 2px white"), marker=dict(symbol="x-thin", size=14, color="#d62728",
                                                                     line=dict(width=3, color="#d62728")),
                      customdata=[p[2]["comment"][:80] for p in points], showlegend=False, meta={"role": "pins"},
                      hovertemplate="%{text}: %{customdata}<extra></extra>")


def _section_ij(data, spec):
    if "cell" in spec:
        return spec["cell"][0] - 1, spec["cell"][1] - 1
    wc = gq.well_cells(data).set_index("well_name")
    if spec.get("well") is None:
        return data["nx"] // 2, data["ny"] // 2
    return int(wc.loc[spec["well"], "i"]), int(wc.loc[spec["well"], "j"])


def figure(spec: dict, data: dict, pins: list[dict] = ()):
    """The plotly figure for a checked spec. Each trace has meta.role, which locate() turns a click into an anchor
    with. pins: comments on this version (with 'anchor'), drawn where their anchor is on this view."""
    import plotly.graph_objects as go
    fig_name = spec["fig"]
    if fig_name == "surfaces":
        fig = gq.fig_surfaces(data)
        fig.update_annotations(font_size=11)
        fig.update_layout(height=420, title_font_size=14)
        for n, t in enumerate(fig.data):
            s = data["spec"]["surfaces"][n // 2]
            t.meta = {"role": "surface" if n % 2 == 0 else "tops", "surface": s}
    elif fig_name == "layer_map":
        a, k = data["arrays"][spec["kw"]], spec["layer"] - 1
        xs, ys = gq.cell_centres(data)
        z = np.log10(np.clip(a[k], 1e-6, None)) if spec["log"] else a[k]
        fig = go.Figure(go.Heatmap(x=xs, y=ys, z=z, colorscale="Viridis", meta={"role": "layer"},
                                   colorbar=dict(title=("log10 " if spec["log"] else "") + spec["kw"]),
                                   hovertemplate="x %{x:.0f}<br>y %{y:.0f}<br>%{z:.4g}<extra></extra>"))
        wc = gq.well_cells(data)
        fig.add_trace(go.Scatter(x=wc["x"], y=wc["y"], mode="markers+text", text=wc["well_name"].astype(str),
                                 textposition="top center", meta={"role": "wells"}, showlegend=False,
                                 textfont=dict(color="white", size=13, shadow="1px 1px 2px black"),
                                 marker=dict(color="white", size=9, line=dict(color="black", width=1)),
                                 hovertemplate="%{text}<extra></extra>"))
        z_name = zone_of(data, spec["layer"])
        fig.update_layout(**{**gq.LAYOUT, "height": 520},
                          title=f"{spec['kw']} layer {spec['layer']} of {data['nz']}" + (f" (zone {z_name})" if z_name else ""))
        fig.update_yaxes(scaleanchor="x", scaleratio=1)
        pts = _pins_layer_map(data, spec, pins)
        if pts:
            fig.add_trace(_pin_trace(pts))
    elif fig_name == "section":
        i, j = _section_ij(data, spec)
        fig = gq.fig_section(data, spec["kw"], spec.get("well"), spec["along"], spec["log"], cell=(i, j))
        if spec.get("well"):
            fig.update_layout(title=fig.layout.title.text.replace(f"cell {i + 1}, {j + 1}", spec["well"]))
        for n, t in enumerate(fig.data):
            t.meta = {"role": "section"} if n == 0 else {"role": "skip"}
        pts = _pins_section(data, spec, i, j, pins)
        if pts:
            fig.add_trace(_pin_trace(pts))
    elif fig_name == "well":
        fig = gq.fig_well(data, spec["well"])
        cols = {"x": "PORO", "x2": "PERMX", "x3": "NTG"}
        for t in fig.data:
            t.meta = {"role": "well_track", "kw": cols.get(t.xaxis or "x", "PORO")}
    elif fig_name == "at_wells":
        fig = gq.fig_at_wells(data)
        for t in fig.data:
            t.meta = {"role": "at_wells" if t.mode == "markers" else "skip"}
    elif fig_name == "distributions":
        fig = gq.fig_distributions(data)
    else:
        fig = gq.fig_poroperm(data)
    return fig


def locate(spec: dict, data: dict, role: str | None, point: dict) -> dict:
    """The anchor for a click: role is the clicked trace's meta.role, point has x, y and text (plotly's)."""
    x, y, text = point.get("x"), point.get("y"), point.get("text")
    fig = spec["fig"]
    if role == "pins" and text:
        return {"comment": int(str(text).lstrip("#"))}
    if role in ("wells", "tops") and text:
        return {"well": str(text), **({"surface": point["surface"]} if role == "tops" and point.get("surface") else {})}
    if role == "surface" and x is not None:
        i, j = _xy_to_ij(data, x, y)
        return {"surface": point.get("surface"), "column": [i + 1, j + 1]}
    if role == "layer" and x is not None:
        i, j = _xy_to_ij(data, x, y)
        a = {"cell": [i + 1, j + 1, spec["layer"]], "kw": spec["kw"]}
        w = _well_at(data, i, j)
        return {**a, "well": w} if w else a
    if role == "section" and x is not None:
        si, sj = _section_ij(data, spec)
        if spec["along"] == "x":
            i, j = _xy_to_ij(data, x, data["areal"]["y0"] + (sj + 0.5) * data["areal"]["dy"])
        else:
            i, j = _xy_to_ij(data, data["areal"]["x0"] + (si + 0.5) * data["areal"]["dx"], x)
        col = data["layer_depths"][:, j, i]
        k = int(np.searchsorted(col, float(y), side="right")) - 1
        if not 0 <= k < data["nz"]:
            return {"column": [i + 1, j + 1]}
        a = {"cell": [i + 1, j + 1, k + 1], "kw": spec["kw"]}
        w = _well_at(data, i, j)
        return {**a, "well": w} if w else a
    if role == "well_track" and y is not None:
        wc = gq.well_cells(data).set_index("well_name")
        i, j = int(wc.loc[spec["well"], "i"]), int(wc.loc[spec["well"], "j"])
        k = int(np.searchsorted(data["layer_depths"][:, j, i], float(y), side="right")) - 1
        a = {"well": spec["well"], "kw": point.get("kw", "PORO")}
        return {**a, "layer": k + 1} if 0 <= k < data["nz"] else a
    if role == "at_wells" and text:
        w, _, lay = str(text).rpartition(" L")
        return {"well": w, "layer": int(lay)} if lay.isdigit() else {"well": str(text)}
    if fig == "well":
        return {"well": spec["well"]}
    return {}


# ---------------------------------------------------------------- what an anchor or a view points at (for the agent)
def _r(v, n=4):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return v
    return None if not np.isfinite(v) else float(f"{v:.{n}g}")


def describe(anchor: dict, data: dict) -> dict:
    """The model's values at an anchor, so the agent can reason about the place the engineer pointed at."""
    a, out = anchor or {}, {"label": label(anchor)}
    arr, ld = data["arrays"], data["layer_depths"]
    wc = gq.well_cells(data).set_index("well_name")
    if "cell" in a:
        i, j, k = (c - 1 for c in a["cell"])
        if not (i < data["nx"] and j < data["ny"] and k < data["nz"]):
            raise ToolError(INVALID_INPUT, f"anchor cell {a['cell']} is outside the grid {data['nx']}x{data['ny']}x{data['nz']}")
        xs, ys = gq.cell_centres(data)
        d = np.hypot(wc["x"] - xs[i], wc["y"] - ys[j]) if len(wc) else None
        out["cell"] = {"ijk": a["cell"], "x": _r(xs[i], 7), "y": _r(ys[j], 7), "zone": zone_of(data, k + 1),
                       "top_tvd": _r(ld[k, j, i], 6), "base_tvd": _r(ld[k + 1, j, i], 6),
                       "values": {kw: _r(arr[kw][k, j, i]) for kw in data["keywords"]}}
        if d is not None:
            out["cell"]["nearest_well"] = {"well": str(d.idxmin()), "distance_m": _r(d.min())}
        lay = data["arrays"][a.get("kw", "PORO")][k] if a.get("kw", "PORO") in arr else None
        if lay is not None:
            out["cell"]["layer_range"] = {"kw": a.get("kw", "PORO"), "min": _r(lay.min()), "mean": _r(lay.mean()),
                                          "max": _r(lay.max())}
    if "well" in a and str(a["well"]) in wc.index:
        w = str(a["well"])
        aw = gq.at_wells(data)
        aw = aw[aw["well_name"].astype(str) == w] if len(aw) else aw
        if "layer" in a and len(aw):
            aw = aw[aw["layer"] == int(a["layer"])]
        cols = ["layer", "phi_log", "phi_model", "perm_log", "perm_model", "ntg_log", "ntg_model"]
        out["well"] = {"well": w, "column": [int(wc.loc[w, "i"]) + 1, int(wc.loc[w, "j"]) + 1],
                       "at_layers": [{c: _r(r[c]) if c != "layer" else int(r[c]) for c in cols} for _, r in aw.iterrows()]}
        mf = gq.tops_misfit(data)
        mf = mf[mf["well_name"].astype(str) == w]
        if "surface" in a:
            mf = mf[mf["surface"] == a["surface"]]
        out["well"]["tops_misfit_m"] = {r["surface"]: _r(r["misfit"]) for _, r in mf.iterrows()}
    if "layer" in a and "cell" not in a:
        k = int(a["layer"]) - 1
        out["layer"] = {"layer": k + 1, "zone": zone_of(data, k + 1),
                        "stats": {kw: {"min": _r(arr[kw][k].min()), "mean": _r(arr[kw][k].mean()),
                                       "max": _r(arr[kw][k].max())} for kw in data["keywords"]}}
    if "zone" in a:
        z = next((z for z in zones(data) if z["zone"] == a["zone"]), None)
        if z:
            ks = slice(z["first"] - 1, z["last"])
            out["zone"] = {**z, "mean": {kw: _r(arr[kw][ks].mean()) for kw in data["keywords"]}}
    if "surface" in a:
        mf = gq.tops_misfit(data)
        mf = mf[mf["surface"] == a["surface"]]
        out["surface"] = {"surface": a["surface"], "misfit_m": {str(r["well_name"]): _r(r["misfit"]) for _, r in mf.iterrows()}}
        if "column" in a:
            ci, cj = a["column"][0] - 1, a["column"][1] - 1
            out["surface"]["tvd_at_column"] = _r(ld[gq.zone_index(data)[a["surface"]], cj, ci], 6)
    if "finding" in a:
        out["finding"] = next((f for f in data["findings"] if f"{f['area']}: {f['check']}" == a["finding"]), None)
    return out


def summarize(spec: dict, data: dict) -> dict:
    """In words and numbers, what a view shows (the agent can't see the picture)."""
    f, arr = spec["fig"], data["arrays"]
    out = {"view": spec}
    if f == "layer_map":
        lay = arr[spec["kw"]][spec["layer"] - 1]
        out["shows"] = f"{spec['kw']} map of layer {spec['layer']} (zone {zone_of(data, spec['layer'])})"
        out["range"] = {"min": _r(lay.min()), "mean": _r(lay.mean()), "max": _r(lay.max())}
        wc = gq.well_cells(data)
        out["at_wells"] = {str(r.well_name): _r(lay[r.j, r.i]) for r in wc.itertuples()}
    elif f == "section":
        i, j = _section_ij(data, spec)
        sl = arr[spec["kw"]][:, j, :] if spec["along"] == "x" else arr[spec["kw"]][:, :, i]
        out["shows"] = (f"{spec['kw']} section along {spec['along']} through "
                        f"{spec.get('well') or f'cell {i + 1},{j + 1}'} ({'row j' if spec['along'] == 'x' else 'column i'} "
                        f"{(j if spec['along'] == 'x' else i) + 1})")
        out["range"] = {"min": _r(sl.min()), "mean": _r(sl.mean()), "max": _r(sl.max())}
        out["by_layer_mean"] = [_r(v) for v in sl.mean(axis=1)]
    elif f == "well":
        out["shows"] = f"well {spec['well']}: logs, upscaled values per layer and the model column"
        out.update(describe({"well": spec["well"]}, data))
    elif f == "surfaces":
        mf = gq.tops_misfit(data)
        out["shows"] = "surface depth maps with the well tops"
        out["misfit_m"] = {s: {str(r["well_name"]): _r(r["misfit"]) for _, r in mf[mf["surface"] == s].iterrows()}
                           for s in data["spec"]["surfaces"]}
    elif f == "at_wells":
        aw = gq.at_wells(data)
        out["shows"] = "model porosity against the upscaled logs at each well and layer, and leave-one-well-out"
        out["rms_d_phi"] = _r(np.sqrt((aw["d_phi"] ** 2).mean())) if len(aw) else None
        out["worst"] = [{"well": str(r.well_name), "layer": int(r.layer), "d_phi": _r(r.d_phi)}
                        for r in aw.reindex(aw["d_phi"].abs().sort_values(ascending=False).index).head(5).itertuples()] if len(aw) else []
    elif f == "distributions":
        out["shows"] = "porosity and log10 permeability histograms, model cells against log samples"
        out["model_mean_phi"] = _r(arr["PORO"].mean())
    else:
        out["shows"] = "porosity against permeability: model cells, log samples and upscaled values"
    return out
