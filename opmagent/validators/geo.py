"""Geo model rules: grid, cell properties, PVTNUM, faults (the static model)."""
from __future__ import annotations


from ._util import Issues, contiguous_regions, read_array, rows

FRACTION = {"PORO", "NTG"}
NONNEG = {"PERMX", "PERMY", "PERMZ", "MULTX", "MULTY", "MULTZ", "MULTX-", "MULTY-", "MULTZ-", "MULTPV", "DX", "DY", "DZ"}
REGION = {"PVTNUM"}


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

    # --- faults inside the grid
    for f in rows(con, "SELECT * FROM faults WHERE version_id=?", vid):
        if f["i2"] > nx or f["j2"] > ny or f["k2"] > nz:
            iss.error("fault_range", "fault segment outside the grid", f"fault {f['fault_name']}")
    names = {r["fault_name"] for r in rows(con, "SELECT fault_name FROM faults WHERE version_id=?", vid)}
    for m in rows(con, "SELECT * FROM fault_multipliers WHERE version_id=?", vid):
        if m["fault_name"] not in names:
            iss.error("fault_name", f"MULTFLT for unknown fault {m['fault_name']}")
    return iss
