"""Geo QC notebook: the standard template as the geo QC report, re-runs, investigations, and the checks."""
import dataclasses
import json

import nbformat
import numpy as np
import pytest

from opmagent.geo.pipeline import build_geo_model
from opmagent.notebooks import geo_qc_lib as gq
from opmagent.servers.data_server import DataService, wrap

pytest.importorskip("lasio")
from tests.test_geo_modeler import SPEC, write_inputs  # noqa: E402


def call(svc, name, **kw):
    return wrap(svc, name)(**kw)


def build(cfg, tmp_path, name="FieldG_Geo", spec=SPEC):
    tops, logs = write_inputs(tmp_path)
    return build_geo_model(cfg, tops=tops, logs=logs, spec=spec, name=name)


def test_geo_notebook_report_rerun_and_investigation(tmp_path, cfg):
    g = build(cfg, tmp_path)
    assert g["ok"] and g["status"] == "qc_ready", g
    assert g["qc_report"].endswith("geo_qc.html"), g
    folder = cfg.file_store / "qc" / "geo_FieldG_Geo_v1"
    s = json.loads((folder / "qc_summary.json").read_text())
    assert s["counts"]["fail"] == 0 and s["tables_from_db"]
    assert {f["area"] for f in s["findings"]} == set(gq.AREAS)
    # a clean build: nothing to flag (contacts and SCAL are checked with the res model, not here)
    assert s["overall"] == "pass" and g["qc_overall"] == "pass" and "warn" not in g["spoken_summary"], s
    assert not {"contacts", "scal"} & {f["area"] for f in s["findings"]}
    html = (folder / "geo_qc.html").read_text()
    for text in ("FieldG_Geo version 1", "Surfaces honour the well tops", "leave one well out", "section along x"):
        assert text in html, text
    assert (folder / "logs.csv").is_file()
    qc = DataService("reviewer", cfg).get_qc_report(kind="geo", version_id=g["version_id"])
    assert qc["qc"]["metrics"]["qc_overall"] == "pass" and len(qc["qc"]["metrics"]["qc_notebook_sha256"]) == 64

    # frozen under review; re-run after return_to_draft, then resubmit with the new report
    geo, vid = DataService("geo", cfg), g["version_id"]
    assert call(geo, "run_qc_notebook", version_id=vid)["code"] == "FORBIDDEN"
    assert call(geo, "return_to_draft", kind="geo", version_id=vid)["ok"]
    rr = call(geo, "run_qc_notebook", version_id=vid)
    assert rr["ok"] and rr["overall"] == "pass", rr
    assert call(geo, "submit_for_review", kind="geo", version_id=vid, qc_report_file_id=rr["qc_report_file_id"])["ok"]

    # investigation: the starter loads the geo data; the agent adds a cell and runs it
    inv = call(geo, "create_investigation_notebook", version_id=vid, purpose="porosity at well C")
    assert inv["ok"] and "/qc/geo_FieldG_Geo_v1/investigations/01_" in inv["path"], inv
    nb = nbformat.read(inv["path"], as_version=4)
    nb.cells[4].source = "print('MEANPHI', round(float(arr['PORO'].mean()), 3))\nqc.fig_well(data, 'C')"
    nbformat.write(nb, inv["path"])
    out = call(geo, "run_investigation_notebook", path=inv["path"])
    assert out["ok"] and out["ran_clean"], out
    assert "MEANPHI 0.2" in open(out["html"]).read()
    # the fluid profile can't open the geo version's notebooks
    assert call(DataService("fluid", cfg), "run_qc_notebook", version_id=vid)["code"] in ("NOT_FOUND", "FORBIDDEN")


