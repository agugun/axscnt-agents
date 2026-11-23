"""SCAL workbook: template generator and reader (the res modeler's relative permeability and capillary pressure).

Sheets
  Info       unit_system (METRIC: Pc in bar; FIELD: Pc in psi), study / field name, lab, date
  RockTypes  one row per SATNUM: Name, Method (corey | tables), Source, and for corey the parameters
             Swc, Sorw, Sorg, Sgc, nw, now, ng, nog, krw_max, krg_max (blank = the default, noted)
  SWOF       measured water-oil points: SATNUM, Sw, krw, krow, Pcow   (method tables)
  SGOF       measured gas-oil points:   SATNUM, Sg, krg, krog, Pcog   (method tables)

read_scal() returns the res spec's `scal` (a list of rock types, as res.pipeline.scal_tables takes it), the
workbook's unit system, and the gaps {severity, item, message}: 'required' ones make the workbook unusable.
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

COREY = ["swc", "sorw", "sorg", "sgc", "nw", "now", "ng", "nog", "krw_max", "krg_max"]
COREY_HEAD = ["Swc", "Sorw", "Sorg", "Sgc", "nw", "now", "ng", "nog", "krw_max", "krg_max"]
ROCK_HEAD = ["SATNUM", "Name", "Method (corey or tables)", "Source (lab report, analogue, ...)", *COREY_HEAD]
SWOF_HEAD = ["SATNUM", "Sw", "krw", "krow", "Pcow"]
SGOF_HEAD = ["SATNUM", "Sg", "krg", "krog", "Pcog"]
INFO_FIELDS = [
    ("unit_system", "METRIC (Pc in bar) or FIELD (Pc in psi)", True),
    ("field", "Field or study name", False),
    ("lab", "Laboratory or source of the data", False),
    ("date", "Report date", False),
]
SHEETS = ("Info", "RockTypes", "SWOF", "SGOF")
_HEAD = Font(bold=True)
_FILL = PatternFill("solid", fgColor="E2EFDA")


def is_scal_workbook(path: str | Path) -> bool:
    try:
        wb = load_workbook(path, read_only=True)
    except Exception:  # noqa: BLE001
        return False
    try:
        return "RockTypes" in wb.sheetnames
    finally:
        wb.close()


def _header(ws, cols, width=16):
    ws.append(cols)
    for c in ws[1]:
        c.font, c.fill = _HEAD, _FILL
    for i, _ in enumerate(cols):
        ws.column_dimensions[chr(65 + i)].width = width


def write_template(path: str | Path, example: bool = False) -> Path:
    """The SCAL workbook template. example=True: rock type 1 as Corey, rock type 2 as measured tables."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Info"
    _header(ws, ["Field", "Value", "Description"], width=30)
    demo = {"unit_system": "METRIC", "field": "DEMO", "lab": "example values, not measured"}
    for key, desc, req in INFO_FIELDS:
        ws.append([key, demo.get(key) if example else None, desc + (" (required)" if req else "")])
    ws = wb.create_sheet("RockTypes")
    _header(ws, ROCK_HEAD)
    ws.column_dimensions["C"].width = ws.column_dimensions["D"].width = 28
    sw_pts = [0.15, 0.3, 0.45, 0.6, 0.75]
    if example:
        ws.append([1, "Good sand", "corey", "analogue field", 0.15, 0.25, 0.10, 0.05, 2.5, 2.0, 2.0, 2.5, 0.35, 0.8])
        ws.append([2, "Shaly sand", "tables", "SCAL report 2024, plug 12"])
    ws = wb.create_sheet("SWOF")
    _header(ws, SWOF_HEAD)
    if example:
        for sw, krw, krow, pc in zip(sw_pts, [0, 0.02, 0.08, 0.18, 0.30], [1.0, 0.45, 0.15, 0.02, 0], [3.0, 1.0, 0.4, 0.1, 0]):
            ws.append([2, sw, krw, krow, pc])
    ws = wb.create_sheet("SGOF")
    _header(ws, SGOF_HEAD)
    if example:
        for sg, krg, krog in zip([0, 0.05, 0.25, 0.45, 0.70], [0, 0, 0.15, 0.40, 0.75], [1.0, 0.85, 0.35, 0.08, 0]):
            ws.append([2, sg, krg, krog, 0.0])
    wb.save(path)
    return Path(path)


