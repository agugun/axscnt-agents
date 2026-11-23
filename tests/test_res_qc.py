"""Res QC notebook: the checks on synthetic models (what each one catches), and the notebook run by the res build on
the hello grid, through the static view (what Flow sees)."""
import shutil

import numpy as np
import pandas as pd
import pytest

from opmagent.notebooks import res_qc_lib as qc


def _data(swof=None, sgof=None, grid=None, contacts=None, aquifers=(), conns=(), rock=None):
    swof = swof if swof is not None else [{"satnum": 1, "sw": s, "krw": k, "krow": o, "pcow": p}
                                          for s, k, o, p in ((0.2, 0, 1, 2), (0.5, 0.1, 0.3, 0.5), (0.8, 0.4, 0, 0))]
    sgof = sgof if sgof is not None else [{"satnum": 1, "sg": s, "krg": k, "krog": o, "pcog": 0}
                                          for s, k, o in ((0, 0, 1), (0.05, 0, 0.8), (0.7, 0.8, 0))]
    t = {"swof": swof, "sgof": sgof, "rock": rock or [{"region": 1, "p_ref": 200, "cr": 4.5e-5}],
         "contacts": contacts or [{"eqlnum": 1, "datum_depth": 2000, "datum_pressure": 200, "owc_depth": 2100,
                                   "goc_depth": None}],
         "aquifers": list(aquifers), "aquifer_connections": list(conns), "res_properties": []}
    return {"tables": t, "swof": pd.DataFrame(swof), "sgof": pd.DataFrame(sgof), "grid": grid,
            "unit_system": "METRIC", "dims": (4, 3, 2), "model_name": "T", "version": 1}


def _grid(active=None, satnum=1, eqlnum=1):
    act = np.ones((2, 3, 4), bool) if active is None else active
    return {"active": act, "PORV": np.where(act, 100.0, 0.0), "n_active": int(act.sum()),
            "DEPTH": np.broadcast_to(np.array([2005.0, 2015.0])[:, None, None], (2, 3, 4)).copy(),
            "DZ": np.full((2, 3, 4), 10.0), "SATNUM": np.full((2, 3, 4), float(satnum)),
            "EQLNUM": np.full((2, 3, 4), float(eqlnum)), "FIPNUM": np.ones((2, 3, 4))}


def by(findings, check):
    return next(f for f in findings if check in f["check"])


def test_scal_end_points_and_consistency():
    ep = qc.end_points(_data()).iloc[0]
    assert ep["swc"] == 0.2 and ep["sorw"] == pytest.approx(0.2) and ep["krw_at_sorw"] == 0.4
    assert ep["sgc"] == 0.7 and ep["sorg"] == pytest.approx(0.1)
    f = qc.scal_checks(_data())
    assert all(x["status"] == "pass" for x in f), f
    bad = _data(sgof=[{"satnum": 1, "sg": s, "krg": k, "krog": o, "pcog": 0}
                      for s, k, o in ((0, 0, 0.9), (0.85, 0.8, 0))])          # kro 0.9 vs 1.0; Sg past 1 - Swc
    f = qc.scal_checks(bad)
    assert by(f, "kro at connate water")["status"] == "warn" and by(f, "SGOF ends within")["status"] == "warn"


def test_regions_against_the_grid():
    assert qc.region_checks(_data())[0]["status"] == "n/a"                   # no static view
    f = qc.region_checks(_data(grid=_grid(satnum=2)))                         # SATNUM 2 has no table
    assert by(f, "SATNUM values have tables")["status"] == "fail"
    assert by(f, "every SATNUM table is used")["status"] == "warn"           # table 1 unused
    v = qc.region_volumes(_data(grid=_grid()), "FIPNUM")
    assert v["pore_volume"].iloc[0] == 2400 and v["cells"].iloc[0] == 24


def test_contacts_against_cell_faces():
    f = qc.contact_checks(_data(grid=_grid()))
    assert by(f, "datum within")["status"] == "pass"                         # 2000 = the top face
    assert "below the region" in by(f, "OWC 2100")["detail"]
    f = qc.contact_checks(_data(grid=_grid(), contacts=[{"eqlnum": 1, "datum_depth": 1900, "datum_pressure": 190,
                                                           "owc_depth": 2010, "goc_depth": 2050}]))
    assert by(f, "datum within")["status"] == "warn" and by(f, "GOC above OWC")["status"] == "fail"


def test_rock_range():
    assert qc.rock_checks(_data(rock=[{"region": 1, "p_ref": 200, "cr": 0.01}]))[0]["status"] == "warn"


def test_aquifer_checks():
    aq = {"aquifer_id": 1, "aq_type": "FETKOVICH", "water_volume": 1000.0, "productivity_index": 5}
    conn = {"aquifer_id": 1, "i1": 1, "i2": 1, "j1": 1, "j2": 3, "k1": 1, "k2": 2, "face": "I-"}
    act = np.ones((2, 3, 4), bool)
    act[:, :, 0] = False                                                      # the connected column is inactive
    f = qc.aquifer_checks(_data(grid=_grid(active=act), aquifers=[aq], conns=[conn]))
    assert by(f, "connected cells are active")["status"] == "fail"
    assert by(f, "size against the reservoir")["status"] == "warn"            # 1000 vs 1800 pore volume
    f = qc.aquifer_checks(_data(grid=_grid(), aquifers=[dict(aq, water_volume=1e6)], conns=[conn]))
    assert all(x["status"] == "pass" for x in f), f
    assert qc.aquifer_checks(_data())[0]["status"] == "n/a"


@pytest.mark.skipif(not shutil.which("flow"), reason="the static view needs the OPM Flow binary")
def test_res_build_runs_the_notebook(cfg):
    from opmagent.core import db
    from opmagent.hello.models import build_fluid, build_geo, build_res  # noqa: F401
    from opmagent.servers.data_server import DataService, wrap
    g = build_geo(cfg)
    r = build_res(cfg, g["version_id"])
    assert r["ok"], r
    assert r["qc_notebook"].endswith("res_qc.ipynb") and r["qc_report"].endswith("res_qc.html")
    assert r["qc_findings"]["fail"] == 0, r
    con = db.connect(cfg.database)
    rep = con.execute("SELECT f.path FROM res_model_versions v JOIN files f ON f.id = v.qc_report_file_id "
                      "WHERE v.id = ?", (r["version_id"],)).fetchone()[0]
    assert rep == r["qc_report"]
    html = open(rep).read()
    assert "Res model QC" in html and "active cells (the static view" in html
    res = DataService("res", cfg)
    inv = wrap(res, "create_investigation_notebook")(version_id=r["version_id"], purpose="kr end points")
    assert inv["ok"] and "res_qc_lib" in open(inv["path"]).read()
