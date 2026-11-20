"""Round-trip comparison of two OPM runs (original vs rebuilt deck), in three levels:

1. static  - INIT file: pore volume, permeability, porosity, NTG, transmissibilities, depth, regions
2. init    - first restart step: pressure, saturations, Rs/Rv
3. dynamic - summary vectors over time

Levels stop at the first failure, because a static difference explains any later one.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

STATIC = ["PORV", "PERMX", "PERMY", "PERMZ", "PORO", "NTG", "TRANX", "TRANY", "TRANZ", "DEPTH", "DX", "DY", "DZ",
          "SATNUM", "PVTNUM", "EQLNUM", "FIPNUM", "IMBNUM"]
# plus every end-point scaling array (SWL, ISWL, KRW, ...) the INIT files contain: unset cells are -1e20 there
INIT = ["PRESSURE", "SWAT", "SGAS", "RS", "RV"]
TOL = {"static": 1e-6, "init": 1e-6, "dynamic": 1e-4}   # INIT/UNRST are single precision (eps 1.2e-7)


def _rel(a, b) -> tuple[float, int]:
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if a.shape != b.shape:
        return float("inf"), -1
    scale = max(np.abs(a).max(initial=0.0), 1e-30)
    d = np.abs(a - b) / scale
    i = int(d.argmax()) if d.size else -1
    return (float(d.max()) if d.size else 0.0), i


def _ijk(idx, actnum_to_global, nx, ny):
    g = actnum_to_global[idx] if actnum_to_global is not None and idx < len(actnum_to_global) else idx
    return (g % nx + 1, (g // nx) % ny + 1, g // (nx * ny) + 1)


def compare_static(run_a: Path, run_b: Path, stem_a: str, stem_b: str, tol=TOL["static"]) -> dict:
    from opm.io.ecl import EclFile, EGrid
    A, B = EclFile(str(run_a / f"{stem_a}.INIT")), EclFile(str(run_b / f"{stem_b}.INIT"))
    names_a = {x[0] for x in A.arrays}
    names_b = {x[0] for x in B.arrays}
    g = EGrid(str(run_a / f"{stem_a}.EGRID"))
    nx, ny, _ = g.dimension
    act = np.flatnonzero(np.array(g.export_actnum()) > 0) if hasattr(g, "export_actnum") else None
    from .importer import ENDPOINT_KWS
    out, fails = {}, []
    for k in STATIC + sorted(ENDPOINT_KWS & (names_a | names_b)):
        if k not in names_a and k not in names_b:
            continue
        if (k in names_a) != (k in names_b):
            out[k] = {"missing_in": "rebuilt" if k in names_a else "original"}
            fails.append(k)
            continue
        d, i = _rel(A[k], B[k])
        out[k] = {"max_rel_diff": d}
        if d > tol:
            a, b = np.asarray(A[k]), np.asarray(B[k])
            out[k]["first_cell"] = _ijk(i, act, nx, ny) if i >= 0 else None
            out[k]["values"] = [float(a[i]), float(b[i])] if i >= 0 and i < len(a) and i < len(b) else None
            fails.append(k)
    return {"level": "static", "passed": not fails, "failed": fails, "arrays": out}


def compare_static_decks(deck_a: Path, deck_b: Path, tol=TOL["static"]) -> dict:
    """Static comparison from the parsed decks (no simulator output needed): final cell properties,
    active cells, and per-cell depth and volume from OPM's processed grid."""
    from .importer import GRID_PROPS, REGION_PROPS, parse
    (_, sa), (_, sb) = parse(deck_a), parse(deck_b)
    ga, gb = sa.grid(), sb.grid()
    out, fails = {}, []
    if (ga.nx, ga.ny, ga.nz, ga.nactive) != (gb.nx, gb.ny, gb.nz, gb.nactive):
        return {"level": "static", "passed": False, "failed": ["GRID"],
                "arrays": {"GRID": {"original": [ga.nx, ga.ny, ga.nz, ga.nactive], "rebuilt": [gb.nx, gb.ny, gb.nz, gb.nactive]}}}
    fa, fb = sa.field_props(), sb.field_props()
    for k in GRID_PROPS + ["SWATINIT"] + REGION_PROPS:
        vals = []
        for f in (fa, fb):
            v = None
            for getter in (f.get_double_array, f.get_int_array):
                try:
                    v = np.asarray(getter(k))
                    break
                except Exception:  # noqa: BLE001
                    continue
            vals.append(v)
        if vals[0] is None and vals[1] is None:
            continue
        if (vals[0] is None) != (vals[1] is None):
            out[k] = {"missing_in": "rebuilt" if vals[1] is None else "original"}
            fails.append(k)
            continue
        d, _ = _rel(vals[0], vals[1])
        out[k] = {"max_rel_diff": d}
        if d > tol:
            fails.append(k)
    n = ga.cartesianSize
    depth_a = np.array([ga.getCellDepth(g) for g in range(n)])
    depth_b = np.array([gb.getCellDepth(g) for g in range(n)])
    vol_a = np.array([ga.getCellVolume(g) for g in range(n)])
    vol_b = np.array([gb.getCellVolume(g) for g in range(n)])
    for k, a, b in (("CELL_DEPTH", depth_a, depth_b), ("CELL_VOLUME", vol_a, vol_b)):
        d, i = _rel(a, b)
        out[k] = {"max_rel_diff": d}
        if d > tol:
            out[k]["first_cell"] = (i % ga.nx + 1, (i // ga.nx) % ga.ny + 1, i // (ga.nx * ga.ny) + 1)
            fails.append(k)
    return {"level": "static", "passed": not fails, "failed": fails, "arrays": out}


def compare_init(run_a: Path, run_b: Path, stem_a: str, stem_b: str, tol=TOL["init"]) -> dict:
    from opm.io.ecl import ERst
    A, B = ERst(str(run_a / f"{stem_a}.UNRST")), ERst(str(run_b / f"{stem_b}.UNRST"))
    sa, sb = A.report_steps[0], B.report_steps[0]
    out, fails = {}, []
    for k in INIT:
        ha, hb = A.has(k, sa) if hasattr(A, "has") else (k, sa) in A, B.has(k, sb) if hasattr(B, "has") else (k, sb) in B
        if not ha and not hb:
            continue
        if ha != hb:
            out[k] = {"missing_in": "rebuilt" if ha else "original"}
            fails.append(k)
            continue
        d, i = _rel(A[k, sa], B[k, sb])
        out[k] = {"max_rel_diff": d}
        if d > tol:
            fails.append(k)
    return {"level": "init", "passed": not fails, "failed": fails, "arrays": out}


def compare_summary(smspec_a: Path, smspec_b: Path, tol=TOL["dynamic"]) -> dict:
    from opm.io.ecl import ESmry
    A, B = ESmry(str(smspec_a)), ESmry(str(smspec_b))
    ka, kb = set(A.keys()), set(B.keys())
    skip = {"TIME", "YEARS", "DAY", "MONTH", "YEAR", "TIMESTEP", "TCPU", "TCPUDAY", "TCPUTS", "ELAPSED", "MSUMLINS",
            "MSUMNEWT", "NEWTON", "NLINEARS", "NLINSMAX", "NLINSMIN", "MLINEARS"}
    ta, tb = np.asarray(A["TIME"]), np.asarray(B["TIME"])
    common_t = np.intersect1d(np.round(ta, 6), np.round(tb, 6))
    ia = np.isin(np.round(ta, 6), common_t)
    ib = np.isin(np.round(tb, 6), common_t)
    out, fails = {}, []
    for k in sorted((ka & kb) - skip):
        try:
            a, b = np.asarray(A[k])[ia], np.asarray(B[k])[ib]
        except Exception:  # noqa: BLE001
            continue
        d, i = _rel(a, b)
        out[k] = d
        if d > tol:
            fails.append(k)
    missing = sorted((ka ^ kb) - skip)
    worst = sorted(out.items(), key=lambda kv: -kv[1])[:10]
    return {"level": "dynamic", "passed": not fails, "failed": fails, "vectors_compared": len(out),
            "times_compared": int(len(common_t)), "missing_vectors": missing[:20], "worst": worst}


def compare(run_a: Path, run_b: Path, stem_a: str, stem_b: str, deck_a: Path | None = None, deck_b: Path | None = None) -> dict:
    levels = []
    both_init = (run_a / f"{stem_a}.INIT").exists() and (run_b / f"{stem_b}.INIT").exists()
    static = (lambda: compare_static(run_a, run_b, stem_a, stem_b)) if both_init else \
             (lambda: compare_static_decks(deck_a, deck_b))
    for fn in (static,
               lambda: compare_init(run_a, run_b, stem_a, stem_b),
               lambda: compare_summary(run_a / f"{stem_a}.SMSPEC", run_b / f"{stem_b}.SMSPEC")):
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001
            r = {"level": "?", "passed": False, "error": f"{type(e).__name__}: {e}"}
        levels.append(r)
        if not r["passed"]:
            break
    return {"passed": len(levels) == 3 and all(l["passed"] for l in levels), "levels": levels}
