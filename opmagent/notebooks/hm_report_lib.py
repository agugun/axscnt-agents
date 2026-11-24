"""Helpers for the HM report notebook (hm_report.ipynb).

Read only: the notebook loads the payload `opmagent.hm.service.write_report` wrote (misfit per well and quantity,
findings, objective, well positions) and the observed and simulated monthly series next to it (observed.csv,
simulated.csv). It plots; it computes nothing that changes the misfit.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .qc_lib import LAYOUT

LABEL = {"oil": "Oil rate", "water": "Water rate", "gas": "Gas rate", "water_inj": "Water injection rate",
         "gas_inj": "Gas injection rate", "bhp": "BHP"}
OBS = dict(mode="markers", marker=dict(size=6, symbol="circle-open", color="#222"))
SIM = dict(mode="lines", line=dict(width=2, color="#0969da"))
BAND = dict(mode="lines", line=dict(width=0), fillcolor="rgba(120,120,120,0.18)", showlegend=False, hoverinfo="skip")


def load(payload_path: str | Path) -> dict:
    p = Path(payload_path)
    data = json.loads(p.read_text())
    data["obs"] = pd.read_csv(p.parent / "observed.csv")
    data["sim"] = pd.read_csv(p.parent / "simulated.csv")
    data["base"] = pd.read_csv(p.parent / "base.csv") if (p.parent / "base.csv").exists() else None
    return data


def header(data: dict):
    from IPython.display import HTML
    m = data.get("misfit")
    mtxt = f"{m:.3f} over {data['points']} points" if m is not None else "not defined"
    title = (f"History match {html.escape(data['hm']['name'])} (revision {data['hm']['revision']})" if data.get("hm")
             else f"History match report: run {data['run_id']}")
    best = data.get("best_case")
    case = (f"<p>Best case {best['case_no']} ({html.escape(str(best.get('label') or 'search'))})"
            + (f"; base case misfit {data['base_case']['misfit']:.3f}" if data.get("base_case") else "") + "</p>"
            if best else "")
    return HTML(f"<h1>{title}</h1>"
                f"<p>Deck {html.escape(str(data.get('deck_name')))} (id {data['deck_id']})"
                f"{' &middot; study ' + html.escape(data['study']) if data.get('study') else ''}"
                f" &middot; observations: {html.escape(data['source'].replace('_', ' '))}"
                f" &middot; built {data.get('created_at', '')}</p>"
                f"<p><b>Misfit {mtxt}</b></p>" + case)


def findings(data: dict):
    from IPython.display import HTML
    items = data.get("findings") or ["No large differences."]
    return HTML("<ul>" + "".join(f"<li>{html.escape(f)}</li>" for f in items) + "</ul>")


def terms_frame(data: dict) -> pd.DataFrame:
    t = pd.DataFrame(data.get("terms") or [])
    if t.empty:
        return t
    cols = [c for c in ("well", "quantity", "n", "misfit", "honoured", "in_total", "cum_error_pct", "last_error")
            if c in t]
    return t[cols].sort_values("misfit", ascending=False).reset_index(drop=True)


def wells_frame(data: dict) -> pd.DataFrame:
    w = pd.DataFrame(data.get("wells") or [])
    if w.empty:
        return w
    for c in ("controls", "honoured"):
        if c in w:
            w[c] = w[c].map(lambda x: ", ".join(x) if isinstance(x, list) else x)
    return w


def _sigma(data: dict, q: str, obs: np.ndarray) -> np.ndarray:
    s = data["objective"]["sigma"].get(q, {"rel": 0.1, "abs": 1.0})
    return np.maximum(s.get("rel", 0) * np.abs(obs), s.get("abs", 0))


def wells(data: dict) -> list[str]:
    return sorted(set(data["obs"]["well"]) & set(data["sim"]["well"]))


def fig_well(data: dict, well: str):
    """Observed against simulated per quantity for one well, with the measurement band (± σ)."""
    o, s = data["obs"][data["obs"]["well"] == well], data["sim"][data["sim"]["well"] == well]
    qs = [q for q in LABEL if q in set(o["quantity"])
          and (o[o["quantity"] == q]["value"].abs().sum() > 0 or q == "bhp")]
    if not qs:
        return None
    terms = {t["quantity"]: t for t in data.get("terms") or [] if t["well"] == well}

    def note(q):
        """Is q given to Flow (honoured)? In every month, or in some: a well can switch control (ORAT, RESV)."""
        t = terms.get(q)
        if not t:
            return ""
        if not t.get("in_total", True):
            return " (honoured: given to Flow)"
        n = t.get("honoured_months") or 0
        return f" (given to Flow in {n} month(s), matched in {t['n']})" if n else ""
    fig = make_subplots(rows=len(qs), cols=1, shared_xaxes=True, vertical_spacing=0.06,
                        subplot_titles=[LABEL[q] + note(q) for q in qs])
    for i, q in enumerate(qs, start=1):
        oq = o[o["quantity"] == q].sort_values("month")
        sq = s[s["quantity"] == q].sort_values("month")
        sig = _sigma(data, q, oq["value"].to_numpy())
        fig.add_trace(go.Scatter(x=oq["month"], y=oq["value"] + sig, **BAND), row=i, col=1)
        fig.add_trace(go.Scatter(x=oq["month"], y=oq["value"] - sig, fill="tonexty", **BAND), row=i, col=1)
        fig.add_trace(go.Scatter(x=oq["month"], y=oq["value"], name="observed", showlegend=i == 1, **OBS),
                      row=i, col=1)
        if data.get("base") is not None:
            b = data["base"]
            bq = b[(b["well"] == well) & (b["quantity"] == q)].sort_values("month")
            fig.add_trace(go.Scatter(x=bq["month"], y=bq["value"], name="base case", showlegend=i == 1, mode="lines",
                                     line=dict(width=1.5, color="#8c959f", dash="dash")), row=i, col=1)
        fig.add_trace(go.Scatter(x=sq["month"], y=sq["value"], name="best case" if data.get("best_case") else
                                 "simulated", showlegend=i == 1, **SIM), row=i, col=1)
    w = {d["well"]: d for d in data.get("wells", [])}.get(well, {})
    for key, dash, name in (("breakthrough_obs", "dot", "observed"), ("breakthrough_sim", "dash", "simulated")):
        if w.get(key):
            fig.add_vline(x=w[key], line_dash=dash, line_color="#cf222e", opacity=0.6)
            fig.add_annotation(x=w[key], y=1, yref="paper", text=f"water ({name})", showarrow=False,
                               font=dict(size=10, color="#cf222e"), yshift=8 if name == "observed" else -6)
    fig.update_layout(**{**LAYOUT, "height": 220 * len(qs) + 80},
                      title=f"{well}: misfit {w['misfit']:.2f}" if w.get("misfit") is not None else well)
    return fig


def fig_field(data: dict):
    """Field cumulative oil, water and gas (sums of the wells' monthly volumes), observed against simulated."""
    def cum(df, q):
        d = df[df["quantity"] == q].copy()
        if d.empty:
            return None
        d["vol"] = d["value"] * d["days"]
        return d.groupby("month")["vol"].sum().sort_index().cumsum()
    qs = [q for q in ("oil", "water", "gas", "water_inj", "gas_inj") if q in set(data["obs"]["quantity"])]
    qs = [q for q in qs if data["obs"][data["obs"]["quantity"] == q]["value"].abs().sum() > 0]
    if not qs:
        return None
    fig = make_subplots(rows=1, cols=len(qs), subplot_titles=[f"Cumulative {LABEL[q].lower().replace(' rate', '')}"
                                                              for q in qs])
    # simulated on the observed months only, so both cumulatives cover the same time
    months = set(data["obs"]["month"])
    sim = data["sim"][data["sim"]["month"].isin(months)]
    for i, q in enumerate(qs, start=1):
        co, cs = cum(data["obs"], q), cum(sim, q)
        fig.add_trace(go.Scatter(x=co.index, y=co.values, name="observed", showlegend=i == 1, **OBS), row=1, col=i)
        if cs is not None:
            fig.add_trace(go.Scatter(x=cs.index, y=cs.values, name="simulated", showlegend=i == 1, **SIM),
                          row=1, col=i)
    fig.update_layout(**LAYOUT, title="Field")
    return fig


def fig_misfit_bars(data: dict):
    """Misfit per well, stacked by quantity (terms in the total only)."""
    t = pd.DataFrame(data.get("terms") or [])
    if t.empty:
        return None
    t = t[t["in_total"]]
    if t.empty:
        return None
    n = t.groupby("well")["n"].sum()
    fig = go.Figure()
    for q, g in t.groupby("quantity"):
        g = g.set_index("well")
        fig.add_trace(go.Bar(x=g.index, y=(g["weight"] * g["sum_sq"] / n[g.index]).values, name=LABEL.get(q, q)))
    fig.update_layout(**LAYOUT, barmode="stack", title="Misfit per well, by quantity", yaxis_title="misfit")
    return fig


def fig_misfit_map(data: dict):
    """Misfit per well at the well positions (grid i, j): regional patterns show."""
    pos = data.get("well_positions") or {}
    w = [d for d in data.get("wells", []) if d["well"] in pos and d.get("misfit") is not None]
    if not w:
        return None
    x = [pos[d["well"]][0] for d in w]
    y = [pos[d["well"]][1] for d in w]
    m = [d["misfit"] for d in w]
    size = 12 + 30 * np.sqrt(np.asarray(m) / max(max(m), 1e-12))
    fig = go.Figure(go.Scatter(x=x, y=y, mode="markers+text", text=[d["well"] for d in w], textposition="top center",
                               marker=dict(size=size, color=m, colorscale="OrRd", showscale=True,
                                           colorbar=dict(title="misfit"), line=dict(width=1, color="#444"))))
    fig.update_layout(**{**LAYOUT, "height": 460}, title="Misfit at the well positions",
                      xaxis_title="i", yaxis_title="j", yaxis=dict(autorange="reversed", scaleanchor="x"))
    return fig


def fig_tornado(data: dict):
    """Misfit change per parameter at its low and high end, against the base case (the sensitivity)."""
    tor = data.get("tornado")
    if not tor:
        return None
    tor = list(reversed(tor))      # rank 1 on top
    names = [d["name"] for d in tor]
    fig = go.Figure()
    fig.add_trace(go.Bar(y=names, x=[d["delta_low"] for d in tor], orientation="h", name="low end",
                         marker_color="#0969da", text=[f"{d['low']:g}" for d in tor], textposition="auto"))
    fig.add_trace(go.Bar(y=names, x=[d["delta_high"] for d in tor], orientation="h", name="high end",
                         marker_color="#bf8700", text=[f"{d['high']:g}" for d in tor], textposition="auto"))
    fig.add_vline(x=0, line_color="#444")
    fig.update_layout(**{**LAYOUT, "height": 120 + 40 * len(tor)}, barmode="overlay",
                      title="Sensitivity: misfit change from the base case (left: better)",
                      xaxis_title="misfit change")
    return fig


def tornado_frame(data: dict) -> pd.DataFrame:
    t = pd.DataFrame(data.get("tornado") or [])
    if t.empty:
        return t
    return t[["rank", "name", "kind", "low", "high", "misfit_low", "misfit_high", "best_reduction", "better_end",
              "effect"]]


def parameters_frame(data: dict) -> pd.DataFrame:
    ps = pd.DataFrame(data.get("parameters") or [])
    if ps.empty:
        return ps
    best = (data.get("best_case") or {}).get("values") or {}
    ps["best"] = ps["name"].map(best)
    ps["at_bound"] = [("low" if abs(b - lo) < 1e-12 else "high" if abs(b - hi) < 1e-12 else "")
                      if b == b and b is not None else "" for b, lo, hi in zip(ps["best"], ps["low"], ps["high"])]
    ps["target"] = ps["target"].map(lambda t: ", ".join(f"{k} {v}" for k, v in t.items()) if t else "all cells")
    return ps[["name", "kind", "owner", "target", "low", "start", "high", "scale", "best", "at_bound", "active"]]


def cases_frame(data: dict) -> pd.DataFrame:
    c = pd.DataFrame(data.get("cases") or [])
    if c.empty:
        return c
    vals = pd.DataFrame(list(c["values"]))
    return pd.concat([c[["case_no", "label", "misfit", "wall_s"]], vals], axis=1)


def fig_convergence(data: dict):
    """The optimiser's misfit per evaluation (log scale)."""
    o = data.get("optimise")
    if not o or not o.get("history"):
        return None
    h = [max(x, 1e-16) for x in o["history"]]
    fig = go.Figure(go.Scatter(x=list(range(1, len(h) + 1)), y=h, mode="lines+markers", line=dict(color="#0969da")))
    fig.update_layout(**LAYOUT, title=f"Search: {o['evaluations']} evaluation(s), {o['cases_run']} case(s); "
                                      f"stopped: {o['stopped_because']}",
                      xaxis_title="evaluation", yaxis_title="misfit (residual sum of squares)", yaxis_type="log")
    return fig
