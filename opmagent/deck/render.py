"""Render an OPM Flow deck (black oil, METRIC) from approved fluid, geo and well models.

Deterministic: the same models and spec always give the same text. Large arrays stay in their
include files; small tables are written inline.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
DEFAULT_SUMMARY = ["FOPR", "FWPR", "FGPR", "FWIR", "FOPT", "FWPT", "FWIT", "FPR", "FWCT", "FGOR",
                   "WOPR", "WWPR", "WWIR", "WBHP", "WWCT", "WGOR"]


class RenderError(ValueError):
    pass


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _ecl_date(d: date) -> str:
    return f"{d.day} '{MONTHS[d.month - 1]}' {d.year}"


def _num(x) -> str:
    if x is None:
        return "1*"
    if isinstance(x, int):
        return str(x)
    return f"{x:.10g}"


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1 if d.day > 28 else d.day)


def report_dates(start: date, end: date, frequency: str) -> list[date]:
    step = {"monthly": 1, "quarterly": 3, "yearly": 12}.get(frequency)
    if not step:
        raise RenderError("report frequency must be monthly, quarterly or yearly")
    out, d = [], _add_months(start, step)
    while d <= end:
        out.append(d)
        d = _add_months(d, step)
    if not out or out[-1] != end:
        out.append(end)
    return out


RES_TABLES = ("swof", "sgof", "rock", "contacts", "aquifers", "aquifer_connections")


def combine(geo: dict, res: dict) -> dict:
    """The geo model with the res model's tables and arrays merged in: the view the renderer works on (the deck
    needs both halves; each model owns its own half). The res version must be defined on this geo version."""
    if res["header"].get("geo_version_id") != geo["header"]["id"]:
        raise RenderError(f"res {res['header']['model_name']} v{res['header']['version']} is defined on geo version "
                          f"{res['header'].get('geo_version_id')}, not on {geo['header']['id']}")
    t = {k: v for k, v in geo["tables"].items() if k not in RES_TABLES}
    t.update({k: list(res["tables"].get(k) or []) for k in RES_TABLES})
    t["geo_properties"] = list(geo["tables"]["geo_properties"]) + list(res["tables"].get("res_properties") or [])
    return {"header": geo["header"], "tables": t, "res_header": res["header"]}


def _region_count(geo: dict, kw: str) -> int:
    p = next((p for p in geo["tables"]["geo_properties"] if p["keyword"] == kw), None)
    if not p:
        return 1
    if p["const_value"] is not None:
        return int(p["const_value"])
    from ..validators._util import read_array
    return int(max(read_array(p["path"])))


def areal(geo: dict) -> dict | None:
    """Regular areal layout (x0, y0, dx, dy) of a grid, if known: uniform cartesian grids, or the
    geo modeler's corner-point grids (recorded in spec_json['areal'])."""
    h = geo["header"]
    spec = h.get("spec_json") or {}
    if isinstance(spec, dict) and spec.get("areal"):
        a = spec["areal"]
        return {"x0": a["x0"], "y0": a["y0"], "dx": a["dx"], "dy": a["dy"]}
    if h["grid_type"] == "cartesian" and h.get("dx") and h.get("dy"):
        return {"x0": 0.0, "y0": 0.0, "dx": h["dx"], "dy": h["dy"]}
    return None


def layer_bounds(geo: dict, i: int, j: int) -> list[tuple[float, float]] | None:
    """Top and bottom depth of each layer in column (i, j)."""
    h = geo["header"]
    if h["grid_type"] == "cartesian" and h.get("dz") and h.get("tops") is not None:
        return [(h["tops"] + k * h["dz"], h["tops"] + (k + 1) * h["dz"]) for k in range(h["nz"])]
    spec = h.get("spec_json") or {}
    if isinstance(spec, dict) and spec.get("layer_depths_file"):
        import numpy as np
        z = np.load(spec["layer_depths_file"])          # shape (nz+1, ny, nx): surfaces at cell centres
        return [(float(z[k, j - 1, i - 1]), float(z[k + 1, j - 1, i - 1])) for k in range(h["nz"])]
    return None


