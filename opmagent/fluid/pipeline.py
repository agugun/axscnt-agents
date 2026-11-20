"""Fluid modeler pipeline: lab data -> tuned EOS -> black-oil tables -> draft -> QC report -> review.

The agent drives this through `opmagent fluid build` (or step by step through the MCP tools).
It uses the same DataService (fluid profile) and PVTService as the MCP servers, so every rule applies.
"""
from __future__ import annotations

import re

from ..core.errors import INVALID_INPUT, ToolError
from ..core.models import review_state
from ..reports.fluid_report import build_report
from ..servers.data_server import DataService
from ..servers.data_server import wrap as dwrap
from ..servers.pvt_server import PVTService
from ..servers.pvt_server import wrap as pwrap


def _ok(r: dict) -> dict:
    if not r.get("ok"):
        raise ToolError(r.get("code", "ERROR"), r.get("message", "step failed"), r.get("details"))
    return r


def _dense(lab_ps: list[float], lo: float, hi: float, n: int = 15) -> list[float]:
    grid = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
    return sorted(set(round(p, 3) for p in grid + list(lab_ps)), reverse=True)


def build_fluid_model(fi: dict, model_name: str, *, data: DataService, pvt: PVTService, options: dict | None = None,
                      origin: str = "built", user_description: str | None = None, allow_default_stages: bool = False,
                      accept_tolerance_miss: bool = False, gaps: list[dict] | None = None, n: int = 12,
                      inline_js: bool = True) -> dict:
    options = options or {}
    gaps = gaps or []
    required = [g for g in gaps if g["severity"] == "required" and not (allow_default_stages and g["item"] == "separator")]
    if required:
        raise ToolError(INVALID_INPUT, "required lab data is missing", required)
    D = lambda name, **kw: _ok(dwrap(data, name)(**kw))  # noqa: E731
    P = lambda name, **kw: _ok(pwrap(pvt, name)(**kw))  # noqa: E731
    assumptions = [f"{g['item']}: {g['message']}" for g in gaps if g["severity"] == "recommended"]

    stages = fi.get("separator_stages") or None
    if not stages:
        assumptions.append("separator: field stages not given; default 2-stage separator (30 bar/40 degC, then stock tank)")

    # 1. characterise and tune
    ch = P("characterize", composition=fi["composition"], plus_fraction=fi["plus_fraction"],
           temperature_c=fi["temperature_c"], **{k: v for k, v in options.items() if v is not None})
    tuned = P("tune", fluid_handle=ch["fluid_handle"], psat_lab=fi["psat_lab"],
              lab_dl=fi.get("dl") or None, lab_viscosity=fi.get("viscosity") or None)
    h = tuned["fluid_handle"]
    if not tuned["within_tolerance"] and not accept_tolerance_miss:
        return {"ok": False, "status": "needs_decision", "checks": tuned["checks"], "psat_tuning": tuned["psat_tuning"],
                "spoken_summary": tuned["spoken_summary"] + " I stopped before writing the model; tell me to accept "
                "the miss, change EOS options, or check the lab data."}
    if not tuned["within_tolerance"]:
        assumptions.append("tolerance miss accepted by the user: " +
                           ", ".join(f"{k} {c['error']*100:+.1f}%" for k, c in tuned["checks"].items() if not c["passed"]))

    # 2. simulations for QC
    psat = tuned["psat_tuning"]["psat_after"]
    p_min = fi.get("p_min") or round(0.1 * psat, 1)
    p_max = fi.get("p_max") or round(3 * psat, 1)
    qc_sim = {
        "cce": P("simulate_cce", fluid_handle=h, pressures=_dense([r["pressure"] for r in fi.get("cce", [])], p_min, p_max))["rows"],
        "dl": P("simulate_dl", fluid_handle=h, pressures=_dense([r["pressure"] for r in fi.get("dl", [])], p_min, psat * 0.98))["rows"],
        "viscosity": P("simulate_viscosity", fluid_handle=h, pressures=_dense([r["pressure"] for r in fi.get("viscosity", [])], p_min, p_max))["rows"],
    }
    sep = P("simulate_separator", fluid_handle=h, stages=stages)

    # 3. black-oil tables
    bo = P("generate_black_oil", fluid_handle=h, p_min=p_min, p_max=p_max, n=n, stages=stages,
           salinity_ppm=fi.get("salinity_ppm", 0.0))
    assumptions += bo["summary"]["assumptions"]
    if not fi.get("rsvd"):
        rs_pb = max(r["rs"] for r in bo["tables"]["pvto"] if r["is_saturated"])  # exactly the table value
        bo["tables"]["rsvd"] = [{"eqlnum": 1, "depth": 0.0, "rs": rs_pb},
                                {"eqlnum": 1, "depth": 10000.0, "rs": rs_pb}]
        assumptions.append(f"RSVD: no depth data; constant initial Rs = Rs at bubble point ({rs_pb:.1f} sm3/sm3)")

    # 4. draft version
    src = [D("register_file", path=fi["source_file"], kind="lab_report")["file_id"]] if fi.get("source_file") else []
    v = D("create_version", kind="fluid", model_name=model_name, origin=origin, source_file_ids=src, header={
        "unit_system": "METRIC", "phases": "OIL,WATER,GAS", "model_type": "black_oil", "has_disgas": 1, "has_vapoil": 0,
        "user_description": user_description,
        "spec_json": {"fluid_spec": tuned["spec"], "psat_tuning": tuned["psat_tuning"], "checks": tuned["checks"],
                      "separator_stages": stages, "p_range": [p_min, p_max], "sample": fi.get("sample_name")},
        "assumptions_json": assumptions})
    vid = v["version_id"]
    for table, rows in bo["tables"].items():
        D("write_tables", kind="fluid", version_id=vid, table=table, rows=rows, replace=True)
    val = D("run_validator", kind="fluid", version_id=vid)
    if not val["passed"]:
        return {"ok": False, "status": "draft", "version_id": vid, "validator": val,
                "spoken_summary": val["spoken_summary"] + " The draft is kept for fixing."}

    # 5. QC report and submission
    m = fi.get("measured", {})
    metrics = {"psat_lab": fi["psat_lab"], "psat_model": psat,
               "psat_error_pct": tuned["psat_tuning"]["psat_error_after"] * 100,
               "rs_at_pb": bo["summary"]["rs_at_pb"], "bo_at_pb": bo["summary"]["bo_at_pb"],
               "gor_model": sep["gor_sm3_sm3"], "gor_measured": m.get("measured_gor_sm3_sm3"),
               "api_model": sep["api"], "api_measured": m.get("measured_api")}
    for k, c in tuned["checks"].items():
        if k != "psat":
            metrics[f"{k}_max_error_pct"] = round(c["error"] * 100, 2)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", model_name)
    report = build_report(data.cfg.file_store / "qc" / f"fluid_{slug}_v{v['version']}.html", model_name=model_name,
                          version=v["version"], sample=fi.get("sample_name"), metrics=metrics, checks=tuned["checks"],
                          assumptions=assumptions, warnings=val["warnings"], tables=bo["tables"], qc_sim=qc_sim,
                          lab={"cce": fi.get("cce", []), "dl": fi.get("dl", []), "viscosity": fi.get("viscosity", [])},
                          spec=tuned["spec"], inline_js=inline_js)
    rid = D("register_file", path=str(report), kind="qc_report")["file_id"]
    sub = D("submit_for_review", kind="fluid", version_id=vid, qc_report_file_id=rid,
            metrics={k: (round(x, 4) if isinstance(x, float) else x) for k, x in metrics.items()})
    return {"ok": True, "status": sub["header"]["status"], "version_id": vid, "version": v["version"],
            "qc_report": str(report), "metrics": metrics, "checks": tuned["checks"], "assumptions": assumptions,
            "spoken_summary": f"Fluid {model_name} version {v['version']} {review_state(sub)}. Bubble point "
                              f"{psat:.0f} bar, Rs {metrics['rs_at_pb']:.0f}, Bo {metrics['bo_at_pb']:.3f}; "
                              f"all checks within tolerance." if tuned["within_tolerance"] else
                              f"Fluid {model_name} version {v['version']} {review_state(sub)}, with an accepted tolerance miss."}
