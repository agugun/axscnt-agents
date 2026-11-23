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
_PROD_RATES = ("oil_rate", "water_rate", "gas_rate", "liquid_rate")

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
RES_PROPS = {"SATNUM", "EQLNUM", "FIPNUM", "ROCKNUM", "SWATINIT"}   # the res model's arrays; the rest are geo's
UNSUPPORTED = {"TEMP", "THERMAL", "SOLVENT", "POLYMER", "FOAM", "BRINE", "COMPS", "CO2STORE", "H2STORE", "BIOFILM"}
# end-point scaling arrays: a cell the deck never assigns takes its value from the saturation table, so it must stay
# unset in the rebuilt deck (written as N*); OPM's field props fill such cells with placeholders
_EPS_BASE = ["SWL", "SWLPC", "SWCR", "SWU", "SGL", "SGLPC", "SGCR", "SGU", "SOWCR", "SOGCR",
             "KRW", "KRWR", "KRO", "KRORW", "KRORG", "KRG", "KRGR", "PCW", "PCG"]
ENDPOINT_KWS = {p + b + d for b in _EPS_BASE for p in ("", "I") for d in ("", "X", "X-", "Y", "Y-", "Z", "Z-")}


# analytical aquifers the res model holds (rendered back by the deck builder); any other aquifer form keeps every
# aquifer keyword of the deck as passthrough, unchanged
AQ_MAPPED = {"AQUFETP", "AQUCT", "AQUANCON"}
AQ_OTHER = {"AQUFET", "AQUNUM", "AQUCON", "AQUTAB", "AQUFLUX", "AQUCHWAT", "AQUCHGAS", "AQUFETP_", "AQUCT_"}
_FACE = {"X": "I", "Y": "J", "Z": "K"}


def aquifer_mappable(deck) -> tuple[bool, str]:
    """Whether the deck's aquifers fit the res model: AQUFETP / AQUCT with AQUANCON, built-in influence table, no
    salinity or temperature items, no internal connections. (True, '') or (False, why)."""
    names = {kw.name for kw in deck}
    if not names & {"AQUFETP", "AQUCT"}:
        return False, ""
    other = sorted(names & AQ_OTHER)
    if other:
        return False, f"aquifer keywords {other} have no res model home"
    if "AQUANCON" not in names:
        return False, "AQUFETP/AQUCT without AQUANCON"
    for kw in deck:
        if kw.name == "AQUFETP" and any(any(x is not None for x in r[7:]) for r in records(kw)):
            return False, "AQUFETP salinity or temperature items"
        if kw.name == "AQUCT":
            for r in records(kw):
                if (_get(r, 10) or 1) != 1:
                    return False, f"AQUCT aquifer {r[0]} uses influence table {r[10]} (only the built-in table 1)"
                if any(x is not None for x in r[11:]):
                    return False, "AQUCT salinity or temperature items"
        if kw.name == "AQUANCON":
            for r in records(kw):
                if str(_get(r, 10, "NO")).upper().startswith("Y"):
                    return False, "AQUANCON connections to internal cell faces"
    return True, ""


def aquifer_tables(deck) -> tuple[list[dict], list[dict]]:
    """aquifers and aquifer_connections rows from AQUFETP, AQUCT and AQUANCON, in the deck's own units."""
    aqs, conns = [], []
    for kw in deck:
        if kw.name == "AQUFETP":
            for r in records(kw):
                aqs.append({"aquifer_id": int(r[0]), "aq_type": "FETKOVICH", "datum_depth": r[1],
                            "init_pressure": _get(r, 2), "water_volume": r[3], "total_compressibility": r[4],
                            "productivity_index": r[5], "pvtw_table": int(_get(r, 6, 1))})
        elif kw.name == "AQUCT":
            for r in records(kw):
                aqs.append({"aquifer_id": int(r[0]), "aq_type": "CARTER_TRACY", "datum_depth": r[1],
                            "init_pressure": _get(r, 2), "permeability": r[3], "porosity": r[4],
                            "total_compressibility": r[5], "inner_radius": r[6], "thickness": r[7],
                            "angle_deg": _get(r, 8, 360.0), "pvtw_table": int(_get(r, 9, 1)),
                            "influence_table": int(_get(r, 10, 1))})
        elif kw.name == "AQUANCON":
            for r in records(kw):
                face = str(r[7]).upper()
                face = _FACE.get(face[0], face[0]) + face[1:]
                conns.append({"aquifer_id": int(r[0]), "i1": int(r[1]), "i2": int(r[2]), "j1": int(r[3]),
                              "j2": int(r[4]), "k1": int(r[5]), "k2": int(r[6]), "face": face,
                              "influx_coeff": _get(r, 8), "influx_mult": float(_get(r, 9, 1.0))})
    return sorted(aqs, key=lambda a: a["aquifer_id"]), conns


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


