"""Data manager: raw study inbox -> study data tables, read back the way each modeler reads them."""
import pandas as pd
import pytest

from opmagent.core import db, studies
from opmagent.data import store
from opmagent.data.load import canonical, load
from opmagent.well.ingest import read_inputs

pytest.importorskip("lasio")
from tests.study_data import write_raw  # noqa: E402


@pytest.fixture
def con(cfg):
    return db.connect(cfg.database)


def study(cfg, con, name, **kw):
    studies.init_study(con, cfg.studies_dir, name, "base case")
    write_raw(cfg.studies_dir / name / "inbox", **kw)


def test_load_converts_every_source_into_the_database(cfg, con):
    study(cfg, con, "S1")
    m = load(con, "S1")
    assert m["all_ready"], m["issues"]
    d = m["datasets"]
    assert d["tops"]["rows"] == 12 and d["tops"]["surfaces"] == ["TopRes", "MidRes", "BaseRes"]
    assert d["logs"]["rows"] == 4 and d["logs"]["unit"] == "files"     # LAS stays a file, catalogued
    assert d["logs"]["wells"] == ["A", "B", "C", "D"] and {"PHIE", "PERM", "VSH"} <= set(d["logs"]["curves"])
    assert d["wells"]["rows"] == 2 and d["perforations"]["rows"] == 2 and d["controls"]["rows"] == 2
    assert d["pvt"]["rows"] == 1 and d["geo_spec"]["rows"] == 1 and d["contacts"]["rows"] == 1
    assert d["wells"]["sources"] == ["well data.xlsx[Wells] (2 rows)"]
    assert m["unrecognised"] == ["mystery.bin"]
    # originals are filed as they came, in the study's data folder; the catalog points there
    data = cfg.studies_dir / "S1" / "data"
    assert (data / "logs" / "A.las").read_bytes() == (cfg.studies_dir / "S1" / "inbox" / "A.las").read_bytes()
    assert (data / "documents" / "notes.txt").exists() and (data / "tables" / "well data.xlsx").exists()
    cat = {f["original"]: f for f in store.files(con, "S1")}
    assert cat["A.las"]["format"] == "las" and cat["A.las"]["well_name"] == "A" and cat["A.las"]["status"] == "ok"
    assert cat["A.las"]["path"] == str(data / "logs" / "A.las") and cat["notes.txt"]["category"] == "documents"
    assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'data_log_samples'").fetchone()[0] == 0
    # every row knows its source file
    assert con.execute("SELECT COUNT(*) FROM data_tops t JOIN data_sources s ON s.id = t.source_id "
                       "JOIN files f ON f.id = s.file_id WHERE f.kind = 'tops'").fetchone()[0] == 12
    # read back as the modelers take it
    fi, gaps = store.fluid_input(con, "S1")
    assert fi["psat_lab"] == 131.7 and abs(sum(fi["composition"].values()) + fi["plus_fraction"]["mol_percent"]
                                           - 100) < 1 and fi["source_file"].endswith("WELL-A.xlsx")
    assert not [g for g in gaps if g["severity"] == "required"]
    lg = store.logs(con, "S1")
    assert set(lg["well_name"]) == {"A", "B", "C", "D"} and lg["phi"].notna().all()
    spec = store.geo_spec(con, "S1")
    assert spec["contacts"]["owc_depth"] == 2100.0 and spec["layers"] == [3, 2]
    w = read_inputs(store.well_inputs(con, "S1"))
    assert {x["well_name"] for x in w["wells"]} == {"P1", "I1"} and len(w["well_completions"]) == 2
    st = {c["mnemonic"]: c for c in store.log_stats(con, "S1", "A")}
    assert st["PHIE"]["samples"] == 180 and st["PHIE"]["top"] == 1990.0 and 0.15 < st["PHIE"]["mean"] < 0.2
    assert st["PHIE"]["file"] == "A.las"
    smp = store.log_samples(con, "S1", "A", ["PHIE"], top=2000, base=2001)
    assert [r["depth"] for r in smp] == [2000.0, 2000.5, 2001.0] and set(smp[0]) == {"depth", "PHIE"}
    # a reload replaces, it doesn't add
    load(con, "S1")
    assert con.execute("SELECT COUNT(*) FROM data_tops").fetchone()[0] == 12


def test_missing_items_are_reported_not_invented(cfg, con):
    study(cfg, con, "S2", with_logs=False, with_spec=False)
    m = load(con, "S2", write=False)
    assert not m["all_ready"] and m["ready"] == {"fluid": True, "geo": False, "res": False, "well": True}
    geo = {i["item"]: i["severity"] for i in m["issues"] if i["modeler"] == "geo"}
    assert geo["logs"] == "required" and geo["grid spec"] == "required"
    res = {i["item"]: i["severity"] for i in m["issues"] if i["modeler"] == "res"}
    assert res["contacts"] == "required"          # contacts initialise the model: the res modeler's input
    assert con.execute("SELECT COUNT(*) FROM data_sources").fetchone()[0] == 0   # check only: rolled back
    assert not (cfg.studies_dir / "S2" / "data" / "tables").exists()              # and nothing filed
    assert "item(s) still needed" in m["spoken_summary"] and not m["loaded"]