def test_geo_checks_catch_what_is_wrong(tmp_path, cfg):
    g = build(cfg, tmp_path)
    folder = cfg.file_store / "qc" / "geo_FieldG_Geo_v1"
    data = gq.load(folder / "payload.json", cfg.database, g["version_id"])
    status = lambda fs: {f["check"]: f["status"] for f in fs}  # noqa: E731

    # a model cell far from its well's upscaled porosity
    wc = gq.well_cells(data).set_index("well_name")
    i, j = wc.loc["A", "i"], wc.loc["A", "j"]
    data["arrays"]["PORO"] = data["arrays"]["PORO"].copy()
    data["arrays"]["PORO"][0, j, i] += 0.05
    assert status(gq.well_checks(data))["Model phi matches the upscaled logs at the wells"] == "fail"

    # an array file changed after it was registered
    poro = next(f["path"] for f in data["files"] if f["keyword"] == "PORO")
    with open(poro, "a") as f:
        f.write("\n")
    again = gq.load(folder / "payload.json", cfg.database, g["version_id"])
    assert status(gq.file_checks(again))["Grid and arrays are the registered files"] == "fail"
    assert "PORO" not in again["arrays"]      # not read once it changed
    assert status(gq.run_all_checks(again))   # the other checks still run (a missing array is no crash)

    # a layer with no log data is filled with defaults: flagged
    data["upscaled"] = data["upscaled"][data["upscaled"]["layer"] != 5]
    assert status(gq.well_checks(data))["Every layer has log data"] == "warn"


def test_crossing_surfaces_are_flagged(tmp_path, cfg):
    tops, logs = write_inputs(tmp_path)
    import pandas as pd
    t = pd.read_csv(tops)
    t.loc[(t["well_name"] == "D") & (t["surface"] == "MidRes"), "tvd"] = 2010.0   # above TopRes at D
    t.to_csv(tops, index=False)
    g = build_geo_model(cfg, tops=tops, logs=logs, spec=SPEC, name="FieldX_Geo")
    assert g["ok"], g
    s = json.loads((cfg.file_store / "qc" / "geo_FieldX_Geo_v1" / "qc_summary.json").read_text())
    st = {f["check"]: f for f in s["findings"]}
    assert st["Zone TopRes is not pinched to the minimum thickness"]["status"] == "warn"
    assert st["Surfaces honour the well tops"]["status"] in ("warn", "fail")


def test_failing_geo_notebook_keeps_the_draft(tmp_path, cfg, monkeypatch):
    from opmagent.notebooks import runner
    bad = tmp_path / "bad.ipynb"
    nbformat.write(nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("x = 1", metadata={"tags": ["parameters"]}),
                                                   nbformat.v4.new_code_cell("raise RuntimeError('boom')")]), bad)
    monkeypatch.setattr(runner, "GEO_TEMPLATE", bad)
    g = build(cfg, tmp_path, name="FieldB_Geo")
    assert not g["ok"] and g["status"] == "draft" and "boom" in g["error"]


def test_geo_html_report_mode_still_works(tmp_path, cfg):
    g = build(dataclasses.replace(cfg, qc_report="html"), tmp_path, name="FieldC_Geo")
    assert g["ok"] and g["qc_report"].endswith("geo_FieldC_Geo_v1.html") and "qc_notebook" not in g


def test_well_cells_match_the_build():
    data = {"areal": {"x0": 0.0, "y0": 0.0, "dx": 200.0, "dy": 200.0}, "nx": 10, "ny": 10,
            "tops": __import__("pandas").DataFrame([{"well_name": "A", "surface": "T", "x": 300.0, "y": 1999.0, "tvd": 1.0},
                                                    {"well_name": "B", "surface": "T", "x": 5000.0, "y": -5.0, "tvd": 1.0}])}
    wc = gq.well_cells(data).set_index("well_name")
    assert (wc.loc["A", "i"], wc.loc["A", "j"]) == (1, 9)
    assert (wc.loc["B", "i"], wc.loc["B", "j"]) == (9, 0)     # clipped to the grid, as geo.build does
    assert np.isclose(gq.cell_centres(data)[0][1], 300.0)
