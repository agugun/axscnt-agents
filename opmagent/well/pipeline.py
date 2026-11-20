"""Well modeler pipeline: inputs -> draft well model -> validate -> QC report -> submit for review."""
from __future__ import annotations

import re

import plotly.graph_objects as go

from ..core.errors import INVALID_INPUT, ToolError
from ..core.models import review_state
from ..reports.simple_report import well_report
from ..servers.data_server import DataService
from ..servers.data_server import wrap as dwrap
from .ingest import read_inputs


def _ok(r):
    if not r.get("ok"):
        raise ToolError(r.get("code", "ERROR"), r.get("message", "failed"), r.get("details"))
    return r


def build_well_model(cfg, files: dict, name: str, *, start_date: str | None = None, geo_version_id: int | None = None,
                     unit_system: str = "METRIC", description: str | None = None, allow_gaps: bool = False,
                     source_file_ids: list[int] | None = None, data_source: dict | None = None) -> dict:
    """files: paths (or DataFrames, from a study's data store) per input; see well.ingest.read_inputs. From a study
    (data_source = {"study": name}) source_file_ids are the inbox files the tables were loaded from."""
    data = read_inputs(files)
    required = [g for g in data["gaps"] if g["severity"] == "required"]
    if required and not allow_gaps:
        raise ToolError(INVALID_INPUT, "required well data is missing", required)
    dates = sorted({e["event_date"] for e in data["well_events"]} | {c["event_date"] for c in data["well_completions"]})
    start = start_date or (dates[0] if dates else None)
    svc = DataService("well", cfg)
    D = lambda n, **k: _ok(dwrap(svc, n)(**k))  # noqa: E731
    if source_file_ids is not None:
        src = list(source_file_ids)
    else:
        src = [D("register_file", path=p, kind={"surveys": "deviation_survey", "history": "production_data"}.get(
            k, "other_input"))["file_id"] for k, p in files.items() if p and k not in ("history_format",)]
    assumptions = [f"{g['item']}: {g['message']}" for g in data["gaps"]]
    assumptions.append("history aggregated to monthly average rates (volume / producing days)")
    v = D("create_version", kind="well", model_name=name, source_file_ids=src, header={
        "unit_system": unit_system, "start_date": start, "geo_version_id": geo_version_id,
        "user_description": description, "assumptions_json": assumptions,
        "spec_json": {"inputs": data_source or {k: str(p) for k, p in files.items() if isinstance(p, str)}}})
    vid = v["version_id"]
    for t in ("well_groups", "wells", "well_trajectory", "well_completions", "well_events"):
        if data[t]:
            D("write_tables", kind="well", version_id=vid, table=t, rows=data[t])
    val = D("run_validator", kind="well", version_id=vid)
    if not val["passed"]:
        return {"ok": False, "status": "draft", "version_id": vid, "validator": val,
                "spoken_summary": val["spoken_summary"] + " The draft is kept for fixing."}
    m = svc.get_model(kind="well", version_id=vid)
    t = m["tables"]
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", name)
    rep = well_report(cfg.file_store / "qc" / f"well_{slug}_v{v['version']}.html", model=m["header"], wells=t["wells"],
                      completions=t["well_completions"], events=t["well_events"], assumptions=assumptions,
                      warnings=val["warnings"], extra_figs=_history_figs(t["well_events"]))
    rid = D("register_file", path=str(rep), kind="qc_report")["file_id"]
    n_hist = sum(1 for e in t["well_events"] if e["mode"] == "HISTORY")
    sub = D("submit_for_review", kind="well", version_id=vid, qc_report_file_id=rid,
            metrics={"wells": len(t["wells"]), "history_months": n_hist, "perforations": len(t["well_completions"])})
    return {"ok": True, "status": sub["header"]["status"], "version_id": vid, "version": v["version"], "qc_report": str(rep),
            "assumptions": assumptions,
            "spoken_summary": f"Well model {name} version {v['version']} {review_state(sub)}: {len(t['wells'])} wells, "
                              f"{len(t['well_completions'])} perforation records, {n_hist} monthly history rows."}


def _history_figs(events):
    hist = [e for e in events if e["mode"] == "HISTORY" and e["status"] == "OPEN"]
    if not hist:
        return []
    figs = []
    for key, title in (("oil_rate", "Oil rate history (sm3/d)"), ("water_rate", "Water rate history (sm3/d)"),
                       ("inj_rate", "Injection rate history (sm3/d)"), ("bhp", "Bottom-hole pressure history (bar)")):
        fig = go.Figure()
        for w in sorted({e["well_name"] for e in hist}):
            ev = sorted((e for e in hist if e["well_name"] == w and e.get(key) is not None), key=lambda e: e["event_date"])
            if ev:
                fig.add_trace(go.Scatter(x=[e["event_date"] for e in ev], y=[e[key] for e in ev], name=w, mode="lines"))
        if fig.data:
            fig.update_layout(title=title, template="plotly_white", height=360, margin=dict(l=60, r=20, t=50, b=50))
            figs.append(fig)
    return figs
