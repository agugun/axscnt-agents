"""QC reports for models rebuilt from a deck: what each model holds, as plots and tables, before any simulation.

    fluid  PVTO (Bo, Rs, oil viscosity; undersaturated branches), PVDO/PVDG/PVTG, PVTW, densities, RSVD/PBVD
    geo    grid, per-layer maps and histograms of each cell array, layer means, SWOF/SGOF, rock, contacts, faults
    well   well map on the grid (heads, injectors and producers), completion intervals, schedule controls over time

Self-contained HTML (plotly inline), one file per model version, registered as the version's QC report.
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .simple_report import _list, _page, _table

H = 380
UNITS = {"FIELD": {"p": "psia", "bo": "rb/stb", "rs": "Mscf/stb", "bg": "rb/Mscf", "mu": "cP", "d": "ft",
                   "rho": "lb/ft3", "perm": "mD", "q_liq": "stb/d", "q_gas": "Mscf/d"},
         "METRIC": {"p": "bar", "bo": "rm3/sm3", "rs": "sm3/sm3", "bg": "rm3/sm3", "mu": "cP", "d": "m",
                    "rho": "kg/m3", "perm": "mD", "q_liq": "sm3/d", "q_gas": "sm3/d"}}


def _u(unit_system: str | None) -> dict:
    return UNITS.get((unit_system or "METRIC").upper(), UNITS["METRIC"])


def _layout(fig, title, **kw):
    fig.update_layout(title=title, template="plotly_white", height=kw.pop("height", H),
                      margin=dict(l=60, r=20, t=60, b=50), legend=dict(orientation="h", y=-0.2), **kw)
    return fig


def read_grdecl_array(path: Path, n: int | None = None) -> np.ndarray:
    """The values of the first keyword in a GRDECL include file (n*v repeats expanded, comments skipped)."""
    vals: list[float] = []
    started = False
    for line in Path(path).read_text().splitlines():
        line = line.split("--", 1)[0].strip()
        if not line:
            continue
        if not started:
            if re.match(r"^[A-Z][A-Z0-9_+-]*$", line.split()[0]):
                started = True
                rest = line.split()[1:]
                if not rest:
                    continue
                line = " ".join(rest)
            else:
                continue
        done = "/" in line
        for tok in line.split("/", 1)[0].split():
            if "*" in tok:
                k, v = tok.split("*", 1)
                vals.extend([float(v)] * int(k))
            else:
                vals.append(float(tok))
        if done:
            break
    a = np.asarray(vals, dtype=float)
    return a[:n] if n else a


# ---------------------------------------------------------------- fluid
def _fluid(tables: dict, header: dict) -> tuple[list, list]:
    u = _u(header.get("unit_system"))
    figs, sections = [], []
    pvto = tables.get("pvto") or []
    for pvtnum in sorted({r["pvtnum"] for r in pvto}):
        rows = [r for r in pvto if r["pvtnum"] == pvtnum]
        sat = sorted((r for r in rows if r.get("is_saturated")), key=lambda r: r["pressure"])
        fig = make_subplots(rows=1, cols=3, subplot_titles=(f"Bo ({u['bo']})", f"Rs ({u['rs']})",
                                                             f"Oil viscosity ({u['mu']})"))
        p = [r["pressure"] for r in sat]
        fig.add_trace(go.Scatter(x=p, y=[r["bo"] for r in sat], name="Bo saturated", mode="lines+markers"), 1, 1)
        fig.add_trace(go.Scatter(x=p, y=[r["rs"] for r in sat], name="Rs", mode="lines+markers"), 1, 2)
        fig.add_trace(go.Scatter(x=p, y=[r["mu_o"] for r in sat], name="mu_o saturated", mode="lines+markers"), 1, 3)
        for rs in sorted({r["rs"] for r in rows}):   # undersaturated branches: same Rs, rising pressure
            br = sorted((r for r in rows if r["rs"] == rs), key=lambda r: r["pressure"])
            if len(br) > 1:
                fig.add_trace(go.Scatter(x=[r["pressure"] for r in br], y=[r["bo"] for r in br], mode="lines",
                                         line=dict(width=1, dash="dot", color="grey"), showlegend=False), 1, 1)
                fig.add_trace(go.Scatter(x=[r["pressure"] for r in br], y=[r["mu_o"] for r in br], mode="lines",
                                         line=dict(width=1, dash="dot", color="grey"), showlegend=False), 1, 3)
        fig.update_xaxes(title_text=f"Pressure ({u['p']})")
        figs.append(_layout(fig, f"PVTO, PVT region {pvtnum} (dotted: undersaturated branches)"))
    for name, key, ylab in (("pvdo", "bo", "bo"), ("pvdg", "bg", "bg")):
        rows = tables.get(name) or []
        if rows:
            mu = "mu_o" if name == "pvdo" else "mu_g"
            fig = make_subplots(rows=1, cols=2, subplot_titles=(f"{key.capitalize()} ({u[ylab]})",
                                                                 f"Viscosity ({u['mu']})"))
            for pvtnum in sorted({r["pvtnum"] for r in rows}):
                rr = sorted((r for r in rows if r["pvtnum"] == pvtnum), key=lambda r: r["pressure"])
                fig.add_trace(go.Scatter(x=[r["pressure"] for r in rr], y=[r[key] for r in rr],
                                         name=f"{key} region {pvtnum}", mode="lines+markers"), 1, 1)
                fig.add_trace(go.Scatter(x=[r["pressure"] for r in rr], y=[r[mu] for r in rr],
                                         name=f"{mu} region {pvtnum}", mode="lines+markers"), 1, 2)
            fig.update_xaxes(title_text=f"Pressure ({u['p']})")
            figs.append(_layout(fig, name.upper()))
    pvtg = tables.get("pvtg") or []
    if pvtg:
        cols = [k for k in pvtg[0] if k not in ("pvtnum",)]
        sections.append(("PVTG (wet gas)", _table(cols, [[r.get(c) for c in cols] for r in pvtg[:60]])
                         + (f"<p>{len(pvtg) - 60} more rows.</p>" if len(pvtg) > 60 else "")))
    for name, title in (("pvtw", "Water (PVTW)"), ("density", "Surface densities"), ("rsvd", "Rs vs depth (RSVD)"),
                        ("pbvd", "Bubble point vs depth (PBVD)")):
        rows = tables.get(name) or []
        if rows:
            cols = list(rows[0])
            sections.append((title, _table(cols, [[r.get(c) for c in cols] for r in rows])))
    if pvto:
        sat = [r for r in pvto if r.get("is_saturated")]
        sections.insert(0, ("Summary", _table(["Item", "Value"], [
            ["Phases", header.get("phases")], ["Dissolved gas / vaporised oil",
                                                f"{bool(header.get('has_disgas'))} / {bool(header.get('has_vapoil'))}"],
            ["PVT regions", len({r['pvtnum'] for r in pvto})], ["PVTO rows (saturated)", f"{len(pvto)} ({len(sat)})"],
            [f"Pressure range ({u['p']})", f"{min(r['pressure'] for r in pvto):g} to "
                                           f"{max(r['pressure'] for r in pvto):g}"],
            [f"Max Rs ({u['rs']})", f"{max(r['rs'] for r in pvto):g}"]])))
    return figs, sections


# ---------------------------------------------------------------- geo
def _geo(tables: dict, header: dict, arrays: dict, grid: dict) -> tuple[list, list]:
    u = _u(header.get("unit_system"))
    nx, ny, nz = grid["nx"], grid["ny"], grid["nz"]
    n = nx * ny * nz
    figs, sections = [], []
    act = None
    if "ACTNUM" in arrays:
        a = arrays["ACTNUM"]
        act = (a > 0) if a.size == n else None
    ks = sorted({0, nz // 2, nz - 1})
    stats = []
    for kw in [k for k in ("PORO", "PERMX", "PERMY", "PERMZ", "NTG", "SWATINIT", "SATNUM", "PVTNUM", "EQLNUM",
                           "FIPNUM", "TOPS", "DZ") if k in arrays] + \
              sorted(k for k in arrays if k not in ("PORO", "PERMX", "PERMY", "PERMZ", "NTG", "SWATINIT", "SATNUM",
                                                    "PVTNUM", "EQLNUM", "FIPNUM", "TOPS", "DZ", "ACTNUM")):
        a = arrays[kw]
        if a.size == nx * ny and a.size != n:          # e.g. TOPS given for the top layer only
            top = a.reshape(ny, nx)
            stats.append([kw, a.size, f"{a.min():.4g}", f"{a.mean():.4g}", f"{a.max():.4g}", "(top layer only)"])
            fig = go.Figure(go.Heatmap(z=top, colorscale="Viridis", x=np.arange(1, nx + 1), y=np.arange(1, ny + 1),
                                       hovertemplate="I %{x}, J %{y}: %{z:.4g}<extra></extra>"))
            fig.update_yaxes(autorange="reversed")
            figs.append(_layout(fig, f"{kw} ({u['d']}): top layer, map view (J down)", height=340))
            continue
        if a.size != n:
            stats.append([kw, a.size, "", "", "", "(not one value per cell)"])
            continue
        v = a if act is None else np.where(act, a, np.nan)
        cube = v.reshape(nz, ny, nx)
        fin = v[np.isfinite(v)]
        stats.append([kw, int(fin.size), f"{fin.min():.4g}" if fin.size else "", f"{fin.mean():.4g}" if fin.size else "",
                      f"{fin.max():.4g}" if fin.size else "", ""])
        if kw in ("TOPS",) and nz > 1:
            ks_kw = [0]
        else:
            ks_kw = ks
        log = kw.startswith("PERM") and fin.size and fin.min() > 0
        fig = make_subplots(rows=1, cols=len(ks_kw), subplot_titles=[f"Layer {k + 1}" for k in ks_kw],
                            horizontal_spacing=0.05)
        for c, k in enumerate(ks_kw, start=1):
            z = np.log10(cube[k]) if log else cube[k]
            fig.add_trace(go.Heatmap(z=z, colorscale="Viridis", showscale=c == len(ks_kw),
                                     zmin=np.nanmin(np.log10(fin)) if log else float(np.nanmin(fin)),
                                     zmax=np.nanmax(np.log10(fin)) if log else float(np.nanmax(fin)),
                                     hovertemplate="I %{x}, J %{y}: %{z:.4g}<extra></extra>",
                                     x=np.arange(1, nx + 1), y=np.arange(1, ny + 1)), 1, c)
        fig.update_yaxes(autorange="reversed")
        unit = f" ({u['perm']}, log10)" if log else (f" ({u['d']})" if kw in ("TOPS", "DZ") else "")
        figs.append(_layout(fig, f"{kw}{unit}: map view (J down)", height=340))
        if kw in ("PORO", "PERMX", "NTG") and nz > 1:
            means = [np.nanmean(cube[k]) for k in range(nz)]
            f2 = make_subplots(rows=1, cols=2, subplot_titles=("Histogram (cells)", "Mean by layer"))
            f2.add_trace(go.Histogram(x=np.log10(fin[fin > 0]) if log else fin, name=kw, nbinsx=40), 1, 1)
            f2.add_trace(go.Scatter(x=means, y=list(range(1, nz + 1)), mode="lines+markers", name="layer mean"), 1, 2)
            f2.update_yaxes(autorange="reversed", title_text="Layer", row=1, col=2)
            figs.append(_layout(f2, f"{kw}{' (log10)' if log else ''} distribution", height=320))
    sections.append(("Grid", _table(["Item", "Value"], [
        ["Type", header.get("grid_type")], ["Cells (NX x NY x NZ)", f"{nx} x {ny} x {nz} = {n:,}"],
        ["Active cells", f"{grid.get('nactive', ''):,}" if grid.get("nactive") is not None else ""],
        ["Unit system", header.get("unit_system")]]
        + [[k, header[k]] for k in ("dx", "dy", "dz", "top_depth") if header.get(k) is not None])))
    sections.append(("Cell arrays (active cells)", _table(["Array", "Cells", "Min", "Mean", "Max", ""], stats)))
    _tables_and_scal(tables, u, figs, sections)
    return figs, sections


def _tables_and_scal(tables: dict, u: dict, figs: list, sections: list) -> None:
    """SWOF/SGOF plots and the contacts, rock and fault tables, for whichever of them the model holds."""
    for name, x, a_, b_, pc in (("swof", "sw", "krw", "krow", "pcow"), ("sgof", "sg", "krg", "krog", "pcog")):
        rows = tables.get(name) or []
        if not rows:
            continue
        fig = make_subplots(rows=1, cols=2, subplot_titles=("Relative permeability", f"Capillary pressure ({u['p']})"))
        for sat in sorted({r["satnum"] for r in rows}):
            rr = [r for r in rows if r["satnum"] == sat]
            for key in (a_, b_):
                fig.add_trace(go.Scatter(x=[r[x] for r in rr], y=[r[key] for r in rr], mode="lines+markers",
                                         name=f"{key} SATNUM {sat}"), 1, 1)
            fig.add_trace(go.Scatter(x=[r[x] for r in rr], y=[r.get(pc) for r in rr], mode="lines+markers",
                                     name=f"{pc} SATNUM {sat}"), 1, 2)
        fig.update_xaxes(title_text=x.capitalize())
        figs.append(_layout(fig, name.upper()))
    for name, title in (("contacts", "Contacts and initial pressure (EQUIL)"), ("rock", "Rock compressibility"),
                        ("faults", "Faults"), ("fault_multipliers", "Fault multipliers")):
        rows = tables.get(name) or []
        if rows:
            cols = list(rows[0])
            sections.append((title, _table(cols, [[r.get(c) for c in cols] for r in rows[:100]])
                             + (f"<p>{len(rows) - 100} more rows.</p>" if len(rows) > 100 else "")))


def _res(tables: dict, header: dict, arrays: dict, grid: dict) -> tuple[list, list]:
    """The res model: saturation functions, contacts, rock, and its region arrays (cells per region)."""
    u = _u(header.get("unit_system"))
    figs, sections = [], []
    _tables_and_scal(tables, u, figs, sections)
    rows = []
    for kw, a in sorted((arrays or {}).items()):
        vals, counts = np.unique(np.asarray(a).round(6), return_counts=True)
        rows.append([kw, len(vals), ", ".join(f"{v:g}: {c:,}" for v, c in list(zip(vals, counts))[:12])])
    if rows:
        sections.append(("Region arrays and SWATINIT", _table(["Array", "Distinct values", "Cells per value"], rows)))
    return figs, sections


# ---------------------------------------------------------------- well
def _well(tables: dict, header: dict, grid: dict | None, poro_top: np.ndarray | None) -> tuple[list, list]:
    u = _u(header.get("unit_system"))
    wells, comps, events = (tables.get(k) or [] for k in ("wells", "well_completions", "well_events"))
    figs, sections = [], []
    fig = go.Figure()
    if poro_top is not None:
        fig.add_trace(go.Heatmap(z=poro_top, colorscale="Greys", showscale=False, opacity=0.35,
                                 x=np.arange(1, poro_top.shape[1] + 1), y=np.arange(1, poro_top.shape[0] + 1),
                                 hoverinfo="skip"))
    for typ, sym, col in (("INJECTOR", "triangle-down", "#1f77b4"), ("PRODUCER", "circle", "#d62728")):
        ws = [w for w in wells if w.get("well_type") == typ]
        if ws:
            fig.add_trace(go.Scatter(x=[w["head_i"] for w in ws], y=[w["head_j"] for w in ws], mode="markers+text",
                                     text=[w["well_name"] for w in ws], textposition="top center", name=typ.lower(),
                                     marker=dict(size=11, symbol=sym, color=col)))
    if grid:
        fig.update_xaxes(range=[0.5, grid["nx"] + 0.5], title_text="I")
        fig.update_yaxes(range=[grid["ny"] + 0.5, 0.5], title_text="J")
    figs.append(_layout(fig, "Well heads on the grid (background: porosity, top layer)", height=480))
    ij = [c for c in comps if c.get("k1") is not None]
    if ij:
        names = sorted({c["well_name"] for c in ij})
        fig = go.Figure()
        for c in ij:
            fig.add_trace(go.Scatter(x=[c["well_name"]] * 2, y=[c["k1"] - 0.4, c["k2"] + 0.4], mode="lines",
                                     line=dict(width=10, color="#2ca02c" if c.get("state") == "OPEN" else "#999"),
                                     showlegend=False,
                                     hovertext=f"{c['well_name']} {c.get('event_date')}: K {c['k1']}-{c['k2']} "
                                               f"{c.get('state')}"))
        fig.update_yaxes(autorange="reversed", title_text="Layer K")
        fig.update_xaxes(categoryorder="array", categoryarray=names)
        figs.append(_layout(fig, "Completion intervals (green: open)", height=360))
    rate_keys = [("oil_rate", f"oil ({u['q_liq']})"), ("water_rate", f"water ({u['q_liq']})"),
                 ("gas_rate", f"gas ({u['q_gas']})"), ("liquid_rate", f"liquid ({u['q_liq']})"),
                 ("inj_rate", "injection"), ("bhp", f"BHP limit ({u['p']})")]
    present = [(k, lab) for k, lab in rate_keys if any(e.get(k) is not None for e in events)]
    if present:
        fig = make_subplots(rows=len(present), cols=1, shared_xaxes=True, subplot_titles=[l for _, l in present],
                            vertical_spacing=0.06)
        for r_, (k, _) in enumerate(present, start=1):
            for w in sorted({e["well_name"] for e in events if e.get(k) is not None}):
                ev = sorted((e for e in events if e["well_name"] == w and e.get(k) is not None),
                            key=lambda e: e["event_date"])
                fig.add_trace(go.Scatter(x=[e["event_date"] for e in ev], y=[e[k] for e in ev], mode="lines+markers",
                                         line_shape="hv", name=w, legendgroup=w, showlegend=r_ == 1), r_, 1)
        figs.append(_layout(fig, "Schedule: controls and targets over time (per well)", height=220 * len(present) + 80))
    types = {t: sum(1 for w in wells if w.get("well_type") == t) for t in ("PRODUCER", "INJECTOR")}
    sections.append(("Summary", _table(["Item", "Value"], [
        ["Wells", f"{len(wells)} ({types['PRODUCER']} producers, {types['INJECTOR']} injectors)"],
        ["Groups", ", ".join(g["group_name"] for g in tables.get("well_groups") or []) or "FIELD only"],
        ["Completions", len(comps)], ["Schedule events", len(events)],
        ["First / last event", f"{min((e['event_date'] for e in events), default='')} / "
                               f"{max((e['event_date'] for e in events), default='')}"]])))
    sections.append(("Wells", _table(["Well", "Group", "Type", "Phase", "Head I,J", "Ref depth"],
                                     [[w["well_name"], w.get("group_name"), w.get("well_type"),
                                       w.get("preferred_phase"), f"{w.get('head_i')},{w.get('head_j')}",
                                       w.get("ref_depth")] for w in wells])))
    sections.append(("Completions", _table(["Well", "Date", "State", "I", "J", "K1-K2", "Skin"],
                                           [[c["well_name"], c.get("event_date"), c.get("state"), c.get("i"),
                                             c.get("j"), f"{c.get('k1')}-{c.get('k2')}", c.get("skin")]
                                            for c in comps[:300]])))
    return figs, sections


def model_report(path: Path, *, kind: str, name: str, version: int, source: str, tables: dict, header: dict,
                 grid: dict, coverage: dict, warnings: list, assumptions: list, arrays: dict | None = None,
                 poro_top: np.ndarray | None = None) -> Path:
    if kind == "fluid":
        figs, sections = _fluid(tables, header)
    elif kind == "geo":
        figs, sections = _geo(tables, header, arrays or {}, grid)
    elif kind == "res":
        figs, sections = _res(tables, header, arrays or {}, grid)
    else:
        figs, sections = _well(tables, header, grid, poro_top)
    rows = [[t, len(r)] for t, r in tables.items() if isinstance(r, list) and r and t not in ("geo_properties", "res_properties")]
    sections += [("Source", _list([f"deck: {source}", f"schedule: {coverage.get('schedule_mode')}"])),
                 ("Tables taken from the deck", _table(["table", "rows"], rows)),
                 ("Assumptions", _list(assumptions)), ("Validator warnings", _list(warnings))]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_page(f"{kind.capitalize()} model {name} v{version} (from deck)",
                          f"rebuilt from {html.escape(Path(source).name)}; generated {datetime.now():%Y-%m-%d %H:%M}",
                          sections, figs, True))
    return path
