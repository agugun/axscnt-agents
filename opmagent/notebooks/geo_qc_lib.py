"""Helpers for the geo QC notebooks (the standard template geo_qc.ipynb and investigation notebooks).

Read only, like the fluid QC: a notebook loads the payload the geo pipeline wrote (tops, the logs it used,
the upscaled values, the spec) and the model as stored: the header and faults from the
database (read-only connection), and every cell array from its registered file, after checking its sha256.
So it checks what the deck builder will read. Every check returns a finding {area, check, status, detail};
the display helpers and the summary are shared with the fluid QC (qc_lib).
"""
from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from ..core.files import sha256_of
from ..validators._util import read_array
from .qc_lib import LAYOUT, finding, overall, show_findings, show_list, write_summary  # noqa: F401  (re-exported)

AREAS = ("surfaces", "wells", "properties", "volumes", "files")   # SCAL, rock, contacts: the res model's QC
# thresholds (metres, fractions): a build that honours its data sits well inside the pass limits
TOP_WARN, TOP_FAIL = 2.0, 10.0                 # surface vs well top at the well's column
# model cell (at the cell centre) vs upscaled log (at the well): the difference grows with the well's offset
WELL_TOL = {"phi": (0.01, 0.03), "log10_perm": (0.1, 0.3), "ntg": (0.05, 0.15)}
MEAN_PHI_WARN = 0.02                           # model mean porosity vs log mean inside the reservoir
CLIP_WARN = 0.01                               # share of cells sitting on a build clip limit
LOO_PHI_WARN = 0.04                            # leave-one-well-out porosity RMSE
PHI_CLIP, PERM_CLIP = (0.01, 0.45), (0.01, 2e4)   # geo.build clips kriged values to these


