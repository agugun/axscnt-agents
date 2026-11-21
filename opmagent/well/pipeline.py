"""Well modeler pipeline: inputs -> draft well model -> validate -> QC report -> submit for review.

The QC report is the executed standard notebook (opmagent/notebooks/well_qc.ipynb) exported to HTML, kept per
version in <file_store>/qc/well_<model>_v<N>/ with its payload, the measured history (history.csv) and
qc_summary.json. With [qc] report = "html" it is the static report."""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from ..core.errors import INVALID_INPUT, ToolError
from ..core.models import review_state
from ..notebooks import runner
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
    qc_extra, findings_say = {}, ""
    if cfg.qc_report == "notebook":
        folder = runner.qc_dir(cfg.file_store, name, v["version"], kind="well")
        folder.mkdir(parents=True, exist_ok=True)
        hist = data.get("history_input")
        if hist is not None:
            cols = [c for c in ("well_name", "date", "oil", "water", "gas", "water_inj", "gas_inj", "bhp", "thp", "hours")
                    if c in hist.columns]
            h = hist[cols].copy()
            h["date"] = pd.to_datetime(h["date"])
            h["well_name"] = h["well_name"].astype(str)
            h.to_csv(folder / "history.csv", index=False)
        else:
            (folder / "history.csv").unlink(missing_ok=True)
        runner.write_payload(folder, {
            "model_name": name, "version": v["version"], "version_id": vid, "db_path": str(cfg.database),
            "assumptions": assumptions, "validator_warnings": val["warnings"], "gaps": data["gaps"],
            "history_file": "history.csv", "history_format": files.get("history_format", "standard")})
        nb = runner.run_qc("well", folder, cfg.database, vid, timeout=cfg.qc_timeout_s)
        if not nb["ok"]:
            return {"ok": False, "status": "draft", "version_id": vid, "qc_notebook": nb["notebook"],
                    "error": nb["error"], "spoken_summary": f"The QC notebook failed ({nb['error']}). The draft is "
                    f"kept; open {nb['notebook']} to see the failing cell."}
        rep = Path(nb["html"])
        D("register_file", path=nb["notebook"], kind="qc_report", note=f"executed QC notebook, well version {vid}")
        D("register_file", path=nb["payload"], kind="qc_report", note=f"QC payload, well version {vid}")
        if (folder / "history.csv").is_file():
            D("register_file", path=str(folder / "history.csv"), kind="qc_report", note=f"measured history, well version {vid}")
        s = nb["summary"]
        qc_extra = {"qc_notebook": nb["notebook"], "qc_findings": s["counts"], "qc_overall": s["overall"],
                    "qc_notebook_sha256": nb["notebook_sha256"]}
        if s["overall"] != "pass":
            findings_say = (f" The QC notebook flags {s['counts']['fail']} fail and {s['counts']['warn']} warn "
                            f"finding(s) for the reviewer.")
    else:
        rep = well_report(cfg.file_store / "qc" / f"well_{slug}_v{v['version']}.html", model=m["header"], wells=t["wells"],
                          completions=t["well_completions"], events=t["well_events"], assumptions=assumptions,
                          warnings=val["warnings"], extra_figs=_history_figs(t["well_events"]))
    rid = D("register_file", path=str(rep), kind="qc_report")["file_id"]
    n_hist = sum(1 for e in t["well_events"] if e["mode"] == "HISTORY")
    sub = D("submit_for_review", kind="well", version_id=vid, qc_report_file_id=rid,
            metrics={"wells": len(t["wells"]), "history_months": n_hist, "perforations": len(t["well_completions"]),
                     **{k: x for k, x in qc_extra.items() if k != "qc_notebook"}})
    return {"ok": True, "status": sub["header"]["status"], "version_id": vid, "version": v["version"], "qc_report": str(rep),
            **qc_extra, "assumptions": assumptions,
            "spoken_summary": f"Well model {name} version {v['version']} {review_state(sub)}: {len(t['wells'])} wells, "
                              f"{len(t['well_completions'])} perforation records, {n_hist} monthly history rows."
                              + findings_say}


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
