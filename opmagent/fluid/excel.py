"""Excel lab report: template generator and reader.

Sheets: Info, Composition, Separator, CCE, DL, Viscosity. Saturation pressure lives in Info.
Reading returns a fluid_input dict plus a list of gaps (missing required or recommended data).
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

COMPONENT_NAMES = {  # lab name -> NeqSim name
    "N2": "nitrogen", "CO2": "CO2", "H2S": "H2S", "C1": "methane", "C2": "ethane", "C3": "propane",
    "IC4": "i-butane", "NC4": "n-butane", "IC5": "i-pentane", "NC5": "n-pentane", "C6": "n-hexane",
}
INFO_FIELDS = [
    ("sample_name", "Sample or well/test name", True),
    ("reservoir_temperature_c", "Reservoir temperature, degC", True),
    ("saturation_pressure_bar", "Measured saturation (bubble) pressure, bar", True),
    ("unit_system", "METRIC (only METRIC supported now)", True),
    ("salinity_ppm", "Formation water salinity, ppm (0 if unknown)", False),
    ("p_min_bar", "Lowest table pressure, bar", False),
    ("p_max_bar", "Highest table pressure, bar", False),
    ("measured_gor_sm3_sm3", "Separator test GOR, sm3/sm3", False),
    ("measured_bo_fb", "Separator test Bo at bubble point, rm3/sm3", False),
    ("measured_api", "Stock-tank oil API", False),
]
SHEETS = {
    "Separator": ["Stage", "Pressure (bar)", "Temperature (degC)"],
    "CCE": ["Pressure (bar)", "Relative volume (V/Vsat)"],
    "DL": ["Pressure (bar)", "Rs (sm3/sm3)", "Bo (rm3/sm3)", "Oil density (kg/m3)"],
    "Viscosity": ["Pressure (bar)", "Oil viscosity (cP)"],
}
_HEAD = Font(bold=True)
_FILL = PatternFill("solid", fgColor="DDEBF7")


def _header(ws, cols):
    ws.append(cols)
    for c in ws[1]:
        c.font, c.fill = _HEAD, _FILL
    for i, _ in enumerate(cols):
        ws.column_dimensions[chr(65 + i)].width = 28


def write_template(path: str | Path, example: bool = False) -> Path:
    """Create the lab report template. example=True fills in the built-in demo oil."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Info"
    _header(ws, ["Field", "Value", "Description"])
    demo = {"sample_name": "DEMO-1", "reservoir_temperature_c": 100, "saturation_pressure_bar": 131.7,
            "unit_system": "METRIC", "salinity_ppm": 30000, "p_min_bar": 20, "p_max_bar": 400}
    for key, desc, req in INFO_FIELDS:
        ws.append([key, demo.get(key) if example else None, desc + (" (required)" if req else "")])
    ws = wb.create_sheet("Composition")
    _header(ws, ["Component", "Mol %", "MW (g/mol, plus fraction only)", "SG (plus fraction only)"])
    from ..pvt.defaults import DEFAULT_COMPOSITION
    inv = {v: k for k, v in COMPONENT_NAMES.items()}
    for lab in ["N2", "CO2", "H2S", "C1", "C2", "C3", "iC4", "nC4", "iC5", "nC5", "C6"]:
        mol = DEFAULT_COMPOSITION.get(COMPONENT_NAMES[lab.upper()], 0.0) if example else None
        ws.append([lab, mol])
    ws.append(["C7+", 46.0 if example else None, 220.0 if example else None, 0.86 if example else None])
    for name, cols in SHEETS.items():
        ws = wb.create_sheet(name)
        _header(ws, cols)
        if name == "Separator" and example:
            ws.append([1, 30.0, 40.0])
            ws.append([2, 1.01325, 15.56])
    wb.save(path)
    return Path(path)


