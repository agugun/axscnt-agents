"""Deck importer (reverse deck): split an existing OPM/Eclipse deck into fluid, geo and well drafts.

The deck is parsed by OPM (INCLUDEs, defaults, units resolved). Cell arrays are taken from OPM's
final field properties, so EQUALS/COPY/MULTIPLY/ADD/BOX are already applied. Keywords with no model
home are carried verbatim (passthrough) and re-emitted unchanged by the deck builder.
Every keyword is listed in a coverage report as mapped, applied, regenerated or passthrough.
"""
from __future__ import annotations

import fnmatch
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np

SECTIONS = ("RUNSPEC", "GRID", "EDIT", "PROPS", "REGIONS", "SOLUTION", "SUMMARY", "SCHEDULE")
MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
MONTHS["JLY"] = 7

# keyword -> how it is handled
MAPPED = {
    "RUNSPEC": {"OIL", "WATER", "GAS", "DISGAS", "VAPOIL", "METRIC", "FIELD", "START", "DIMENS", "TITLE"},
    "GRID": {"DX", "DY", "DZ", "TOPS", "COORD", "ZCORN", "SPECGRID", "ACTNUM", "PORO", "PERMX", "PERMY", "PERMZ",
             "NTG", "MULTX", "MULTY", "MULTZ", "MULTX-", "MULTY-", "MULTZ-", "MULTPV", "MINPV", "PINCH", "FAULTS", "MULTFLT"},
    "PROPS": {"PVTO", "PVDO", "PVDG", "PVTG", "PVTW", "DENSITY", "ROCK", "SWOF", "SGOF", "SWATINIT"},
    "REGIONS": {"SATNUM", "PVTNUM", "EQLNUM", "FIPNUM"},
    "SOLUTION": {"EQUIL", "RSVD", "PBVD"},
    "SCHEDULE": {"WELSPECS", "COMPDAT", "WCONPROD", "WCONINJE", "WCONHIST", "WCONINJH", "GRUPTREE", "GCONPROD",
                 "GCONINJE", "WECON", "WEFAC", "DATES", "TSTEP"},
}
APPLIED = {"EQUALS", "COPY", "MULTIPLY", "ADD", "BOX", "ENDBOX", "MINVALUE", "MAXVALUE"}
REGENERATED = {"RUNSPEC", "GRID", "EDIT", "PROPS", "REGIONS", "SOLUTION", "SUMMARY", "SCHEDULE", "WELLDIMS",
               "TABDIMS", "EQLDIMS", "UNIFOUT", "INIT", "END"}
# array keywords the importer rewrites from final field properties when they appear in passthrough form
GRID_PROPS = ["PORO", "PERMX", "PERMY", "PERMZ", "NTG", "MULTX", "MULTY", "MULTZ", "MULTX-", "MULTY-", "MULTZ-", "MULTPV"]
REGION_PROPS = ["SATNUM", "PVTNUM", "EQLNUM", "FIPNUM"]
UNSUPPORTED = {"TEMP", "THERMAL", "SOLVENT", "POLYMER", "FOAM", "BRINE", "COMPS", "CO2STORE", "H2STORE", "BIOFILM"}


class ImportError_(Exception):
    pass


# ---------------------------------------------------------------- text tokenizer for str(keyword)
_TOK = re.compile(r"'[^']*'|\S+")


def records(kw) -> list[list]:
    """Records of a keyword from its canonical text; 'N*' defaults become None."""
    lines = str(kw).splitlines()[1:]
    recs, cur = [], []
    for line in lines:
        line = line.split("--", 1)[0]
        for tok in _TOK.findall(line):
            if tok == "/":
                recs.append(cur)
                cur = []
                continue
            if tok.endswith("/") and not tok.startswith("'"):
                tok = tok[:-1]
                _add(cur, tok)
                recs.append(cur)
                cur = []
                continue
            _add(cur, tok)
    if cur:
        recs.append(cur)
    return [r for r in recs if r]


