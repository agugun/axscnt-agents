"""Res model rules: saturation functions, rock, contacts, region arrays, SWATINIT, aquifers (the dynamic model).

The res version is defined on a geo version's grid: array lengths, region numbers and aquifer connections are
checked against that grid."""
from __future__ import annotations

from collections import defaultdict

from ._util import Issues, contiguous_regions, non_decreasing, non_increasing, read_array, rows, strictly_increasing

REGION = {"SATNUM", "EQLNUM", "FIPNUM", "ROCKNUM"}


def _values(con, p) -> list[float] | None:
    if p["const_value"] is not None:
        return [p["const_value"]]
    f = con.execute("SELECT path FROM files WHERE id=?", (p["file_id"],)).fetchone()
    return read_array(f["path"]) if f else None


def validate(con, vid: int) -> list[dict]:
    iss = Issues()
    hdr = rows(con, "SELECT * FROM res_model_versions WHERE id = ?", vid)
    if not hdr:
        iss.error("exists", f"res version {vid} not found")
        return iss
    h = hdr[0]
    geo = rows(con, "SELECT nx, ny, nz, grid_type, tops, dz FROM geo_model_versions WHERE id = ?", h["geo_version_id"])
    if not geo:
        iss.error("geo", f"geo version {h['geo_version_id']} not found")
        return iss
    g = geo[0]
    nx, ny, nz = g["nx"], g["ny"], g["nz"]
    ncell = nx * ny * nz
    props = {p["keyword"]: p for p in rows(con, "SELECT * FROM res_properties WHERE version_id=?", vid)}

    # --- region arrays and SWATINIT on the geo grid
    values: dict[str, list[float]] = {}
    for kw, p in props.items():
        try:
            v = _values(con, p)
        except Exception as e:  # noqa: BLE001
            iss.error("array_read", f"cannot read {kw}: {e}")
            continue
        if v is None:
            iss.error("array_read", f"{kw} file not registered")
            continue
        if p["file_id"] is not None and len(v) != ncell:
            iss.error("array_length", f"{kw} has {len(v)} values, the geo grid has {ncell} cells")
            continue
        values[kw] = v
        if kw == "SWATINIT" and any(x < 0 or x > 1 for x in v):
            iss.error("range", "SWATINIT must be within 0-1")
        if kw in REGION and any(x < 1 or x != int(x) for x in v):
            iss.error("range", f"{kw} must be integers >= 1")
    for kw in REGION & values.keys():
        if not contiguous_regions(int(x) for x in values[kw]):
            iss.error("regions", f"{kw} values must run 1..N without gaps")

    # --- saturation functions per SATNUM
    swof = rows(con, "SELECT * FROM swof WHERE version_id=? ORDER BY satnum, sw", vid)
    sgof = rows(con, "SELECT * FROM sgof WHERE version_id=? ORDER BY satnum, sg", vid)
    nsat = int(max(values.get("SATNUM", [1])))
    for name, tab in (("SWOF", swof), ("SGOF", sgof)):
        regs = {r["satnum"] for r in tab}
        missing = set(range(1, nsat + 1)) - regs
        if tab and missing:
            iss.error("regions", f"{name} has no table for SATNUM {sorted(missing)}, grid uses 1..{nsat}")
        elif tab and max(regs) > nsat:
            # extra tables are valid: IMBNUM (hysteresis), KRNUM and similar region arrays select them
            iss.warn("regions", f"{name} tables {sorted(regs - set(range(1, nsat + 1)))} are not used by SATNUM "
                                f"(1..{nsat}); fine if IMBNUM or another region array selects them")
    if not swof:
        iss.error("required", "SWOF is empty")
    bysat = defaultdict(list)
    for r in swof:
        bysat[r["satnum"]].append(r)
    for s, rr in bysat.items():
        where = f"SWOF satnum {s}"
        if len(rr) < 2:
            iss.error("table_size", "SWOF needs at least 2 rows", where)
            continue
        if not strictly_increasing([r["sw"] for r in rr]):
            iss.error("swof_sw", "Sw must increase", where)
        if not non_decreasing([r["krw"] for r in rr]):
            iss.error("swof_krw", "krw must not fall as Sw rises", where)
        if not non_increasing([r["krow"] for r in rr]):
            iss.error("swof_krow", "krow must not rise as Sw rises", where)
        if rr[0]["krw"] != 0:
            iss.error("swof_end", "krw must be 0 at the first row (connate water)", where)
        if rr[-1]["krow"] != 0:
            iss.error("swof_end", "krow must be 0 at the last row", where)
        if not non_increasing([r["pcow"] for r in rr]):
            iss.warn("swof_pc", "Pcow usually falls as Sw rises", where)
    bysg = defaultdict(list)
    for r in sgof:
        bysg[r["satnum"]].append(r)
    for s, rr in bysg.items():
        where = f"SGOF satnum {s}"
        if len(rr) < 2:
            iss.error("table_size", "SGOF needs at least 2 rows", where)
            continue
        if rr[0]["sg"] != 0:
            iss.error("sgof_sg", "SGOF must start at Sg = 0", where)
        if not strictly_increasing([r["sg"] for r in rr]):
            iss.error("sgof_sg", "Sg must increase", where)
        if not non_decreasing([r["krg"] for r in rr]):
            iss.error("sgof_krg", "krg must not fall as Sg rises", where)
        if not non_increasing([r["krog"] for r in rr]):
            iss.error("sgof_krog", "krog must not rise as Sg rises", where)
        if rr[0]["krg"] != 0:
            iss.error("sgof_end", "krg must be 0 at Sg = 0", where)
        if rr[-1]["krog"] != 0:
            iss.error("sgof_end", "krog must be 0 at the last row", where)
        if s in bysat and abs(bysat[s][0]["krow"] - rr[0]["krog"]) > 1e-6:
            iss.warn("endpoint_consistency", "kro at connate water differs between SWOF and SGOF", where)

    # --- rock and contacts
    rock = rows(con, "SELECT * FROM rock WHERE version_id=?", vid)
    if not rock:
        iss.error("required", "ROCK is empty")
    contacts = rows(con, "SELECT * FROM contacts WHERE version_id=?", vid)
    neql = int(max(values.get("EQLNUM", [1])))
    if {c["eqlnum"] for c in contacts} != set(range(1, neql + 1)):
        iss.error("regions", f"contacts must cover EQLNUM 1..{neql}")
    if g["grid_type"] == "cartesian" and g["tops"] is not None and g["dz"] is not None:
        z0, z1 = g["tops"], g["tops"] + nz * g["dz"]
        for c in contacts:
            if not z0 <= c["datum_depth"] <= z1:
                iss.warn("datum_depth", f"datum {c['datum_depth']:g} is outside the grid ({z0:g}-{z1:g})", f"EQLNUM {c['eqlnum']}")
            if c["goc_depth"] is not None and c["goc_depth"] > z1:
                iss.warn("contacts", "GOC is below the grid", f"EQLNUM {c['eqlnum']}")

    # --- aquifers: connections inside the grid, every aquifer connected, ids 1..n (AQUDIMS counts them)
    aqs = rows(con, "SELECT * FROM aquifers WHERE version_id=?", vid)
    conns = rows(con, "SELECT * FROM aquifer_connections WHERE version_id=?", vid)
    for a in conns:
        if a["i2"] > nx or a["j2"] > ny or a["k2"] > nz:
            iss.error("aquifer_range", "aquifer connection outside the grid", f"aquifer {a['aquifer_id']}")
    for a in aqs:
        where = f"aquifer {a['aquifer_id']}"
        if not any(c["aquifer_id"] == a["aquifer_id"] for c in conns):
            iss.error("aquifer_connections", "aquifer has no grid connections", where)
        if a["aq_type"] == "CARTER_TRACY" and (a["influence_table"] or 1) != 1:
            iss.error("aquifer_table", "only the built-in influence table 1 is rendered (no AQUTAB yet)", where)
        if g["grid_type"] == "cartesian" and g["tops"] is not None and g["dz"] is not None \
                and a["datum_depth"] < g["tops"]:
            iss.warn("aquifer_datum", f"aquifer datum {a['datum_depth']:g} is above the top of the grid", where)
    if aqs and sorted(a["aquifer_id"] for a in aqs) != list(range(1, len(aqs) + 1)):
        iss.error("aquifer_ids", "aquifer ids must run 1..n")
    return iss
