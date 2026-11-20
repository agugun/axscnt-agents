"""Geo model rules."""
from __future__ import annotations

from collections import defaultdict

from ._util import Issues, contiguous_regions, non_decreasing, non_increasing, read_array, rows, strictly_increasing

FRACTION = {"PORO", "NTG", "SWATINIT"}
NONNEG = {"PERMX", "PERMY", "PERMZ", "MULTX", "MULTY", "MULTZ", "MULTX-", "MULTY-", "MULTZ-", "MULTPV", "DX", "DY", "DZ"}
REGION = {"SATNUM", "PVTNUM", "EQLNUM", "FIPNUM", "ROCKNUM"}


def _prop_values(con, p) -> list[float] | None:
    if p["const_value"] is not None:
        return [p["const_value"]]
    f = con.execute("SELECT path FROM files WHERE id=?", (p["file_id"],)).fetchone()
    return read_array(f["path"]) if f else None


def validate(con, vid: int) -> list[dict]:
    iss = Issues()
    hdr = rows(con, "SELECT * FROM geo_model_versions WHERE id = ?", vid)
    if not hdr:
        iss.error("exists", f"geo version {vid} not found")
        return iss
    h = hdr[0]
    nx, ny, nz = h["nx"], h["ny"], h["nz"]
    ncell = nx * ny * nz
    props = {p["keyword"]: p for p in rows(con, "SELECT * FROM geo_properties WHERE version_id=?", vid)}

    # --- required properties and geometry
    for kw in ("PORO", "PERMX", "PERMY", "PERMZ"):
        if kw not in props:
            iss.error("required", f"{kw} is missing")
    if h["grid_type"] == "cartesian":
        for kw, scalar in (("DX", "dx"), ("DY", "dy"), ("DZ", "dz"), ("TOPS", "tops")):
            if h[scalar] is None and kw not in props:
                iss.error("required", f"cartesian grid needs {scalar} or a {kw} array")

    # --- array lengths and ranges
    values: dict[str, list[float]] = {}
    for kw, p in props.items():
        try:
            v = _prop_values(con, p)
        except Exception as e:  # noqa: BLE001
            iss.error("array_read", f"cannot read {kw}: {e}")
            continue
        if v is None:
            iss.error("array_read", f"{kw} file not registered")
            continue
        expected = nx * ny if kw == "TOPS" else ncell
        if p["file_id"] is not None and len(v) not in (expected, ncell):
            iss.error("array_length", f"{kw} has {len(v)} values, expected {expected}")
            continue
        values[kw] = v
        if kw in FRACTION and any(x < 0 or x > 1 for x in v):
            iss.error("range", f"{kw} must be within 0-1")
        if kw in NONNEG and any(x < 0 for x in v):
            iss.error("range", f"{kw} must not be negative")
        if kw.startswith("PERM") and any(x == 0 for x in v):
            iss.warn("range", f"{kw} has zero values")
        if kw == "ACTNUM" and any(x not in (0, 1) for x in v):
            iss.error("range", "ACTNUM must be 0 or 1")
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
        if tab and regs != set(range(1, nsat + 1)):
            iss.error("regions", f"{name} covers SATNUM {sorted(regs)}, grid uses 1..{nsat}")
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
    if h["grid_type"] == "cartesian" and h["tops"] is not None and h["dz"] is not None:
        z0, z1 = h["tops"], h["tops"] + nz * h["dz"]
        for c in contacts:
            if not z0 <= c["datum_depth"] <= z1:
                iss.warn("datum_depth", f"datum {c['datum_depth']:g} is outside the grid ({z0:g}-{z1:g})", f"EQLNUM {c['eqlnum']}")
            if c["goc_depth"] is not None and c["goc_depth"] > z1:
                iss.warn("contacts", "GOC is below the grid", f"EQLNUM {c['eqlnum']}")

    # --- faults and aquifer connections inside the grid
    for f in rows(con, "SELECT * FROM faults WHERE version_id=?", vid):
        if f["i2"] > nx or f["j2"] > ny or f["k2"] > nz:
            iss.error("fault_range", "fault segment outside the grid", f"fault {f['fault_name']}")
    names = {r["fault_name"] for r in rows(con, "SELECT fault_name FROM faults WHERE version_id=?", vid)}
    for m in rows(con, "SELECT * FROM fault_multipliers WHERE version_id=?", vid):
        if m["fault_name"] not in names:
            iss.error("fault_name", f"MULTFLT for unknown fault {m['fault_name']}")
    for a in rows(con, "SELECT * FROM aquifer_connections WHERE version_id=?", vid):
        if a["i2"] > nx or a["j2"] > ny or a["k2"] > nz:
            iss.error("aquifer_range", "aquifer connection outside the grid", f"aquifer {a['aquifer_id']}")
    return iss
