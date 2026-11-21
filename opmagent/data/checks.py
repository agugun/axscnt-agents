"""Checks that need both sides of the study data: the tables in the database and the files they refer to.

Each check returns issues (modeler, severity, item, message). The data manager's load writes them to data_issues
with everything else; a modeler can run them again during its analysis (MCP `check_study_data`). Nothing is
corrected here: a check says what doesn't fit, the data manager or the engineer decides.

    tops_vs_logs         every well with tops has porosity over the reservoir interval (tops table x LAS)
    perforations_vs_surveys  perforations lie inside the surveyed depth of their well (two tables)
    contacts_vs_logs     the OWC agrees with the water saturation logs (contacts x LAS), when there is an SW curve
    production_vs_wells  injectors don't produce, producers don't inject, no negative volumes (two tables)
"""
from __future__ import annotations

import json

import pandas as pd

from ..core.errors import ToolError
from . import store

SW_CURVES = ["SW", "SWT", "SWE", "SWAT", "SW_FINAL"]
COVERAGE = 0.8          # porosity must cover this share of the reservoir interval
CONTACT_TOL = 5.0       # m: how far a log may disagree with the contact before it is reported
PAY_PHI, PAY_SW, WATER_SW = 0.05, 0.5, 0.7


def _surfaces(con, sid) -> list[str]:
    r = con.execute("SELECT spec_json FROM data_geo_spec WHERE study_id = ?", (sid,)).fetchone()
    if r and json.loads(r[0]).get("surfaces"):
        return json.loads(r[0])["surfaces"]
    return store.inventory(con, sid)["datasets"]["tops"]["surfaces"]


def _well_logs(con, sid):
    """Per well: (log table, the geo modeler's tvd/phi rows on the same index). Unreadable files are skipped
    (the integrity check reports them)."""
    from ..geo.build import curve_map, pick_curves
    spec = store.geo_spec(con, sid) or {}
    cm, sv = curve_map(spec.get("curves")), store.survey_xyz(con, sid)
    out = {}
    for w in store.log_wells(con, sid):
        try:
            t = store.log_table(con, sid, w)
        except ToolError:
            continue
        out[w] = (t, pick_curves(t, w, sv, cm))
    return out


def tops_vs_logs(con, sid, logs=None) -> list[tuple]:
    tops = store.tops(con, sid)
    surf = _surfaces(con, sid)
    if tops.empty or len(surf) < 2:
        return []
    logs = _well_logs(con, sid) if logs is None else logs
    issues = []
    for w, g in tops.groupby("well_name"):
        z = dict(zip(g["surface"], g["tvd"]))
        if surf[0] not in z or surf[-1] not in z:
            continue
        top, base = float(z[surf[0]]), float(z[surf[-1]])
        if w not in logs:
            issues.append(("geo", "warning", w, f"{w} has tops but no logs: its properties come only from "
                                                f"the other wells"))
            continue
        lg = logs[w][1]
        inside = lg[(lg["tvd"] >= top) & (lg["tvd"] <= base) & lg["phi"].notna()]
        thick = base - top
        if thick <= 0:
            continue
        cover = (inside["tvd"].max() - inside["tvd"].min()) / thick if len(inside) > 1 else 0.0
        if cover < COVERAGE:
            issues.append(("geo", "warning", w,
                           f"{w}: porosity covers {cover:.0%} of the reservoir interval {surf[0]}-{surf[-1]} "
                           f"({top:.1f}-{base:.1f} m TVD); the log may be short, or the tops or the depth "
                           f"reference (MD/TVD) off"))
    for w in sorted(set(logs) - set(tops["well_name"])):
        issues.append(("geo", "warning", w, f"{w} has logs but no tops: check the well name matches"))
    return issues