MD_TO_SI = 9.869232667160130e-16   # OPM: permeability in SI = deck mD * this, in double arithmetic


def _opm_read(tokens: list[str]) -> np.ndarray:
    """The doubles OPM's deck parser makes of these number tokens (it is not correctly rounded for every input)."""
    from opm.io.parser import Parser
    d = Parser().parse_string(f"RUNSPEC\nDIMENS\n {len(tokens)} 1 1 /\nGRID\nPORO\n " + " ".join(tokens) + " /\n")
    return np.asarray(next(k for k in d if k.name == "PORO").get_raw_array())


def exact_tokens(target: np.ndarray, factor: float = 1.0) -> tuple[list[str], int, np.ndarray]:
    """Deck text for each value such that OPM reads it back as exactly target (after * factor for unit conversion).
    OPM's parser cannot produce about half of all doubles (values only arithmetic yields, e.g. 1 - 0.07), so
    returns the tokens, how many values are still inexact, and what OPM will read (in internal units)."""
    target = np.asarray(target, dtype=float)
    uniq, inv = np.unique(target, return_inverse=True)
    deck = uniq / factor if factor != 1.0 else uniq
    tok = np.array([repr(float(v)) for v in deck], dtype=object)
    todo = np.flatnonzero(_opm_read(list(tok)) * factor != uniq) if factor != 1.0 else \
        np.flatnonzero(_opm_read(list(tok)) != uniq)

    def candidates(v):
        out = [f"{v:.{p}g}" for p in (15, 16, 17)] + [f"{v:.20e}", f"{v:.25e}", f"{v:.30g}"]
        for nb in (np.nextafter(v, np.inf), np.nextafter(v, -np.inf)):
            out.append(repr(float(nb)))
        m, e = f"{v:.16e}".split("e")
        digits = m.replace(".", "").lstrip("-")
        sign = "-" if v < 0 else ""
        for step in (1, -1, 2, -2, 3, -3):
            d = str(int(digits) + step)
            if len(d) == len(digits):
                out.append(f"{sign}{d[0]}.{d[1:]}e{e}")
        return out

    if len(todo):
        cand = [(i, c) for i in todo for c in candidates(float(deck[i]))]
        got = _opm_read([c for _, c in cand]) * factor
        for (i, c), g in zip(cand, got):
            if i in todo and g == uniq[i] and tok[i] != c:
                tok[i] = c
    read = _opm_read(list(tok))
    read = read * factor if factor != 1.0 else read
    left = np.flatnonzero(read != uniq)
    return list(tok[inv]), int(np.isin(inv, left).sum()), read[inv]


def _replay_ops(deck, written: dict, targets: set, nx, ny, nz, active: np.ndarray) -> tuple[list, set]:
    """Some values only arise from arithmetic (SGU = 1 - SWL, PERMZ = 0.13 * PERMX) and OPM cannot read them from
    text. For those arrays, replay the deck's own EQUALS/COPY/MULTIPLY/ADD on the exactly written arrays, so OPM
    computes the same doubles. A replay is kept only if simulating it in double arithmetic reproduces every active
    cell exactly. Returns [(array, keyword text)] in deck order and the set of arrays made exact."""
    full = (1, nx, 1, ny, 1, nz)
    box, ops = full, []
    for kw in deck:
        n = kw.name
        if n in SECTIONS or n == "ENDBOX":
            box = full
        elif n == "BOX":
            box = tuple(int(v) for v in records(kw)[0][:6])
        elif n in ("EQUALS", "COPY", "MULTIPLY", "ADD"):
            for r in records(kw):
                if not r:
                    continue
                tgt, src, val = (str(r[1]).upper(), str(r[0]).upper(), None) if n == "COPY" else (str(r[0]).upper(), None, r[1])
                if tgt in targets:
                    b = tuple(int(v) if v is not None else box[i] for i, v in enumerate((list(r[2:8]) + [None] * 6)[:6]))
                    ops.append((tgt, n, src, val, b))
        elif n in targets:
            ops.append((n, "DIRECT", None, None, full))   # array text in the deck: its base values are not known here
    consts = sorted({repr(float(v)) for *_, v, _ in ops if v is not None})
    opm = dict(zip(consts, _opm_read(consts))) if consts else {}
    shape = (nz, ny, nx)
    act = active.reshape(shape)
    accepted = set(targets)
    while accepted:
        st = {k: w[0].reshape(shape).copy() for k, w in written.items()}
        bad = set()
        for tgt, n, src, val, b in ops:
            if tgt not in accepted or tgt in bad:
                continue
            sl = (slice(b[4] - 1, b[5]), slice(b[2] - 1, b[3]), slice(b[0] - 1, b[1]))
            f = written[tgt][2]
            if n == "COPY" and src in st:
                st[tgt][sl] = st[src][sl]
            elif n == "EQUALS":
                st[tgt][sl] = opm[repr(float(val))] * f
            elif n == "MULTIPLY":
                st[tgt][sl] = st[tgt][sl] * opm[repr(float(val))]
            elif n == "ADD" and f == 1.0:
                st[tgt][sl] = st[tgt][sl] + opm[repr(float(val))]
            else:
                bad.add(tgt)
        for tgt in accepted - bad:
            want = written[tgt][1].reshape(shape)
            mask = act if written[tgt][3] is None else act & written[tgt][3].reshape(shape)
            if not np.array_equal(st[tgt][mask], want[mask]):
                bad.add(tgt)
        if not bad:
            break
        accepted -= bad
    out = []
    for tgt, n, src, val, b in ops:
        if tgt not in accepted:
            continue
        cells = " ".join(str(x) for x in b)
        rec = f"'{src}' '{tgt}' {cells}" if n == "COPY" else f"'{tgt}' {repr(float(val))} {cells}"
        out.append((tgt, f"{n}\n {rec} /\n/\n"))
    return out, accepted