def resolve_completions(geo: dict, well: dict) -> list[dict]:
    """IJK completions as-is; MD completions converted to cells for uniform cartesian grids."""
    h = geo["header"]
    out = []
    traj = defaultdict(list)
    for t in sorted(well["tables"]["well_trajectory"], key=lambda r: (r["well_name"], r["md"])):
        traj[t["well_name"]].append(t)
    for c in well["tables"]["well_completions"]:
        if c["i"] is not None:
            out.append(c)
            continue
        ar = areal(geo)
        if ar is None:
            raise RenderError(f"{c['well_name']}: MD completions need a regular areal grid (cartesian or geo-modeler grid)")
        pts = traj.get(c["well_name"])
        if not pts:
            raise RenderError(f"{c['well_name']}: MD completion but no trajectory")
        cells: dict[tuple[int, int], set[int]] = defaultdict(set)
        n = min(4000, max(2, int((c["bottom_md"] - c["top_md"]) / 0.5) + 1))   # sample every 0.5 m MD
        for s in range(n):
            md = c["top_md"] + (c["bottom_md"] - c["top_md"]) * s / (n - 1)
            for a, b in zip(pts, pts[1:]):
                if a["md"] <= md <= b["md"]:
                    f = (md - a["md"]) / (b["md"] - a["md"]) if b["md"] > a["md"] else 0
                    x, y, z = (a[k] + f * (b[k] - a[k]) for k in ("x", "y", "tvd"))
                    i, j = int((x - ar["x0"]) // ar["dx"]) + 1, int((y - ar["y0"]) // ar["dy"]) + 1
                    if 1 <= i <= h["nx"] and 1 <= j <= h["ny"]:
                        for k, (top, bot) in enumerate(layer_bounds(geo, i, j) or [], start=1):
                            if top <= z < bot:
                                cells[(i, j)].add(k)
                                break
                    break
        if not cells:
            raise RenderError(f"{c['well_name']}: perforation {c['top_md']}-{c['bottom_md']} m MD is outside the grid")
        for (i, j), ks in sorted(cells.items()):
            out.append({**c, "i": i, "j": j, "k1": min(ks), "k2": max(ks)})
    return out


def grid_section(gh: dict, gt: dict, file_paths: dict[int, str]) -> list[str]:
    """The GRID section's body (after the GRID keyword) from a geo model: INIT, the grid itself (inline cartesian
    sizes or the COORD/ZCORN include), the cell arrays (constants inline, files as INCLUDE), MINPV, PINCH, faults.
    geo_properties rows need their file 'path' set. Shared by the deck builder and the static view."""
    L: list[str] = []
    a = L.append
    nx, ny, nz = gh["nx"], gh["ny"], gh["nz"]
    ncell = nx * ny * nz
    a("INIT\n")
    props = {p["keyword"]: p for p in gt["geo_properties"]}
    if gh["grid_type"] == "cartesian":
        for kw, scalar, n in (("DX", "dx", ncell), ("DY", "dy", ncell), ("DZ", "dz", ncell), ("TOPS", "tops", nx * ny)):
            if kw not in props:
                a(f"{kw}\n {n}*{_num(gh[scalar])} /\n")
    else:
        a(f"INCLUDE\n '{file_paths[gh['grid_file_id']]}' /\n")
    for kw in ("DX", "DY", "DZ", "TOPS", "ACTNUM", "PORO", "PERMX", "PERMY", "PERMZ", "NTG",
               "MULTX", "MULTY", "MULTZ", "MULTX-", "MULTY-", "MULTZ-", "MULTPV"):
        p = props.get(kw)
        if not p:
            continue
        n = nx * ny if kw == "TOPS" else ncell
        a(f"{kw}\n {n}*{_num(p['const_value'])} /\n" if p["const_value"] is not None else f"INCLUDE\n '{p['path']}' /\n")
    if gh.get("minpv") is not None:
        a(f"MINPV\n {_num(gh['minpv'])} /\n")
    if gh.get("pinch_threshold") is not None:
        a(f"PINCH\n {_num(gh['pinch_threshold'])} /\n")
    if gt["faults"]:
        a("FAULTS")
        for f in gt["faults"]:
            a(f" '{f['fault_name']}' {f['i1']} {f['i2']} {f['j1']} {f['j2']} {f['k1']} {f['k2']} '{f['face']}' /")
        a("/\n")
    if gt["fault_multipliers"]:
        a("MULTFLT")
        for m in gt["fault_multipliers"]:
            a(f" '{m['fault_name']}' {_num(m['mult'])} /")
        a("/\n")
    return L


def render(spec: dict, fluid: dict, geo: dict, well: dict, file_paths: dict[int, str]) -> str:
    """geo: combine(geo, res), the static and dynamic model together. spec: name, start_date, end_date, report ('monthly'|'quarterly'|'yearly'), summary (list), title."""
    fh, gh, wh = fluid["header"], geo["header"], well["header"]
    ft, gt, wt = fluid["tables"], geo["tables"], well["tables"]
    for p in gt["geo_properties"]:
        p["path"] = file_paths.get(p["file_id"]) if p["file_id"] else None
    start = _d(spec.get("start_date") or wh.get("start_date") or "2026-01-01")
    end = _d(spec["end_date"])
    if end <= start:
        raise RenderError("end_date must be after start_date")
    nx, ny, nz = gh["nx"], gh["ny"], gh["nz"]
    ncell = nx * ny * nz
    comps = resolve_completions(geo, well)
    wells = sorted(wt["wells"], key=lambda w: w["well_name"])
    groups = wt["well_groups"]
    pvto_blocks = defaultdict(lambda: defaultdict(list))
    for r in ft["pvto"]:
        pvto_blocks[r["pvtnum"]][r["rs"]].append(r)
    npvt = max([1] + [r["pvtnum"] for r in ft["pvto"] + ft["pvdg"] + ft["pvtw"]])
    nsat = max([1] + [r["satnum"] for r in gt["swof"]])
    neql = max([1] + [c["eqlnum"] for c in gt["contacts"]])
    max_sat_rows = max([len([r for r in gt["swof"] if r["satnum"] == s]) for s in range(1, nsat + 1)] +
                       [len([r for r in gt["sgof"] if r["satnum"] == s]) for s in range(1, nsat + 1)])
    max_p_rows = max([len(rr) for b in pvto_blocks.values() for rr in b.values()] + [len(ft["pvdg"])])
    max_rs = max([len(b) for b in pvto_blocks.values()] + [1])
    pvtg_blocks = defaultdict(lambda: defaultdict(list))
    for r in ft["pvtg"]:
        pvtg_blocks[r["pvtnum"]][r["pressure"]].append(r)
    if pvtg_blocks:
        max_p_rows = max([max_p_rows] + [len(rr) for b in pvtg_blocks.values() for rr in b.values()])
        max_rs = max([max_rs] + [len(b) for b in pvtg_blocks.values()])
    max_conn = max([sum(c["k2"] - c["k1"] + 1 for c in comps if c["well_name"] == w["well_name"]) for w in wells] + [1])
    L = []
    a = L.append
    pt = spec.get("passthrough") or {}

    def passthrough(section):
        for item in pt.get(section, []):
            a(item["raw_text"].rstrip() + "\n")

    # ---------------- RUNSPEC
    a("-- Generated by opmagent deck builder. Do not edit by hand; rebuild from approved models.")
    rh = geo.get("res_header") or {}
    a(f"-- fluid {fh['model_name']} v{fh['version']} | geo {gh['model_name']} v{gh['version']} | "
      + (f"res {rh['model_name']} v{rh['version']} | " if rh else "") + f"wells {wh['model_name']} v{wh['version']}")
    a("RUNSPEC\n")
    a(f"TITLE\n{spec.get('title') or spec['name']}\n")
    a(f"DIMENS\n {nx} {ny} {nz} /\n")
    phases = {p.strip().upper() for p in fh["phases"].split(",")}
    for ph in ("OIL", "WATER", "GAS"):
        if ph in phases:
            a(ph)
    if fh["has_disgas"]:
        a("DISGAS")
    if fh["has_vapoil"]:
        a("VAPOIL")
    a("\n" + fh["unit_system"] + "\n")
    a(f"START\n {_ecl_date(start)} /\n")
    a(f"WELLDIMS\n {len(wells)} {max_conn} {len(groups) + 1} {len(wells)} /\n")
    a(f"TABDIMS\n {nsat} {npvt} {max_sat_rows} {max_p_rows} 1 {max_rs} /\n")
    a(f"EQLDIMS\n {neql} /\n")
    aqs = sorted(gt.get("aquifers") or [], key=lambda r: r["aquifer_id"])
    aq_conns = gt.get("aquifer_connections") or []
    if aqs:
        cells = {x["aquifer_id"]: 0 for x in aqs}
        for c in aq_conns:
            cells[c["aquifer_id"]] += (c["i2"] - c["i1"] + 1) * (c["j2"] - c["j1"] + 1) * (c["k2"] - c["k1"] + 1)
        a(f"AQUDIMS\n 0 0 1 36 {len(aqs)} {max(cells.values())} /\n")
    a("UNIFOUT\n")
    passthrough("RUNSPEC")

    # ---------------- GRID
    a("GRID\n")
    L += grid_section(gh, gt, file_paths)
    props = {p["keyword"]: p for p in gt["geo_properties"]}
    passthrough("GRID")
    if pt.get("EDIT"):
        a("EDIT\n")
        passthrough("EDIT")

    # ---------------- PROPS
    a("PROPS\n")
    if ft["pvto"]:
        a("PVTO")
        for reg in sorted(pvto_blocks):
            for rs in sorted(pvto_blocks[reg]):
                rows = sorted(pvto_blocks[reg][rs], key=lambda r: r["pressure"])
                first = rows[0]
                a(f" {_num(rs)} {_num(first['pressure'])} {_num(first['bo'])} {_num(first['mu_o'])}")
                for r in rows[1:]:
                    a(f"    {_num(r['pressure'])} {_num(r['bo'])} {_num(r['mu_o'])}")
                a(" /")
            a("/")
        a("")
    if ft["pvtg"]:
        a("PVTG")
        for reg in sorted(pvtg_blocks):
            for p in sorted(pvtg_blocks[reg]):
                rows = sorted(pvtg_blocks[reg][p], key=lambda r: (not r["is_saturated"], -r["rv"]))
                first = rows[0]
                a(f" {_num(p)} {_num(first['rv'])} {_num(first['bg'])} {_num(first['mu_g'])}")
                for r in rows[1:]:
                    a(f"    {_num(r['rv'])} {_num(r['bg'])} {_num(r['mu_g'])}")
                a(" /")
            a("/")
        a("")
    for kw, tab, cols, key in (("PVDO", ft["pvdo"], ("pressure", "bo", "mu_o"), "pvtnum"),
                               ("PVDG", ft["pvdg"], ("pressure", "bg", "mu_g"), "pvtnum")):
        if tab:
            a(kw)
            for reg in range(1, npvt + 1):
                for r in sorted((r for r in tab if r[key] == reg), key=lambda r: r["pressure"]):
                    a(" " + " ".join(_num(r[c]) for c in cols))
                a("/")
            a("")
    if ft["pvtw"]:
        a("PVTW")
        for r in sorted(ft["pvtw"], key=lambda r: r["pvtnum"]):
            a(f" {_num(r['p_ref'])} {_num(r['bw'])} {_num(r['cw'])} {_num(r['mu_w'])} {_num(r['viscosibility'])} /")
        a("")
    a("DENSITY")
    for r in sorted(ft["density"], key=lambda r: r["pvtnum"]):
        a(f" {_num(r['oil'])} {_num(r['water'])} {_num(r['gas'])} /")
    a("")
    a("ROCK")
    for r in sorted(gt["rock"], key=lambda r: r["region"]):
        a(f" {_num(r['p_ref'])} {_num(r['cr'])} /")
    a("")
    for kw, tab, cols in (("SWOF", gt["swof"], ("sw", "krw", "krow", "pcow")), ("SGOF", gt["sgof"], ("sg", "krg", "krog", "pcog"))):
        if tab:
            a(kw)
            for s in range(1, nsat + 1):
                for r in sorted((r for r in tab if r["satnum"] == s), key=lambda r: r[cols[0]]):
                    a(" " + " ".join(_num(r[c]) for c in cols))
                a("/")
            a("")
    if "SWATINIT" in props:
        p = props["SWATINIT"]
        a(f"SWATINIT\n {ncell}*{_num(p['const_value'])} /\n" if p["const_value"] is not None else f"INCLUDE\n '{p['path']}' /\n")
    passthrough("PROPS")

    # ---------------- REGIONS
    regs = [kw for kw in ("SATNUM", "PVTNUM", "EQLNUM", "FIPNUM") if kw in props]
    if regs or pt.get("REGIONS"):
        a("REGIONS\n")
        for kw in regs:
            p = props[kw]
            a(f"{kw}\n {ncell}*{int(p['const_value'])} /\n" if p["const_value"] is not None else f"INCLUDE\n '{p['path']}' /\n")
        passthrough("REGIONS")

    # ---------------- SOLUTION
    a("SOLUTION\n")
    top = gh["tops"] if gh["tops"] is not None else 0.0
    use_rsvd = bool(ft["rsvd"] or ft["pbvd"])
    a("EQUIL")
    for c in sorted(gt["contacts"], key=lambda c: c["eqlnum"]):
        owc = c["owc_depth"] if c["owc_depth"] is not None else top + 10000.0
        goc = c["goc_depth"] if c["goc_depth"] is not None else top - 1000.0
        a(f" {_num(c['datum_depth'])} {_num(c['datum_pressure'])} {_num(owc)} {_num(c['pcow_at_owc'])} "
          f"{_num(goc)} {_num(c['pcog_at_goc'])} " +
          (" ".join(_num(x) for x in extra[c["eqlnum"]]) if (extra := {int(k): v for k, v in (spec.get("equil_extra") or {}).items()}).get(c["eqlnum"])
           else str(1 if use_rsvd else 0)) + " /")
    a("")
    for kw, tab, col in (("RSVD", ft["rsvd"], "rs"), ("PBVD", ft["pbvd"], "pb")):
        if tab:
            a(kw)
            for e in range(1, neql + 1):
                for r in sorted((r for r in tab if r["eqlnum"] == e), key=lambda r: r["depth"]):
                    a(f" {_num(r['depth'])} {_num(r[col])}")
                a("/")
            a("")
    d = lambda x: "1*" if x is None else _num(x)  # noqa: E731
    fet = [r for r in aqs if r["aq_type"] == "FETKOVICH"]
    ct = [r for r in aqs if r["aq_type"] == "CARTER_TRACY"]
    if fet:
        a("AQUFETP")
        for r in fet:
            a(f" {r['aquifer_id']} {_num(r['datum_depth'])} {d(r['init_pressure'])} {_num(r['water_volume'])} "
              f"{_num(r['total_compressibility'])} {_num(r['productivity_index'])} {r['pvtw_table']} /")
        a("/\n")
    if ct:
        a("AQUCT")
        for r in ct:
            a(f" {r['aquifer_id']} {_num(r['datum_depth'])} {d(r['init_pressure'])} {_num(r['permeability'])} "
              f"{_num(r['porosity'])} {_num(r['total_compressibility'])} {_num(r['inner_radius'])} "
              f"{_num(r['thickness'])} {_num(r['angle_deg'] if r['angle_deg'] is not None else 360)} "
              f"{r['pvtw_table']} {r['influence_table'] or 1} /")
        a("/\n")
    if aqs:
        a("AQUANCON")
        for c in sorted(aq_conns, key=lambda c: (c["aquifer_id"], c["id"] if c.get("id") else 0)):
            a(f" {c['aquifer_id']} {c['i1']} {c['i2']} {c['j1']} {c['j2']} {c['k1']} {c['k2']} '{c['face']}' "
              f"{d(c.get('influx_coeff'))} {_num(c['influx_mult'])} /")
        a("/\n")
    if pt.get("SOLUTION"):
        passthrough("SOLUTION")
    else:
        a("RPTRST\n 'BASIC=2' /\n")

    # ---------------- SUMMARY
    a("SUMMARY\n")
    if spec.get("summary_passthrough"):
        for item in spec["summary_passthrough"]:
            a(item["raw_text"].rstrip())
    else:
        for v in spec.get("summary") or DEFAULT_SUMMARY:
            a(v if v.startswith("F") else f"{v}\n/")
        if aqs:   # influx rate, cumulative and pressure per aquifer
            ids = " ".join(str(r["aquifer_id"]) for r in aqs)
            for v in ("AAQR", "AAQT", "AAQP"):
                a(f"{v}\n {ids} /")
    a("")

    # ---------------- SCHEDULE
    a("SCHEDULE\n")
    if spec.get("schedule_mode") == "verbatim":
        passthrough("SCHEDULE")
        a("END")
        return "\n".join(L) + "\n"
    if not spec.get("schedule_extra"):
        a("RPTRST\n 'BASIC=2' /\n")
    if groups:
        a("GRUPTREE")
        for g in groups:
            a(f" '{g['group_name']}' '{g['parent_group']}' /")
        a("/\n")
    a("WELSPECS")
    ar = areal(geo)
    for w in wells:
        if w["head_i"] is None and w["head_x"] is not None and ar:
            w["head_i"] = min(max(int((w["head_x"] - ar["x0"]) // ar["dx"]) + 1, 1), nx)
            w["head_j"] = min(max(int((w["head_y"] - ar["y0"]) // ar["dy"]) + 1, 1), ny)
        if w["head_i"] is None:
            comp = next((c for c in comps if c["well_name"] == w["well_name"]), None)
            if comp:
                w["head_i"], w["head_j"] = comp["i"], comp["j"]
        if w["head_i"] is None:
            raise RenderError(f"{w['well_name']}: wellhead needs I,J or X,Y on a regular grid")
        a(f" '{w['well_name']}' '{w['group_name']}' {w['head_i']} {w['head_j']} {_num(w['ref_depth'])} "
          f"'{w['preferred_phase']}' /")
    a("/\n")
    if any(w["efficiency_factor"] != 1 for w in wells):
        a("WEFAC")
        for w in wells:
            a(f" '{w['well_name']}' {_num(w['efficiency_factor'])} /")
        a("/\n")
    econ = wt["well_econ_limits"]
    if econ:
        a("WECON")
        for e in econ:
            a(f" '{e['well_name']}' {_num(e['min_oil_rate'])} {_num(e['min_gas_rate'])} {_num(e['max_wct'])} "
              f"{_num(e['max_gor'])} {_num(e['max_wgr'])} '{e['workover']}' '{'YES' if e['end_run'] else 'NO'}' /")
        a("/\n")

    # events by date
    by_date: dict[date, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for c in comps:
        by_date[max(_d(c["event_date"]), start)]["comp"].append(c)
    for e in wt["well_events"]:
        by_date[max(_d(e["event_date"]), start)]["event"].append(e)
    for g in wt["group_controls"]:
        by_date[max(_d(g["event_date"]), start)]["group"].append(g)
    wtype = {w["well_name"]: w for w in wells}
    extra_by_date = defaultdict(list)
    for x in spec.get("schedule_extra") or []:
        extra_by_date[max(_d(x["date"]), start)].append(x["raw_text"])
    rdates = ([_d(x) for x in spec["report_dates"]] if spec.get("report_dates")
              else report_dates(start, end, spec.get("report", "monthly")))
    steps = sorted(set(by_date) | set(rdates) | set(extra_by_date))
    for d in steps:
        if d > end:
            break
        if d != start:
            a(f"DATES\n {_ecl_date(d)} /\n/\n")
        for raw in extra_by_date.get(d, []):
            a(raw.rstrip() + "\n")
        ev = by_date.get(d)
        if not ev:
            continue
        if ev["comp"]:
            a("COMPDAT")
            for c in ev["comp"]:
                a(f" '{c['well_name']}' {c['i']} {c['j']} {c['k1']} {c['k2']} '{c['state']}' 1* "
                  f"{_num(c['conn_factor'])} {_num(c['diameter'])} {_num(c['kh'])} {_num(c['skin'])} /")
            a("/\n")
        prod_pred, inj_pred, prod_hist, inj_hist = [], [], [], []
        for e in ev["event"]:
            inj = wtype[e["well_name"]]["well_type"] == "INJECTOR"
            (inj_hist if inj else prod_hist).append(e) if e["mode"] == "HISTORY" else (inj_pred if inj else prod_pred).append(e)
        if prod_pred:
            a("WCONPROD")
            for e in prod_pred:
                a(f" '{e['well_name']}' '{e['status']}' '{e['control'] or 'BHP'}' {_num(e['oil_rate'])} {_num(e['water_rate'])} "
                  f"{_num(e['gas_rate'])} {_num(e['liquid_rate'])} 1* {_num(e['bhp'])} {_num(e['thp'])} {_num(e['vfp_table'])} /")
            a("/\n")
        if inj_pred:
            a("WCONINJE")
            for e in inj_pred:
                phase = wtype[e["well_name"]]["preferred_phase"]
                a(f" '{e['well_name']}' '{phase}' '{e['status']}' '{e['control'] or 'RATE'}' {_num(e['inj_rate'])} 1* "
                  f"{_num(e['bhp'])} {_num(e['thp'])} {_num(e['vfp_table'])} /")
            a("/\n")
        if prod_hist:
            a("WCONHIST")
            for e in prod_hist:
                a(f" '{e['well_name']}' '{e['status']}' '{e['control'] or 'RESV'}' {_num(e['oil_rate'])} {_num(e['water_rate'])} "
                  f"{_num(e['gas_rate'])} 1* 1* {_num(e['thp'])} {_num(e['bhp'])} /")
            a("/\n")
        if inj_hist:
            a("WCONINJH")
            for e in inj_hist:
                phase = wtype[e["well_name"]]["preferred_phase"]
                a(f" '{e['well_name']}' '{phase}' '{e['status']}' {_num(e['inj_rate'])} {_num(e['bhp'])} {_num(e['thp'])} /")
            a("/\n")
        gp = [g for g in ev["group"] if g["kind"] == "PROD"]
        gi = [g for g in ev["group"] if g["kind"] == "INJ"]
        if gp:
            a("GCONPROD")
            for g in gp:
                a(f" '{g['group_name']}' '{g['control']}' {_num(g['oil_rate'])} {_num(g['water_rate'])} {_num(g['gas_rate'])} "
                  f"{_num(g['liquid_rate'])} /")
            a("/\n")
        if gi:
            a("GCONINJE")
            for g in gi:
                a(f" '{g['group_name']}' '{g['inj_phase']}' '{g['control']}' {_num(g['inj_rate'])} {_num(g['resv_rate'])} "
                  f"{_num(g['reinj_frac'])} {_num(g['voidage_frac'])} /")
            a("/\n")
    a("END")
    return "\n".join(L) + "\n"
