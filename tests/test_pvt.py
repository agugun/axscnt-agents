"""PVT engine: generated tables pass the fluid validator; tuning; lab simulations."""
import pytest

from opmagent.servers.data_server import DataService
from opmagent.servers.data_server import wrap as dwrap
from opmagent.servers.pvt_server import PVTService
from opmagent.servers.pvt_server import wrap as pwrap


@pytest.fixture(scope="module")
def pvt():
    return PVTService()


@pytest.fixture(scope="module")
def demo(pvt):
    r = pwrap(pvt, "characterize")(use_default=True)
    assert r["ok"], r
    return r


def test_default_fluid_is_plausible(demo):
    assert 80 < demo["psat_bar"] < 250


def test_generated_tables_pass_validator_and_review(pvt, demo, cfg, qc_file):
    bo = pwrap(pvt, "generate_black_oil")(fluid_handle=demo["fluid_handle"], p_min=20, p_max=400, n=10)
    assert bo["ok"], bo
    t = bo["tables"]
    sat = [r for r in t["pvto"] if r["is_saturated"]]
    assert [r["rs"] for r in sat] == sorted(r["rs"] for r in sat)
    assert max(r["pressure"] for r in sat) == pytest.approx(demo["psat_bar"])

    fl = DataService("fluid", cfg)
    v = dwrap(fl, "create_version")(kind="fluid", model_name="DEMO", header={"unit_system": "METRIC",
                                    "phases": "OIL,WATER,GAS", "spec_json": demo["spec"]}, origin="demo")
    for table, rows in t.items():
        r = dwrap(fl, "write_tables")(kind="fluid", version_id=v["version_id"], table=table, rows=rows)
        assert r["ok"], r
    val = dwrap(fl, "run_validator")(kind="fluid", version_id=v["version_id"])
    assert val["passed"], val["errors"]


def test_tuning_hits_lab_psat(pvt, demo):
    target = demo["psat_bar"] * 1.08
    r = pwrap(pvt, "tune")(fluid_handle=demo["fluid_handle"], psat_lab=target)
    assert r["ok"], r
    assert abs(r["psat_tuning"]["psat_error_after"]) < 0.005
    assert r["checks"]["psat"]["passed"]


def test_tune_reports_dl_mismatch(pvt, demo):
    dl = pwrap(pvt, "simulate_dl")(fluid_handle=demo["fluid_handle"], pressures=[100, 60, 20])["rows"]
    lab = [{"pressure": d["pressure"], "bo": d["bo_d"] * 1.10, "rs": d["rs_d"]} for d in dl if d["pressure"] < demo["psat_bar"] - 1 and d["pressure"] > 2]
    r = pwrap(pvt, "tune")(fluid_handle=demo["fluid_handle"], psat_lab=demo["psat_bar"], lab_dl=lab)
    assert r["ok"] and not r["checks"]["bo"]["passed"] and not r["within_tolerance"]


def test_lab_simulations(pvt, demo):
    h = demo["fluid_handle"]
    cce = pwrap(pvt, "simulate_cce")(fluid_handle=h, pressures=[300, 200, 100, 50])["rows"]
    assert cce[0]["relative_volume"] < 1 < cce[-1]["relative_volume"]
    sep = pwrap(pvt, "simulate_separator")(fluid_handle=h)
    assert 20 < sep["api"] < 50 and sep["bo_at_pb"] > 1


def test_bad_composition(pvt):
    r = pwrap(pvt, "characterize")(composition={"methane": 50}, temperature_c=90)
    assert not r["ok"] and r["code"] == "INVALID_INPUT"
