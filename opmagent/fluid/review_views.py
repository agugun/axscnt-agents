"""Fluid review: the review workbench without views (see opmagent/reviews.py).

A fluid model is tuned from a lab report, so the workbench shows its QC report (the plots are there: phase envelope,
lab experiments, the black-oil tables), its findings, the comments and the decision. No figures, no proposals, no
what-if previews; the fluid modeler answers comments in its next version.

Anchors: {"finding": ...}, {"text": ...} (what the comment is about, e.g. "Psat"), {} (general).
"""
from __future__ import annotations

import json

from ..core import models
from ..core.errors import INVALID_INPUT, ToolError
from ..notebooks import runner

FIGS: tuple[str, ...] = ()
ANCHOR_KEYS = {"finding", "text"}
# the numbers compared between versions (from the build's qc_json metrics)
METRICS = ("psat_lab", "psat_model", "psat_error_pct", "rs_at_pb", "bo_at_pb", "gor_model", "gor_measured",
           "api_model", "api_measured")


def load_version(cfg, con, version_id: int) -> dict:
    h = models.header(con, "fluid", version_id)
    s = runner.qc_dir(cfg.file_store, h["model_name"], h["version"], kind="fluid") / "qc_summary.json"
    qc = h.get("qc_json") or {}
    if isinstance(qc, str):
        qc = json.loads(qc)
    spec = h.get("spec_json") or {}
    if isinstance(spec, str):
        spec = json.loads(spec)
    return {"model_name": h["model_name"], "version": h["version"], "version_id": version_id, "header": h,
            "findings": json.loads(s.read_text()).get("findings", []) if s.is_file() else [],
            "metrics": (qc or {}).get("metrics") or {}, "spec": (spec or {}).get("fluid_spec") or {},
            "kind": "fluid"}


def name(data: dict) -> str:
    return f"v{data['version']}"


def figs(data: dict) -> tuple[str, ...]:
    return FIGS


def view_args(data: dict) -> dict:
    return {}


def meta(data: dict) -> dict:
    h = data["header"]
    return {"kind": "fluid", "model_name": data["model_name"], "version": data["version"],
            "version_id": data["version_id"], "label": name(data), "db_only": False, "views": [], "view_args": {},
            "status": h["status"], "wells": [], "keywords": [], "zones": [], "findings": data["findings"],
            "unit_system": h.get("unit_system"), "assumptions": h.get("assumptions_json") or []}


def check_spec(spec: dict, data: dict, base: dict | None = None) -> dict:
    raise ToolError(INVALID_INPUT, "a fluid model has no views in the workbench: its plots are in the QC report")


def check_anchor(anchor: dict | None) -> dict:
    a = dict(anchor or {})
    extra = set(a) - ANCHOR_KEYS
    if extra:
        raise ToolError(INVALID_INPUT, f"anchor: unknown key(s) {sorted(extra)} (a fluid comment is on a QC finding, "
                                       "or general)")
    return a


def figure(spec: dict, data: dict, pins=(), base: dict | None = None):
    check_spec(spec, data, base)


def locate(spec: dict, data: dict, role, point: dict) -> dict:
    return {}


def describe(anchor: dict, data: dict) -> dict:
    from ..core.review import label
    a, out = anchor or {}, {"label": label(anchor)}
    if "finding" in a:
        out["finding"] = next((f for f in data["findings"] if f"{f['area']}: {f['check']}" == a["finding"]), None)
    return out


def summarize(spec: dict, data: dict, base: dict | None = None) -> dict:
    check_spec(spec, data, base)
    return {}


def metrics(data: dict) -> dict:
    m = data["metrics"]
    return {k: m[k] for k in METRICS if m.get(k) is not None}


def comparable(new: dict, old: dict):
    return None


def diff_extra(new: dict, old: dict) -> dict:
    return {}
