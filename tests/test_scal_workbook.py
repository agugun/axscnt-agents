"""SCAL workbook: template, reader checks, the data manager's inbox load, and the res model built from it."""
import shutil

import pytest
from openpyxl import load_workbook

from opmagent.res.pipeline import scal_tables
from opmagent.res.scal_excel import read_scal, write_template


def test_template_example_reads_back(tmp_path):
    p = write_template(tmp_path / "scal.xlsx", example=True)
    types, unit, gaps = read_scal(p)
    assert unit == "METRIC"
    assert not [g for g in gaps if g["severity"] == "required"], gaps
    assert [t["satnum"] for t in types] == [1, 2]
    assert types[0]["swc"] == 0.15 and types[0]["source"] == "analogue field" and "swof" not in types[0]
    assert len(types[1]["swof"]) == 5 and types[1]["sgof"][-1]["krog"] == 0
    swof, sgof, notes = scal_tables(types)
    assert {r["satnum"] for r in swof} == {1, 2} and "measured tables (SCAL report 2024, plug 12)" in notes[1]


def test_empty_template_says_what_is_missing(tmp_path):
    types, unit, gaps = read_scal(write_template(tmp_path / "scal.xlsx"))
    assert types == [] and {g["item"] for g in gaps if g["severity"] == "required"} == {"unit_system", "RockTypes"}


def _edit(path, sheet, row, values):
    wb = load_workbook(path)
    for col, v in enumerate(values, start=1):
        wb[sheet].cell(row=row, column=col, value=v)
    wb.save(path)


def test_reader_rejects_what_would_break_the_tables(tmp_path):
    p = write_template(tmp_path / "scal.xlsx", example=True)
    _edit(p, "SWOF", 3, [2, 0.10, 0.02, 0.45, 1.0])        # Sw decreasing (0.15 -> 0.10)
    _edit(p, "RockTypes", 4, [4, "Tight", "corey", None, 0.5, 0.6])   # SATNUM gap (no 3), Swc + Sorw >= 1
    _edit(p, "RockTypes", 5, [5, "X", "lookup"])
    _, _, gaps = read_scal(p)
    req = " | ".join(g["message"] for g in gaps if g["severity"] == "required")
    assert "Sw must increase" in req and "Swc + Sorw must be below 1" in req
    assert "Method 'lookup'" in req and "SATNUM must run 1.." in req
    assert any("SATNUM 4: no source given" in g["message"] for g in gaps if g["severity"] == "recommended")


def test_tables_without_sgof_rows(tmp_path):
    p = write_template(tmp_path / "scal.xlsx", example=True)
    wb = load_workbook(p)
    wb["SGOF"].delete_rows(2, 10)
    wb.save(p)
    _, _, gaps = read_scal(p)
    assert any("has no rows in SGOF" in g["message"] for g in gaps if g["severity"] == "required")


def test_study_loads_the_workbook_and_res_uses_it(cfg, tmp_path):
    pytest.importorskip("lasio")
    from dataclasses import replace

    from opmagent.cli import _study_build
    from opmagent.core import db
    from opmagent.data import store
    from opmagent.servers.data_server import DataService, wrap
    from tests.study_data import write_raw

    cfg = replace(cfg, auto_approve=True)
    call = lambda prof, tool, **k: wrap(DataService(prof, cfg), tool)(**k)  # noqa: E731
    con = db.connect(cfg.database)
    folder = call("deck", "init_study", name="FieldK", requirement="SCAL from a workbook")["study"]["folder"]
    write_raw(tmp_path / "raw")
    shutil.copytree(tmp_path / "raw", f"{folder}/inbox", dirs_exist_ok=True)
    p = write_template(f"{folder}/inbox/scal_report.xlsx", example=True)
    wb = load_workbook(p)          # one rock type only: the study's grid has one SATNUM region
    wb["RockTypes"].delete_rows(3)
    wb.save(p)

    call("data", "start_task", name="FieldK")
    m = call("data", "load_study_data", name="FieldK")
    assert m["datasets"]["scal"]["rows"] == 1, m["datasets"]
    assert not [i for i in m["issues"] if i["modeler"] == "res" and i["severity"] == "required"], m["issues"]
    assert any("workbook is used" in i["message"] for i in m["issues"]) or "scal" not in (
        store.geo_spec(con, "FieldK") or {})
    assert store.res_spec(con, "FieldK")["scal"][0]["swc"] == 0.15
    assert call("res", "study_data_rows", name="FieldK", dataset="scal")["rows"][0]["unit_system"] == "METRIC"
    assert con.execute("SELECT category FROM data_files WHERE original = 'scal_report.xlsx'").fetchone()[0] == "scal"
    call("data", "complete_task", name="FieldK", result={})

    for kind in ("geo", "res"):
        call(kind, "start_task", name="FieldK")
        r = _study_build(cfg, con, kind, "FieldK")
        assert r["ok"], r
        call(kind, "complete_task", name="FieldK", result={"version_id": r["version_id"]})
    swc = con.execute("SELECT MIN(sw) FROM swof WHERE version_id = ?", (r["version_id"],)).fetchone()[0]
    assert swc == pytest.approx(0.15)