def _num(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _points(ws, satnum_ok) -> dict[int, list[list[float]]]:
    out: dict[int, list[list[float]]] = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or all(x is None for x in r[:5]):
            continue
        vals = [_num(x) for x in r[:5]]
        if vals[0] is None or not satnum_ok(vals[0]):
            continue
        out.setdefault(int(vals[0]), []).append(vals[1:])
    return out


def _check_curve(gap, s: int, name: str, rows: list[list], sat: str):
    """Saturation increasing, kr in [0, 1], the first column of kr starting at 0, the second ending at 0."""
    if len(rows) < 2:
        gap("required", f"{name} SATNUM {s}", f"{name} for SATNUM {s} has {len(rows)} complete row(s); at least 2")
        return
    if any(v is None for row in rows for v in row[:3]):
        gap("required", f"{name} SATNUM {s}", f"{name} for SATNUM {s}: a row lacks {sat}, a kr or the other kr")
        return
    sats = [r[0] for r in rows]
    if any(b <= a for a, b in zip(sats, sats[1:])):
        gap("required", f"{name} SATNUM {s}", f"{name} for SATNUM {s}: {sat} must increase down the rows")
    if not all(0 <= v <= 1 for r in rows for v in r[:3]):
        gap("required", f"{name} SATNUM {s}", f"{name} for SATNUM {s}: saturations and kr must be within 0-1")
    if rows[0][1] != 0:
        gap("recommended", f"{name} SATNUM {s}", f"{name} for SATNUM {s}: the first {name[1:3].lower()} kr is "
                                                 f"{rows[0][1]}, expected 0 (no flow below the end point)")
    if rows[-1][2] != 0:
        gap("recommended", f"{name} SATNUM {s}", f"{name} for SATNUM {s}: the last oil kr is {rows[-1][2]}, "
                                                 "expected 0 (residual oil)")


def read_scal(path: str | Path) -> tuple[list[dict], str | None, list[dict]]:
    """The workbook as (scal rock types, unit system, gaps). Rock types are in the res spec's form:
    corey {satnum, name, source, swc, ...} (only the parameters given) or tables {satnum, name, source, swof, sgof}."""
    wb = load_workbook(path, data_only=True)
    gaps: list[dict] = []

    def gap(sev, item, msg):
        gaps.append({"severity": sev, "item": item, "message": msg})

    missing = [s for s in SHEETS if s not in wb.sheetnames]
    if missing:
        gap("required", "sheets", f"missing sheets: {', '.join(missing)} (use opmagent res template)")
        return [], None, gaps
    info = {}
    for row in wb["Info"].iter_rows(min_row=2, values_only=True):
        if row and row[0]:
            info[str(row[0]).strip()] = row[1]
    unit = str(info.get("unit_system") or "").strip().upper() or None
    if unit not in ("METRIC", "FIELD"):
        gap("required", "unit_system", "Info unit_system must be METRIC (Pc in bar) or FIELD (Pc in psi)")

    types, seen = [], set()
    for n, row in enumerate(wb["RockTypes"].iter_rows(min_row=2, values_only=True), start=2):
        if not row or all(x is None for x in row):
            continue
        s = _num(row[0])
        if s is None or s != int(s) or s < 1:
            gap("required", f"RockTypes row {n}", f"row {n}: SATNUM must be a whole number from 1")
            continue
        s = int(s)
        if s in seen:
            gap("required", f"SATNUM {s}", f"SATNUM {s} is on more than one row")
            continue
        seen.add(s)
        method = str(row[2] or "corey").strip().lower()
        t = {"satnum": s, "name": (str(row[1]).strip() if row[1] else f"rock type {s}")}
        if row[3]:
            t["source"] = str(row[3]).strip()
        else:
            gap("recommended", f"SATNUM {s}", f"SATNUM {s}: no source given (lab report, analogue, ...)")
        if method == "corey":
            for k, v in zip(COREY, row[4:4 + len(COREY)]):
                if _num(v) is not None:
                    t[k] = _num(v)
            left = [h for k, h in zip(COREY, COREY_HEAD) if k not in t]
            if left:
                gap("recommended", f"SATNUM {s}", f"SATNUM {s}: defaults used for {', '.join(left)}")
            bad = [h for k, h in zip(COREY, COREY_HEAD) if k in t and (t[k] < 0 or (k.startswith(("s", "kr"))
                                                                                     and t[k] > 1))]
            if bad:
                gap("required", f"SATNUM {s}", f"SATNUM {s}: {', '.join(bad)} out of range (saturations and kr "
                                               "end points 0-1, exponents positive)")
            if t.get("swc", 0) + t.get("sorw", 0) >= 1:
                gap("required", f"SATNUM {s}", f"SATNUM {s}: Swc + Sorw must be below 1")
        elif method == "tables":
            t["method"] = "tables"
        else:
            gap("required", f"SATNUM {s}", f"SATNUM {s}: Method '{method}', expected corey or tables")
            continue
        types.append(t)
    if not types:
        gap("required", "RockTypes", "no rock types: one row per SATNUM in RockTypes")
    elif sorted(seen) != list(range(1, len(seen) + 1)):
        gap("required", "SATNUM", f"SATNUM must run 1..{len(seen)} without gaps; found {sorted(seen)}")

    swof, sgof = _points(wb["SWOF"], lambda v: True), _points(wb["SGOF"], lambda v: True)
    for t in types:
        s = t["satnum"]
        if t.pop("method", None) == "tables":
            if s not in swof or s not in sgof:
                gap("required", f"SATNUM {s}", f"SATNUM {s} is 'tables' but has no rows in "
                                               f"{' and '.join(x for x, d in (('SWOF', swof), ('SGOF', sgof)) if s not in d)}")
                continue
            _check_curve(gap, s, "SWOF", swof[s], "Sw")
            _check_curve(gap, s, "SGOF", sgof[s], "Sg")
            t["swof"] = [{"sw": r[0], "krw": r[1], "krow": r[2], "pcow": r[3] or 0.0} for r in swof[s]]
            t["sgof"] = [{"sg": r[0], "krg": r[1], "krog": r[2], "pcog": r[3] or 0.0} for r in sgof[s]]
        elif s in swof or s in sgof:
            gap("recommended", f"SATNUM {s}", f"SATNUM {s} is 'corey': its SWOF/SGOF rows are not used")
    extra = sorted((set(swof) | set(sgof)) - seen)
    if extra:
        gap("recommended", "SWOF/SGOF", f"rows for SATNUM {extra}, which RockTypes doesn't list: not used")
    return types, unit, gaps