def _assigned_cells(deck, names, nx, ny, nz) -> dict[str, np.ndarray]:
    """Global cells (i fastest) each array gets a value in: direct keyword, EQUALS or COPY, honouring BOX/ENDBOX.
    ADD/MULTIPLY only change cells that already have a value, so they assign nothing."""
    masks = {n: np.zeros((nz, ny, nx), dtype=bool) for n in names}
    full = (1, nx, 1, ny, 1, nz)
    box = full

    def mark(name, rec_box):
        b = [int(v) if v is not None else box[i] for i, v in enumerate((list(rec_box) + [None] * 6)[:6])]
        masks[name][b[4] - 1:b[5], b[2] - 1:b[3], b[0] - 1:b[1]] = True

    for kw in deck:
        n = kw.name
        if n in SECTIONS or n == "ENDBOX":
            box = full
        elif n == "BOX":
            box = tuple(int(v) for v in records(kw)[0][:6])
        elif n in masks:
            mark(n, ())
        elif n == "EQUALS":
            for r in records(kw):
                if r and str(r[0]).upper() in masks:
                    mark(str(r[0]).upper(), r[2:8])
        elif n == "COPY":
            for r in records(kw):
                if len(r) > 1 and str(r[1]).upper() in masks:
                    mark(str(r[1]).upper(), r[2:8])
    return {n: m.ravel() for n, m in masks.items()}


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
    map_aq, aq_why = aquifer_mappable(deck)
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
        if map_aq and name in AQ_MAPPED:
            handling = "mapped"
        elif map_aq and name == "AQUDIMS":
            handling = "regenerated"
        elif name in MAPPED.get(section, set()) or (section == "EDIT" and name in GRID_PROPS):
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

    inexact = {}   # array -> cells OPM cannot read back bit-exactly (reported in coverage)
    written = {}   # array -> (what OPM reads from the written file, global; target values; factor)

    def write_array(name, values_active, is_int=False, fill=0, assigned=None, factor=1.0):
        """values_active: OPM's final values (internal units = deck units * factor).
        assigned: global bool mask; cells outside it are written as N* (left unset, as in the source deck)."""
        full = np.full(ncell, fill * factor, dtype=float)
        full[active_idx] = values_active
        p = out_dir / f"{name.replace('-', 'M')}.INC"
        if is_int:
            vals = [str(int(v)) for v in full]
        else:   # arrays must be bit-identical after OPM parses them again
            vals, bad, read = exact_tokens(full, factor)
            written[name] = (read, full, factor, assigned)
            if bad:
                inexact[name] = bad
        toks = vals
        if assigned is not None and not assigned.all():
            toks, run = [], 0
            for v, a in zip(vals, assigned):
                if a:
                    if run:
                        toks.append(f"{run}*")
                        run = 0
                    toks.append(v)
                else:
                    run += 1
            if run:
                toks.append(f"{run}*")
        with open(p, "w") as f:
            f.write(f"{name}\n")
            row = []
            for v in toks:
                row.append(v)
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
        si = np.asarray(arr)
        factor = MD_TO_SI if name.startswith("PERM") else 1.0   # field props are SI (m2); decks use mD in METRIC and FIELD
        arr = si / factor
        if np.allclose(arr, arr[0]) and actnum.all():
            props.append({"keyword": name, "const_value": float(arr[0])})
        else:
            props.append({"keyword": name, "file": write_array(name, si, factor=factor)})
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
    eps_assigned = _assigned_cells(deck, sorted((touched - extra_mapped) & ENDPOINT_KWS), nx, ny, nz)
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
        path = write_array(name, arr, is_int=is_int, fill=1 if is_int else 0, assigned=eps_assigned.get(name))
        passthrough[sec].append({"keyword": name, "raw_text": f"INCLUDE\n '{path}' /\n"})
    # values OPM cannot read from text: recompute them with the deck's own operations, after the arrays are set
    if inexact:
        act_mask = np.zeros(ncell, dtype=bool)
        act_mask[active_idx] = True
        replay, exact = _replay_ops(deck, written, set(inexact), nx, ny, nz, act_mask)
        for tgt, text in replay:
            sec = "GRID" if tgt in GRID_PROPS else {"EDIT": "GRID"}.get(touched_in.get(tgt, "GRID"), touched_in.get(tgt, "GRID"))
            passthrough[sec].append({"keyword": tgt, "raw_text": text})
        for tgt in exact:
            inexact.pop(tgt)

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
    inj_phase = {}   # well -> phase injected in WCONINJE/WCONINJH (WELSPECS phase may say OIL, e.g. Norne)
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
                if name in ("WCONINJE", "WCONINJH") and _get(r, 1) in ("WATER", "GAS"):
                    seen = inj_phase.setdefault(r[0], [])
                    if _get(r, 1) not in seen:
                        seen.append(_get(r, 1))
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
        prod_evs = [e for e in evs if e.get("inj_rate") is None and e.get("control") != "RATE"
                    and (e.get("control") is not None or any(e.get(k) is not None for k in _PROD_RATES))]
        # WCONHIST with all rates zero is a placeholder (Norne opens its injectors this way): it produces nothing
        placeholder = [e for e in prod_evs if e["mode"] == "HISTORY" and not any(e.get(k) for k in _PROD_RATES)]
        produces = len(prod_evs) > len(placeholder)
        injects = any(e.get("inj_rate") is not None or e.get("control") == "RATE" for e in evs)
        phases = inj_phase.get(w["well_name"], [])
        if len(phases) > 1:   # WAG: one phase per well in the model, so the schedule must stay verbatim
            verbatim_reasons.append(f"{w['well_name']} alternates injected phase {'/'.join(phases)}")
        phase = (phases[0] if phases else None) or \
            (w["preferred_phase"] if w["preferred_phase"] in ("WATER", "GAS") else None)
        if injects and not produces and phase:
            # the model keeps one phase per well and uses it as the injected phase, so it follows WCONINJE
            w["well_type"], w["preferred_phase"] = "INJECTOR", phase
            for e in placeholder:   # zero-rate producer entries on an injector: same flow as shut
                e.update(status="SHUT", control=None)
        else:
            w["well_type"] = "PRODUCER"
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
        "geo": {"header": geo_header, "props": [p for p in props if p["keyword"] not in RES_PROPS],
                "tables": {"faults": faults,
                           "fault_multipliers": [{"fault_name": k, "mult": v} for k, v in multflt.items()]}},
        "res": {"header": {"unit_system": unit}, "props": [p for p in props if p["keyword"] in RES_PROPS],
                "tables": {"rock": rock, "swof": swof, "sgof": sgof, "contacts": contacts,
                           **dict(zip(("aquifers", "aquifer_connections"),
                                      aquifer_tables(deck) if map_aq else ([], [])))}},
        "well": {"header": {"unit_system": unit, "start_date": start.isoformat()},
                 "tables": {"well_groups": [g for g in groups.values() if g["group_name"] != "FIELD"],
                            "wells": list(wells.values()), "well_completions": comps, "well_events": events}},
        "spec": spec, "files": files,
        "coverage": {"counts": dict(counts), "keywords": coverage, "schedule_mode": schedule_mode,
                     "schedule_reasons": sorted(set(verbatim_reasons))[:30],
                     **({"aquifer_passthrough": aq_why} if aq_why else {}),
                     "inexact_cells": inexact},   # cells whose value OPM's parser cannot reproduce exactly
        "grid": {"nx": nx, "ny": ny, "nz": nz, "nactive": int(grid.nactive)},
    }


def save_spec(spec: dict, path: Path) -> Path:
    path.write_text(json.dumps(spec, indent=1, default=str))
    return path
