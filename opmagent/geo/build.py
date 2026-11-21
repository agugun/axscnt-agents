"""Geo modeler: well tops + logs -> layer-cake corner-point grid with kriged properties.

Inputs
  tops     CSV: well_name, surface, x, y, tvd         (one row per well and surface)
  logs     LAS files (DEPT = TVD, or MD with a survey) or CSV: well_name, tvd, phi[, perm][, vsh]
  spec     dict (JSON): surfaces (ordered, top to base), layers per zone, areal grid; contacts (optional) only for
           the HCPV in the volume summary. SCAL, rock and contacts as model data belong to the res model.

Method
  1. each surface: ordinary kriging of the well tops onto the grid nodes (IDW below 3 wells); surfaces
     are forced not to cross (minimum thickness);
  2. layers split each zone proportionally; vertical pillars -> COORD/ZCORN;
  3. logs upscaled per well per layer (porosity arithmetic, permeability geometric, NTG from Vsh cutoff),
     then kriged per layer across the grid; permeability from a poro-perm transform when not logged;
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- interpolation
def interpolate(xs, ys, vals, gx, gy, *, method="auto", length_scale=None, nugget=0.0):
    """Interpolate scattered values to points (gx, gy). Ordinary kriging (gstools) with >= 3 points, else IDW."""
    xs, ys, vals = (np.asarray(v, dtype=float) for v in (xs, ys, vals))
    ok = np.isfinite(vals)
    xs, ys, vals = xs[ok], ys[ok], vals[ok]
    gx, gy = np.asarray(gx, dtype=float), np.asarray(gy, dtype=float)
    if len(vals) == 0:
        return np.full(gx.shape, np.nan)
    if len(vals) == 1 or np.ptp(vals) == 0:
        return np.full(gx.shape, vals.mean())
    if method in ("auto", "kriging") and len(vals) >= 3:
        try:
            import gstools as gs
            span = math.hypot(np.ptp(gx), np.ptp(gy)) or 1.0
            model = gs.Spherical(dim=2, var=float(np.var(vals)) or 1e-6, len_scale=length_scale or span / 2,
                                 nugget=nugget)
            krig = gs.krige.Ordinary(model, cond_pos=[xs, ys], cond_val=vals, exact=True)
            field, _ = krig((gx.ravel(), gy.ravel()), mesh_type="unstructured", return_var=True)
            return np.asarray(field).reshape(gx.shape)
        except ImportError:
            pass
    d = np.hypot(gx[..., None] - xs, gy[..., None] - ys)
    w = 1.0 / np.maximum(d, 1e-6) ** 2
    return (w * vals).sum(-1) / w.sum(-1)


# ---------------------------------------------------------------- logs
CURVES = {"phi": ["PHIE", "PHIT", "PORO", "PHI", "NPHI_CORR"], "perm": ["PERM", "KLOGH", "KH", "PERMX"],
          "vsh": ["VSH", "VCL", "VSHALE"], "tvd": ["TVD", "TVDSS"]}


def curve_map(overrides: dict | None = None) -> dict:
    """Which log mnemonics stand for porosity, permeability, shale volume and TVD (spec 'curves' first)."""
    cm = {k: list(v) for k, v in CURVES.items()}
    for k, v in (overrides or {}).items():
        cm[k] = [v] + cm.get(k, [])
    return cm


def pick_curves(df: pd.DataFrame, well: str, surveys: pd.DataFrame | None, cm: dict) -> pd.DataFrame:
    """One well's log table (first column = the depth index, then a column per curve) -> rows well_name, tvd,
    phi, perm, vsh. Depth is TVD if there is a TVD curve, else MD converted with the survey, else MD = TVD."""
    depth_col = df.columns[0]
    out = pd.DataFrame(index=df.index)
    out["well_name"] = well
    tvd = next((c for c in cm["tvd"] if c in df.columns), None)
    if tvd:
        out["tvd"] = df[tvd]
    elif surveys is not None and well in set(surveys["well_name"]):
        s = surveys[surveys["well_name"] == well].sort_values("md")
        out["tvd"] = np.interp(df[depth_col], s["md"], s["tvd"])
    else:
        out["tvd"] = df[depth_col]   # vertical well: MD = TVD
    for k in ("phi", "perm", "vsh"):
        c = next((c for c in cm[k] if c in df.columns), None)
        out[k] = df[c] if c else np.nan
    return out


def clean_logs(frames: list[pd.DataFrame]) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame(columns=["well_name", "tvd", "phi", "perm", "vsh"])
    out = pd.concat(frames, ignore_index=True)
    out["well_name"] = out["well_name"].astype(str)
    out.loc[(out["phi"] < 0) | (out["phi"] > 0.6), "phi"] = np.nan
    return out


def read_logs(paths: list[str], surveys: pd.DataFrame | None = None, curve_map_: dict | None = None) -> pd.DataFrame:
    """Return rows well_name, tvd, phi, perm, vsh from LAS or CSV files."""
    cm = curve_map(curve_map_)
    frames = []
    for p in paths:
        p = Path(p)
        if p.suffix.lower() == ".las":
            import lasio
            las = lasio.read(str(p))
            frames.append(pick_curves(las.df().reset_index(), str(las.well.WELL.value or p.stem), surveys, cm))
        else:
            df = pd.read_csv(p)
            df.columns = [c.lower() for c in df.columns]
            for k in ("perm", "vsh"):
                if k not in df:
                    df[k] = np.nan
            frames.append(df[["well_name", "tvd", "phi", "perm", "vsh"]])
    return clean_logs(frames)


# ---------------------------------------------------------------- build
def build_grid(tops: pd.DataFrame, logs: pd.DataFrame, spec: dict, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    surfaces = spec["surfaces"]
    layers = spec["layers"]
    if len(layers) != len(surfaces) - 1:
        raise ValueError("layers needs one entry per zone (len(surfaces) - 1)")
    t = tops.copy()
    t.columns = [c.lower() for c in t.columns]
    missing = set(surfaces) - set(t["surface"])
    if missing:
        raise ValueError(f"no tops for surfaces {sorted(missing)}")
    # areal grid
    wx, wy = t["x"].astype(float), t["y"].astype(float)
    a = spec.get("areal") or {}
    margin = a.get("margin", 500.0)
    dx, dy = a.get("dx", 100.0), a.get("dy", 100.0)
    x0 = a.get("x0", math.floor((wx.min() - margin) / dx) * dx)
    y0 = a.get("y0", math.floor((wy.min() - margin) / dy) * dy)
    nx = a.get("nx", int(math.ceil((wx.max() + margin - x0) / dx)))
    ny = a.get("ny", int(math.ceil((wy.max() + margin - y0) / dy)))
    nz = int(sum(layers))
    nxg, nyg = np.meshgrid(x0 + dx * np.arange(nx + 1), y0 + dy * np.arange(ny + 1))   # nodes (ny+1, nx+1)
    cxg, cyg = np.meshgrid(x0 + dx * (np.arange(nx) + 0.5), y0 + dy * (np.arange(ny) + 0.5))  # centres
    min_th = spec.get("min_thickness", 0.5)
    surf_nodes, surf_cent = [], []
    for s in surfaces:
        r = t[t["surface"] == s]
        zn = interpolate(r["x"], r["y"], r["tvd"], nxg, nyg)
        zc = interpolate(r["x"], r["y"], r["tvd"], cxg, cyg)
        if surf_nodes:
            zn = np.maximum(zn, surf_nodes[-1] + min_th)
            zc = np.maximum(zc, surf_cent[-1] + min_th)
        surf_nodes.append(zn)
        surf_cent.append(zc)
    # layer surfaces (nz+1)
    def split(surf):
        out = []
        for z, (top, base) in enumerate(zip(surf[:-1], surf[1:])):
            n = layers[z]
            for f in range(n):
                out.append(top + (base - top) * f / n)
        out.append(surf[-1])
        return np.array(out)
    ln, lc = split(surf_nodes), split(surf_cent)          # (nz+1, ny+1, nx+1), (nz+1, ny, nx)
    # COORD: vertical pillars from top to base
    ztop, zbot = float(ln.min()) - 10, float(ln.max()) + 10
    coord = np.stack([nxg, nyg, np.full_like(nxg, ztop), nxg, nyg, np.full_like(nxg, zbot)], axis=-1)   # (ny+1, nx+1, 6)
    # ZCORN (2nx x 2ny x 2nz), Eclipse ordering: x fastest, then y, then z
    zc = np.empty((2 * nz, 2 * ny, 2 * nx))
    for k in range(nz):
        for top_bot, layer in ((0, k), (1, k + 1)):
            node = ln[layer]
            zc[2 * k + top_bot, 0::2, 0::2] = node[:-1, :-1]
            zc[2 * k + top_bot, 0::2, 1::2] = node[:-1, 1:]
            zc[2 * k + top_bot, 1::2, 0::2] = node[1:, :-1]
            zc[2 * k + top_bot, 1::2, 1::2] = node[1:, 1:]
    grid_file = out_dir / "GRID.GRDECL"
    with open(grid_file, "w") as f:
        f.write(f"SPECGRID\n {nx} {ny} {nz} 1 F /\n\nCOORD\n")
        for v in coord.reshape(-1, 6):
            f.write(" " + " ".join(f"{x:.3f}" for x in v) + "\n")
        f.write("/\n\nZCORN\n")
        flat = zc.ravel()
        for i in range(0, len(flat), 8):
            f.write(" " + " ".join(f"{x:.3f}" for x in flat[i:i + 8]) + "\n")
        f.write("/\n")
    depths_file = out_dir / "layer_depths.npy"
    np.save(depths_file, lc)

    # ---------------- properties
    pp = spec.get("petro", {})
    vsh_cut = pp.get("vsh_cutoff", 0.5)
    kv_kh = pp.get("kv_kh", 0.1)
    a_k, b_k = pp.get("poro_perm", [0.01, 30.0])        # k = a * 10^(b*phi) in mD when perm isn't logged
    lg = logs.copy()
    wells_xy = t.groupby(t["well_name"].astype(str))[["x", "y"]].mean()
    phi = np.zeros((nz, ny, nx)); perm = np.zeros((nz, ny, nx)); ntg = np.ones((nz, ny, nx))
    upscaled = []
    for k in range(nz):
        rows = []
        for w, g in lg.groupby("well_name"):
            if w not in wells_xy.index:
                continue
            wxv, wyv = wells_xy.loc[w]
            i = int(np.clip((wxv - x0) // dx, 0, nx - 1)); j = int(np.clip((wyv - y0) // dy, 0, ny - 1))
            top, bot = lc[k, j, i], lc[k + 1, j, i]
            s = g[(g["tvd"] >= top) & (g["tvd"] < bot)]
            if s.empty or s["phi"].isna().all():
                continue
            p_ = float(s["phi"].mean())
            kk = s["perm"].dropna()
            k_ = float(np.exp(np.log(kk.clip(lower=1e-3)).mean())) if len(kk) else a_k * 10 ** (b_k * p_)
            n_ = float((s["vsh"] < vsh_cut).mean()) if s["vsh"].notna().any() else 1.0
            rows.append((wxv, wyv, p_, math.log(k_), n_, w))
            upscaled.append({"well_name": w, "layer": k + 1, "phi": p_, "perm": k_, "ntg": n_})
        if rows:
            r = np.array([x[:5] for x in rows], dtype=float)
            phi[k] = np.clip(interpolate(r[:, 0], r[:, 1], r[:, 2], cxg, cyg), 0.01, 0.45)
            perm[k] = np.exp(interpolate(r[:, 0], r[:, 1], r[:, 3], cxg, cyg)).clip(0.01, 2e4)
            ntg[k] = np.clip(interpolate(r[:, 0], r[:, 1], r[:, 4], cxg, cyg), 0.0, 1.0)
        else:  # no log data in this layer: zone/field average
            phi[k] = pp.get("default_phi", 0.2)
            perm[k] = a_k * 10 ** (b_k * phi[k])
    def write(name, arr, fmt="{:.6g}"):
        p = out_dir / f"{name}.INC"
        flat = arr.ravel()  # (nz, ny, nx) C-order == Eclipse order (x fastest)
        with open(p, "w") as f:
            f.write(f"{name}\n")
            for i in range(0, len(flat), 8):
                f.write(" " + " ".join(fmt.format(v) for v in flat[i:i + 8]) + "\n")
            f.write("/\n")
        return str(p)
    files = {"PORO": write("PORO", phi), "PERMX": write("PERMX", perm), "PERMY": write("PERMY", perm),
             "PERMZ": write("PERMZ", perm * kv_kh), "NTG": write("NTG", ntg)}
    # volumes for QC
    th = lc[1:] - lc[:-1]
    bulk = th * dx * dy
    pv = bulk * phi * ntg
    owc = (spec.get("contacts") or {}).get("owc_depth")
    centre = 0.5 * (lc[1:] + lc[:-1])
    hcpv = pv * (centre < owc) if owc is not None else pv
    zone_of_layer = np.repeat(np.arange(len(layers)), layers)
    zones = []
    for z, name in enumerate(surfaces[:-1]):
        m = zone_of_layer == z
        zones.append({"zone": name, "layers": int(layers[z]), "pore_volume_rm3": float(pv[m].sum()),
                      "hcpv_rm3": float(hcpv[m].sum()), "mean_phi": float(phi[m].mean()), "mean_perm_md": float(np.exp(np.log(perm[m]).mean()))})
    return {"nx": nx, "ny": ny, "nz": nz, "grid_file": str(grid_file), "layer_depths_file": str(depths_file),
            "areal": {"x0": x0, "y0": y0, "dx": dx, "dy": dy}, "files": files, "zones": zones,
            "upscaled": upscaled, "arrays": {"phi": phi, "perm": perm, "ntg": ntg}, "top": float(ln.min()),
            "base": float(ln.max())}
