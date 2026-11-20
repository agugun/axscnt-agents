"""QC reports for geo and well models: self-contained HTML (plotly bundled for offline use)."""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

LAYOUT = dict(template="plotly_white", height=360, margin=dict(l=60, r=20, t=50, b=50), legend=dict(orientation="h", y=-0.2))


def _table(headers: list[str], rows: list[list]) -> str:
    th = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    tr = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tr>{th}</tr>{tr}</table>"


def _page(title: str, meta: str, sections: list[tuple[str, str]], figs: list, inline_js: bool) -> str:
    plots = "".join(f"<div class='plot'>{f.to_html(full_html=False, include_plotlyjs=(('inline' if inline_js else 'cdn') if i == 0 else False), config={'displaylogo': False})}</div>"
                    for i, f in enumerate(figs))
    body = "".join(f"<h2>{html.escape(h)}</h2>{c}" for h, c in sections)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#222}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #eee;padding:6px 8px;text-align:left}}
th{{background:#f6f8fa}}h1{{font-size:22px}}h2{{font-size:17px;margin-top:28px}}.meta{{color:#666}}.plot{{margin:24px 0}}</style>
</head><body><h1>{html.escape(title)}</h1><p class="meta">{html.escape(meta)}</p>{body}<h2>Plots</h2>{plots}</body></html>"""


def _list(items) -> str:
    return "<ul>" + "".join(f"<li>{html.escape(str(i))}</li>" for i in items) + "</ul>" if items else "<ul><li>None</li></ul>"


def geo_report(path, *, model, metrics: dict, assumptions, warnings, swof, sgof, inline_js=True) -> Path:
    fig = make_subplots(rows=1, cols=2, subplot_titles=("SWOF", "SGOF"))
    for col, (x, a, b, tab) in enumerate((("sw", "krw", "krow", swof), ("sg", "krg", "krog", sgof)), start=1):
        if tab:
            fig.add_trace(go.Scatter(x=[r[x] for r in tab], y=[r[a] for r in tab], name=a, mode="lines+markers"), 1, col)
            fig.add_trace(go.Scatter(x=[r[x] for r in tab], y=[r[b] for r in tab], name=b, mode="lines+markers"), 1, col)
    fig.update_layout(title="Relative permeability (SATNUM 1)", **LAYOUT)
    doc = _page(f"Geo model QC: {model['model_name']} version {model['version']}",
                f"generated {datetime.now():%Y-%m-%d %H:%M}",
                [("Summary", _table(["Item", "Value"], [[k, v] for k, v in metrics.items()])),
                 ("Assumptions", _list(assumptions)), ("Validator warnings", _list(w["message"] for w in warnings))],
                [fig], inline_js)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(doc)
    return Path(path)


def well_report(path, *, model, wells, completions, events, assumptions, warnings, inline_js=True, extra_figs=None) -> Path:
    fig = go.Figure()
    for w in wells:
        ev = sorted((e for e in events if e["well_name"] == w["well_name"]), key=lambda e: e["event_date"])
        if ev:
            fig.add_trace(go.Scatter(x=[e["event_date"] for e in ev], y=[w["well_name"]] * len(ev), mode="markers+text",
                                     text=[e["control"] or e["status"] for e in ev], textposition="top center",
                                     name=w["well_name"], marker=dict(size=12, symbol="diamond")))
    fig.update_layout(title="Schedule events", xaxis_title="Date", **LAYOUT)
    doc = _page(f"Well model QC: {model['model_name']} version {model['version']}",
                f"generated {datetime.now():%Y-%m-%d %H:%M}",
                [("Wells", _table(["Well", "Group", "Type", "Phase", "Head I,J", "Ref depth"],
                                  [[w["well_name"], w["group_name"], w["well_type"], w["preferred_phase"],
                                    f"{w['head_i']},{w['head_j']}", w["ref_depth"]] for w in wells])),
                 ("Completions", _table(["Well", "Date", "State", "I", "J", "K1-K2", "MD", "Skin"],
                                        [[c["well_name"], c["event_date"], c["state"], c["i"], c["j"],
                                          f"{c['k1']}-{c['k2']}" if c["k1"] else "", f"{c['top_md']}-{c['bottom_md']}" if c["top_md"] else "",
                                          c["skin"]] for c in completions])),
                 ("Controls", _table(["Well", "Date", "Mode", "Control", "Oil", "Water inj/prod", "BHP"],
                                     [[e["well_name"], e["event_date"], e["mode"], e["control"], e["oil_rate"],
                                       e["inj_rate"] or e["water_rate"], e["bhp"]] for e in events[:200]])
                  + (f"<p>{len(events) - 200} more rows not shown.</p>" if len(events) > 200 else "")),
                 ("Assumptions", _list(assumptions)), ("Validator warnings", _list(w["message"] for w in warnings))],
                [fig] + list(extra_figs or []), inline_js)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(doc)
    return Path(path)
