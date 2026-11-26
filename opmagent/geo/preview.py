"""What-if previews (geo review workbench, phase 3): a scratch build of a spec patch on a geo version.

A preview runs the same build as a version (geo.build.build_grid) on the base version's own data (the study's tables
and logs, or the tops and LAS files the base recorded) with base spec + patch, into <file_store>/previews/. Nothing is
registered and no version is made. Its summary (spec changes, numbers and QC findings against the base) is kept in
review_previews; the grid is deleted after [geo_review] preview_keep_days. The workbench draws a preview like a version
(view specs with "preview_id"), and a proposal can carry its preview, so the engineer sees what a change does before
accepting it. The engineer's own previews become proposals the agent builds (decision 4).
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from ..core import models
from ..core import proposals as props
from ..core.errors import INVALID_INPUT, NOT_FOUND, ToolError
from ..notebooks import geo_qc_lib as gq


def base_inputs(con, version_id: int, surveys=None) -> dict:
    """The data a geo version was built from, to build it again with another spec: tops, logs (with the spec's
    curve choice), surveys, and where they came from (study or files)."""
    from pathlib import Path as _P

    from ..core.files import sha256_of
    from .build import read_logs
    h = models.header(con, "geo", version_id)
    sj = h.get("spec_json") or {}
    study = (sj.get("data_source") or {}).get("study")
    if study:
        from ..data import store
        return {"study": study, "tops": store.tops(con, study), "surveys": store.survey_xyz(con, study),
                "logs": lambda curves: store.logs(con, study, curves),
                "source_file_ids": store.source_file_ids(con, study, store.FOR["geo"])}
    files = [dict(r) for r in con.execute(
        "SELECT f.id, f.path, f.kind, f.sha256 FROM geo_version_sources s JOIN files f ON f.id = s.file_id "
        "WHERE s.version_id = ? ORDER BY f.id", (version_id,))]
    tops = [f for f in files if f["kind"] == "tops"]
    logs = [f for f in files if f["kind"] == "well_log"]
    if len(tops) != 1 or not logs:
        raise ToolError(INVALID_INPUT, f"geo v{h['version']} doesn't record one tops file and its logs (a version from "
                                       "a deck, or built by hand): it can't be rebuilt with another spec")
    changed = [f["path"] for f in tops + logs if not (_P(f["path"]).is_file() and sha256_of(f["path"]) == f["sha256"])]
    if changed:
        raise ToolError(INVALID_INPUT, f"the base version's input files changed or are gone: {changed}")
    sv = None
    if isinstance(surveys, pd.DataFrame):
        sv = surveys
    elif surveys:
        from ..well.ingest import read_table
        from ..well.ingest import surveys as to_xyz
        sv = pd.DataFrame(to_xyz(read_table(surveys), {}))
    paths = [f["path"] for f in logs]
    return {"study": None, "tops": pd.read_csv(tops[0]["path"]), "surveys": surveys, "tops_path": tops[0]["path"],
            "log_paths": paths, "logs": lambda curves: read_logs(paths, sv, curves),
            "source_file_ids": [tops[0]["id"]] + [f["id"] for f in logs]}


def _row(con, preview_id: int) -> dict:
    r = con.execute("SELECT * FROM review_previews WHERE id = ?", (preview_id,)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"no preview {preview_id}")
    d = dict(r)
    d["spec_patch"] = json.loads(d.pop("spec_patch_json"))
    d["summary"] = json.loads(d.pop("summary_json") or "null")
    return d


def get(con, preview_id: int) -> dict:
    return _row(con, preview_id)


def prune(cfg, con) -> int:
    """Delete preview grids older than [geo_review] preview_keep_days (the rows and summaries stay)."""
    n = 0
    for r in con.execute("SELECT id, folder FROM review_previews WHERE removed_at IS NULL AND "
                         "julianday('now') - julianday(created_at) > ?", (cfg.review_preview_keep_days,)).fetchall():
        shutil.rmtree(r["folder"], ignore_errors=True)
        con.execute("UPDATE review_previews SET removed_at = datetime('now') WHERE id = ?", (r["id"],))
        n += 1
    return n


def load(cfg, con, preview_id: int, base: dict | None = None) -> dict:
    """A preview as the workbench draws it: the base version's data with the preview's spec, arrays and layers."""
    from . import review_views as views
    p = _row(con, preview_id)
    folder = Path(p["folder"])
    if p["removed_at"] or not (folder / "preview.npz").is_file():
        raise ToolError(NOT_FOUND, f"preview {preview_id}'s grid was removed (older than "
                                   f"{cfg.review_preview_keep_days} days); its summary is kept")
    base = base or views.load_version(cfg, con, p["base_version_id"])
    z = np.load(folder / "preview.npz")
    meta = json.loads((folder / "preview.json").read_text())
    arrays = {k: z[k] for k in ("PORO", "PERMX", "PERMY", "PERMZ", "NTG")}
    d = {**base, "spec": meta["spec"], "arrays": arrays, "layer_depths": z["layer_depths"], "nz": int(arrays["PORO"].shape[0]),
         "upscaled": pd.DataFrame(meta["upscaled"], columns=["well_name", "layer", "phi", "perm", "ntg"]),
         "logs": pd.read_csv(folder / "logs.csv"), "zones": meta["zones"], "files": [], "version_id": None,
         "preview_id": preview_id, "label": f"preview #{preview_id}", "keywords": sorted(arrays),
         "header": {**base["header"], "status": "preview"}}
    d["findings"] = meta.get("findings") or []
    return d


def run(cfg, con, base_version_id: int, spec_patch: dict, author_role: str, author: str) -> dict:
    """Build the preview, keep its grid and summary, and return the summary (with preview_id)."""
    from . import review_diff
    from . import review_views as views
    from .build import build_grid
    h = models.header(con, "geo", base_version_id)
    base_spec = props.base_spec(con, "geo", base_version_id)
    if not base_spec:
        raise ToolError(INVALID_INPUT, f"geo v{h['version']} has no spec to change (a version from a deck)")
    if not isinstance(spec_patch, dict) or not spec_patch:
        raise ToolError(INVALID_INPUT, "spec_patch must be a non-empty JSON object (a merge patch on the spec)")
    bad = set(spec_patch) - set(props.PATCHABLE["geo"])
    if bad:
        raise ToolError(INVALID_INPUT, f"spec_patch can change {props.PATCHABLE['geo']}, not {sorted(bad)}")
    spec = props.merge_patch(base_spec, spec_patch)
    props._check_geo_spec(spec)
    changes = props.spec_diff(base_spec, spec)
    if not changes:
        raise ToolError(INVALID_INPUT, "the patch changes nothing in the base spec")
    prune(cfg, con)
    inp = base_inputs(con, base_version_id)
    folder = cfg.file_store / "previews" / f"geo_{h['model_name']}_v{h['version']}_{uuid.uuid4().hex[:8]}"
    t0 = time.monotonic()
    lg = inp["logs"](spec.get("curves"))
    try:
        g = build_grid(inp["tops"], lg, spec, folder)
    except ValueError as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise ToolError(INVALID_INPUT, str(e)) from e
    elapsed = time.monotonic() - t0
    kv = (spec.get("petro") or {}).get("kv_kh", 0.1)
    ar = g["arrays"]
    np.savez_compressed(folder / "preview.npz", PORO=ar["phi"], PERMX=ar["perm"], PERMY=ar["perm"], PERMZ=ar["perm"] * kv,
                        NTG=ar["ntg"], layer_depths=np.load(g["layer_depths_file"]))
    lg[["well_name", "tvd", "phi", "perm", "vsh"]].to_csv(folder / "logs.csv", index=False)
    for f in list(g["files"].values()) + [g["grid_file"]]:
        Path(f).unlink(missing_ok=True)          # the .INC files and grid: the npz is what a preview needs
    (folder / "preview.json").write_text(json.dumps({"spec": spec, "zones": g["zones"], "upscaled": g["upscaled"]},
                                                    default=float))
    pid = con.execute("INSERT INTO review_previews(model_kind, model_name, base_version_id, spec_patch_json, author_role, "
                      "author, folder, elapsed_s) VALUES ('geo',?,?,?,?,?,?,?)",
                      (h["model_name"], base_version_id, json.dumps(spec_patch), author_role, author, str(folder),
                       round(elapsed, 3))).lastrowid
    base = views.load_version(cfg, con, base_version_id)
    d = load(cfg, con, pid, base)
    d["findings"] = gq.run_all_checks({**d, "files": []})
    meta = json.loads((folder / "preview.json").read_text())
    meta["findings"] = d["findings"]
    (folder / "preview.json").write_text(json.dumps(meta, default=float))
    s = review_diff.compare(d, base)
    summary = {"preview_id": pid, "base_version_id": base_version_id, "base_version": h["version"],
               "grid": f"{g['nx']} x {g['ny']} x {g['nz']}", "elapsed_s": round(elapsed, 2), "spec_patch": spec_patch,
               "changes": changes, **{k: s[k] for k in ("numbers", "findings", "maps")}}
    con.execute("UPDATE review_previews SET summary_json = ? WHERE id = ?", (json.dumps(summary, default=float), pid))
    return summary