def _num(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows(ws, ncols):
    out = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        vals = [_num(x) for x in r[:ncols]]
        if all(v is not None for v in vals):
            out.append(vals)
    return out


def read_lab_report(path: str | Path) -> tuple[dict, list[dict]]:
    """Read the workbook into a fluid_input dict and a list of gaps {severity, item, message}."""
    wb = load_workbook(path, data_only=True)
    gaps: list[dict] = []

    def gap(sev, item, msg):
        gaps.append({"severity": sev, "item": item, "message": msg})

    missing = [s for s in ["Info", "Composition", *SHEETS] if s not in wb.sheetnames]
    if missing:
        gap("required", "sheets", f"missing sheets: {', '.join(missing)} (use the template)")
        return {}, gaps

    info = {}
    for row in wb["Info"].iter_rows(min_row=2, values_only=True):
        if row and row[0]:
            info[str(row[0]).strip()] = row[1]
    for key, desc, req in INFO_FIELDS:
        if req and info.get(key) in (None, ""):
            gap("required", key, f"{desc} is missing")
    if str(info.get("unit_system") or "METRIC").upper() != "METRIC":
        gap("required", "unit_system", "only METRIC is supported so far")

    comp, plus = {}, None
    for row in wb["Composition"].iter_rows(min_row=2, values_only=True):
        if not row or row[0] is None:
            continue
        name, mol = str(row[0]).strip(), _num(row[1])
        if mol is None or mol == 0:
            continue
        if name.upper().endswith("+"):
            plus = {"name": name.rstrip("+").upper(), "mol_percent": mol, "mw_g_mol": _num(row[2]), "sg": _num(row[3])}
        elif name.upper() in COMPONENT_NAMES:
            comp[COMPONENT_NAMES[name.upper()]] = mol
        else:
            gap("required", "composition", f"unknown component '{name}' (use N2, CO2, H2S, C1-C6, C7+)")
    total = sum(comp.values()) + (plus["mol_percent"] if plus else 0)
    if not comp:
        gap("required", "composition", "composition is empty")
    elif not 99.0 <= total <= 101.0:
        gap("required", "composition", f"composition sums to {total:.2f} mol %, expected 100")
    if not plus:
        gap("required", "plus_fraction", "C7+ row with mol %, MW and SG is missing")
    elif plus["mw_g_mol"] is None or plus["sg"] is None:
        gap("required", "plus_fraction", "C7+ needs MW and SG")

    stages = [{"pressure": p, "temperature": t} for _, p, t in _rows(wb["Separator"], 3)]
    if not stages:
        gap("required", "separator", "field separator stages are missing")
    cce = [{"pressure": p, "relative_volume": rv} for p, rv in _rows(wb["CCE"], 2)]
    dl = [{"pressure": p, "rs": rs, "bo": bo, "oil_density": d} for p, rs, bo, d in _rows(wb["DL"], 4)]
    visc = [{"pressure": p, "mu_o": mu} for p, mu in _rows(wb["Viscosity"], 2)]
    for name, data in (("CCE", cce), ("DL", dl), ("Viscosity", visc)):
        if not data:
            gap("recommended", name, f"no {name} data: the model can't be checked against it")

    psat = _num(info.get("saturation_pressure_bar"))
    fluid_input = {
        "sample_name": info.get("sample_name"),
        "temperature_c": _num(info.get("reservoir_temperature_c")),
        "psat_lab": psat,
        "unit_system": "METRIC",
        "salinity_ppm": _num(info.get("salinity_ppm")) or 0.0,
        "p_min": _num(info.get("p_min_bar")),
        "p_max": _num(info.get("p_max_bar")),
        "composition": comp,
        "plus_fraction": plus,
        "separator_stages": stages,
        "measured": {k: _num(info.get(k)) for k in ("measured_gor_sm3_sm3", "measured_bo_fb", "measured_api")},
        "cce": cce, "dl": dl, "viscosity": visc,
        "source_file": str(Path(path).resolve()),
    }
    if fluid_input["p_min"] is None and psat:
        gap("recommended", "p_min_bar", "lowest table pressure not given; will use 10% of Psat")
    if fluid_input["p_max"] is None and psat:
        gap("recommended", "p_max_bar", "highest table pressure not given; will use 3 x Psat")
    return fluid_input, gaps
