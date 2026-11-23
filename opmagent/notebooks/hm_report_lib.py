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
    return data


def header(data: dict):
    from IPython.display import HTML
    m = data.get("misfit")
    mtxt = f"{m:.3f} over {data['points']} points" if m is not None else "not defined"
    return HTML(f"<h1>History match report: run {data['run_id']}</h1>"
                f"<p>Deck {html.escape(str(data.get('deck_name')))} (id {data['deck_id']})"
                f"{' &middot; study ' + html.escape(data['study']) if data.get('study') else ''}"
                f" &middot; observations: {html.escape(data['source'].replace('_', ' '))}"
                f" &middot; built {data.get('created_at', '')}</p>"
                f"<p><b>Misfit {mtxt}</b></p>")


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
    hon = {w["well"]: w.get("honoured", []) for w in data.get("wells", [])}.get(well, [])
    fig = make_subplots(rows=len(qs), cols=1, shared_xaxes=True, vertical_spacing=0.06,
                        subplot_titles=[LABEL[q] + (" (honoured: given to Flow)" if q in hon else "") for q in qs])
    for i, q in enumerate(qs, start=1):
        oq = o[o["quantity"] == q].sort_values("month")
        sq = s[s["quantity"] == q].sort_values("month")
        sig = _sigma(data, q, oq["value"].to_numpy())
        fig.add_trace(go.Scatter(x=oq["month"], y=oq["value"] + sig, **BAND), row=i, col=1)
        fig.add_trace(go.Scatter(x=oq["month"], y=oq["value"] - sig, fill="tonexty", **BAND), row=i, col=1)
        fig.add_trace(go.Scatter(x=oq["month"], y=oq["value"], name="observed", showlegend=i == 1, **OBS),
                      row=i, col=1)
        fig.add_trace(go.Scatter(x=sq["month"], y=sq["value"], name="simulated", showlegend=i == 1, **SIM),
                      row=i, col=1)
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
