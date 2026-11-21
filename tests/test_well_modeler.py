"""Well modeler: CSV inputs -> well model -> review -> deck with MD completions and monthly history."""
import numpy as np
import pandas as pd
import pytest

from opmagent.deck.build import build_deck
from opmagent.hello import models as hm
from opmagent.servers.data_server import DataService
from opmagent.well.ingest import min_curvature, monthly_history
from opmagent.well.pipeline import build_well_model


def test_min_curvature_vertical_and_build():
    x, y, z = min_curvature([0, 1000, 2000], [0, 0, 0], [0, 0, 0])
    assert np.allclose(z, [0, 1000, 2000]) and np.allclose(x, 0)
    x, y, z = min_curvature([0, 100], [0, 90], [0, 90])  # quarter circle, east
    assert x[-1] == pytest.approx(2 * 100 / np.pi, rel=1e-6) and z[-1] == pytest.approx(2 * 100 / np.pi, rel=1e-6)


def test_monthly_history_from_daily():
    wells = {"P1": {"well_type": "PRODUCER", "preferred_phase": "OIL"}}
    days = pd.date_range("2026-01-01", "2026-02-28")
    df = pd.DataFrame({"well_name": "P1", "date": days, "oil": 100.0, "water": 10.0, "gas": 8000.0, "hours": 12.0})
    ev = monthly_history(df, wells)
    assert len(ev) == 2 and ev[0]["oil_rate"] == pytest.approx(200.0)  # 31 days x 100 over 15.5 producing days


def write_inputs(d):
    pd.DataFrame([{"well_name": "P1", "type": "PRODUCER", "phase": "OIL", "x": 950, "y": 950, "ref_depth": 2000},
                  {"well_name": "I1", "type": "INJECTOR", "phase": "WATER", "x": 50, "y": 50, "ref_depth": 2000}]
                 ).to_csv(d / "wells.csv", index=False)
    rows = []
    for w, x0 in (("P1", 950.0), ("I1", 50.0)):
        for md, inc in ((0, 0), (1900, 0), (2000, 10), (2100, 20)):
            rows.append({"well_name": w, "md": md, "inc": inc, "azi": 45, "x0": x0, "y0": x0})
    pd.DataFrame(rows).to_csv(d / "surveys.csv", index=False)
    pd.DataFrame([{"well_name": "P1", "date": "2026-01-01", "top_md": 2001, "bottom_md": 2028},
                  {"well_name": "I1", "date": "2026-01-01", "top_md": 2001, "bottom_md": 2028}]).to_csv(d / "perfs.csv", index=False)
    days = pd.date_range("2026-01-01", "2026-06-30")
    pd.concat([pd.DataFrame({"well_name": "P1", "date": days, "oil": 400.0, "water": 20.0, "gas": 32000.0, "bhp": 180.0}),
               pd.DataFrame({"well_name": "I1", "date": days, "water_inj": 450.0})]).to_csv(d / "history.csv", index=False)
    pd.DataFrame([{"well_name": "P1", "date": "2026-07-01", "control": "ORAT", "rate": 400, "bhp": 100},
                  {"well_name": "I1", "date": "2026-07-01", "control": "RATE", "rate": 450, "bhp": 350}]).to_csv(d / "controls.csv", index=False)
    return {"wells": d / "wells.csv", "surveys": d / "surveys.csv", "perforations": d / "perfs.csv",
            "history": d / "history.csv", "controls": d / "controls.csv"}


def approve(cfg, kind, vid):
    rv = DataService("reviewer", cfg)
    rid = rv.get_qc_report(kind=kind, version_id=vid)["qc_report"]["id"]
    assert rv.approve(kind=kind, version_id=vid, qc_report_file_id=rid)["ok"]


@pytest.mark.skipif(pytest.importorskip("opm.io") is None, reason="needs opm")
def test_well_model_to_deck(cfg, tmp_path):
    r = build_well_model(cfg, {k: str(v) for k, v in write_inputs(tmp_path).items()}, "FieldT_Wells")
    assert r["ok"] and r["status"] == "qc_ready", r
    approve(cfg, "well", r["version_id"])
    f = hm.build_fluid(cfg, origin="built", name="FieldT_Oil")
    g = hm.build_geo(cfg, origin="built", name="FieldT_Geo")
    approve(cfg, "fluid", f["version_id"])
    approve(cfg, "geo", g["version_id"])
    r_ = hm.build_res(cfg, g["version_id"], origin="built", name="FieldT_Res")
    approve(cfg, "res", r_["version_id"])
    d = build_deck({"name": "FieldT", "fluid": "FieldT_Oil", "geo": "FieldT_Geo", "res": "FieldT_Res", "well": "FieldT_Wells",
                    "start_date": "2026-01-01", "end_date": "2027-01-01", "report": "monthly"}, cfg)
    assert d["ok"], d["validation_log"]
    text = open(d["path"]).read()
    assert "WCONHIST" in text and "WCONINJH" in text and "WCONPROD" in text
    assert " 'P1' 10 10 1 3 'OPEN'" in text   # MD 2001-2028 on a 45 deg path from (950, 950) lands in column (10, 10)
