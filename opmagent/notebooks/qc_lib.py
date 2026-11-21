"""Helpers for the fluid QC notebooks (the standard template and the agent's investigation notebooks).

Read only: a notebook loads the QC payload the pipeline wrote and the model's tables straight from the
database (read-only connection), so it plots what the deck builder will actually read. It may rebuild the
EOS fluid from the stored spec to compute extra things (phase envelope, sensitivities) but never writes
model data. Every check returns a finding {area, check, status: pass|warn|fail|n/a, detail}.
"""
from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

LAB = dict(mode="markers", marker=dict(size=9, symbol="circle-open", line=dict(width=2)))
MODEL = dict(mode="lines", line=dict(width=2))
LAYOUT = dict(template="plotly_white", height=380, margin=dict(l=60, r=20, t=50, b=50),
              legend=dict(orientation="h", y=-0.2))
TABLES = {"pvto": "pvtnum, rs, pressure", "pvdg": "pvtnum, pressure", "pvtw": "pvtnum", "density": "pvtnum",
          "rsvd": "eqlnum, depth", "pbvd": "eqlnum, depth"}
STATUS_ORDER = {"fail": 0, "warn": 1, "pass": 2, "n/a": 3}
CCE_WARN, SEP_WARN = 0.02, 0.05      # informational thresholds (no configured tolerance for these)


def finding(area: str, check: str, status: str, detail: str = "") -> dict:
    return {"area": area, "check": check, "status": status, "detail": detail}


# ---------------------------------------------------------------- loading
def load(payload_path: str | Path, db_path: str | Path | None = None, version_id: int | None = None) -> dict:
    """The QC payload plus the version's tables as stored in the database (read-only)."""
    data = json.loads(Path(payload_path).read_text())
    db_path = db_path or data.get("db_path")
    version_id = version_id or data.get("version_id")
    data["tables"] = read_tables(db_path, version_id) if db_path and version_id else {}
    data["tables_from_db"] = bool(data["tables"])
    return data


def read_tables(db_path: str | Path, version_id: int) -> dict[str, list[dict]]:
    con = sqlite3.connect(f"file:{Path(db_path).expanduser()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return {t: [dict(r) for r in con.execute(f"SELECT * FROM {t} WHERE version_id = ? ORDER BY {order}",
                                                 (int(version_id),))]
                for t, order in TABLES.items()}
    finally:
        con.close()


def fluid_spec(data: dict):
    """The tuned EOS fluid spec, ready for opmagent.pvt.engine (for sensitivities in investigations)."""
    from ..pvt.engine import FluidSpec
    return FluidSpec(**data["spec"])


# ---------------------------------------------------------------- display helpers
def header(data: dict):
    from IPython.display import HTML
    s = data["spec"]
    eos = (f"{s['eos']}, volume shift {'on' if s['volume_shift'] else 'off'}, {s['pseudo_components']} pseudo-components, "
           f"C1/C7+ interaction coefficient {s.get('kij_c1_plus')}")
    src = "database" if data.get("tables_from_db") else "payload (database not available)"
    return HTML(f"<h1>Fluid model QC: {html.escape(data['model_name'])} version {data['version']}</h1>"
                f"<p>Sample {html.escape(str(data.get('sample') or 'n/a'))} &middot; built {data.get('created_at', '')}"
                f" &middot; EOS: {html.escape(eos)} &middot; tables read from the {src}</p>")


