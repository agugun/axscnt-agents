"""Geo modeler pipeline: tops + logs + spec -> draft geo model -> validate -> QC report -> submit."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ..core.errors import INVALID_INPUT, ToolError
from ..reports.simple_report import _list, _page, _table
from ..servers.data_server import DataService
from ..servers.data_server import wrap as dwrap
from .build import build_grid, corey, read_logs


def _ok(r):
    if not r.get("ok"):
        raise ToolError(r.get("code", "ERROR"), r.get("message", "failed"), r.get("details"))
    return r


def _report(path, name, version, g, logs, spec, assumptions, warnings, swof, sgof):
    figs = []
    arr = g["arrays"]
    nz = g["nz"]
    for prop, title, log in (("phi", "Porosity", False), ("perm", "Permeability (mD, log10)", True)):
        ks = sorted({0, nz // 2, nz - 1})
        fig = make_subplots(rows=1, cols=len(ks), subplot_titles=[f"Layer {k + 1}" for k in ks])
        for c, k in enumerate(ks, start=1):
            z = np.log10(arr[prop][k]) if log else arr[prop][k]
            fig.add_trace(go.Heatmap(z=z, colorscale="Viridis", showscale=c == len(ks)), 1, c)
        fig.update_layout(title=f"{title} maps", template="plotly_white", height=360)
        figs.append(fig)
    fig = go.Figure()
    fig.add_trace(go.Histogram(x=arr["phi"].ravel(), name="Model cells", histnorm="probability", opacity=0.6))
    if len(logs):
        fig.add_trace(go.Histogram(x=logs["phi"].dropna(), name="Log samples", histnorm="probability", opacity=0.6))
    fig.update_layout(title="Porosity: model vs logs", barmode="overlay", template="plotly_white", height=360)
    figs.append(fig)
    if len(logs) and logs["perm"].notna().any():
        fig = go.Figure(go.Scatter(x=logs["phi"], y=logs["perm"], mode="markers", marker=dict(size=4), name="logs"))
        fig.update_layout(title="Poro-perm crossplot (logs)", xaxis_title="phi", yaxis_title="perm (mD)", yaxis_type="log",
                          template="plotly_white", height=360)
        figs.append(fig)
    fig = make_subplots(rows=1, cols=2, subplot_titles=("SWOF", "SGOF"))
    fig.add_trace(go.Scatter(x=[r["sw"] for r in swof], y=[r["krw"] for r in swof], name="krw"), 1, 1)
    fig.add_trace(go.Scatter(x=[r["sw"] for r in swof], y=[r["krow"] for r in swof], name="krow"), 1, 1)
    fig.add_trace(go.Scatter(x=[r["sg"] for r in sgof], y=[r["krg"] for r in sgof], name="krg"), 1, 2)
    fig.add_trace(go.Scatter(x=[r["sg"] for r in sgof], y=[r["krog"] for r in sgof], name="krog"), 1, 2)
    fig.update_layout(title="Relative permeability", template="plotly_white", height=360)
    figs.append(fig)
    zones = _table(["Zone", "Layers", "Pore volume (rm3)", "HCPV above OWC (rm3)", "Mean phi", "Mean perm (mD)"],
                   [[z["zone"], z["layers"], f"{z['pore_volume_rm3']:,.0f}", f"{z['hcpv_rm3']:,.0f}", f"{z['mean_phi']:.3f}",
                     f"{z['mean_perm_md']:.1f}"] for z in g["zones"]])
    ups = _table(["Well", "Layer", "phi", "perm (mD)", "NTG"],
                 [[u["well_name"], u["layer"], f"{u['phi']:.3f}", f"{u['perm']:.1f}", f"{u['ntg']:.2f}"] for u in g["upscaled"][:150]])
    doc = _page(f"Geo model QC: {name} version {version}", f"generated {datetime.now():%Y-%m-%d %H:%M}",
                [("Grid", _table(["Item", "Value"], [["Cells", f"{g['nx']} x {g['ny']} x {g['nz']}"],
                                                     ["Areal origin, spacing", f"({g['areal']['x0']:.0f}, {g['areal']['y0']:.0f}), {g['areal']['dx']:.0f} x {g['areal']['dy']:.0f} m"],
                                                     ["Depth range", f"{g['top']:.1f} - {g['base']:.1f} m TVD"]])),
                 ("Volumes by zone", zones), ("Upscaled logs (first 150)", ups),
                 ("Assumptions", _list(assumptions)), ("Validator warnings", _list(w["message"] for w in warnings))],
                figs, True)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(doc)
    return Path(path)


def build_geo_model(cfg, *, tops: str, logs: list[str], spec: dict, name: str, surveys: str | None = None,
                    description: str | None = None) -> dict:
    for key in ("surfaces", "layers", "contacts"):
        if key not in spec:
            raise ToolError(INVALID_INPUT, f"geo spec needs '{key}'")
    c = spec["contacts"]
    if "datum_depth" not in c or "datum_pressure" not in c:
        raise ToolError(INVALID_INPUT, "contacts need datum_depth and datum_pressure (and owc_depth / goc_depth if present)")
    t = pd.read_csv(tops)
    sv = None
    if surveys:
        from ..well.ingest import read_table, surveys as to_xyz
        sv = pd.DataFrame(to_xyz(read_table(surveys), {}))
    lg = read_logs(logs, sv, spec.get("curves"))
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", name)
    svc = DataService("geo", cfg)
    D = lambda n, **k: _ok(dwrap(svc, n)(**k))  # noqa: E731
    existing = [m["version"] for m in svc.list_models(kind="geo")["models"] if m["model_name"] == name]
    out = cfg.file_store / "models" / "geo" / f"{slug}_v{(max(existing) if existing else 0) + 1}"
    try:
        g = build_grid(t, lg, spec, out)
    except ValueError as e:
        raise ToolError(INVALID_INPUT, str(e)) from e
    swof, sgof = corey(spec)
    assumptions = [
        "surfaces: ordinary kriging of well tops (inverse-distance weighting below 3 wells); crossing prevented",
        f"layers split proportionally: {dict(zip(spec['surfaces'][:-1], spec['layers']))}",
        "properties: logs upscaled per layer (phi arithmetic, perm geometric), then kriged per layer",
        f"PERMY = PERMX; PERMZ = {spec.get('petro', {}).get('kv_kh', 0.1)} x PERMX",
        "relative permeability: Corey " + json.dumps(spec.get("scal", {})),
        "no faults (vertical pillars)",
    ]
    if lg["perm"].isna().all():
        assumptions.append(f"no permeability log: k = a*10^(b*phi) with a, b = {spec.get('petro', {}).get('poro_perm', [0.01, 30.0])}")
    src = [D("register_file", path=tops, kind="tops")["file_id"]] + [D("register_file", path=p, kind="well_log")["file_id"] for p in logs]
    gid = D("register_file", path=g["grid_file"], kind="grid")["file_id"]
    v = D("create_version", kind="geo", model_name=name, source_file_ids=src, header={
        "unit_system": "METRIC", "grid_type": "corner_point", "nx": g["nx"], "ny": g["ny"], "nz": g["nz"],
        "grid_file_id": gid, "user_description": description, "assumptions_json": assumptions,
        "spec_json": {"geo_spec": spec, "areal": g["areal"], "layer_depths_file": g["layer_depths_file"]}})
    vid = v["version_id"]
    props = [{"keyword": k, "file_id": D("register_file", path=p, kind="property")["file_id"]} for k, p in g["files"].items()]
    D("write_tables", kind="geo", version_id=vid, table="geo_properties", rows=props)
    D("write_tables", kind="geo", version_id=vid, table="swof", rows=swof)
    D("write_tables", kind="geo", version_id=vid, table="sgof", rows=sgof)
    rock = spec.get("rock", {"p_ref": c["datum_pressure"], "cr": 4.5e-5})
    D("write_tables", kind="geo", version_id=vid, table="rock", rows=[{"p_ref": rock["p_ref"], "cr": rock["cr"]}])
    D("write_tables", kind="geo", version_id=vid, table="contacts", rows=[{
        "datum_depth": c["datum_depth"], "datum_pressure": c["datum_pressure"], "owc_depth": c.get("owc_depth"),
        "goc_depth": c.get("goc_depth")}])
    val = D("run_validator", kind="geo", version_id=vid)
    if not val["passed"]:
        return {"ok": False, "status": "draft", "version_id": vid, "validator": val,
                "spoken_summary": val["spoken_summary"] + " The draft is kept for fixing."}
    rep = _report(cfg.file_store / "qc" / f"geo_{slug}_v{v['version']}.html", name, v["version"], g, lg, spec,
                  assumptions, val["warnings"], swof, sgof)
    rid = D("register_file", path=str(rep), kind="qc_report")["file_id"]
    tot = sum(z["hcpv_rm3"] for z in g["zones"])
    D("submit_for_review", kind="geo", version_id=vid, qc_report_file_id=rid,
      metrics={"cells": g["nx"] * g["ny"] * g["nz"], "hcpv_rm3": round(tot), "zones": len(g["zones"])})
    return {"ok": True, "status": "qc_ready", "version_id": vid, "version": v["version"], "qc_report": str(rep),
            "grid": f"{g['nx']} x {g['ny']} x {g['nz']}", "zones": g["zones"], "assumptions": assumptions,
            "spoken_summary": f"Geo model {name} version {v['version']} is waiting for your review: grid {g['nx']} by "
                              f"{g['ny']} by {g['nz']}, hydrocarbon pore volume {tot / 1e6:.1f} million reservoir cubic metres."}
