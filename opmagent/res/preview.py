"""What-if previews for res models (the review workbench, phase 4).

A res preview is the patched spec resolved as the build would (res.pipeline.resolve_spec: SCAL tables, contacts,
rock, aquifers, one region number per layer) on the base version's own grid (its static view, or the geo version's
depths and pore volumes), with the region arrays replaced from the new rules. No Flow run, no version: it takes a
fraction of a second. Kept in review_previews like a geo preview (the spec only; it is resolved again to draw it).
"""
from __future__ import annotations

import json
import time
import uuid

import numpy as np
import pandas as pd

from ..core import models
from ..core import proposals as props
from ..core.errors import INVALID_INPUT, NOT_FOUND, ToolError
from ..geo.preview import get, prune  # noqa: F401  (the table is shared)
from ..notebooks import res_qc_lib as rq


def apply(base: dict, spec: dict, geo_header: dict) -> dict:
    """The base version's data with a spec resolved on it: tables, region arrays, the new spec."""
    from .pipeline import resolve_spec
    r = resolve_spec(spec, geo_header)
    t = dict(base["tables"])
    t.update(swof=r["swof"], sgof=r["sgof"], rock=[{k: x[k] for k in ("region", "p_ref", "cr")} for x in r["rock"]],
             contacts=[{k: c.get(k) for k in ("eqlnum", "datum_depth", "datum_pressure", "owc_depth", "goc_depth")}
                       for c in r["contacts"]],
             aquifers=r["aquifers"], aquifer_connections=r["aquifer_connections"])
    grid = None
    if base["grid"]:
        grid = dict(base["grid"])
        nx, ny, nz = base["dims"]
        for kw, (_rule, per_layer) in r["layers"].items():
            grid[kw] = np.where(grid["active"], np.broadcast_to(per_layer[:, None, None], (nz, ny, nx)), np.nan)
    return {**base, "spec": spec, "tables": t, "swof": pd.DataFrame(r["swof"]), "sgof": pd.DataFrame(r["sgof"]),
            "grid": grid}


def load(cfg, con, preview_id: int, base: dict | None = None) -> dict:
    from . import review_views as views
    p = get(con, preview_id)
    if p["model_kind"] != "res":
        raise ToolError(INVALID_INPUT, f"preview {preview_id} is a {p['model_kind']} preview")
    base = base or views.load_version(cfg, con, p["base_version_id"])
    spec = props.merge_patch(base["spec"], p["spec_patch"])
    gh = models.header(con, "geo", base["header"]["geo_version_id"])
    d = apply(base, spec, gh)
    d.update(version_id=None, preview_id=preview_id, label=f"preview #{preview_id}",
             header={**base["header"], "status": "preview"}, findings=(p["summary"] or {}).get("findings_all") or [])
    if not d["findings"]:
        d["findings"] = rq.run_all_checks(d)
    return d


def run(cfg, con, base_version_id: int, spec_patch: dict, author_role: str, author: str) -> dict:
    from ..geo import review_diff
    from . import review_views as views
    h = models.header(con, "res", base_version_id)
    base_spec = props.base_spec(con, "res", base_version_id)
    if not base_spec:
        raise ToolError(INVALID_INPUT, f"res v{h['version']} has no spec to change (a version from a deck)")
    if not isinstance(spec_patch, dict) or not spec_patch:
        raise ToolError(INVALID_INPUT, "spec_patch must be a non-empty JSON object (a merge patch on the spec)")
    bad = set(spec_patch) - set(props.PATCHABLE["res"])
    if bad:
        raise ToolError(INVALID_INPUT, f"spec_patch can change {props.PATCHABLE['res']}, not {sorted(bad)}")
    spec = props.merge_patch(base_spec, spec_patch)
    changes = props.spec_diff(base_spec, spec)
    if not changes:
        raise ToolError(INVALID_INPUT, "the patch changes nothing in the base spec")
    t0 = time.monotonic()
    base = views.load_version(cfg, con, base_version_id)
    gh = models.header(con, "geo", h["geo_version_id"])
    d = apply(base, spec, gh)
    d["findings"] = rq.run_all_checks(d)
    elapsed = time.monotonic() - t0
    folder = cfg.file_store / "previews" / f"res_{h['model_name']}_v{h['version']}_{uuid.uuid4().hex[:8]}"
    pid = con.execute("INSERT INTO review_previews(model_kind, model_name, base_version_id, spec_patch_json, author_role, "
                      "author, folder, elapsed_s) VALUES ('res',?,?,?,?,?,?,?)",
                      (h["model_name"], base_version_id, json.dumps(spec_patch), author_role, author, str(folder),
                       round(elapsed, 3))).lastrowid
    s = review_diff.compare(d, base, "res")
    nx, ny, nz = base["dims"]
    summary = {"preview_id": pid, "kind": "res", "base_version_id": base_version_id, "base_version": h["version"],
               "grid": f"{nx} x {ny} x {nz}", "elapsed_s": round(elapsed, 2), "spec_patch": spec_patch, "changes": changes,
               **{k: s[k] for k in ("numbers", "findings", "maps")}, "findings_all": d["findings"]}
    con.execute("UPDATE review_previews SET summary_json = ? WHERE id = ?", (json.dumps(summary, default=float), pid))
    return summary