def test_volve_history_is_loaded_as_production(cfg, con):
    study(cfg, con, "S3")
    inbox = cfg.studies_dir / "S3" / "inbox"
    pd.DataFrame([{"DATEPRD": f"2026-01-{d:02d}", "NPD_WELL_BORE_NAME": "P1", "ON_STREAM_HRS": 24,
                   "BORE_OIL_VOL": 300.0, "BORE_GAS_VOL": 30000.0, "BORE_WAT_VOL": 10.0} for d in (1, 2, 3)]
                 ).to_excel(inbox / "Volve production data.xlsx", index=False)
    m = load(con, "S3")
    assert m["datasets"]["history"]["rows"] == 3 and m["datasets"]["history"]["dates"] == ["2026-01-01", "2026-01-03"]
    ev = [e for e in read_inputs(store.well_inputs(con, "S3"))["well_events"] if e["mode"] == "HISTORY"]
    assert ev and ev[0]["oil_rate"] == pytest.approx(300.0)


def test_a_file_changed_after_loading_is_refused_until_reloaded(cfg, con):
    from opmagent.core.errors import ToolError
    study(cfg, con, "S4")
    load(con, "S4")
    las = cfg.studies_dir / "S4" / "data" / "logs" / "B.las"
    las.write_text(las.read_text().replace("~A", "~A ", 1))   # someone edits the filed copy
    with pytest.raises(ToolError, match="changed since it was loaded"):
        store.logs(con, "S4")
    v = store.verify(con, "S4")
    assert [b["original"] for b in v["not_ok"]] == ["B.las"] and "Load the study again" in v["spoken_summary"]
    # touching a file without changing it is fine
    import os
    a = cfg.studies_dir / "S4" / "data" / "logs" / "A.las"
    os.utime(a, (1, 1))
    assert not store.log_table(con, "S4", "A").empty
    # reloading files the inbox copy again; everything is as loaded
    m = load(con, "S4")
    assert m["datasets"]["files"]["not_ok"] == 0 and not store.verify(con, "S4")["not_ok"]


def test_checks_combine_tables_and_files(cfg, con):
    import lasio
    import numpy as np
    study(cfg, con, "S5")
    inbox = cfg.studies_dir / "S5" / "inbox"
    # well C: porosity log stops halfway through the reservoir; well D: SW says oil below the OWC (2100 m)
    c = lasio.read(str(inbox / "C.las"))
    keep = c.index < 2025
    c2 = lasio.LASFile()
    c2.well.WELL.value = "C"
    c2.append_curve("DEPT", c.index[keep], unit="m")
    c2.append_curve("PHIE", c["PHIE"][keep])
    c2.write(str(inbox / "C.las"), version=2)
    z = np.arange(2050.0, 2120.0, 0.5)
    d = lasio.LASFile()
    d.well.WELL.value = "D"
    d.append_curve("DEPT", z, unit="m")
    d.append_curve("TVD", z, unit="m")
    d.append_curve("PHIE", np.full(z.size, 0.2))
    d.append_curve("SW", np.where(z < 2110, 0.3, 0.9))
    d.write(str(inbox / "D_sw.las"), version=2)
    # P1 perforated below its survey; I1 (injector) reports oil
    with pd.ExcelWriter(inbox / "more wells.xlsx") as x:
        pd.DataFrame([{"WELL": "P1", "Date": "2026-06-01", "MD_TOP": 2150, "MD_BOTTOM": 2300}]).to_excel(
            x, sheet_name="Perfs", index=False)
        pd.DataFrame([{"WELL": "I1", "Date": "2025-01-01", "Oil": 50.0, "Water": 0.0}]).to_excel(
            x, sheet_name="History", index=False)
    m = load(con, "S5")
    msgs = {(i["modeler"], i["item"]): i["message"] for i in m["issues"]}
    assert "porosity covers" in msgs[("geo", "C")]
    assert "below the OWC" in msgs[("res", "D")]   # contacts vs SW logs: the res modeler's concern
    assert "outside its survey" in msgs[("well", "P1")] and not m["ready"]["well"]
    assert "injector but reports oil" in msgs[("well", "I1")]
    assert ("geo", "A") not in msgs and ("geo", "B") not in msgs
    from opmagent.servers.data_server import DataService, wrap
    from dataclasses import replace
    r = wrap(DataService("geo", replace(cfg)), "check_study_data")(name="S5")
    assert r["ok"] and {i["modeler"] for i in r["issues"]} == {"geo"}


def test_canonical_column_names():
    df = canonical(pd.DataFrame(columns=["WELL", "Easting", "Northing", "TVDSS", "MD-Top", "md bottom"]))
    assert list(df.columns) == ["well_name", "x", "y", "tvd", "top_md", "bottom_md"]