# ---------------------------------------------------------------- loading
def _ro(db_path):
    con = sqlite3.connect(f"file:{Path(db_path).expanduser()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load(payload_path: str | Path, db_path: str | Path | None = None, version_id: int | None = None) -> dict:
    """The payload plus the version as stored: header, tables, arrays (nz, ny, nx) and the layer depths."""
    payload_path = Path(payload_path)
    data = json.loads(payload_path.read_text())
    db_path = db_path or data.get("db_path")
    version_id = int(version_id or data.get("version_id"))
    con = _ro(db_path)
    try:
        h = dict(con.execute("SELECT * FROM geo_model_versions WHERE id = ?", (version_id,)).fetchone())
        nx, ny, nz = h["nx"], h["ny"], h["nz"]
        tables = {t: [dict(r) for r in con.execute(f"SELECT * FROM {t} WHERE version_id = ? ORDER BY {o}", (version_id,))]
                  for t, o in (("faults", "id"),)}
        files, arrays = [], {}
        for p in con.execute("SELECT g.keyword, g.const_value, f.path, f.sha256 FROM geo_properties g "
                             "LEFT JOIN files f ON f.id = g.file_id WHERE g.version_id = ? ORDER BY g.keyword",
                             (version_id,)):
            if p["path"] is None:
                arrays[p["keyword"]] = np.full((nz, ny, nx), float(p["const_value"]))
                continue
            same = Path(p["path"]).is_file() and sha256_of(p["path"]) == p["sha256"]
            files.append({"keyword": p["keyword"], "path": p["path"], "same": same})
            if same:
                v = np.asarray(read_array(p["path"]), dtype=float)
                arrays[p["keyword"]] = v.reshape((nz, ny, nx)) if v.size == nx * ny * nz else v
        if h["grid_file_id"]:
            g = con.execute("SELECT path, sha256 FROM files WHERE id = ?", (h["grid_file_id"],)).fetchone()
            files.append({"keyword": "GRID (COORD/ZCORN)", "path": g["path"],
                          "same": Path(g["path"]).is_file() and sha256_of(g["path"]) == g["sha256"]})
    finally:
        con.close()
    spec_json = json.loads(h["spec_json"] or "{}")
    spec_c = (data.get("spec") or {}).get("contacts")
    data["contacts"] = [dict(spec_c, eqlnum=1)] if spec_c else []   # the spec's, for HCPV and sections only
    data.update(header=h, nx=nx, ny=ny, nz=nz, tables=tables, arrays=arrays, files=files, tables_from_db=True,
                areal=spec_json.get("areal") or data.get("areal"))
    lf = spec_json.get("layer_depths_file") or data.get("layer_depths_file")
    data["layer_depths"] = np.load(lf) if lf and Path(lf).is_file() else None     # (nz+1, ny, nx), cell centres
    folder = payload_path.parent
    data["tops"] = pd.DataFrame(data.get("tops") or [], columns=["well_name", "surface", "x", "y", "tvd"])
    lp = folder / (data.get("logs_file") or "logs.csv")
    data["logs"] = pd.read_csv(lp) if lp.is_file() else pd.DataFrame(columns=["well_name", "tvd", "phi", "perm", "vsh"])
    data["upscaled"] = pd.DataFrame(data.get("upscaled") or [], columns=["well_name", "layer", "phi", "perm", "ntg"])
    return data


def header(data: dict):
    from IPython.display import HTML
    a = data["areal"] or {}
    return HTML(f"<h1>Geo model QC: {html.escape(data['model_name'])} version {data['version']}</h1>"
                f"<p>Built {data.get('created_at', '')} &middot; grid {data['nx']} x {data['ny']} x {data['nz']}, "
                f"cells {a.get('dx', 0):g} x {a.get('dy', 0):g} m &middot; {len(data['tops']['well_name'].unique())} wells "
                f"with tops, {len(data['logs']['well_name'].unique())} with logs &middot; arrays read from their "
                f"registered files, tables from the database</p>")


# ---------------------------------------------------------------- geometry helpers
def zone_index(data: dict) -> dict[str, int]:
    """Surface name -> its index in the layer-depth stack (top of each zone, then the base)."""
    idx = np.concatenate([[0], np.cumsum(data["spec"]["layers"])])
    return {s: int(i) for s, i in zip(data["spec"]["surfaces"], idx)}


def well_cells(data: dict) -> pd.DataFrame:
    """Each well's column (i, j, 0-based) as the build places it: the cell under its mean top x, y."""
    a, t = data["areal"], data["tops"]
    if t.empty:
        return pd.DataFrame(columns=["well_name", "x", "y", "i", "j"])
    w = t.groupby(t["well_name"].astype(str))[["x", "y"]].mean().reset_index()
    w["i"] = np.clip((w["x"] - a["x0"]) // a["dx"], 0, data["nx"] - 1).astype(int)
    w["j"] = np.clip((w["y"] - a["y0"]) // a["dy"], 0, data["ny"] - 1).astype(int)
    return w


def cell_centres(data: dict):
    a = data["areal"]
    return (a["x0"] + a["dx"] * (np.arange(data["nx"]) + 0.5), a["y0"] + a["dy"] * (np.arange(data["ny"]) + 0.5))


# ---------------------------------------------------------------- checks
def tops_misfit(data: dict) -> pd.DataFrame:
    ld, t = data["layer_depths"], data["tops"]
    if ld is None or t.empty:
        return pd.DataFrame(columns=["well_name", "surface", "top_tvd", "model_tvd", "misfit"])
    zi, wc = zone_index(data), well_cells(data).set_index("well_name")
    rows = []
    for r in t.itertuples():
        w = str(r.well_name)
        if r.surface not in zi or w not in wc.index:
            continue
        i, j = wc.loc[w, "i"], wc.loc[w, "j"]
        m = float(ld[zi[r.surface], j, i])
        rows.append({"well_name": w, "surface": r.surface, "top_tvd": float(r.tvd), "model_tvd": m, "misfit": m - float(r.tvd)})
    return pd.DataFrame(rows)


def surface_checks(data: dict) -> list[dict]:
    area, out = "surfaces", []
    mf = tops_misfit(data)
    if mf.empty:
        return [finding(area, "Surfaces honour the well tops", "n/a", "no tops or layer depths in the payload")]
    worst = mf.loc[mf["misfit"].abs().idxmax()]
    mx = abs(worst["misfit"])
    st = "pass" if mx <= TOP_WARN else "warn" if mx <= TOP_FAIL else "fail"
    out.append(finding(area, "Surfaces honour the well tops", st,
                       f"largest misfit {worst['misfit']:+.2f} m ({worst['well_name']}, {worst['surface']}); "
                       f"limits {TOP_WARN:g} m warn, {TOP_FAIL:g} m fail; mean |misfit| {mf['misfit'].abs().mean():.2f} m"))
    ld, zi = data["layer_depths"], zone_index(data)
    surf = data["spec"]["surfaces"]
    min_th = float(data["spec"].get("min_thickness", 0.5))
    for top, base in zip(surf[:-1], surf[1:]):
        th = ld[zi[base]] - ld[zi[top]]
        pinched = float((th <= min_th + 1e-6).mean())
        out.append(finding(area, f"Zone {top} is not pinched to the minimum thickness",
                           "pass" if pinched == 0 else "warn",
                           f"{pinched:.1%} of columns at {min_th:g} m (surfaces were stopped from crossing there); "
                           f"thickness {th.min():.1f}-{th.max():.1f} m" if pinched else
                           f"thickness {th.min():.1f}-{th.max():.1f} m"))
    return out


def at_wells(data: dict) -> pd.DataFrame:
    """Each upscaled log value next to the model cell at that well and layer."""
    u, arr = data["upscaled"], data["arrays"]
    if u.empty or "PORO" not in arr:
        return pd.DataFrame()
    wc = well_cells(data).set_index("well_name")
    rows = []
    for r in u.itertuples():
        if r.well_name not in wc.index:
            continue
        i, j, k = wc.loc[r.well_name, "i"], wc.loc[r.well_name, "j"], int(r.layer) - 1
        xs, ys = cell_centres(data)
        off = float(np.hypot(wc.loc[r.well_name, "x"] - xs[i], wc.loc[r.well_name, "y"] - ys[j]))
        rows.append({"well_name": r.well_name, "layer": int(r.layer), "offset_m": off, "phi_log": r.phi, "phi_model": arr["PORO"][k, j, i],
                     "perm_log": r.perm, "perm_model": arr["PERMX"][k, j, i] if "PERMX" in arr else np.nan,
                     "ntg_log": r.ntg, "ntg_model": arr["NTG"][k, j, i] if "NTG" in arr else np.nan})
    df = pd.DataFrame(rows)
    df["d_phi"] = df["phi_model"] - df["phi_log"]
    df["d_log10_perm"] = np.log10(df["perm_model"]) - np.log10(df["perm_log"])
    df["d_ntg"] = df["ntg_model"] - df["ntg_log"]
    return df


def leave_one_out(data: dict) -> pd.DataFrame:
    """Porosity at each well and layer predicted from the other wells only (the build's interpolator)."""
    from ..geo.build import interpolate
    u = data["upscaled"]
    wc = well_cells(data).set_index("well_name")
    rows = []
    for k, g in u.groupby("layer"):
        g = g[g["well_name"].isin(wc.index)]
        if len(g) < 3:
            continue
        xy = wc.loc[g["well_name"], ["x", "y"]].to_numpy(float)
        for n, r in enumerate(g.itertuples()):
            keep = np.arange(len(g)) != n
            p = interpolate(xy[keep, 0], xy[keep, 1], g["phi"].to_numpy()[keep], np.array([xy[n, 0]]), np.array([xy[n, 1]]))
            rows.append({"well_name": r.well_name, "layer": int(k), "phi_log": r.phi, "phi_predicted": float(np.clip(p[0], *PHI_CLIP))})
    return pd.DataFrame(rows)


def well_checks(data: dict) -> list[dict]:
    area, out = "wells", []
    df = at_wells(data)
    if df.empty:
        return [finding(area, "Model honours the upscaled logs at the wells", "n/a", "no upscaled log values")]
    for key, col in (("phi", "d_phi"), ("log10_perm", "d_log10_perm"), ("ntg", "d_ntg")):
        d = df[col].abs().dropna()
        if d.empty:
            continue
        ok, warn = WELL_TOL[key]
        w = df.loc[d.idxmax()]
        st = "pass" if d.max() <= ok else "warn" if d.max() <= warn else "fail"
        out.append(finding(area, f"Model {key.replace('log10_', 'log10 ')} matches the upscaled logs at the wells", st,
                           f"largest difference {df.loc[d.idxmax(), col]:+.3g} ({w['well_name']}, layer {int(w['layer'])}, "
                           f"{w['offset_m']:.0f} m from its cell centre); limits {ok:g} pass, {warn:g} warn"))
    wc = well_cells(data)
    shared = wc[wc.duplicated(["i", "j"], keep=False)]["well_name"].tolist()
    if shared:
        out.append(finding(area, "Each well has its own grid column", "warn",
                           f"{shared} share a column, so the model can't honour each one's logs; refine dx/dy"))
    nz, used = data["nz"], set(data["upscaled"]["layer"])
    empty = [k for k in range(1, nz + 1) if k not in used]
    out.append(finding(area, "Every layer has log data", "pass" if not empty else "warn",
                       f"layers {empty} have no log samples: filled with the default porosity and the poro-perm "
                       "transform" if empty else f"all {nz} layers"))
    tw, lw = set(data["tops"]["well_name"].astype(str)), set(data["logs"]["well_name"].astype(str))
    if lw - tw:
        out.append(finding(area, "Every logged well has tops", "warn",
                           f"logs of {sorted(lw - tw)} are not used: no tops place them on the grid"))
    loo = leave_one_out(data)
    if len(loo):
        rmse = float(np.sqrt(((loo["phi_predicted"] - loo["phi_log"]) ** 2).mean()))
        out.append(finding(area, "Porosity predicted from the other wells (leave one well out)",
                           "pass" if rmse <= LOO_PHI_WARN else "warn",
                           f"RMSE {rmse:.3f} over {len(loo)} well-layers; above {LOO_PHI_WARN:g} the wells don't "
                           "predict each other and the map between them is weakly constrained"))
    return out


def property_checks(data: dict) -> list[dict]:
    area, out, arr = "properties", [], data["arrays"]
    petro = data["spec"].get("petro", {})
    if "PERMX" in arr and "PERMY" in arr:
        same = np.allclose(arr["PERMY"], arr["PERMX"], rtol=1e-5)
        out.append(finding(area, "PERMY = PERMX", "pass" if same else "fail",
                           "as the build assumes" if same else "PERMY differs from PERMX"))
    if "PERMX" in arr and "PERMZ" in arr:
        r = arr["PERMZ"] / arr["PERMX"]
        kv = float(petro.get("kv_kh", 0.1))
        ok = np.allclose(r, kv, rtol=1e-3)
        out.append(finding(area, "PERMZ / PERMX = kv/kh", "pass" if ok else "fail",
                           f"spec kv/kh {kv:g}; arrays {r.min():.4g}-{r.max():.4g}"))
    for kw, (lo, hi) in (("PORO", PHI_CLIP), ("PERMX", PERM_CLIP)):
        if kw in arr:
            a = arr[kw]
            share = float(((a <= lo * (1 + 1e-5)) | (a >= hi * (1 - 1e-5))).mean())
            out.append(finding(area, f"{kw} not held at the build's clip limits", "pass" if share <= CLIP_WARN else "warn",
                               f"{share:.1%} of cells at {lo:g} or {hi:g}; the kriged values went outside the range there"))
    if "NTG" in arr:
        n = arr["NTG"]
        has_vsh = data["logs"]["vsh"].notna().any() if len(data["logs"]) else False
        out.append(finding(area, "NTG within 0-1", "pass" if 0 <= n.min() and n.max() <= 1 else "fail",
                           f"{n.min():.2f}-{n.max():.2f}, mean {n.mean():.2f}"
                           + ("" if has_vsh else "; no Vsh log, so NTG = 1")))
    lg = reservoir_logs(data)
    if "PORO" in arr and lg["phi"].notna().any():
        d = float(arr["PORO"].mean() - lg["phi"].mean())
        out.append(finding(area, "Model mean porosity close to the log mean", "pass" if abs(d) <= MEAN_PHI_WARN else "warn",
                           f"model {arr['PORO'].mean():.3f}, logs {lg['phi'].mean():.3f} inside the reservoir "
                           f"(difference {d:+.3f}; cells are equally weighted, logs by sample)"))
    return out


def reservoir_logs(data: dict) -> pd.DataFrame:
    """Log samples between each well's top and base surface (the interval the build upscales)."""
    lg, t = data["logs"], data["tops"]
    s = data["spec"]["surfaces"]
    if lg.empty or t.empty:
        return lg
    tw = t.pivot_table(index="well_name", columns="surface", values="tvd")
    if s[0] not in tw or s[-1] not in tw:
        return lg.iloc[0:0]
    lg = lg[lg["well_name"].astype(str).isin(tw.index.astype(str))].copy()
    tw.index = tw.index.astype(str)
    top, base = lg["well_name"].astype(str).map(tw[s[0]]), lg["well_name"].astype(str).map(tw[s[-1]])
    return lg[(lg["tvd"] >= top) & (lg["tvd"] < base)]



def volumes(data: dict) -> pd.DataFrame:
    """Pore volume and HCPV per zone, recomputed from the stored arrays and layer depths."""
    ld, arr, a = data["layer_depths"], data["arrays"], data["areal"]
    th = ld[1:] - ld[:-1]
    ntg = arr.get("NTG", np.ones_like(arr["PORO"]))
    pv = th * a["dx"] * a["dy"] * arr["PORO"] * ntg
    c = data["contacts"]
    owc = c[0].get("owc_depth") if c else None
    centre = 0.5 * (ld[1:] + ld[:-1])
    hc = pv * (centre < owc) if owc is not None else pv
    zone = np.repeat(np.arange(len(data["spec"]["layers"])), data["spec"]["layers"])
    rows = []
    for z, name in enumerate(data["spec"]["surfaces"][:-1]):
        m = zone == z
        rows.append({"zone": name, "layers": int(m.sum()), "bulk_rm3": float((th[m] * a["dx"] * a["dy"]).sum()),
                     "pore_volume_rm3": float(pv[m].sum()), "hcpv_rm3": float(hc[m].sum()),
                     "mean_phi": float(arr["PORO"][m].mean()), "mean_ntg": float(ntg[m].mean())})
    return pd.DataFrame(rows)


def volume_checks(data: dict) -> list[dict]:
    area = "volumes"
    if data["layer_depths"] is None or "PORO" not in data["arrays"]:
        return [finding(area, "Volumes", "n/a", "no layer depths or porosity")]
    v = volumes(data)
    built = {z["zone"]: z for z in data.get("zones", [])}
    out = []
    worst = max((abs(r.pore_volume_rm3 - built[r.zone]["pore_volume_rm3"]) / max(built[r.zone]["pore_volume_rm3"], 1e-9)
                 for r in v.itertuples() if r.zone in built), default=None)
    if worst is not None:
        out.append(finding(area, "Stored arrays give the build's pore volumes", "pass" if worst <= 1e-4 else "fail",
                           f"largest relative difference {worst:.2e} (the arrays the deck builder reads vs the build's "
                           "own numbers)"))
    tot, hc = v["pore_volume_rm3"].sum(), v["hcpv_rm3"].sum()
    out.append(finding(area, "Hydrocarbon pore volume", "pass" if hc > 0 else "fail",
                       f"{hc / 1e6:.2f} million rm3, {hc / tot:.0%} of the pore volume {tot / 1e6:.2f} million rm3"))
    return out



def file_checks(data: dict) -> list[dict]:
    changed = [f["keyword"] for f in data["files"] if not f["same"]]
    return [finding("files", "Grid and arrays are the registered files", "fail" if changed else "pass",
                    f"changed or missing since registration: {changed}" if changed else
                    f"{len(data['files'])} files, sha256 as registered")]


def run_all_checks(data: dict) -> list[dict]:
    out = file_checks(data)
    for fn in (surface_checks, well_checks, property_checks, volume_checks):
        try:
            out += fn(data)
        except Exception as e:  # noqa: BLE001  a check that can't run is a finding, not a crash
            out.append(finding(fn.__name__.replace("_checks", ""), "check could run", "warn", f"{type(e).__name__}: {e}"))
    return out


# ---------------------------------------------------------------- plots
def fig_surfaces(data: dict):
    ld, zi = data["layer_depths"], zone_index(data)
    xs, ys = cell_centres(data)
    mf = tops_misfit(data)
    surf = data["spec"]["surfaces"]
    # each surface on its own colour range (a shared one would hide the dip): the range is in its title
    titles = [f"{s} ({ld[zi[s]].min():.1f}-{ld[zi[s]].max():.1f} m)" for s in surf]
    fig = make_subplots(rows=1, cols=len(surf), subplot_titles=titles, horizontal_spacing=0.04)
    for c, s in enumerate(surf, start=1):
        fig.add_trace(go.Heatmap(x=xs, y=ys, z=ld[zi[s]], colorscale="Viridis_r", showscale=False, hovertemplate="x %{x:.0f}<br>y %{y:.0f}<br>TVD %{z:.1f}<extra></extra>"), 1, c)
        m = mf[mf["surface"] == s].merge(data["tops"][["well_name", "surface", "x", "y"]].astype({"well_name": str}),
                                         on=["well_name", "surface"])
        fig.add_trace(go.Scatter(x=m["x"], y=m["y"], mode="markers+text", text=m["well_name"], textposition="top center",
                                 marker=dict(color="white", size=8, line=dict(color="black", width=1)), showlegend=False,
                                 customdata=m["misfit"], hovertemplate="%{text}: misfit %{customdata:+.2f} m<extra></extra>"), 1, c)
    fig.update_layout(**{**LAYOUT, "height": 380}, title="Surfaces (cell centres, TVD; shallow is yellow) and well tops")
    return fig


def fig_layer_maps(data: dict, kw: str, log: bool = False):
    a = data["arrays"][kw]
    nz = a.shape[0]
    ks = sorted({0, nz // 2, nz - 1})
    xs, ys = cell_centres(data)
    fig = make_subplots(rows=1, cols=len(ks), subplot_titles=[f"Layer {k + 1}" for k in ks], horizontal_spacing=0.04)
    for c, k in enumerate(ks, start=1):
        fig.add_trace(go.Heatmap(x=xs, y=ys, z=np.log10(a[k]) if log else a[k], colorscale="Viridis", showscale=c == len(ks)), 1, c)
    wc = well_cells(data)
    for c in range(1, len(ks) + 1):
        fig.add_trace(go.Scatter(x=wc["x"], y=wc["y"], mode="markers", marker=dict(color="white", size=6,
                                 line=dict(color="black", width=1)), text=wc["well_name"], showlegend=False,
                                 hovertemplate="%{text}<extra></extra>"), 1, c)
    fig.update_layout(**{**LAYOUT, "height": 360}, title=f"{kw} maps" + (" (log10)" if log else ""))
    return fig


def fig_section(data: dict, kw: str = "PORO", well: str | None = None, along: str = "x", log: bool = False, nbins: int = 240,
                cell: tuple[int, int] | None = None):
    """Vertical section through a well's row (along x) or column (along y): the property by depth, layer lines.
    cell: (i, j), 0-based, instead of a well: the section through that cell's row or column."""
    ld, a = data["layer_depths"], data["arrays"][kw]
    wc = well_cells(data).set_index("well_name")
    if cell is not None:
        i, j = int(cell[0]), int(cell[1])
        well = None
    else:
        well = well or (wc.index[0] if len(wc) else None)
        i, j = (int(wc.loc[well, "i"]), int(wc.loc[well, "j"])) if well is not None else (data["nx"] // 2, data["ny"] // 2)
    xs, ys = cell_centres(data)
    if along == "x":
        h, prop, pos = ld[:, j, :], a[:, j, :], xs
    else:
        h, prop, pos = ld[:, :, i], a[:, :, i], ys
    z = np.linspace(h.min(), h.max(), nbins)
    img = np.full((nbins, h.shape[1]), np.nan)
    for c in range(h.shape[1]):
        k = np.searchsorted(h[:, c], z, side="right") - 1
        ok = (k >= 0) & (k < prop.shape[0])
        img[ok, c] = prop[k[ok], c]
    fig = go.Figure(go.Heatmap(x=pos, y=z, z=np.log10(img) if log else img, colorscale="Viridis",
                               colorbar=dict(title=("log10 " if log else "") + kw)))
    zi = set(zone_index(data).values())
    for k in range(h.shape[0]):
        fig.add_trace(go.Scatter(x=pos, y=h[k], mode="lines", showlegend=False, hoverinfo="skip",
                                 line=dict(color="black" if k in zi else "rgba(0,0,0,0.25)", width=1.5 if k in zi else 0.6)))
    for w, r in wc.iterrows():
        if (along == "x" and r["j"] == j) or (along == "y" and r["i"] == i):
            fig.add_vline(x=r["x"] if along == "x" else r["y"], line=dict(color="white", dash="dot"), annotation_text=w)
    cc = data["contacts"]
    for c in cc[:1]:
        for key, colour in (("owc_depth", "#1f77b4"), ("goc_depth", "#d62728")):
            if c.get(key) is not None:
                fig.add_hline(y=c[key], line=dict(color=colour, dash="dash"), annotation_text=key.split("_")[0].upper())
    fig.update_yaxes(autorange="reversed", title="TVD (m)")
    fig.update_xaxes(title=f"{along} (m)")
    fig.update_layout(**{**LAYOUT, "height": 420},
                      title=f"{kw} section along {along} through {well or (f'cell {i + 1}, {j + 1}' if cell is not None else 'the centre')} ({'row j' if along == 'x' else 'column i'} "
                            f"{(j if along == 'x' else i) + 1})")
    return fig


def fig_well(data: dict, well: str):
    """One well: log samples, the upscaled value per layer and the model column, against depth."""
    lg = data["logs"][data["logs"]["well_name"].astype(str) == str(well)].sort_values("tvd")
    u = data["upscaled"][data["upscaled"]["well_name"].astype(str) == str(well)]
    wc = well_cells(data).set_index("well_name")
    ld, arr = data["layer_depths"], data["arrays"]
    i, j = int(wc.loc[well, "i"]), int(wc.loc[well, "j"])
    tops_, bots = ld[:-1, j, i], ld[1:, j, i]
    fig = make_subplots(rows=1, cols=3, shared_yaxes=True, subplot_titles=("Porosity", "Permeability (mD)", "NTG / Vsh"))
    step = lambda vals: (np.repeat(vals, 2), np.column_stack([tops_, bots]).ravel())  # noqa: E731
    for c, (col, kw, logc) in enumerate((("phi", "PORO", "phi"), ("perm", "PERMX", "perm"), ("ntg", "NTG", "vsh")), start=1):
        if lg[logc].notna().any():
            fig.add_trace(go.Scatter(x=lg[logc], y=lg["tvd"], mode="lines", line=dict(color="#bbb", width=1),
                                     name="log" if c == 1 else f"log {logc}", showlegend=c in (1, 3)), 1, c)
        if kw in arr:
            x, y = step(arr[kw][:, j, i])
            fig.add_trace(go.Scatter(x=x, y=y, mode="lines", line=dict(color="#1f77b4", width=2), name="model cell",
                                     showlegend=c == 1), 1, c)
        if len(u):
            mid = 0.5 * (tops_ + bots)[u["layer"].to_numpy(int) - 1]
            fig.add_trace(go.Scatter(x=u[col], y=mid, mode="markers", marker=dict(color="#d62728", size=7, symbol="diamond"),
                                     name="upscaled", showlegend=c == 1), 1, c)
    fig.update_xaxes(type="log", row=1, col=2)
    fig.update_yaxes(autorange="reversed", title="TVD (m)", row=1, col=1)
    zi = zone_index(data)
    for s, k in zi.items():
        fig.add_hline(y=float(ld[k, j, i]), line=dict(color="black", width=0.8, dash="dot"), annotation_text=s,
                      annotation_position="right")
    fig.update_layout(**{**LAYOUT, "height": 460}, title=f"Well {well}: logs, upscaled values and the model column")
    return fig


def fig_distributions(data: dict):
    arr, lg = data["arrays"], reservoir_logs(data)
    fig = make_subplots(rows=1, cols=2, subplot_titles=("Porosity", "log10 permeability (mD)"))
    fig.add_trace(go.Histogram(x=arr["PORO"].ravel(), histnorm="probability", name="model cells", opacity=0.6,
                               marker_color="#1f77b4"), 1, 1)
    if lg["phi"].notna().any():
        fig.add_trace(go.Histogram(x=lg["phi"].dropna(), histnorm="probability", name="log samples", opacity=0.6,
                                   marker_color="#d62728"), 1, 1)
    if "PERMX" in arr:
        fig.add_trace(go.Histogram(x=np.log10(arr["PERMX"].ravel()), histnorm="probability", showlegend=False,
                                   opacity=0.6, marker_color="#1f77b4"), 1, 2)
    if lg["perm"].notna().any():
        fig.add_trace(go.Histogram(x=np.log10(lg["perm"].dropna().clip(lower=1e-3)), histnorm="probability",
                                   showlegend=False, opacity=0.6, marker_color="#d62728"), 1, 2)
    fig.update_layout(**LAYOUT, barmode="overlay", title="Distributions: model cells vs log samples in the reservoir")
    return fig


def fig_poroperm(data: dict, max_points: int = 4000):
    arr, lg = data["arrays"], reservoir_logs(data)
    rng = np.random.default_rng(0)
    p, k = arr["PORO"].ravel(), arr["PERMX"].ravel()
    idx = rng.choice(p.size, min(p.size, max_points), replace=False)
    fig = go.Figure(go.Scatter(x=p[idx], y=k[idx], mode="markers", marker=dict(size=3, color="#1f77b4", opacity=0.4),
                               name="model cells"))
    if lg["perm"].notna().any():
        fig.add_trace(go.Scatter(x=lg["phi"], y=lg["perm"], mode="markers", marker=dict(size=4, color="#d62728"),
                                 name="log samples"))
    u = data["upscaled"]
    if len(u):
        fig.add_trace(go.Scatter(x=u["phi"], y=u["perm"], mode="markers", name="upscaled",
                                 marker=dict(size=8, symbol="diamond", color="#2ca02c", line=dict(width=1, color="black"))))
    fig.update_layout(**LAYOUT, title="Porosity vs permeability", xaxis_title="porosity", yaxis_title="PERMX (mD)",
                      yaxis_type="log")
    return fig


def fig_at_wells(data: dict):
    df, loo = at_wells(data), leave_one_out(data)
    fig = make_subplots(rows=1, cols=2, subplot_titles=("Model cell vs upscaled log", "Predicted from the other wells"))
    lim = [0, max(0.4, float(df[["phi_log", "phi_model"]].max().max()) if len(df) else 0.4)]
    for c in (1, 2):
        fig.add_trace(go.Scatter(x=lim, y=lim, mode="lines", line=dict(color="#999", dash="dot"), showlegend=False), 1, c)
    if len(df):
        fig.add_trace(go.Scatter(x=df["phi_log"], y=df["phi_model"], mode="markers", text=df["well_name"] + " L" + df["layer"].astype(str),
                                 name="model", hovertemplate="%{text}<br>log %{x:.3f}<br>model %{y:.3f}<extra></extra>"), 1, 1)
    if len(loo):
        fig.add_trace(go.Scatter(x=loo["phi_log"], y=loo["phi_predicted"], mode="markers", name="leave one out",
                                 text=loo["well_name"] + " L" + loo["layer"].astype(str),
                                 hovertemplate="%{text}<br>log %{x:.3f}<br>predicted %{y:.3f}<extra></extra>"), 1, 2)
    fig.update_xaxes(title="upscaled log porosity")
    fig.update_yaxes(title="model porosity", row=1, col=1)
    fig.update_layout(**LAYOUT, title="Porosity at the wells")
    return fig
