"""Version diff for the geo review workbench (docs/design/geo-review.md, section 5): what changed between two versions
of a geo model, and whether it fixed what the engineer pointed at.

  spec        the dotted keys that differ between the two specs
  numbers     grid, zone pore volume / HCPV / mean phi and perm, tops misfit RMS per surface, at-well and
              leave-one-out errors, mean porosity, side by side with the change
  findings    QC findings fixed, still there, new (by area and check)
  maps        how the two can be compared cell by cell: by layer (same grid), by zone (same areal grid and zones),
              or not at all; the diff_map view draws it
  threads     the comments the new version answers, each with the view it was made on (to draw for both versions)
"""
from __future__ import annotations

import numpy as np

from ..core import models
from ..core import proposals as props
from ..core import review
from ..notebooks import geo_qc_lib as gq
from . import review_views as views


def default_base(con, version_id: int, kind: str = "geo") -> int | None:
    """The version this one should be compared with: the base of the proposal it was built from, else the one before."""
    h = models.header(con, kind, version_id)
    pid = (h.get("spec_json") or {}).get("proposal_id")
    if pid:
        r = con.execute("SELECT base_version_id FROM model_proposals WHERE id = ?", (pid,)).fetchone()
        if r:
            return r[0]
    prev = [m for m in models.list_models(con, kind) if m["model_name"] == h["model_name"] and m["version"] < h["version"]]
    return max(prev, key=lambda m: m["version"])["version_id"] if prev else None


def comparable(new: dict, old: dict) -> str | None:
    if (new["nx"], new["ny"]) != (old["nx"], old["ny"]) or new["areal"] != old["areal"]:
        return None
    if new["nz"] == old["nz"] and new["spec"]["layers"] == old["spec"]["layers"]:
        return "layer"
    if [z["zone"] for z in views.zones(new)] == [z["zone"] for z in views.zones(old)]:
        return "zone"
    return None


def zone_mean(data: dict, kw: str, zone: str) -> np.ndarray:
    z = next(z for z in views.zones(data) if z["zone"] == zone)
    return data["arrays"][kw][z["first"] - 1:z["last"]].mean(axis=0)


def _rms(v) -> float | None:
    v = np.asarray(v, dtype=float)
    v = v[np.isfinite(v)]
    return float(np.sqrt((v ** 2).mean())) if v.size else None


def metrics(data: dict) -> dict:
    out = {"cells": data["nx"] * data["ny"] * data["nz"], "grid": f"{data['nx']} x {data['ny']} x {data['nz']}",
           "mean_PORO": float(data["arrays"]["PORO"].mean()) if "PORO" in data["arrays"] else None}
    for kw in data["keywords"]:     # every property: geometric mean for permeabilities, else arithmetic
        if kw == "PORO":
            continue
        a = data["arrays"][kw]
        out[f"mean_{kw}"] = float(np.exp(np.log(np.clip(a, 1e-9, None)).mean())) if kw.startswith("PERM") else float(a.mean())
    mf = gq.tops_misfit(data)
    for s in data["spec"]["surfaces"]:
        out[f"tops_rms_m.{s}"] = _rms(mf.loc[mf["surface"] == s, "misfit"]) if len(mf) else None
    aw = gq.at_wells(data)
    for c in ("d_phi", "d_log10_perm", "d_ntg"):
        out[f"at_wells_rms.{c[2:]}"] = _rms(aw[c]) if len(aw) else None
    try:
        loo = gq.leave_one_out(data)
        out["loo_rms.phi"] = _rms(loo["phi_predicted"] - loo["phi_log"]) if len(loo) else None
    except Exception:  # noqa: BLE001  a check that can't run leaves its number out
        out["loo_rms.phi"] = None
    for z in data.get("zones") or []:          # the build's zone summary (payload)
        for k in ("pore_volume_rm3", "hcpv_rm3", "mean_phi", "mean_perm_md"):
            out[f"zone.{z['zone']}.{k}"] = z.get(k)
    return out


def _change(old, new):
    """new - old; 0 when the two agree to the 5 significant digits shown, or within 1e-6 of their size (kriging
    solves aren't bit-for-bit, and an error statistic near zero is all noise); None unless both are numbers."""
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    if not (num(old) and num(new)):
        return None
    d = new - old
    if abs(d) <= 1e-6 * max(abs(old), abs(new), 1e-12) or _r(old) == _r(new):
        return 0.0
    return _r(d)


def _r(v):
    return float(f"{v:.5g}") if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def compare(new: dict, old: dict, kind: str = "geo") -> dict:
    """Two loaded models (versions or a preview) side by side: spec, numbers, QC findings, how maps compare."""
    from ..reviews import views as views_for
    V = views_for(kind)
    mn, mo = V.metrics(new), V.metrics(old)
    numbers = [{"key": k, "old": _r(mo.get(k)), "new": _r(mn.get(k)), "change": _change(mo.get(k), mn.get(k))}
               for k in list(dict.fromkeys(list(mo) + list(mn)))]
    key = lambda f: f"{f['area']}: {f['check']}"  # noqa: E731
    fo, fn = {key(f): f for f in old["findings"]}, {key(f): f for f in new["findings"]}
    bad = ("warn", "fail")
    findings = {
        "fixed": [{"finding": k, "was": fo[k]["status"], "detail": fn.get(k, {}).get("detail")}
                  for k in fo if fo[k]["status"] in bad and fn.get(k, {}).get("status") not in bad],
        "still": [{"finding": k, "status": fn[k]["status"], "detail": fn[k]["detail"]}
                  for k in fn if fn[k]["status"] in bad and fo.get(k, {}).get("status") in bad],
        "new": [{"finding": k, "status": fn[k]["status"], "detail": fn[k]["detail"]}
                for k in fn if fn[k]["status"] in bad and fo.get(k, {}).get("status") not in bad]}
    return {"spec": props.spec_diff(old.get("spec") or {}, new.get("spec") or {}), "numbers": numbers,
            "findings": findings, "maps": V.comparable(new, old), **V.diff_extra(new, old)}


def diff(cfg, con, version_id: int, base_version_id: int | None = None, kind: str = "geo") -> dict:
    """The version diff for any reviewable kind (geo, res, well)."""
    from ..reviews import views as views_for
    V = views_for(kind)
    base_version_id = base_version_id or default_base(con, version_id, kind)
    if base_version_id is None:
        return {"version_id": version_id, "base_version_id": None, "message": "no earlier version to compare with"}
    new, old = V.load_version(cfg, con, version_id), V.load_version(cfg, con, base_version_id)
    if new["model_name"] != old["model_name"]:
        from ..core.errors import INVALID_INPUT, ToolError
        raise ToolError(INVALID_INPUT, f"versions {version_id} and {base_version_id} are of different models")
    c = compare(new, old, kind)
    built_from = [p for p in props.list_proposals(con, kind, new["model_name"]) if p["built_version_id"] == version_id] \
        if kind in props.KINDS else []
    fids = {f["id"] for f in review.list_feedback(con, kind, new["model_name"], status=None)
            if f["addressed_in_version_id"] == version_id}
    for p in built_from:
        fids |= set(p["feedback_ids"])
    return {"version_id": version_id, "version": new["version"], "base_version_id": base_version_id,
            "base_version": old["version"], **c, "proposals": [{"id": p["id"], "changes": p["changes"], "rationale": p["rationale"]}
                                           for p in built_from],
            "threads": [review.get(con, f) for f in sorted(fids)]}
