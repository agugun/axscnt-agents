"""Geo modeler: tops + LAS logs -> corner-point model -> review; then a full deck with all three modelers, run."""
import numpy as np
import pandas as pd
import pytest

from opmagent.deck.build import build_deck
from opmagent.geo.build import interpolate
from opmagent.geo.pipeline import build_geo_model
from opmagent.hello import models as hm
from opmagent.servers.data_server import DataService
from opmagent.sim.runner import run_deck, summary_values
from opmagent.well.pipeline import build_well_model

lasio = pytest.importorskip("lasio")

WELLS = {"A": (300.0, 300.0), "B": (1700.0, 400.0), "C": (500.0, 1600.0), "D": (1600.0, 1700.0)}
SPEC = {"surfaces": ["TopRes", "MidRes", "BaseRes"], "layers": [3, 2],
        "areal": {"x0": 0.0, "y0": 0.0, "dx": 200.0, "dy": 200.0, "nx": 10, "ny": 10},
        "contacts": {"datum_depth": 2000.0, "datum_pressure": 200.0, "owc_depth": 2100.0},
        "petro": {"vsh_cutoff": 0.5, "kv_kh": 0.1}}


def top(x, y, s):
    return {"TopRes": 2000.0, "MidRes": 2030.0, "BaseRes": 2050.0}[s] + 0.01 * x   # dips east


def write_inputs(d):
    rows = [{"well_name": w, "surface": s, "x": x, "y": y, "tvd": top(x, y, s)} for w, (x, y) in WELLS.items()
            for s in SPEC["surfaces"]]
    pd.DataFrame(rows).to_csv(d / "tops.csv", index=False)
    logs = []
    for w, (x, y) in WELLS.items():
        las = lasio.LASFile()
        las.well.WELL.value = w
        z = np.arange(1990.0, 2080.0, 0.5)
        phi = 0.18 + 0.04 * (w in "BD") + 0.01 * np.sin(z)
        las.append_curve("DEPT", z, unit="m")
        las.append_curve("PHIE", phi)
        las.append_curve("PERM", 10 ** (1 + 8 * phi))
        las.append_curve("VSH", np.where(np.sin(z / 3) > 0.8, 0.7, 0.2))
        p = d / f"{w}.las"
        las.write(str(p), version=2)
        logs.append(str(p))
    return str(d / "tops.csv"), logs


def test_interpolate_exact_at_wells():
    xs, ys, v = [0, 100, 0, 100], [0, 0, 100, 100], [1.0, 2.0, 3.0, 4.0]
    out = interpolate(xs, ys, v, np.array(xs, float), np.array(ys, float))
    assert np.allclose(out, v, atol=1e-6)


def approve(cfg, kind, vid):
    rv = DataService("reviewer", cfg)
    rid = rv.get_qc_report(kind=kind, version_id=vid)["qc_report"]["id"]
    assert rv.approve(kind=kind, version_id=vid, qc_report_file_id=rid)["ok"]


def test_geo_to_running_deck(cfg, tmp_path):
    tops, logs = write_inputs(tmp_path)
    g = build_geo_model(cfg, tops=tops, logs=logs, spec=SPEC, name="FieldG_Geo")
    assert g["ok"] and g["status"] == "qc_ready", g
    assert g["grid"] == "10 x 10 x 5" and g["zones"][0]["hcpv_rm3"] > 0
    approve(cfg, "geo", g["version_id"])
    pytest.importorskip("opm.simulators")
    f = hm.build_fluid(cfg, origin="built", name="FieldG_Oil")
    approve(cfg, "fluid", f["version_id"])
    # wells: vertical, perforated over the reservoir
    pd.DataFrame([{"well_name": "P1", "type": "PRODUCER", "phase": "OIL", "x": 1700, "y": 1700, "ref_depth": 2000},
                  {"well_name": "I1", "type": "INJECTOR", "phase": "WATER", "x": 300, "y": 300, "ref_depth": 2000}]
                 ).to_csv(tmp_path / "wells.csv", index=False)
    sv = [{"well_name": w, "md": md, "x": x, "y": y, "tvd": md} for w, x, y in (("P1", 1700, 1700), ("I1", 300, 300))
          for md in (0.0, 2200.0)]
    pd.DataFrame(sv).to_csv(tmp_path / "surveys.csv", index=False)
    pd.DataFrame([{"well_name": "P1", "date": "2026-01-01", "top_md": 2018, "bottom_md": 2066},
                  {"well_name": "I1", "date": "2026-01-01", "top_md": 2004, "bottom_md": 2052}]).to_csv(tmp_path / "perfs.csv", index=False)
    pd.DataFrame([{"well_name": "P1", "date": "2026-01-01", "control": "ORAT", "rate": 300, "bhp": 100},
                  {"well_name": "I1", "date": "2026-01-01", "control": "RATE", "rate": 350, "bhp": 350}]).to_csv(tmp_path / "ctl.csv", index=False)
    w = build_well_model(cfg, {"wells": str(tmp_path / "wells.csv"), "surveys": str(tmp_path / "surveys.csv"),
                               "perforations": str(tmp_path / "perfs.csv"), "controls": str(tmp_path / "ctl.csv")},
                         "FieldG_Wells", start_date="2026-01-01")
    assert w["ok"], w
    approve(cfg, "well", w["version_id"])
    d = build_deck({"name": "FieldG", "fluid": "FieldG_Oil", "geo": "FieldG_Geo", "well": "FieldG_Wells",
                    "start_date": "2026-01-01", "years": 2, "report": "quarterly"}, cfg)
    assert d["ok"], d["validation_log"]
    text = open(d["path"]).read()
    assert "'P1' 9 9 " in text and "'I1' 2 2 " in text           # x,y -> columns on the 200 m grid
    r = run_deck(d["path"], tmp_path / "run", cfg.flow_bin)
    assert r["ok"], r["log_tail"]
    v = summary_values(r["smspec"], ["FOPR", "FPR"])
    assert v["FOPR"]["last"] == pytest.approx(300, rel=0.05)
