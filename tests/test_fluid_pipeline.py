"""Fluid modeler end to end: Excel -> tuned EOS -> tables -> draft -> QC report -> qc_ready."""
import random

import pytest
from openpyxl import load_workbook

from opmagent.fluid.excel import read_lab_report, write_template
from opmagent.fluid.pipeline import build_fluid_model
from opmagent.pvt.defaults import default_spec
from opmagent.pvt.engine import FluidSpec, saturation_pressure, simulate_dl, simulate_viscosity
from opmagent.servers.data_server import DataService
from opmagent.servers.pvt_server import PVTService


@pytest.fixture(scope="module")
def pvt():
    return PVTService()


@pytest.fixture(scope="module")
def truth():
    """A 'true' fluid slightly different from the default, to give the tuner work."""
    spec = FluidSpec(**{**default_spec().to_dict(), "kij_c1_plus": 0.02})
    return spec, saturation_pressure(spec)


def lab_workbook(path, truth, bo_scale=1.0):
    spec, psat = truth
    write_template(path, example=True)
    wb = load_workbook(path)
    for row in wb["Info"].iter_rows(min_row=2):
        if row[0].value == "saturation_pressure_bar":
            row[1].value = round(psat, 2)
    rnd = random.Random(7)
    for r in simulate_dl(spec, [120, 90, 60, 30]):
        if r["pressure"] > 2:
            wb["DL"].append([r["pressure"], r["rs_d"] * (1 + rnd.uniform(-0.01, 0.01)), r["bo_d"] * bo_scale, r["oil_density"]])
    for r in simulate_viscosity(spec, [250, 150, 80]):
        wb["Viscosity"].append([r["pressure"], r["mu_o"]])
    wb.save(path)
    return path


def test_template_roundtrip(tmp_path):
    fi, gaps = read_lab_report(write_template(tmp_path / "t.xlsx", example=True))
    assert not [g for g in gaps if g["severity"] == "required"]
    assert abs(sum(fi["composition"].values()) + fi["plus_fraction"]["mol_percent"] - 100) < 1e-6
    assert len(fi["separator_stages"]) == 2


def test_empty_template_lists_required_gaps(tmp_path):
    fi, gaps = read_lab_report(write_template(tmp_path / "t.xlsx"))
    req = {g["item"] for g in gaps if g["severity"] == "required"}
    assert {"reservoir_temperature_c", "saturation_pressure_bar", "composition", "separator"} <= req


def test_build_to_qc_ready(tmp_path, cfg, pvt, truth):
    fi, gaps = read_lab_report(lab_workbook(tmp_path / "lab.xlsx", truth))
    r = build_fluid_model(fi, "FieldA_Oil", data=DataService("fluid", cfg), pvt=pvt, gaps=gaps, inline_js=False)
    assert r["ok"] and r["status"] == "qc_ready", r
    assert abs(r["metrics"]["psat_error_pct"]) < 0.5
    assert all(c["passed"] for c in r["checks"].values())
    html = open(r["qc_report"]).read()
    assert "FieldA_Oil version 1" in html and "within tolerance" in html
    pend = DataService("reviewer", cfg).list_pending_reviews()
    assert pend["pending"][0]["model_name"] == "FieldA_Oil"


def test_tolerance_miss_stops_for_decision(tmp_path, cfg, pvt, truth):
    fi, gaps = read_lab_report(lab_workbook(tmp_path / "lab.xlsx", truth, bo_scale=1.08))
    data = DataService("fluid", cfg)
    r = build_fluid_model(fi, "FieldA_Oil", data=data, pvt=pvt, gaps=gaps, inline_js=False)
    assert not r["ok"] and r["status"] == "needs_decision" and not r["checks"]["bo"]["passed"]
    assert data.list_models(kind="fluid")["models"] == []  # nothing written
    r = build_fluid_model(fi, "FieldA_Oil", data=data, pvt=pvt, gaps=gaps, inline_js=False, accept_tolerance_miss=True)
    assert r["ok"] and any("tolerance miss accepted" in a for a in r["assumptions"])
