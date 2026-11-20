"""Fluid model QC report: one self-contained HTML file with plots and metrics."""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

LAB = dict(mode="markers", marker=dict(size=9, symbol="circle-open", line=dict(width=2)))
MODEL = dict(mode="lines", line=dict(width=2))
LAYOUT = dict(template="plotly_white", height=380, margin=dict(l=60, r=20, t=50, b=50),
              legend=dict(orientation="h", y=-0.2))


def _fig_html(fig, first: bool, inline_js: bool) -> str:
    js = ("inline" if inline_js else "cdn") if first else False
    return fig.to_html(full_html=False, include_plotlyjs=js, config={"displaylogo": False})


def _lab_vs_model(title, xlab, ylab, lab_x, lab_y, mod_x, mod_y, name):
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=mod_x, y=mod_y, name=f"Model {name}", **MODEL))
    if lab_x:
        fig.add_trace(go.Scatter(x=lab_x, y=lab_y, name=f"Lab {name}", **LAB))
    fig.update_layout(title=title, xaxis_title=xlab, yaxis_title=ylab, **LAYOUT)
    return fig


def build_report(path: str | Path, *, model_name: str, version: int, sample: str | None, metrics: dict,
                 checks: dict, assumptions: list[str], warnings: list[dict], tables: dict, qc_sim: dict,
                 lab: dict, spec: dict, inline_js: bool = True) -> Path:
    figs = []
    # CCE
    if qc_sim.get("cce"):
        m = qc_sim["cce"]
        figs.append(_lab_vs_model("CCE relative volume", "Pressure (bar)", "V / Vsat",
                                  [r["pressure"] for r in lab.get("cce", [])], [r["relative_volume"] for r in lab.get("cce", [])],
                                  [r["pressure"] for r in m], [r["relative_volume"] for r in m], "V/Vsat"))
    # DL Bo and Rs
    if qc_sim.get("dl"):
        m, l = qc_sim["dl"], lab.get("dl", [])
        fig = make_subplots(rows=1, cols=2, subplot_titles=("DL Bo", "DL Rs"))
        fig.add_trace(go.Scatter(x=[r["pressure"] for r in m], y=[r["bo_d"] for r in m], name="Model Bo", **MODEL), 1, 1)
        fig.add_trace(go.Scatter(x=[r["pressure"] for r in m], y=[r["rs_d"] for r in m], name="Model Rs", **MODEL), 1, 2)
        if l:
            fig.add_trace(go.Scatter(x=[r["pressure"] for r in l], y=[r["bo"] for r in l], name="Lab Bo", **LAB), 1, 1)
            fig.add_trace(go.Scatter(x=[r["pressure"] for r in l], y=[r["rs"] for r in l], name="Lab Rs", **LAB), 1, 2)
        fig.update_xaxes(title_text="Pressure (bar)")
        fig.update_yaxes(title_text="Bo (rm3/sm3)", row=1, col=1)
        fig.update_yaxes(title_text="Rs (sm3/sm3)", row=1, col=2)
        fig.update_layout(title="Differential liberation", **LAYOUT)
        figs.append(fig)
    # viscosity
    if qc_sim.get("viscosity"):
        m = [r for r in qc_sim["viscosity"] if r["mu_o"]]
        figs.append(_lab_vs_model("Oil viscosity (reservoir fluid)", "Pressure (bar)", "Viscosity (cP)",
                                  [r["pressure"] for r in lab.get("viscosity", [])], [r["mu_o"] for r in lab.get("viscosity", [])],
                                  [r["pressure"] for r in m], [r["mu_o"] for r in m], "mu_o"))
    # final PVTO
    pvto = tables["pvto"]
    fig = make_subplots(rows=1, cols=3, subplot_titles=("Bo", "Rs (saturated)", "Oil viscosity"))
    blocks = {}
    for r in pvto:
        blocks.setdefault(r["rs"], []).append(r)
    sat = sorted((r for r in pvto if r["is_saturated"]), key=lambda r: r["pressure"])
    for rs, rr in sorted(blocks.items()):
        rr = sorted(rr, key=lambda r: r["pressure"])
        fig.add_trace(go.Scatter(x=[r["pressure"] for r in rr], y=[r["bo"] for r in rr], mode="lines",
                                 line=dict(width=1, dash="dot"), name=f"Rs {rs:.0f}", showlegend=False), 1, 1)
        fig.add_trace(go.Scatter(x=[r["pressure"] for r in rr], y=[r["mu_o"] for r in rr], mode="lines",
                                 line=dict(width=1, dash="dot"), showlegend=False), 1, 3)
    for col, key in ((1, "bo"), (3, "mu_o")):
        fig.add_trace(go.Scatter(x=[r["pressure"] for r in sat], y=[r[key] for r in sat], name="Saturated",
                                 showlegend=col == 1, **MODEL), 1, col)
    fig.add_trace(go.Scatter(x=[r["pressure"] for r in sat], y=[r["rs"] for r in sat], name="Rs", showlegend=False, **MODEL), 1, 2)
    fig.update_xaxes(title_text="Pressure (bar)")
    fig.update_layout(title="PVTO table (dotted: undersaturated branches)", **LAYOUT)
    figs.append(fig)
    # PVDG
    pvdg = tables["pvdg"]
    fig = make_subplots(rows=1, cols=2, subplot_titles=("Bg (rm3/sm3)", "Gas viscosity (cP)"))
    fig.add_trace(go.Scatter(x=[r["pressure"] for r in pvdg], y=[r["bg"] for r in pvdg], name="Bg", **MODEL), 1, 1)
    fig.add_trace(go.Scatter(x=[r["pressure"] for r in pvdg], y=[r["mu_g"] for r in pvdg], name="mu_g", **MODEL), 1, 2)
    fig.update_xaxes(title_text="Pressure (bar)")
    fig.update_layout(title="PVDG table", **LAYOUT)
    figs.append(fig)

    def row(label, value, check=None):
        cls = "" if check is None else ("pass" if check else "fail")
        badge = "" if check is None else f"<span class='{cls}'>{'within tolerance' if check else 'OUTSIDE tolerance'}</span>"
        return f"<tr><td>{html.escape(label)}</td><td>{html.escape(str(value))}</td><td>{badge}</td></tr>"

    rows = [row("Saturation pressure, lab / model (bar)",
                f"{metrics.get('psat_lab', float('nan')):.1f} / {metrics['psat_model']:.1f}"
                f" ({metrics.get('psat_error_pct', 0):+.2f}%)", checks.get("psat", {}).get("passed"))]
    for k, label in (("bo", "DL Bo, max error"), ("rs", "DL Rs, max error"), ("mu_o", "Oil viscosity, max error")):
        if k in checks:
            c = checks[k]
            rows.append(row(f"{label} (tolerance {c['tolerance']*100:.0f}%)", f"{c['error']*100:+.2f}%", c["passed"]))
        else:
            rows.append(row(label, "no lab data", None))
    rows += [row("Rs at bubble point (sm3/sm3)", f"{metrics['rs_at_pb']:.1f}"),
             row("Bo at bubble point (rm3/sm3)", f"{metrics['bo_at_pb']:.4f}"),
             row("Separator GOR, model / measured (sm3/sm3)",
                 f"{metrics['gor_model']:.1f} / {metrics['gor_measured'] if metrics.get('gor_measured') else 'n/a'}"),
             row("Stock-tank oil API, model / measured",
                 f"{metrics['api_model']:.1f} / {metrics['api_measured'] if metrics.get('api_measured') else 'n/a'}")]
    eos = (f"{spec['eos']}, volume shift {'on' if spec['volume_shift'] else 'off'}, {spec['pseudo_components']} pseudo-components, "
           f"C1/C7+ interaction coefficient {spec.get('kij_c1_plus')}")
    warn_html = "".join(f"<li>{html.escape(w['message'])}{' (' + html.escape(w['where']) + ')' if w.get('where') else ''}</li>"
                        for w in warnings) or "<li>None</li>"
    assum_html = "".join(f"<li>{html.escape(a)}</li>" for a in assumptions)
    plots = "".join(f"<div class='plot'>{_fig_html(f, i == 0, inline_js)}</div>" for i, f in enumerate(figs))
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>QC: {html.escape(model_name)} v{version}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#222}}
table{{border-collapse:collapse;width:100%}}td{{border-bottom:1px solid #eee;padding:6px 8px}}
.pass{{color:#1a7f37;font-weight:600}}.fail{{color:#cf222e;font-weight:600}}.plot{{margin:24px 0}}
h1{{font-size:22px}}h2{{font-size:17px;margin-top:28px}}.meta{{color:#666}}</style></head><body>
<h1>Fluid model QC: {html.escape(model_name)} version {version}</h1>
<p class="meta">Sample {html.escape(str(sample or 'n/a'))} &middot; generated {datetime.now():%Y-%m-%d %H:%M} &middot; EOS: {html.escape(eos)}</p>
<h2>Summary</h2><table>{''.join(rows)}</table>
<h2>Assumptions</h2><ul>{assum_html}</ul>
<h2>Validator warnings</h2><ul>{warn_html}</ul>
<h2>Plots</h2>{plots}
</body></html>"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc)
    return path
