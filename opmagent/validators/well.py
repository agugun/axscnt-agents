"""Well model rules."""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from ._util import Issues, rows

REQUIRED_VALUE = {"ORAT": "oil_rate", "WRAT": "water_rate", "GRAT": "gas_rate", "LRAT": "liquid_rate",
                  "BHP": "bhp", "THP": "thp", "RATE": "inj_rate"}
PRODUCER_CONTROLS = {"ORAT", "WRAT", "GRAT", "LRAT", "RESV", "BHP", "THP"}
INJECTOR_CONTROLS = {"RATE", "RESV", "BHP", "THP"}


def _date(s: str):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def validate(con, vid: int) -> list[dict]:
    iss = Issues()
    hdr = rows(con, "SELECT * FROM well_model_versions WHERE id = ?", vid)
    if not hdr:
        iss.error("exists", f"well version {vid} not found")
        return iss
    h = hdr[0]
    wells = {w["well_name"]: w for w in rows(con, "SELECT * FROM wells WHERE version_id=?", vid)}
    if not wells:
        iss.error("required", "no wells defined")

    # --- group tree rooted at FIELD, no loops
    groups = {g["group_name"]: g["parent_group"] for g in rows(con, "SELECT * FROM well_groups WHERE version_id=?", vid)}
    for g, parent in groups.items():
        if parent != "FIELD" and parent not in groups:
            iss.error("group_tree", f"group {g} has unknown parent {parent}")
        seen, cur = set(), g
        while cur != "FIELD" and cur in groups:
            if cur in seen:
                iss.error("group_tree", f"group tree has a loop through {g}")
                break
            seen.add(cur)
            cur = groups[cur]
    for name, w in wells.items():
        if w["group_name"] != "FIELD" and w["group_name"] not in groups:
            iss.error("group_tree", f"well {name} is in unknown group {w['group_name']}")

    # --- trajectories and completions
    traj = defaultdict(list)
    for t in rows(con, "SELECT * FROM well_trajectory WHERE version_id=? ORDER BY well_name, md", vid):
        traj[t["well_name"]].append(t)
    for name, pts in traj.items():
        for p in pts:
            if p["tvd"] > p["md"] + 1e-6:
                iss.error("trajectory", f"TVD {p['tvd']:g} exceeds MD {p['md']:g}", f"well {name}")
                break
    geo = None
    if h["geo_version_id"]:
        geo = rows(con, "SELECT nx, ny, nz FROM geo_model_versions WHERE id=?", h["geo_version_id"])
        geo = geo[0] if geo else None
    start = _date(h["start_date"]) if h["start_date"] else None
    for c in rows(con, "SELECT * FROM well_completions WHERE version_id=?", vid):
        where = f"well {c['well_name']} completion {c['id']}"
        if _date(c["event_date"]) is None:
            iss.error("date", f"bad date '{c['event_date']}'", where)
        if c["top_md"] is not None and traj.get(c["well_name"]):
            mds = [p["md"] for p in traj[c["well_name"]]]
            if c["top_md"] < mds[0] or c["bottom_md"] > mds[-1]:
                iss.error("perf_md", "perforation outside the trajectory's MD range", where)
        if c["top_md"] is not None and not traj.get(c["well_name"]) and c["i"] is None:
            iss.error("perf_md", "MD completion but the well has no trajectory", where)
        if c["i"] is not None and geo:
            if not (1 <= c["i"] <= geo["nx"] and 1 <= c["j"] <= geo["ny"] and 1 <= c["k1"] and c["k2"] <= geo["nz"]):
                iss.error("perf_ijk", "completion cell outside the grid", where)

    # --- events / controls
    vfp = {(v["table_no"], v["kind"]) for v in rows(con, "SELECT * FROM vfp_tables WHERE version_id=?", vid)}
    events = rows(con, "SELECT * FROM well_events WHERE version_id=? ORDER BY well_name, event_date", vid)
    for e in events:
        where = f"well {e['well_name']} {e['event_date']} {e['mode']}"
        d = _date(e["event_date"])
        if d is None:
            iss.error("date", f"bad date '{e['event_date']}'", where)
        elif start and d < start:
            iss.error("date", f"event before start date {start}", where)
        w = wells.get(e["well_name"])
        if not w or e["status"] != "OPEN" or not e["control"]:
            continue
        inj = w["well_type"] == "INJECTOR"
        allowed = INJECTOR_CONTROLS if inj else PRODUCER_CONTROLS
        if e["control"] not in allowed:
            iss.error("control", f"{e['control']} is not a valid control for {'an injector' if inj else 'a producer'}", where)
            continue
        col = REQUIRED_VALUE.get(e["control"])
        if col and e[col] is None and e["mode"] == "PREDICTION":
            iss.error("control_value", f"{e['control']} control needs {col}", where)
        if e["control"] == "THP" and (e["vfp_table"], "INJ" if inj else "PROD") not in vfp:
            iss.error("vfp", f"THP control needs VFP table {e['vfp_table']}", where)
        if e["mode"] == "PREDICTION" and not inj and e["bhp"] is None:
            iss.error("bhp_limit", "producer needs a BHP limit in prediction", where)
        if e["mode"] == "PREDICTION" and inj and e["inj_rate"] is None and e["control"] == "RATE":
            iss.error("control_value", "injector needs an injection rate", where)
        if e["mode"] == "HISTORY" and not inj and all(e[c] is None for c in ("oil_rate", "water_rate", "gas_rate")):
            iss.error("history_rates", "history row needs observed rates", where)
    for name, w in wells.items():
        if not any(c for c in rows(con, "SELECT 1 FROM well_completions WHERE version_id=? AND well_name=?", vid, name)):
            iss.error("completions", f"well {name} has no completions")
        if not any(e["well_name"] == name for e in events):
            iss.warn("events", f"well {name} has no control events")

    for g in rows(con, "SELECT * FROM group_controls WHERE version_id=?", vid):
        if g["group_name"] != "FIELD" and g["group_name"] not in groups:
            iss.error("group_controls", f"controls for unknown group {g['group_name']}")
    return iss