def _add(cur, tok):
    m = re.fullmatch(r"(\d+)\*(.*)", tok)
    if m:
        n, v = int(m.group(1)), m.group(2)
        cur.extend([None] * n if v == "" else [_val(v)] * n)
    elif tok == "*" or tok == "1*":
        cur.append(None)
    else:
        cur.append(_val(tok))


def _val(tok):
    if tok.startswith("'"):
        return tok.strip("'")
    try:
        return int(tok)
    except ValueError:
        try:
            return float(tok)
        except ValueError:
            return tok


def _get(rec, i, default=None):
    return rec[i] if i < len(rec) and rec[i] is not None else default


# ---------------------------------------------------------------- parsing
def parse(deck_path: str | Path):
    import opm.io as io
    from opm.io.ecl_state import EclipseState
    from opm.io.parser import ParseContext, Parser
    ctx = ParseContext([("PARSE_RANDOM_SLASH", io.action.ignore)])
    deck = Parser().parse(str(deck_path), ctx)
    return deck, EclipseState(deck)


def _ecl_date(rec) -> date:
    return date(int(rec[2]), MONTHS[str(rec[1]).upper()[:3]], int(rec[0]))


def import_deck(deck_path: str | Path, out_dir: str | Path) -> dict:
    """Parse a deck and return the model contents, the deck spec with passthrough, and the coverage report.
    Cell arrays are written as include files into out_dir."""
    deck_path, out_dir = Path(deck_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    deck, st = parse(deck_path)
    grid = st.grid()
    nx, ny, nz = grid.nx, grid.ny, grid.nz
    ncell = nx * ny * nz
    fp = st.field_props()

    section = "RUNSPEC"
    coverage = []           # {keyword, section, handling}
    passthrough = defaultdict(list)   # section -> [{keyword, raw_text}]
    kws = defaultdict(list)           # (section, name) -> [kw]
    schedule_kws = []                 # ordered (name, kw)
    for kw in deck:
        name = kw.name
        if name in SECTIONS:
            section = name
            coverage.append({"keyword": name, "section": name, "handling": "regenerated"})
            continue
        if name in UNSUPPORTED:
            raise ImportError_(f"{name}: unsupported feature (only black oil is supported)")
        if section == "SCHEDULE":
            schedule_kws.append((name, kw))
        if name in MAPPED.get(section, set()) or (section == "EDIT" and name in GRID_PROPS):
            handling = "mapped"
            kws[(section, name)].append(kw)
        elif name in APPLIED:
            handling = "applied"   # final arrays come from OPM
            kws[(section, name)].append(kw)
        elif name in REGENERATED:
            handling = "regenerated"
        else:
            handling = "passthrough"
            passthrough[section].append({"keyword": name, "raw_text": str(kw).rstrip() + "\n"})
        coverage.append({"keyword": name, "section": section, "handling": handling})

    def one(section, name):
        k = kws.get((section, name))
        return k[-1] if k else None

    def has(section, name):
        return bool(kws.get((section, name)))

    # ------------------------------------------------ runspec
    phases = [p for p in ("OIL", "WATER", "GAS") if has("RUNSPEC", p)]
    unit = "FIELD" if has("RUNSPEC", "FIELD") else "METRIC"
    start = _ecl_date(records(one("RUNSPEC", "START"))[0]) if has("RUNSPEC", "START") else date(2000, 1, 1)
    title = " ".join(str(x) for x in (records(one("RUNSPEC", "TITLE")) or [[deck_path.stem]])[0]) if has("RUNSPEC", "TITLE") else deck_path.stem

    # ------------------------------------------------ geo
    corner = one("GRID", "COORD") is not None
    act_kw = one("GRID", "ACTNUM")
    actnum = np.array(act_kw.get_int_array()) if act_kw is not None else np.ones(ncell, dtype=int)
    active_idx = np.flatnonzero(actnum)
    if len(active_idx) != grid.nactive:
        raise ImportError_(f"active cell count changed by MINPV/PINCH ({len(active_idx)} -> {grid.nactive}); "
                           "array mapping would be ambiguous")

    files = []

    def write_array(name, values_active, is_int=False, fill=0):
        full = np.full(ncell, fill, dtype=float)
        full[active_idx] = values_active
        p = out_dir / f"{name.replace('-', 'M')}.INC"
        fmt = (lambda v: str(int(v))) if is_int else (lambda v: repr(float(v)))   # full double precision: arrays must be bit-identical
        with open(p, "w") as f:
            f.write(f"{name}\n")
            row = []
            for v in full:
                row.append(fmt(v))
                if len(row) == 8:
                    f.write(" " + " ".join(row) + "\n")
                    row = []
            if row:
                f.write(" " + " ".join(row) + "\n")
            f.write("/\n")
        files.append(str(p))
        return str(p)

    props = []
    for name in GRID_PROPS + ["SWATINIT"]:
        try:
            arr = fp.get_double_array(name)
        except Exception:  # noqa: BLE001  (keyword absent)
            continue
        if not len(arr):
            continue
        if name.startswith("PERM"):
            arr = np.asarray(arr) / 9.869232667160130e-16   # field props are SI (m2); decks use mD in METRIC and FIELD
        if np.allclose(arr, arr[0]) and actnum.all():
            props.append({"keyword": name, "const_value": float(arr[0])})
        else:
            props.append({"keyword": name, "file": write_array(name, arr)})
    for name in REGION_PROPS:
        try:
            arr = fp.get_int_array(name)
        except Exception:  # noqa: BLE001
            continue
        if not len(arr):
            continue
        if (arr == arr[0]).all() and actnum.all():
            props.append({"keyword": name, "const_value": float(arr[0])})
        else:
            props.append({"keyword": name, "file": write_array(name, arr, is_int=True, fill=1)})
    # passthrough region-type arrays that edit operators may have touched (e.g. FLUXNUM, IMBNUM)
    touched_in: dict[str, str] = {}   # array name -> section of the edit operator that set it
    for (sec, n), ks in kws.items():
        if n in ("EQUALS", "COPY", "MULTIPLY", "ADD"):
            for k in ks:
                for r in records(k):
                    if not r:
                        continue
                    targets = [r[1]] if n == "COPY" and len(r) > 1 and isinstance(r[1], str) else [r[0]]
                    for tname in targets:
                        touched_in.setdefault(str(tname).upper(), sec)
    touched = set(touched_in)
    for sec in ("GRID", "REGIONS", "EDIT", "PROPS"):
        keep = []
        for p in passthrough[sec]:
            if p["keyword"] in touched:
                continue  # regenerated below from final arrays
            keep.append(p)
        passthrough[sec] = keep
    extra_mapped = set(GRID_PROPS + REGION_PROPS + ["SWATINIT", "ACTNUM"])
    for name in sorted(touched - extra_mapped):
        arr = None
        for getter, is_int in ((fp.get_int_array, True), (fp.get_double_array, False)):
            try:
                arr = getter(name)
                break
            except Exception:  # noqa: BLE001
                continue
        if arr is None or not len(arr):
            continue
        sec = touched_in.get(name, "GRID")
        if sec == "EDIT":
            sec = "GRID"
        path = write_array(name, arr, is_int=is_int, fill=1 if is_int else 0)
        passthrough[sec].append({"keyword": name, "raw_text": f"INCLUDE\n '{path}' /\n"})

    geo_header = {"unit_system": unit, "grid_type": "corner_point" if corner else "cartesian", "nx": nx, "ny": ny, "nz": nz}
    if corner:
        gp = out_dir / "GRID.GRDECL"
        with open(gp, "w") as f:
            for name in ("SPECGRID", "COORD", "ZCORN"):
                k = one("GRID", name)
                if k is not None:
                    f.write(str(k).rstrip() + "\n\n")
            if act_kw is not None:
                f.write(str(act_kw).rstrip() + "\n")
        files.append(str(gp))
        geo_header["grid_file"] = str(gp)
    else:
        if touched & {"DX", "DY", "DZ", "TOPS"}:
            raise ImportError_("cartesian geometry edited with EQUALS/BOX is not supported yet")
        for kw_name, col in (("DX", "dx"), ("DY", "dy"), ("DZ", "dz"), ("TOPS", "tops")):
            k = one("GRID", kw_name)
            vals = np.array(k.get_raw_array()) if k is not None else np.array([])
            if len(vals) and np.allclose(vals, vals[0]):
                geo_header[col] = float(vals[0])
            elif len(vals):
                p = out_dir / f"{kw_name}.INC"
                p.write_text(kw_name + "\n" + "\n".join(" " + " ".join(f"{v:.10g}" for v in vals[i:i + 8])
                                                       for i in range(0, len(vals), 8)) + "\n/\n")
                files.append(str(p))
                props.append({"keyword": kw_name, "file": str(p)})
        if not actnum.all():
            props.append({"keyword": "ACTNUM", "file": write_array("ACTNUM", np.ones(len(active_idx)), is_int=True)})
    if has("GRID", "MINPV"):
        geo_header["minpv"] = float(records(one("GRID", "MINPV"))[0][0])
    if has("GRID", "PINCH"):
        r = records(one("GRID", "PINCH"))[0]
        geo_header["pinch_threshold"] = float(_get(r, 0, 0.001))
        if len([x for x in r[1:] if x is not None]):
            passthrough["GRID"].append({"keyword": "PINCH", "raw_text": str(one("GRID", "PINCH")).rstrip() + "\n"})
            geo_header.pop("pinch_threshold")

    faults = [{"fault_name": r[0], "i1": r[1], "i2": r[2], "j1": r[3], "j2": r[4], "k1": r[5], "k2": r[6], "face": r[7]}
              for k in kws.get(("GRID", "FAULTS"), []) for r in records(k)]
    multflt = {r[0]: r[1] for k in kws.get(("GRID", "MULTFLT"), []) + kws.get(("EDIT", "MULTFLT"), []) for r in records(k)}

    def table_rows(name, ncol):
        """Flat table keyword (SWOF etc): one record per region."""
        k = one("PROPS", name)
        if k is None:
            return []
        out = []
        for reg, rec in enumerate(records(k), start=1):
            vals = [v for v in rec]
            for i in range(0, len(vals) - ncol + 1, ncol):
                out.append((reg, vals[i:i + ncol]))
        return out

    swof = [{"satnum": r, "sw": v[0], "krw": v[1], "krow": v[2], "pcow": v[3] or 0.0} for r, v in table_rows("SWOF", 4)]
    sgof = [{"satnum": r, "sg": v[0], "krg": v[1], "krog": v[2], "pcog": v[3] or 0.0} for r, v in table_rows("SGOF", 4)]
    rock = [{"region": i, "p_ref": rec[0], "cr": rec[1]} for i, rec in enumerate(records(one("PROPS", "ROCK")) or [], 1)] if has("PROPS", "ROCK") else []

    contacts, equil_extra = [], {}
    if has("SOLUTION", "EQUIL"):
        for e, r in enumerate(records(one("SOLUTION", "EQUIL")), start=1):
            contacts.append({"eqlnum": e, "datum_depth": r[0], "datum_pressure": r[1], "owc_depth": _get(r, 2),
                             "pcow_at_owc": _get(r, 3, 0.0), "goc_depth": _get(r, 4), "pcog_at_goc": _get(r, 5, 0.0)})
            if len(r) > 6:
                equil_extra[e] = r[6:]

    # ------------------------------------------------ fluid
    def pvt_live(name, cols):
        """PVTO/PVTG: one record per outer value: [outer, p/rv..., ...]; regions separated by empty records."""
        k = one("PROPS", name)
        if k is None:
            return []
        rows = []
        reg_records = []  # region boundaries: a line with only '/'

        cur = []
        for line in str(k).splitlines()[1:]:
            if line.strip() == "/":
                reg_records.append(cur)
                cur = []
            else:
                cur.append(line)
        if cur:
            reg_records.append(cur)
        for reg, lines in enumerate([r for r in reg_records if any(x.strip() for x in r)], start=1):
            class _K:  # minimal object with __str__ for records()
                def __str__(self_inner):
                    return name + "\n" + "\n".join(lines)
            for rec in records(_K()):
                outer, rest = rec[0], rec[1:]
                for i in range(0, len(rest), 3):
                    rows.append({"pvtnum": reg, cols[0]: outer, cols[1]: rest[i], cols[2]: rest[i + 1], cols[3]: rest[i + 2],
                                 "is_saturated": 1 if i == 0 else 0})
        return rows

    pvto = pvt_live("PVTO", ("rs", "pressure", "bo", "mu_o"))
    pvtg_raw = pvt_live("PVTG", ("pressure", "rv", "bg", "mu_g"))
    pvdo = [{"pvtnum": r, "pressure": v[0], "bo": v[1], "mu_o": v[2]} for r, v in table_rows("PVDO", 3)]
    pvdg = [{"pvtnum": r, "pressure": v[0], "bg": v[1], "mu_g": v[2]} for r, v in table_rows("PVDG", 3)]
    pvtw = [{"pvtnum": i, "p_ref": r[0], "bw": r[1], "cw": r[2], "mu_w": r[3], "viscosibility": _get(r, 4, 0.0)}
            for i, r in enumerate(records(one("PROPS", "PVTW")) or [], 1)] if has("PROPS", "PVTW") else []
    density = [{"pvtnum": i, "oil": r[0], "water": r[1], "gas": r[2]}
               for i, r in enumerate(records(one("PROPS", "DENSITY")) or [], 1)] if has("PROPS", "DENSITY") else []

    def depth_table(name, col):
        k = one("SOLUTION", name)
        if k is None:
            return []
        out = []
        for e, rec in enumerate(records(k), start=1):
            for i in range(0, len(rec) - 1, 2):
                out.append({"eqlnum": e, "depth": rec[i], col: rec[i + 1]})
        return out

    rsvd, pbvd = depth_table("RSVD", "rs"), depth_table("PBVD", "pb")
    fluid_header = {"unit_system": unit, "phases": ",".join(phases), "has_disgas": int(has("RUNSPEC", "DISGAS")),
                    "has_vapoil": int(has("RUNSPEC", "VAPOIL")),
                    "model_type": "black_oil" if has("RUNSPEC", "DISGAS") else ("dead_oil" if "OIL" in phases else "dry_gas")}
    if fluid_header["has_vapoil"]:
        fluid_header["model_type"] = "wet_gas" if not has("RUNSPEC", "DISGAS") else "black_oil"

    # ------------------------------------------------ wells and schedule
    wells, comps, events, groups, gctrl, econ = {}, [], [], {}, [], []
    cur = start
    verbatim_reasons = []
    schedule_extra = []
    report_dates = []
    for name, kw in schedule_kws:
        recs = records(kw)
        if name == "DATES":
            for r in recs:
                cur = _ecl_date(r)
                report_dates.append(cur)
        elif name == "TSTEP":
            for r in recs:
                for dt in r:
                    cur = cur + timedelta(days=float(dt))
                    report_dates.append(cur)
        elif name == "WELSPECS":
            for r in recs:
                if any(x is not None for x in r[6:]) and r[6:] != [0.0, "STD", "SHUT", "YES", 0][:len(r[6:])]:
                    verbatim_reasons.append(f"WELSPECS {r[0]} uses items beyond 6")
                wells[r[0]] = {"well_name": r[0], "group_name": _get(r, 1, "FIELD"), "head_i": r[2], "head_j": r[3],
                               "ref_depth": _get(r, 4), "preferred_phase": r[5]}
        elif name == "COMPDAT":
            for r in recs:
                w = wells.get(r[0], {})
                i, j = _get(r, 1) or w.get("head_i"), _get(r, 2) or w.get("head_j")
                if any(_get(r, n) is not None for n in (6, 12, 13)):
                    verbatim_reasons.append(f"COMPDAT {r[0]} uses SATNUM/direction/Dfactor items")
                comps.append({"well_name": r[0], "event_date": cur.isoformat(), "i": i, "j": j, "k1": r[3], "k2": r[4],
                              "state": "OPEN" if _get(r, 5, "OPEN") in ("OPEN", "AUTO") else "SHUT",
                              "conn_factor": _get(r, 7), "diameter": _get(r, 8), "kh": _get(r, 9) if (_get(r, 9) or 0) > 0 else None,
                              "skin": _get(r, 10, 0.0)})
        elif name in ("WCONPROD", "WCONINJE", "WCONHIST", "WCONINJH"):
            expanded = []
            for r in recs:
                if any(ch in str(r[0]) for ch in "*?"):
                    expanded += [[w] + r[1:] for w in wells if fnmatch.fnmatchcase(w, r[0])]
                else:
                    expanded.append(r)
            for r in expanded:
                if any(isinstance(x, str) and x not in ("OPEN", "SHUT", "STOP", "AUTO", "WATER", "GAS", "OIL", "ORAT", "WRAT",
                                                        "GRAT", "LRAT", "RESV", "BHP", "THP", "RATE", "GRUP") for x in r[1:]):
                    verbatim_reasons.append(f"{name} {r[0]} uses UDQ/UDA values")
                status_item = 2 if name in ("WCONINJE", "WCONINJH") else 1   # injectors: item 2 is the phase
                ev = {"well_name": r[0], "event_date": cur.isoformat(), "status": _get(r, status_item, "OPEN")}
                if name == "WCONPROD":
                    ev.update(mode="PREDICTION", control=_get(r, 2), oil_rate=_get(r, 3), water_rate=_get(r, 4), gas_rate=_get(r, 5),
                              liquid_rate=_get(r, 6), bhp=_get(r, 8), thp=_get(r, 9), vfp_table=_get(r, 10))
                elif name == "WCONINJE":
                    ev.update(mode="PREDICTION", control=_get(r, 3), inj_rate=_get(r, 4), bhp=_get(r, 6), thp=_get(r, 7),
                              vfp_table=_get(r, 8))
                elif name == "WCONHIST":
                    ev.update(mode="HISTORY", control=_get(r, 2), oil_rate=_get(r, 3), water_rate=_get(r, 4), gas_rate=_get(r, 5),
                              thp=_get(r, 8), bhp=_get(r, 9))
                else:
                    ev.update(mode="HISTORY", inj_rate=_get(r, 3), bhp=_get(r, 4), thp=_get(r, 5))
                if ev["status"] == "AUTO":
                    ev["status"] = "OPEN"
                events.append(ev)
        elif name == "GRUPTREE":
            for r in recs:
                groups[r[0]] = {"group_name": r[0], "parent_group": _get(r, 1, "FIELD")}
        elif name in ("GCONPROD", "GCONINJE", "WECON", "WEFAC"):
            verbatim_reasons.append(f"{name} is not mapped yet")
        elif name in MAPPED["SCHEDULE"]:
            pass
        elif name.startswith("W") or name.startswith("G") or name.startswith("C") or name.startswith("VFP") \
                or name.startswith("ACTION") or name.startswith("UD"):
            verbatim_reasons.append(f"{name} acts on wells or groups")
        else:
            schedule_extra.append({"date": cur.isoformat(), "keyword": name, "raw_text": str(kw).rstrip() + "\n"})
    for w in wells.values():
        evs = [e for e in events if e["well_name"] == w["well_name"]]
        produces = any(e.get("oil_rate") is not None or e.get("water_rate") is not None or e.get("gas_rate") is not None
                       or (e.get("control") in ("ORAT", "WRAT", "GRAT", "LRAT", "RESV") and e.get("inj_rate") is None)
                       for e in evs)
        injects = any(e.get("inj_rate") is not None or e.get("control") == "RATE" for e in evs)
        w["well_type"] = "INJECTOR" if injects and not produces and w["preferred_phase"] in ("WATER", "GAS") else "PRODUCER"
        if w["preferred_phase"] not in ("OIL", "WATER", "GAS", "LIQ"):
            w["preferred_phase"] = "OIL"
        if w["group_name"] == "FIELD" or w["group_name"] in groups:
            pass
        else:
            groups.setdefault(w["group_name"], {"group_name": w["group_name"], "parent_group": "FIELD"})
    unknown = sorted({x["well_name"] for x in events + comps} - set(wells))
    if unknown:
        raise ImportError_(f"schedule refers to wells not defined in WELSPECS: {unknown[:5]}")
    # merge events on the same date and well (later keyword wins), keeping one row per (well, date, mode)
    merged = {}
    for e in events:
        merged[(e["well_name"], e["event_date"], e["mode"])] = e
    events = list(merged.values())
    schedule_mode = "verbatim" if verbatim_reasons else "model"
    if schedule_mode == "verbatim":
        passthrough["SCHEDULE"] = [{"keyword": n, "raw_text": str(k).rstrip() + "\n"} for n, k in schedule_kws]
    else:
        passthrough["SCHEDULE"] = []
    summary_pt = passthrough.pop("SUMMARY", [])

    spec = {"name": None, "title": title, "start_date": start.isoformat(),
            "end_date": (report_dates[-1] if report_dates else start + timedelta(days=1)).isoformat(),
            "report_dates": [d.isoformat() for d in report_dates], "schedule_mode": schedule_mode,
            "schedule_extra": schedule_extra if schedule_mode == "model" else [],
            "summary_passthrough": summary_pt, "passthrough": {k: v for k, v in passthrough.items() if v},
            "equil_extra": equil_extra, "source_deck": str(deck_path.resolve())}
    counts = defaultdict(int)
    for c in coverage:
        counts[c["handling"]] += 1
    return {
        "fluid": {"header": fluid_header, "tables": {"pvto": pvto, "pvdo": pvdo, "pvdg": pvdg, "pvtg": pvtg_raw,
                                                     "pvtw": pvtw, "density": density, "rsvd": rsvd, "pbvd": pbvd}},
        "geo": {"header": geo_header, "props": props, "tables": {"faults": faults,
                "fault_multipliers": [{"fault_name": k, "mult": v} for k, v in multflt.items()], "rock": rock,
                "swof": swof, "sgof": sgof, "contacts": contacts}},
        "well": {"header": {"unit_system": unit, "start_date": start.isoformat()},
                 "tables": {"well_groups": [g for g in groups.values() if g["group_name"] != "FIELD"],
                            "wells": list(wells.values()), "well_completions": comps, "well_events": events}},
        "spec": spec, "files": files,
        "coverage": {"counts": dict(counts), "keywords": coverage, "schedule_mode": schedule_mode,
                     "schedule_reasons": sorted(set(verbatim_reasons))[:30]},
        "grid": {"nx": nx, "ny": ny, "nz": nz, "nactive": int(grid.nactive)},
    }


def save_spec(spec: dict, path: Path) -> Path:
    path.write_text(json.dumps(spec, indent=1, default=str))
    return path
