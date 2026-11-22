"""Geo modeler pipeline: tops + logs + spec -> draft geo model -> validate -> QC report -> submit.

The QC report is the executed standard notebook (opmagent/notebooks/geo_qc.ipynb) exported to HTML, kept per
version in <file_store>/qc/geo_<model>_v<N>/ with its payload, the logs it used and qc_summary.json. With
[qc] report = "html" it is the static report below."""
from __future__ import annotations

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
from ..core.models import review_state
from ..notebooks import runner
from .build import build_grid, read_logs


def _ok(r):
    if not r.get("ok"):
        raise ToolError(r.get("code", "ERROR"), r.get("message", "failed"), r.get("details"))
    return r


def _report(path, name, version, g, logs, spec, assumptions, warnings):
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


def build_geo_model(cfg, *, tops, logs, spec: dict, name: str, surveys=None, description: str | None = None,
                    source_file_ids: list[int] | None = None, data_source: dict | None = None) -> dict:
    """tops: CSV path or DataFrame; logs: LAS/CSV paths or a DataFrame (well_name, tvd, phi, perm, vsh); surveys:
    a survey file, or a DataFrame of x, y, tvd stations. From a study (data_source = {"study": name}) the tables
    come from the database and source_file_ids are the inbox files they were loaded from."""
    for key in ("surfaces", "layers"):
        if key not in spec:
            raise ToolError(INVALID_INPUT, f"geo spec needs '{key}'")
    t = tops if isinstance(tops, pd.DataFrame) else pd.read_csv(tops)
    sv = None
    if isinstance(surveys, pd.DataFrame):
        sv = surveys
    elif surveys:
        from ..well.ingest import read_table, surveys as to_xyz
        sv = pd.DataFrame(to_xyz(read_table(surveys), {}))
    lg = logs if isinstance(logs, pd.DataFrame) else read_logs(logs, sv, spec.get("curves"))
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", name)
    svc = DataService("geo", cfg)
    D = lambda n, **k: _ok(dwrap(svc, n)(**k))  # noqa: E731
    existing = [m["version"] for m in svc.list_models(kind="geo")["models"] if m["model_name"] == name]
    out = cfg.file_store / "models" / "geo" / f"{slug}_v{(max(existing) if existing else 0) + 1}"
    try:
        g = build_grid(t, lg, spec, out)
    except ValueError as e:
        raise ToolError(INVALID_INPUT, str(e)) from e
    assumptions = [
        "surfaces: ordinary kriging of well tops (inverse-distance weighting below 3 wells); crossing prevented",
        f"layers split proportionally: {dict(zip(spec['surfaces'][:-1], spec['layers']))}",
        "properties: logs upscaled per layer (phi arithmetic, perm geometric), then kriged per layer",
        f"PERMY = PERMX; PERMZ = {spec.get('petro', {}).get('kv_kh', 0.1)} x PERMX",
        "no faults (vertical pillars)",
    ]
    if lg["perm"].isna().all():
        assumptions.append(f"no permeability log: k = a*10^(b*phi) with a, b = {spec.get('petro', {}).get('poro_perm', [0.01, 30.0])}")
    if source_file_ids is not None:
        src = list(source_file_ids)
    else:
        src = [D("register_file", path=tops, kind="tops")["file_id"]] + \
              [D("register_file", path=p, kind="well_log")["file_id"] for p in logs]
    gid = D("register_file", path=g["grid_file"], kind="grid")["file_id"]
    v = D("create_version", kind="geo", model_name=name, source_file_ids=src, header={
        "unit_system": "METRIC", "grid_type": "corner_point", "nx": g["nx"], "ny": g["ny"], "nz": g["nz"],
        "grid_file_id": gid, "user_description": description, "assumptions_json": assumptions,
        "spec_json": {"geo_spec": spec, "areal": g["areal"], "layer_depths_file": g["layer_depths_file"],
                      **({"data_source": data_source} if data_source else {})}})
    vid = v["version_id"]
    props = [{"keyword": k, "file_id": D("register_file", path=p, kind="property")["file_id"]} for k, p in g["files"].items()]
    D("write_tables", kind="geo", version_id=vid, table="geo_properties", rows=props)
    val = D("run_validator", kind="geo", version_id=vid)
    if not val["passed"]:
        return {"ok": False, "status": "draft", "version_id": vid, "validator": val,
                "spoken_summary": val["spoken_summary"] + " The draft is kept for fixing."}
    qc_extra, findings_say = {}, ""
    if cfg.qc_report == "notebook":
        folder = runner.qc_dir(cfg.file_store, name, v["version"], kind="geo")
        folder.mkdir(parents=True, exist_ok=True)
        lg[["well_name", "tvd", "phi", "perm", "vsh"]].to_csv(folder / "logs.csv", index=False)
        tcols = [c for c in ("well_name", "surface", "x", "y", "tvd") if c in t.columns.str.lower()]
        runner.write_payload(folder, {
            "model_name": name, "version": v["version"], "version_id": vid, "db_path": str(cfg.database),
            "spec": spec, "areal": g["areal"], "layer_depths_file": g["layer_depths_file"],
            "assumptions": assumptions, "validator_warnings": val["warnings"], "zones": g["zones"],
            "upscaled": g["upscaled"], "logs_file": "logs.csv",
            "tops": t.rename(columns=str.lower)[tcols].astype({"well_name": str}).to_dict("records")})
        nb = runner.run_qc("geo", folder, cfg.database, vid, timeout=cfg.qc_timeout_s)
        if not nb["ok"]:
            return {"ok": False, "status": "draft", "version_id": vid, "qc_notebook": nb["notebook"],
                    "error": nb["error"], "spoken_summary": f"The QC notebook failed ({nb['error']}). The draft is "
                    f"kept; open {nb['notebook']} to see the failing cell."}
        rep = Path(nb["html"])
        D("register_file", path=nb["notebook"], kind="qc_report", note=f"executed QC notebook, geo version {vid}")
        D("register_file", path=nb["payload"], kind="qc_report", note=f"QC payload, geo version {vid}")
        D("register_file", path=str(folder / "logs.csv"), kind="qc_report", note=f"logs used, geo version {vid}")
        s = nb["summary"]
        qc_extra = {"qc_notebook": nb["notebook"], "qc_findings": s["counts"], "qc_overall": s["overall"],
                    "qc_notebook_sha256": nb["notebook_sha256"]}
        if s["overall"] != "pass":
            findings_say = (f" The QC notebook flags {s['counts']['fail']} fail and {s['counts']['warn']} warn "
                            f"finding(s) for the reviewer.")
    else:
        rep = _report(cfg.file_store / "qc" / f"geo_{slug}_v{v['version']}.html", name, v["version"], g, lg, spec,
                      assumptions, val["warnings"])
    rid = D("register_file", path=str(rep), kind="qc_report")["file_id"]
    tot = sum(z["hcpv_rm3"] for z in g["zones"])
    sub = D("submit_for_review", kind="geo", version_id=vid, qc_report_file_id=rid,
            metrics={"cells": g["nx"] * g["ny"] * g["nz"], "hcpv_rm3": round(tot), "zones": len(g["zones"]),
                     **{k: x for k, x in qc_extra.items() if k != "qc_notebook"}})
    from .static_view import after_build
    sv = after_build(cfg, svc.con, vid)
    say = sv.pop("static_view_say")
    return {"ok": True, "status": sub["header"]["status"], "version_id": vid, "version": v["version"], "qc_report": str(rep),
            **qc_extra, **sv, "grid": f"{g['nx']} x {g['ny']} x {g['nz']}", "zones": g["zones"], "assumptions": assumptions,
            "spoken_summary": f"Geo model {name} version {v['version']} {review_state(sub)}: grid {g['nx']} by "
                              f"{g['ny']} by {g['nz']}, hydrocarbon pore volume {tot / 1e6:.1f} million reservoir "
                              f"cubic metres." + findings_say + say}
