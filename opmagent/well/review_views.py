"""Well review: views, anchors and what they point at (the review workbench, phase 4; see opmagent/reviews.py).

Reviewed only: a well model is built from data (wells, surveys, perforations, history, controls), so a change is a
data edit (the data manager's, through a study request), not a proposal.

Views (the well QC library's, on the model as stored):
  plan              the well paths over the top layer's porosity, perforated parts thick
  well_section      one well's path (TVD against displacement) with its perforations and the geo zones
  perforated_cells  every perforated cell by well and layer, sized by length, coloured by PERMX
  history           one well's monthly rates as the deck applies them, with the measured averages
  cumulative        field cumulative volumes, deck against measured
  schedule          per well: open and shut periods, history and prediction, perforation dates

Anchors: {"well": "P1"}, {"well": "P1", "md": 2310.5}, {"well": "P1", "perforation": 12},
{"well": "P1", "cell": [i, j, k]}, {"well": "P1", "date": "2019-04-01", "quantity": "oil_rate"}, {"date": ...},
{"finding": ...}, {} (general).
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from ..core import models
from ..core.errors import INVALID_INPUT, ToolError
from ..notebooks import runner
from ..notebooks import well_qc_lib as wq

FIGS = ("plan", "well_section", "perforated_cells", "history", "cumulative", "schedule")
ANCHOR_KEYS = {"well", "md", "perforation", "cell", "date", "quantity", "finding", "text"}


def load_version(cfg, con, version_id: int) -> dict:
    h = models.header(con, "well", version_id)
    folder = runner.qc_dir(cfg.file_store, h["model_name"], h["version"], kind="well")
    payload = folder / "payload.json"
    if payload.is_file():
        data = wq.load(payload, cfg.database, version_id)
    else:                                       # a version from a deck: the tables, no measured history
        t = {k: [dict(r) for r in con.execute(f"SELECT * FROM {k} WHERE version_id = ? ORDER BY {o}", (version_id,))]
             for k, o in (("wells", "well_name"), ("well_groups", "group_name"), ("well_trajectory", "well_name, md"),
                          ("well_completions", "well_name, event_date, id"), ("well_events", "well_name, event_date, mode"))}
        data = {"model_name": h["model_name"], "version": h["version"], "version_id": version_id, "tables": t,
                "geo": wq.load_geo(con, h["geo_version_id"]) if h["geo_version_id"] else None,
                "wells": pd.DataFrame(t["wells"]), "trajectory": pd.DataFrame(t["well_trajectory"]),
                "completions": pd.DataFrame(t["well_completions"]), "events": pd.DataFrame(t["well_events"]),
                "measured": pd.DataFrame(), "db_only": True}
    data["header"] = models.header(con, "well", version_id)
    data["spec"] = {}                          # built from data, not a spec: no spec changes to show
    s = folder / "qc_summary.json"
    data["findings"] = json.loads(s.read_text()).get("findings", []) if s.is_file() else []
    data["kind"] = "well"
    return data


def name(data: dict) -> str:
    return f"v{data['version']}"


def wells(data: dict) -> list[str]:
    return sorted(str(w) for w in data["wells"]["well_name"]) if len(data["wells"]) else []


def figs(data: dict) -> tuple[str, ...]:
    """Paths need surveys (a model from a deck has cell completions, no trajectory); history needs events."""
    out = []
    if len(data["trajectory"]):
        out += ["plan", "well_section"]
        if data["geo"] is not None and not wq.place(data).empty:
            out.append("perforated_cells")
    if len(data["events"]):
        out += ["schedule", "history", "cumulative"]
    return tuple(out)


def view_args(data: dict) -> dict:
    w = wells(data)
    return {"plan": [], "perforated_cells": [], "cumulative": [], "schedule": [],
            "well_section": [{"name": "well", "choices": w}], "history": [{"name": "well", "choices": w}]}


def meta(data: dict) -> dict:
    g = data["geo"]
    return {"kind": "well", "model_name": data["model_name"], "version": data["version"], "version_id": data["version_id"],
            "label": name(data), "db_only": bool(data.get("db_only")), "views": list(figs(data)),
            "view_args": view_args(data), "status": data["header"]["status"], "wells": wells(data),
            "geo": {"version_id": g["version_id"], "model_name": g["model_name"], "version": g["version"]} if g else None,
            "start_date": data["header"]["start_date"], "keywords": [], "zones": [], "findings": data["findings"],
            "assumptions": data["header"].get("assumptions_json") or []}


def check_spec(spec: dict, data: dict, base: dict | None = None) -> dict:
    if not isinstance(spec, dict) or spec.get("fig") not in figs(data):
        raise ToolError(INVALID_INPUT, f"view: 'fig' must be one of {figs(data)} for this model")
    f = spec["fig"]
    out = {"fig": f, "version_id": data["version_id"]}
    if "version_id" in spec and int(spec["version_id"]) != data["version_id"]:
        raise ToolError(INVALID_INPUT, f"view is of version {spec['version_id']}, not {data['version_id']}")
    known = {"fig", "version_id"} | ({"well"} if f in ("well_section", "history") else set())
    extra = set(spec) - known
    if extra:
        raise ToolError(INVALID_INPUT, f"view {f}: unknown argument(s) {sorted(extra)}")
    if f in ("well_section", "history"):
        w = str(spec.get("well") or (wells(data) or [None])[0])
        if w not in wells(data):
            raise ToolError(INVALID_INPUT, f"view: no well {w!r} in this model")
        out["well"] = w
    return out


def check_anchor(anchor: dict | None) -> dict:
    a = dict(anchor or {})
    extra = set(a) - ANCHOR_KEYS
    if extra:
        raise ToolError(INVALID_INPUT, f"anchor: unknown key(s) {sorted(extra)}")
    return a


def figure(spec: dict, data: dict, pins=(), base: dict | None = None):
    import plotly.graph_objects as go
    f = spec["fig"]
    if f == "plan":
        fig = wq.fig_plan(data)
        for t in fig.data:
            if getattr(t, "mode", None) == "markers+text":
                t.meta = {"role": "well", "well": str(t.text[0])}
            elif t.hovertext and " MD " in str(t.hovertext):
                t.meta = {"role": "perf_text", "text": str(t.hovertext)}
            else:
                t.meta = {"role": "skip"}
    elif f == "well_section":
        fig = wq.fig_section(data, spec["well"])
        comp = data["completions"]
        comp = comp[comp["well_name"] == spec["well"]].dropna(subset=["top_md"]) if len(comp) else comp
        perfs = iter(comp["id"].tolist() if len(comp) else [])
        for n, t in enumerate(fig.data):
            if n == 0:
                t.meta = {"role": "path", "well": spec["well"]}
            elif str(t.name or "").startswith("perf "):
                t.meta = {"role": "perforation", "well": spec["well"], "perforation": int(next(perfs, 0))}
            else:
                t.meta = {"role": "skip"}
    elif f == "perforated_cells":
        fig = wq.fig_perforated_cells(data) or go.Figure()
        for t in fig.data:
            t.meta = {"role": "perf_cell"}
    elif f == "history":
        fig = wq.fig_history(data, spec["well"])
        inj = data["wells"].set_index("well_name")["well_type"].get(spec["well"]) == "INJECTOR"
        cols = (["inj_rate"] if inj else ["oil_rate", "water_rate", "gas_rate"]) + ["bhp"]
        axes = {("x" if n == 0 else f"x{n + 1}"): c for n, c in enumerate(cols)}
        for t in fig.data:
            t.meta = {"role": "history", "well": spec["well"], "quantity": axes.get(t.xaxis or "x", cols[0])}
    elif f == "cumulative":
        fig = wq.fig_cumulative(data) or go.Figure()
        for t in fig.data:
            t.meta = {"role": "date"}
    else:
        fig = wq.fig_schedule(data) or go.Figure()
        for t in fig.data:
            t.meta = {"role": "schedule"}
    return fig


def locate(spec: dict, data: dict, role, point: dict) -> dict:
    x, y = point.get("x"), point.get("y")
    if role == "well":
        return {"well": point.get("well") or str(point.get("text"))}
    if role == "path":
        md = point.get("customdata")
        md = md[0] if isinstance(md, list) else md
        return {"well": point["well"], **({"md": round(float(md), 1)} if md is not None else {})}
    if role == "perforation":
        return {"well": point["well"], "perforation": int(point["perforation"])}
    if role == "perf_cell" and point.get("customdata"):
        cd = point["customdata"]
        return {"well": str(x), "cell": [int(cd[0]), int(cd[1]), int(y)]}
    if role == "history" and x is not None:
        return {"well": point["well"], "date": str(x)[:10], "quantity": point.get("quantity")}
    if role == "schedule" and y is not None:
        return {"well": str(y), "date": str(x)[:10]}
    if role == "date" and x is not None:
        return {"date": str(x)[:10]}
    if role == "perf_text":
        w = str(point.get("text", "")).split(" ")[0]
        return {"well": w} if w in wells(data) else {}
    if spec.get("well"):
        return {"well": spec["well"]}
    return {}


def _r(v, n=4):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return v
    return None if not np.isfinite(v) else float(f"{v:.{n}g}")


def _rec(row: dict) -> dict:
    return {k: (_r(v) if isinstance(v, (float, np.floating)) else v) for k, v in row.items() if k != "version_id"}


def describe(anchor: dict, data: dict) -> dict:
    from ..core.review import label
    a, out = anchor or {}, {"label": label(anchor)}
    w = a.get("well")
    if w is not None:
        if w not in wells(data):
            raise ToolError(INVALID_INPUT, f"anchor: no well {w!r} in this model")
        row = data["wells"][data["wells"]["well_name"] == w].iloc[0].to_dict()
        comp = data["completions"][data["completions"]["well_name"] == w] if len(data["completions"]) else data["completions"]
        ev = data["events"][data["events"]["well_name"] == w] if len(data["events"]) else data["events"]
        out["well"] = {**_rec(row), "perforations": len(comp),
                       "events": {m: int((ev["mode"] == m).sum()) for m in ("HISTORY", "PREDICTION")} if len(ev) else {},
                       "findings": [f for f in data["findings"] if w in str(f.get("detail", "")) and f["status"] != "pass"]}
    if "perforation" in a:
        c = data["completions"][data["completions"]["id"] == a["perforation"]]
        out["perforation"] = _rec(c.iloc[0].to_dict()) if len(c) else None
        p = wq.place(data)
        if len(p):
            out["perforation_cells"] = [_rec(r) for r in p[p["completion_id"] == a["perforation"]].to_dict("records")]
    if "md" in a and w is not None:
        tr = data["trajectory"][data["trajectory"]["well_name"] == w].sort_values("md")
        if len(tr):
            x, y, z = wq._interp(tr, np.array([float(a["md"])]))
            out["at_md"] = {"x": _r(x[0], 7), "y": _r(y[0], 7), "tvd": _r(z[0], 6)}
    if "cell" in a:
        p = wq.place(data)
        if len(p):
            i, j, k = a["cell"]
            hit = p[(p["i"] == i) & (p["j"] == j) & (p["k"] == k) & ((p["well_name"] == w) if w else True)]
            out["cell"] = [_rec(r) for r in hit.to_dict("records")]
    if "date" in a and w is not None and len(data["events"]):
        ev = data["events"][data["events"]["well_name"] == w].sort_values("event_date")
        now = ev[ev["event_date"] <= a["date"]].tail(1)
        out["event"] = _rec(now.iloc[0].to_dict()) if len(now) else None
        m = data["measured"]
        if len(m):
            mm = m[(m["well_name"].astype(str) == w) & (m["date"].dt.strftime("%Y-%m") == str(a["date"])[:7])]
            if len(mm):
                out["measured_month"] = {k: _r(v) for k, v in mm.drop(columns=["well_name", "date"]).sum(numeric_only=True).items()}
    if "finding" in a:
        out["finding"] = next((f for f in data["findings"] if f"{f['area']}: {f['check']}" == a["finding"]), None)
    return out


def summarize(spec: dict, data: dict, base: dict | None = None) -> dict:
    f = spec["fig"]
    out = {"view": spec}
    if f in ("well_section", "history"):
        out["shows"] = f"well {spec['well']}: " + ("path, perforations and zones" if f == "well_section"
                                                    else "monthly rates the deck applies against the measured")
        out.update(describe({"well": spec["well"]}, data))
    elif f == "perforated_cells":
        p = wq.place(data)
        out["shows"] = "perforated cells by well and layer"
        out["by_well"] = {str(w): {"cells": int(len(g)), "length_m": _r(g["length_m"].sum())}
                          for w, g in p[p["k"] > 0].groupby("well_name")} if len(p) else {}
    else:
        out["shows"] = {"plan": "well paths on a plan", "cumulative": "field cumulative volumes",
                        "schedule": "open and shut periods per well"}[f]
        out["numbers"] = metrics(data)
    return out


def metrics(data: dict) -> dict:
    w, c, ev = data["wells"], data["completions"], data["events"]
    out = {"wells": len(w), "producers": int((w["well_type"] == "PRODUCER").sum()) if len(w) else 0,
           "injectors": int((w["well_type"] == "INJECTOR").sum()) if len(w) else 0, "perforations": len(c),
           "history_events": int((ev["mode"] == "HISTORY").sum()) if len(ev) else 0,
           "prediction_events": int((ev["mode"] == "PREDICTION").sum()) if len(ev) else 0}
    try:
        v = wq.model_volumes(data)
        if len(v):
            out["cum_oil_model"] = float(v["oil_model"].sum())
            if "inj_model" in v:
                out["cum_inj_model"] = float(v["inj_model"].sum())
            if "oil" in v:
                out["cum_oil_measured"] = float(v["oil"].sum())
    except Exception:  # noqa: BLE001  volumes need history; without it they are left out
        pass
    p = wq.place(data) if data["geo"] is not None else pd.DataFrame()
    if len(p):
        out["perforated_cells"] = int((p["k"] > 0).sum())
        out["perforated_length_m"] = float(p["length_m"].sum())
    return out


def comparable(new: dict, old: dict):
    return None


def diff_extra(new: dict, old: dict) -> dict:
    return {}