def perforations_vs_surveys(con, sid) -> list[tuple]:
    inp = store.well_inputs(con, sid)
    perfs, sv = inp.get("perforations"), inp.get("surveys")
    if perfs is None:
        return []
    rng = {} if sv is None else {w: (g["md"].min(), g["md"].max()) for w, g in sv.groupby("well_name")}
    issues = []
    for r in perfs.itertuples():
        if r.top_md >= r.bottom_md:
            issues.append(("well", "required", r.well_name,
                           f"{r.well_name} perforation {r.top_md}-{r.bottom_md} m MD: top is not above bottom"))
        elif r.well_name not in rng:
            issues.append(("well", "required", r.well_name,
                           f"{r.well_name} is perforated but has no survey: the perforation can't be placed"))
        elif r.top_md < rng[r.well_name][0] - 1e-6 or r.bottom_md > rng[r.well_name][1] + 1e-6:
            lo, hi = rng[r.well_name]
            issues.append(("well", "required", r.well_name,
                           f"{r.well_name} perforation {r.top_md}-{r.bottom_md} m MD is outside its survey "
                           f"({lo}-{hi} m MD)"))
    return issues


def contacts_vs_logs(con, sid, logs=None) -> list[tuple]:
    c = con.execute("SELECT owc_depth FROM data_contacts WHERE study_id = ?", (sid,)).fetchone()
    if not c or c["owc_depth"] is None:
        return []
    owc = float(c["owc_depth"])
    logs = _well_logs(con, sid) if logs is None else logs
    issues = []
    for w, (t, lg) in logs.items():
        col = next((s for s in SW_CURVES if s in t.columns), None)
        if not col:
            continue
        d = pd.DataFrame({"tvd": lg["tvd"], "phi": lg["phi"], "sw": t[col]}).dropna()
        d = d[d["phi"] > PAY_PHI]
        if d.empty:
            continue
        pay = d[d["sw"] < PAY_SW]
        water = d[d["sw"] > WATER_SW]
        if len(pay) and pay["tvd"].max() > owc + CONTACT_TOL:
            issues.append(("res", "warning", w, f"{w}: hydrocarbon (SW < {PAY_SW}) down to {pay['tvd'].max():.1f} "
                                                f"m TVD, below the OWC at {owc:.1f} m"))
        if len(pay) and len(water):
            # the water leg: water below the top of the pay with no pay under it
            below_pay = water[water["tvd"] > pay["tvd"].min()]
            if len(below_pay) and below_pay["tvd"].min() < owc - CONTACT_TOL and \
                    not len(pay[pay["tvd"] > below_pay["tvd"].min()]):
                issues.append(("res", "warning", w, f"{w}: water (SW > {WATER_SW}) from "
                                                    f"{below_pay['tvd'].min():.1f} m TVD, above the OWC at {owc:.1f} m"))
    return issues


def production_vs_wells(con, sid) -> list[tuple]:
    inp = store.well_inputs(con, sid)
    h, w = inp.get("history"), inp.get("wells")
    if h is None or w is None:
        return []
    kind = dict(zip(w["well_name"], w["type"]))
    issues = []
    for col in ("oil", "water", "gas", "water_inj", "gas_inj"):
        if col in h and (h[col] < 0).any():
            bad = sorted(set(h.loc[h[col] < 0, "well_name"]))
            issues.append(("well", "warning", "history", f"negative {col} volumes for {', '.join(bad[:5])}"))
    for well, g in h.groupby("well_name"):
        t = kind.get(well)
        prod = sum(float(g[c].clip(lower=0).sum()) for c in ("oil", "gas") if c in g)
        inj = sum(float(g[c].clip(lower=0).sum()) for c in ("water_inj", "gas_inj") if c in g)
        if t == "INJECTOR" and prod > 0:
            issues.append(("well", "warning", well, f"{well} is an injector but reports oil or gas production: "
                                                    f"check its type, or whether it was converted (a date)"))
        if t == "PRODUCER" and inj > 0:
            issues.append(("well", "warning", well, f"{well} is a producer but reports injection: check its type, "
                                                    f"or whether it was converted (a date)"))
    return issues


def files_as_loaded(con, sid) -> list[tuple]:
    v = store.verify(con, sid)
    return [("data", "warning", b["original"], f"{b['original']} has {b['status']} since it was loaded")
            for b in v["not_ok"]]


def run_checks(con, sid) -> list[tuple]:
    """All checks; the logs are read once and shared."""
    sid = store.study_id(con, sid)
    logs = _well_logs(con, sid)
    return (files_as_loaded(con, sid) + tops_vs_logs(con, sid, logs) + contacts_vs_logs(con, sid, logs)
            + perforations_vs_surveys(con, sid) + production_vs_wells(con, sid))
