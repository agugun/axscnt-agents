"""Fluid model rules (black oil)."""
from __future__ import annotations

from collections import defaultdict

from ._util import Issues, contiguous_regions, non_decreasing, non_increasing, rows, strictly_increasing

# plausible surface densities: METRIC kg/m3, FIELD lb/ft3
DENSITY_RANGE = {
    "METRIC": {"oil": (600, 1100), "water": (950, 1250), "gas": (0.5, 5.0)},
    "FIELD": {"oil": (37, 69), "water": (59, 78), "gas": (0.03, 0.31)},
}


def validate(con, vid: int) -> list[dict]:
    iss = Issues()
    hdr = rows(con, "SELECT * FROM fluid_model_versions WHERE id = ?", vid)
    if not hdr:
        iss.error("exists", f"fluid version {vid} not found")
        return iss
    h = hdr[0]
    phases = {p.strip().upper() for p in h["phases"].split(",")}

    pvto = rows(con, "SELECT * FROM pvto WHERE version_id=? ORDER BY pvtnum, rs, pressure", vid)
    pvdo = rows(con, "SELECT * FROM pvdo WHERE version_id=? ORDER BY pvtnum, pressure", vid)
    pvdg = rows(con, "SELECT * FROM pvdg WHERE version_id=? ORDER BY pvtnum, pressure", vid)
    pvtg = rows(con, "SELECT * FROM pvtg WHERE version_id=? ORDER BY pvtnum, pressure, rv", vid)
    pvtw = rows(con, "SELECT * FROM pvtw WHERE version_id=? ORDER BY pvtnum", vid)
    dens = rows(con, "SELECT * FROM density WHERE version_id=? ORDER BY pvtnum", vid)
    rsvd = rows(con, "SELECT * FROM rsvd WHERE version_id=? ORDER BY eqlnum, depth", vid)
    pbvd = rows(con, "SELECT * FROM pbvd WHERE version_id=? ORDER BY eqlnum, depth", vid)

    # --- required tables
    if "OIL" in phases:
        if h["has_disgas"] and not pvto:
            iss.error("required", "DISGAS is on but PVTO is empty")
        if not h["has_disgas"] and not pvdo:
            iss.error("required", "dead oil needs PVDO")
    if "GAS" in phases and not (pvtg if h["has_vapoil"] else pvdg):
        iss.error("required", "gas phase needs " + ("PVTG" if h["has_vapoil"] else "PVDG"))
    if "WATER" in phases and not pvtw:
        iss.error("required", "water phase needs PVTW")
    if not dens:
        iss.error("required", "DENSITY is empty")

    # --- PVTO
    by_region = defaultdict(lambda: defaultdict(list))
    for r in pvto:
        by_region[r["pvtnum"]][r["rs"]].append(r)
    for reg, blocks in by_region.items():
        sat_line = []
        rs_sorted = sorted(blocks)
        for rs in rs_sorted:
            blk = sorted(blocks[rs], key=lambda r: r["pressure"])
            sats = [r for r in blk if r["is_saturated"]]
            where = f"PVTO region {reg}, Rs={rs:g}"
            if len(sats) != 1:
                iss.error("pvto_block", f"each Rs block needs exactly one saturated row, found {len(sats)}", where)
                continue
            if blk[0] is not sats[0]:
                iss.error("pvto_block", "saturated row must be the lowest pressure of its Rs block", where)
            sat_line.append(sats[0])
            under = blk[1:]
            if under:
                if not non_increasing([sats[0]["bo"]] + [r["bo"] for r in under]):
                    iss.error("pvto_under_bo", "Bo must fall with pressure above Pb", where)
                if not non_decreasing([sats[0]["mu_o"]] + [r["mu_o"] for r in under]):
                    iss.warn("pvto_under_mu", "oil viscosity usually rises with pressure above Pb", where)
        if sat_line:
            ps = [r["pressure"] for r in sat_line]
            bos = [r["bo"] for r in sat_line]
            if not strictly_increasing(ps):
                iss.error("pvto_sat_p", "saturation pressure must increase with Rs", f"PVTO region {reg}")
            if not non_decreasing(bos):
                iss.error("pvto_sat_bo", "Bo must increase with Rs along the saturated line", f"PVTO region {reg}")
            top = sorted(blocks[rs_sorted[-1]], key=lambda r: r["pressure"])
            if len(top) < 2:
                iss.error("pvto_undersat_top", "highest Rs block needs undersaturated rows (OPM requirement)", f"PVTO region {reg}")

    # --- PVDO / PVDG
    for name, tab, bcol, mucol in (("PVDO", pvdo, "bo", "mu_o"), ("PVDG", pvdg, "bg", "mu_g")):
        regs = defaultdict(list)
        for r in tab:
            regs[r["pvtnum"]].append(r)
        for reg, rr in regs.items():
            if len(rr) < 2:
                iss.error("table_size", f"{name} needs at least 2 rows", f"{name} region {reg}")
                continue
            if not strictly_increasing([r[bcol] for r in reversed(rr)]):
                iss.error("fvf_decreasing", f"{bcol.capitalize()} must fall as pressure rises", f"{name} region {reg}")
            if not non_decreasing([r[mucol] for r in rr]):
                iss.warn("visc_increasing", "viscosity usually rises with pressure", f"{name} region {reg}")

    # --- PVTW plausibility
    for r in pvtw:
        if not 0.9 <= r["bw"] <= 1.2:
            iss.warn("pvtw_bw", f"Bw={r['bw']:g} is unusual (expected 0.9-1.2)", f"PVTW region {r['pvtnum']}")
        if not 0.1 <= r["mu_w"] <= 2.0:
            iss.warn("pvtw_mu", f"water viscosity {r['mu_w']:g} cP is unusual", f"PVTW region {r['pvtnum']}")

    # --- DENSITY plausibility
    rng = DENSITY_RANGE[h["unit_system"]]
    for r in dens:
        for ph in ("oil", "water", "gas"):
            lo, hi = rng[ph]
            if not lo <= r[ph] <= hi:
                iss.warn("density_range", f"{ph} density {r[ph]:g} outside {lo}-{hi}", f"DENSITY region {r['pvtnum']}")

    # --- one table set per PVT region
    sets = {name: {r["pvtnum"] for r in tab} for name, tab in
            (("PVTO", pvto), ("PVDO", pvdo), ("PVDG", pvdg), ("PVTG", pvtg), ("PVTW", pvtw), ("DENSITY", dens)) if tab}
    all_regs = set().union(*sets.values()) if sets else set()
    if all_regs and not contiguous_regions(all_regs):
        iss.error("regions", f"PVT regions must be 1..N without gaps, got {sorted(all_regs)}")
    for name, s in sets.items():
        if s != all_regs:
            iss.error("regions", f"{name} covers regions {sorted(s)}, expected {sorted(all_regs)}")

    # --- RSVD / PBVD
    both = {r["eqlnum"] for r in rsvd} & {r["eqlnum"] for r in pbvd}
    if both:
        iss.error("rsvd_pbvd", f"EQLNUM {sorted(both)} has both RSVD and PBVD; use one")
    if pvto:
        max_rs = max(r["rs"] for r in pvto if r["is_saturated"])
        max_p = max(r["pressure"] for r in pvto)
        for r in rsvd:
            if r["rs"] > max_rs * (1 + 1e-9):
                iss.error("rsvd_range", f"Rs {r['rs']:g} at depth {r['depth']:g} exceeds PVTO max saturated Rs {max_rs:g}", f"RSVD eqlnum {r['eqlnum']}")
        for r in pbvd:
            if r["pb"] > max_p * (1 + 1e-9):
                iss.error("pbvd_range", f"Pb {r['pb']:g} at depth {r['depth']:g} exceeds PVTO pressure range {max_p:g}", f"PBVD eqlnum {r['eqlnum']}")
    return iss