def findings_frame(findings: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(findings, columns=["area", "check", "status", "detail"])
    return df.sort_values("status", key=lambda s: s.map(STATUS_ORDER), kind="stable").reset_index(drop=True)


def show_findings(findings: list[dict]):
    from IPython.display import HTML
    colour = {"pass": "#1a7f37", "warn": "#9a6700", "fail": "#cf222e", "n/a": "#666"}
    td = "<td style='text-align:left'>"
    rows = "".join(f"<tr>{td}{html.escape(f['area'])}</td>{td}{html.escape(f['check'])}</td>"
                   f"<td style='text-align:left;color:{colour[f['status']]};font-weight:600'>{f['status']}</td>"
                   f"{td}{html.escape(f['detail'])}</td></tr>" for f in findings_frame(findings).to_dict("records"))
    th = "".join(f"<th style='text-align:left'>{h}</th>" for h in ("Area", "Check", "Status", "Detail"))
    return HTML(f"<table><tr>{th}</tr>{rows}</table>")


def show_tuning(data: dict):
    """The tuner's checks against the configured tolerances (the build's pass/fail gate)."""
    from IPython.display import HTML
    m, rows = data["metrics"], []
    for k, label in (("psat", "Saturation pressure"), ("bo", "DL Bo, max error"), ("rs", "DL Rs, max error"),
                     ("mu_o", "Oil viscosity, max error")):
        c = data["checks"].get(k)
        if c is None:
            rows.append((label, "no lab data", ""))
            continue
        badge = "<span style='color:#1a7f37;font-weight:600'>within tolerance</span>" if c["passed"] else \
                "<span style='color:#cf222e;font-weight:600'>OUTSIDE tolerance</span>"
        rows.append((f"{label} (tolerance {c['tolerance']*100:.0f}%)", f"{c['error']*100:+.2f}%", badge))
    rows += [("Saturation pressure, lab / model (bar)", f"{m['psat_lab']:.1f} / {m['psat_model']:.1f}", ""),
             ("Rs at bubble point (sm3/sm3)", f"{m['rs_at_pb']:.1f}", ""),
             ("Bo at bubble point (rm3/sm3)", f"{m['bo_at_pb']:.4f}", "")]
    body = "".join(f"<tr><td style='text-align:left'>{html.escape(a)}</td><td>{html.escape(b)}</td>"
                   f"<td style='text-align:left'>{c}</td></tr>" for a, b, c in rows)
    return HTML(f"<table>{body}</table>")


def show_list(items: list[str], empty: str = "None", title: str | None = None):
    from IPython.display import HTML
    return HTML((f"<h4>{html.escape(title)}</h4>" if title else "") + "<ul>" + ("".join(f"<li>{html.escape(str(i))}</li>" for i in items) or f"<li>{empty}</li>") + "</ul>")


# ---------------------------------------------------------------- lab data QC
def _monotonic(values: list[float], increasing: bool, strict: bool = False) -> bool:
    pairs = list(zip(values, values[1:]))
    if increasing:
        return all(b > a if strict else b >= a for a, b in pairs)
    return all(b < a if strict else b <= a for a, b in pairs)


def lab_checks(data: dict) -> list[dict]:
    lab, out, area = data["lab"], [], "lab data"
    total = sum(lab["composition"].values()) + (lab.get("plus_fraction") or {}).get("mol_percent", 0.0)
    dev = abs(total - 100.0)
    out.append(finding(area, "Composition sums to 100 mol %", "pass" if dev < 0.1 else "warn" if dev <= 1 else "fail",
                       f"{total:.2f} mol %"))
    dl = sorted(lab.get("dl") or [], key=lambda r: r["pressure"])
    if len(dl) >= 2:
        for key, label in (("rs", "Rs"), ("bo", "Bo")):
            vals = [r[key] for r in dl if r.get(key) is not None]
            ok = _monotonic(vals, increasing=True)
            out.append(finding(area, f"DL {label} increases with pressure", "pass" if ok else "fail",
                               "monotonic" if ok else f"not monotonic: {[round(v, 3) for v in vals]}"))
        dens = [r["oil_density"] for r in dl if r.get("oil_density")]
        if len(dens) >= 2:
            ok = _monotonic(dens, increasing=False)
            out.append(finding(area, "DL oil density falls as pressure rises", "pass" if ok else "warn",
                               "consistent" if ok else "density does not fall with pressure; check the DL densities"))
    else:
        out.append(finding(area, "DL data", "n/a", "no DL data"))
    cce = sorted(lab.get("cce") or [], key=lambda r: r["pressure"])
    psat = data["metrics"]["psat_lab"]
    if len(cce) >= 2:
        above = [r for r in cce if r["pressure"] > psat * 1.001]
        below = [r for r in cce if r["pressure"] < psat * 0.999]
        bad = [r["pressure"] for r in above if r["relative_volume"] > 1.002] + \
              [r["pressure"] for r in below if r["relative_volume"] < 0.998]
        out.append(finding(area, "CCE break consistent with lab Psat", "pass" if not bad else "warn",
                           f"Psat {psat:.1f} bar" if not bad else
                           f"V/Vsat on the wrong side of 1 at {bad} bar for Psat {psat:.1f} bar"))
    else:
        out.append(finding(area, "CCE data", "n/a", "no CCE data"))
    return out


# ---------------------------------------------------------------- lab vs model
def dl_model_rows(data: dict) -> list[dict]:
    """DL simulated at the lab's own pressure steps (DL is path dependent: a denser grid liberates gas
    differently, so the dense plotting curve is not the one to compare point by point)."""
    return data["model"].get("dl_lab_steps") or data["model"].get("dl") or []


def match_frame(lab_rows: list[dict], model_rows: list[dict], lab_key: str, model_key: str) -> pd.DataFrame:
    model = {round(r["pressure"], 3): r for r in model_rows}
    rows = []
    for r in lab_rows:
        m = model.get(round(float(r["pressure"]), 3))
        if m is None or r.get(lab_key) in (None, 0) or m.get(model_key) is None:
            continue
        rows.append({"pressure": r["pressure"], "lab": r[lab_key], "model": m[model_key],
                     "error_pct": 100.0 * (m[model_key] - r[lab_key]) / r[lab_key]})
    return pd.DataFrame(rows, columns=["pressure", "lab", "model", "error_pct"])


def match_checks(data: dict) -> list[dict]:
    out, area = [], "lab vs model"
    names = {"psat": "Saturation pressure", "bo": "DL Bo", "rs": "DL Rs", "mu_o": "Oil viscosity"}
    for k, c in data["checks"].items():
        out.append(finding(area, f"{names.get(k, k)} within tolerance", "pass" if c["passed"] else "fail",
                           f"max error {c['error']*100:+.2f}% (tolerance {c['tolerance']*100:.0f}%)"))
    for k in ("bo", "rs", "mu_o"):
        if k not in data["checks"]:
            out.append(finding(area, f"{names[k]} within tolerance", "n/a", "no lab data"))
    cce = match_frame(data["lab"].get("cce") or [], data["model"].get("cce") or [], "relative_volume", "relative_volume")
    if len(cce):
        e = cce["error_pct"].abs().max()
        out.append(finding(area, "CCE relative volume", "pass" if e <= CCE_WARN * 100 else "warn",
                           f"max error {e:.2f}% (informational limit {CCE_WARN*100:.0f}%)"))
    dl = match_frame(data["lab"].get("dl") or [], dl_model_rows(data), "oil_density", "oil_density")
    if len(dl):
        e = dl["error_pct"].abs().max()
        out.append(finding(area, "DL oil density", "pass" if e <= CCE_WARN * 100 else "warn",
                           f"max error {e:.2f}% (informational limit {CCE_WARN*100:.0f}%)"))
    meas, sep = data["lab"].get("measured") or {}, data["model"].get("separator") or {}
    for mk, sk, label in (("measured_gor_sm3_sm3", "gor_sm3_sm3", "Separator GOR"), ("measured_api", "api", "Stock-tank API"),
                          ("measured_bo_fb", "bo_at_pb", "Separator Bo at Pb")):
        if meas.get(mk) and sep.get(sk) is not None:
            e = (sep[sk] - meas[mk]) / meas[mk]
            out.append(finding(area, label, "pass" if abs(e) <= SEP_WARN else "warn",
                               f"model {sep[sk]:.3g} vs measured {meas[mk]:.3g} ({e*100:+.1f}%)"))
    return out


# ---------------------------------------------------------------- black-oil tables
def table_checks(data: dict) -> list[dict]:
    """The checks OPM Flow relies on, run on the tables as stored."""
    t, out, area = data["tables"], [], "tables"
    if not data.get("tables_from_db"):
        return [finding(area, "Tables read from the database", "fail", "database not available to the notebook")]
    pvto = t.get("pvto") or []
    if not pvto:
        out.append(finding(area, "PVTO present", "fail", "no PVTO rows"))
    for pvtnum in sorted({r["pvtnum"] for r in pvto}):
        rows = [r for r in pvto if r["pvtnum"] == pvtnum]
        sat = sorted((r for r in rows if r["is_saturated"]), key=lambda r: r["rs"])
        tag = f" (PVTNUM {pvtnum})" if pvtnum > 1 or len({r['pvtnum'] for r in pvto}) > 1 else ""
        for key, inc, label in (("pressure", True, "Saturated Pb increases with Rs"),
                                ("bo", True, "Saturated Bo increases with Rs"),
                                ("mu_o", False, "Saturated oil viscosity falls with Rs")):
            ok = _monotonic([r[key] for r in sat], increasing=inc, strict=key == "pressure")
            out.append(finding(area, label + tag, "pass" if ok else "fail" if key != "mu_o" else "warn",
                               "monotonic" if ok else "not monotonic"))
        bad_bo, bad_mu = [], []
        for rs in sorted({r["rs"] for r in rows}):
            br = sorted((r for r in rows if r["rs"] == rs), key=lambda r: r["pressure"])
            if len(br) > 1:
                if not _monotonic([r["bo"] for r in br], increasing=False, strict=True):
                    bad_bo.append(round(rs, 1))
                if not _monotonic([r["mu_o"] for r in br], increasing=True):
                    bad_mu.append(round(rs, 1))
        out.append(finding(area, "Undersaturated Bo falls with pressure" + tag, "fail" if bad_bo else "pass",
                           f"Rs blocks {bad_bo}" if bad_bo else "all branches"))
        out.append(finding(area, "Undersaturated oil viscosity rises with pressure" + tag, "warn" if bad_mu else "pass",
                           f"Rs blocks {bad_mu}" if bad_mu else "all branches"))
        if sat:
            top = [r for r in rows if r["rs"] == sat[-1]["rs"]]
            out.append(finding(area, "Highest Rs block has an undersaturated branch" + tag,
                               "pass" if len(top) > 1 else "fail",
                               f"{len(top) - 1} undersaturated row(s); OPM needs at least one"))
    pvdg = sorted(t.get("pvdg") or [], key=lambda r: (r["pvtnum"], r["pressure"]))
    if pvdg:
        ok_bg = _monotonic([r["bg"] for r in pvdg], increasing=False, strict=True)
        ok_mu = _monotonic([r["mu_g"] for r in pvdg], increasing=True)
        out.append(finding(area, "PVDG Bg falls with pressure", "pass" if ok_bg else "fail", "" if ok_bg else "not monotonic"))
        out.append(finding(area, "PVDG gas viscosity rises with pressure", "pass" if ok_mu else "warn",
                           "" if ok_mu else "not monotonic"))
    else:
        out.append(finding(area, "PVDG present", "fail", "no PVDG rows"))
    for name in ("pvtw", "density"):
        out.append(finding(area, f"{name.upper()} present", "pass" if t.get(name) else "fail",
                           f"{len(t.get(name) or [])} row(s)"))
    p_max = (data.get("p_range") or [None, None])[1]
    if p_max and pvto:
        top_p = max(r["pressure"] for r in pvto)
        out.append(finding(area, "PVTO covers the maximum pressure", "pass" if top_p >= p_max * 0.999 else "warn",
                           f"table to {top_p:.0f} bar, requested {p_max:.0f} bar"))
    rsvd = t.get("rsvd") or []
    if rsvd and pvto:
        rs_max = max(r["rs"] for r in pvto if r["is_saturated"])
        hi = max(r["rs"] for r in rsvd)
        out.append(finding(area, "RSVD within the PVTO Rs range", "pass" if hi <= rs_max * 1.0001 else "fail",
                           f"RSVD max {hi:.1f}, PVTO max saturated Rs {rs_max:.1f}"))
    return out


def run_all_checks(data: dict) -> list[dict]:
    return lab_checks(data) + match_checks(data) + table_checks(data)


def overall(findings: list[dict]) -> str:
    statuses = {f["status"] for f in findings}
    return "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"


def write_summary(path: str | Path, findings: list[dict], data: dict) -> dict:
    counts = {s: sum(f["status"] == s for f in findings) for s in ("pass", "warn", "fail", "n/a")}
    s = {"model_name": data["model_name"], "version": data["version"], "version_id": data.get("version_id"),
         "overall": overall(findings), "counts": counts, "findings": findings,
         "tables_from_db": data.get("tables_from_db", False)}
    Path(path).write_text(json.dumps(s, indent=2))
    return s


# ---------------------------------------------------------------- plots
def _xy(rows, x, y):
    rows = [r for r in rows if r.get(y) is not None]
    return {"x": [r[x] for r in rows], "y": [r[y] for r in rows]}


def fig_lab_vs_model(title, xlab, ylab, lab_rows, lab_key, model_rows, model_key, name):
    fig = go.Figure()
    fig.add_trace(go.Scatter(**_xy(model_rows, "pressure", model_key), name=f"Model {name}", **MODEL))
    if lab_rows:
        fig.add_trace(go.Scatter(**_xy(lab_rows, "pressure", lab_key), name=f"Lab {name}", **LAB))
    fig.update_layout(title=title, xaxis_title=xlab, yaxis_title=ylab, **LAYOUT)
    return fig


def fig_dl(data: dict):
    """Model on the lab's own steps (solid) and on the dense grid (dotted); DL depends on the step sequence."""
    dense, steps, lab = data["model"].get("dl") or [], data["model"].get("dl_lab_steps") or [], data["lab"].get("dl") or []
    fig = make_subplots(rows=1, cols=3, subplot_titles=("DL Bo (rm3/sm3)", "DL Rs (sm3/sm3)", "Oil density (kg/m3)"))
    for col, (mk, lk) in enumerate((("bo_d", "bo"), ("rs_d", "rs"), ("oil_density", "oil_density")), start=1):
        if steps:
            fig.add_trace(go.Scatter(**_xy(steps, "pressure", mk), name="Model, lab steps", showlegend=col == 1,
                                     **MODEL), 1, col)
        fig.add_trace(go.Scatter(**_xy(dense, "pressure", mk), name="Model, dense grid" if steps else "Model",
                                 showlegend=col == 1, mode="lines",
                                 line=dict(width=1, dash="dot") if steps else MODEL["line"]), 1, col)
        if lab:
            fig.add_trace(go.Scatter(**_xy(lab, "pressure", lk), name="Lab", showlegend=col == 1, **LAB), 1, col)
    fig.update_xaxes(title_text="Pressure (bar)")
    fig.update_layout(title="Differential liberation", **LAYOUT)
    return fig


def fig_composition(components: list[dict]):
    """Log mole fraction against molar mass: the plus-fraction split should continue the C6- trend."""
    hc = [c for c in components if c["name"] not in ("nitrogen", "CO2", "H2S", "water") and c["mol_percent"] > 0]
    fig = go.Figure()
    for pseudo, label in ((False, "Defined components"), (True, "Pseudo-components (C7+ split)")):
        cc = [c for c in hc if c["pseudo"] == pseudo]
        fig.add_trace(go.Scatter(x=[c["mw_g_mol"] for c in cc], y=[c["mol_percent"] for c in cc], mode="markers",
                                 text=[c["name"] for c in cc], name=label, marker=dict(size=9)))
    fig.update_layout(title="Characterised composition", xaxis_title="Molar mass (g/mol)",
                      yaxis_title="Mol %", yaxis_type="log", **LAYOUT)
    return fig


def fig_phase_envelope(env: dict, data: dict):
    fig = go.Figure()
    for key, name in (("bubble", "Bubble line"), ("dew", "Dew line")):
        if env.get(key):
            t, p = zip(*env[key])
            fig.add_trace(go.Scatter(x=t, y=p, name=name, **MODEL))
    t_res, psat = data["spec"]["temperature_c"], data["metrics"]["psat_model"]
    fig.add_trace(go.Scatter(x=[t_res], y=[psat], name="Psat at reservoir T (model)", mode="markers",
                             marker=dict(size=11, symbol="x")))
    fig.add_trace(go.Scatter(x=[t_res], y=[data["metrics"]["psat_lab"]], name="Psat (lab)", **LAB))
    stages = data["lab"].get("separator_stages") or []
    if stages:
        fig.add_trace(go.Scatter(x=[s["temperature"] for s in stages], y=[s["pressure"] for s in stages],
                                 name="Separator stages", mode="markers+lines", line=dict(dash="dot")))
    for key, name in (("cricondenbar", "Cricondenbar"), ("cricondentherm", "Cricondentherm")):
        pt = env.get(key)
        if pt:
            fig.add_trace(go.Scatter(x=[pt["temperature_c"]], y=[pt["pressure"]], name=name, mode="markers",
                                     marker=dict(size=8, symbol="diamond")))
    fig.update_layout(title="P-T phase envelope", xaxis_title="Temperature (degC)", yaxis_title="Pressure (bar)", **LAYOUT)
    return fig


def fig_pvto(pvto: list[dict]):
    fig = make_subplots(rows=1, cols=3, subplot_titles=("Bo (rm3/sm3)", "Rs (saturated, sm3/sm3)", "Oil viscosity (cP)"))
    for pvtnum in sorted({r["pvtnum"] for r in pvto}):
        rows = [r for r in pvto if r["pvtnum"] == pvtnum]
        for rs in sorted({r["rs"] for r in rows}):
            br = sorted((r for r in rows if r["rs"] == rs), key=lambda r: r["pressure"])
            for col, key in ((1, "bo"), (3, "mu_o")):
                fig.add_trace(go.Scatter(x=[r["pressure"] for r in br], y=[r[key] for r in br], mode="lines+markers",
                                         marker=dict(size=4), line=dict(width=1, dash="dot"), showlegend=False,
                                         hovertext=f"Rs {rs:.1f}"), 1, col)
        sat = sorted((r for r in rows if r["is_saturated"]), key=lambda r: r["pressure"])
        for col, key in ((1, "bo"), (2, "rs"), (3, "mu_o")):
            fig.add_trace(go.Scatter(x=[r["pressure"] for r in sat], y=[r[key] for r in sat],
                                     name=f"Saturated (PVTNUM {pvtnum})", showlegend=col == 1, **MODEL), 1, col)
    fig.update_xaxes(title_text="Pressure (bar)")
    fig.update_layout(title="PVTO as stored (dotted: undersaturated branches)", **LAYOUT)
    return fig


def fig_pvdg(pvdg: list[dict]):
    fig = make_subplots(rows=1, cols=2, subplot_titles=("Bg (rm3/sm3)", "Gas viscosity (cP)"))
    for pvtnum in sorted({r["pvtnum"] for r in pvdg}):
        rows = sorted((r for r in pvdg if r["pvtnum"] == pvtnum), key=lambda r: r["pressure"])
        fig.add_trace(go.Scatter(x=[r["pressure"] for r in rows], y=[r["bg"] for r in rows], name=f"PVTNUM {pvtnum}",
                                 **MODEL), 1, 1)
        fig.add_trace(go.Scatter(x=[r["pressure"] for r in rows], y=[r["mu_g"] for r in rows], showlegend=False,
                                 **MODEL), 1, 2)
    fig.update_xaxes(title_text="Pressure (bar)")
    fig.update_layout(title="PVDG as stored", **LAYOUT)
    return fig
