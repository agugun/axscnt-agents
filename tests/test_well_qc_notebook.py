"""Well QC notebook: the standard template as the well QC report, re-runs, investigations, and the checks."""
import dataclasses
import json

import nbformat
import pandas as pd
import pytest

from opmagent.hello import models as hm
from opmagent.notebooks import well_qc_lib as wq
from opmagent.servers.data_server import DataService, wrap
from opmagent.well.pipeline import build_well_model
from tests.test_well_modeler import write_inputs


def call(svc, name, **kw):
    return wrap(svc, name)(**kw)


def summary(cfg, name):
    s = json.loads((cfg.file_store / "qc" / f"well_{name}_v1" / "qc_summary.json").read_text())
    return s, {f["check"]: f for f in s["findings"]}


def test_well_notebook_report_rerun_and_investigation(tmp_path, cfg):
    g = hm.build_geo(cfg, origin="built", name="T_Geo")
    r = build_well_model(cfg, {k: str(v) for k, v in write_inputs(tmp_path).items()}, "T_Wells",
                         geo_version_id=g["version_id"])
    assert r["ok"] and r["status"] == "qc_ready" and r["qc_report"].endswith("well_qc.html"), r
    s, st = summary(cfg, "T_Wells")
    assert {f["area"] for f in s["findings"]} == set(wq.AREAS) and s["counts"]["fail"] == 0
    # the injector's history has no BHP: the only warning
    assert [f["check"] for f in s["findings"] if f["status"] == "warn"] == ["History has bottom-hole pressures"]
    assert st["Perforations inside the grid's layers"]["status"] == "pass"
    assert st["Volumes the deck applies match the measured volumes"]["status"] == "pass"
    html = (cfg.file_store / "qc" / "well_T_Wells_v1" / "well_qc.html").read_text()
    for text in ("T_Wells version 1", "placed in geo T_Geo v1", "Perforated cells", "Schedule"):
        assert text in html, text
    qc = DataService("reviewer", cfg).get_qc_report(kind="well", version_id=r["version_id"])
    assert qc["qc"]["metrics"]["qc_overall"] == "warn"

    well, vid = DataService("well", cfg), r["version_id"]
    assert call(well, "run_qc_notebook", version_id=vid)["code"] == "FORBIDDEN"
    assert call(well, "return_to_draft", kind="well", version_id=vid)["ok"]
    rr = call(well, "run_qc_notebook", version_id=vid)
    assert rr["ok"] and rr["overall"] == "warn", rr
    assert call(well, "submit_for_review", kind="well", version_id=vid, qc_report_file_id=rr["qc_report_file_id"])["ok"]
    inv = call(well, "create_investigation_notebook", version_id=vid, purpose="P1 perforated cells")
    nb = nbformat.read(inv["path"], as_version=4)
    nb.cells[4].source = "p = qc.place(data)\nprint('CELLS', len(p[p.well_name == 'P1']))"
    nbformat.write(nb, inv["path"])
    out = call(well, "run_investigation_notebook", path=inv["path"])
    assert out["ok"] and out["ran_clean"] and "CELLS 3" in open(out["html"]).read(), out


def test_well_checks_catch_what_is_wrong(tmp_path, cfg):
    g = hm.build_geo(cfg, origin="built", name="B_Geo")
    f = write_inputs(tmp_path)
    days = pd.date_range("2026-01-01", "2026-09-30")
    days = days[days.month != 3]                                    # no March rows
    p1 = pd.DataFrame({"well_name": "P1", "date": days, "oil": 400.0, "water": 20.0, "gas": 32000.0, "bhp": 180.0,
                       "hours": 12.0})                              # half the time on stream
    p1.loc[p1["date"].dt.month == 8, "oil"] = 6000.0                # a spike in August
    i1 = pd.DataFrame({"well_name": "I1", "date": days, "water_inj": 450.0, "bhp": 300.0})
    pd.concat([p1, i1]).to_csv(f["history"], index=False)
    pd.DataFrame([{"well_name": "P1", "date": "2026-02-01", "top_md": 2001, "bottom_md": 2090},   # opens late; runs below
                  {"well_name": "I1", "date": "2026-01-01", "top_md": 2001, "bottom_md": 2028}]).to_csv(f["perforations"], index=False)
    pd.DataFrame([{"well_name": "P1", "date": "2026-10-01", "control": "ORAT", "rate": 4000, "bhp": 100},
                  {"well_name": "I1", "date": "2026-10-01", "control": "RATE", "rate": 450, "bhp": 350}]).to_csv(f["controls"], index=False)
    r = build_well_model(cfg, {k: str(v) for k, v in f.items()}, "B_Wells", geo_version_id=g["version_id"])
    assert r["ok"] and r["qc_overall"] == "fail", r
    _, st = summary(cfg, "B_Wells")
    assert st["Perforations open before the well flows"]["status"] == "fail"
    assert st["Perforations inside the grid's layers"]["status"] == "warn"
    vol = st["Volumes the deck applies match the measured volumes"]
    # P1: rates over 15.5 producing days applied for 31 (+100 %), and February's rate carries on through the
    # missing March; I1 is on stream all the time but carries February through March too (+12.8 %)
    assert vol["status"] == "fail" and "P1 water +125.6%" in vol["detail"] and "I1 water_inj +12.8%" in vol["detail"], vol
    assert st["No missing months inside each well's history"]["status"] == "warn"
    assert st["No monthly rate above 5x the well's median"]["status"] == "warn"
    assert st["Prediction targets within 2x the last history rates"]["status"] == "warn"


def test_without_a_geo_version_grid_checks_are_skipped(tmp_path, cfg):
    r = build_well_model(cfg, {k: str(v) for k, v in write_inputs(tmp_path).items()}, "N_Wells")
    assert r["ok"], r
    _, st = summary(cfg, "N_Wells")
    assert st["Perforations placed in the grid"]["status"] == "n/a"
    assert "Wells inside the geo grid" not in st


def test_failing_well_notebook_keeps_the_draft(tmp_path, cfg, monkeypatch):
    from opmagent.notebooks import runner
    bad = tmp_path / "bad.ipynb"
    nbformat.write(nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("x = 1", metadata={"tags": ["parameters"]}),
                                                   nbformat.v4.new_code_cell("raise RuntimeError('boom')")]), bad)
    monkeypatch.setattr(runner, "WELL_TEMPLATE", bad)
    r = build_well_model(cfg, {k: str(v) for k, v in write_inputs(tmp_path).items()}, "F_Wells")
    assert not r["ok"] and r["status"] == "draft" and "boom" in r["error"]


def test_well_html_report_mode_still_works(tmp_path, cfg):
    r = build_well_model(dataclasses.replace(cfg, qc_report="html"),
                         {k: str(v) for k, v in write_inputs(tmp_path).items()}, "H_Wells")
    assert r["ok"] and r["qc_report"].endswith("well_H_Wells_v1.html") and "qc_notebook" not in r


def test_dogleg_and_station_spacing():
    pts = pd.DataFrame({"md": [0, 100, 200], "x": [0, 0, 0], "y": [0, 0, 50], "tvd": [0, 100, 186.6]})
    assert wq.dogleg(pts)[0] == pytest.approx(30 * 30 / 100, rel=1e-3)     # 30 deg over 100 m
    data = {"wells": pd.DataFrame([{"well_name": "W"}]),
            "trajectory": pd.DataFrame({"well_name": "W", "md": [0, 100], "x": [0, 0], "y": [0, 0], "tvd": [0, 150]})}
    assert {f["check"]: f["status"] for f in wq.trajectory_checks(data)}["Station spacing fits the measured depth"] == "fail"
