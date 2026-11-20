"""Data server services and profiles (called directly, without MCP transport)."""
import pytest

from opmagent.servers.data_server import TOOLS, DataService, wrap
from tests.conftest import fluid_rows

FLUID_HDR = {"unit_system": "METRIC", "phases": "OIL,WATER,GAS"}


def call(svc, name, **kw):
    return wrap(svc, name)(**kw)


def build_fluid(svc, name="FieldA_Oil", rows=None):
    v = call(svc, "create_version", kind="fluid", model_name=name, header=FLUID_HDR)
    assert v["ok"], v
    for table, rr in (rows or fluid_rows()).items():
        r = call(svc, "write_tables", kind="fluid", version_id=v["version_id"], table=table, rows=rr)
        assert r["ok"], r
    return v["version_id"]


def submit(svc, vid, qc_file):
    fid = call(svc, "register_file", path=str(qc_file), kind="qc_report")["file_id"]
    return call(svc, "submit_for_review", kind="fluid", version_id=vid, qc_report_file_id=fid), fid


def test_profiles_expose_only_their_tools():
    assert "approve" not in TOOLS["fluid"] and "approve" in TOOLS["reviewer"]
    assert "write_tables" not in TOOLS["deck"] and "write_tables" not in TOOLS["reviewer"]
    for p in ("fluid", "geo", "well", "import", "deck", "run"):
        assert not {"approve", "reject"} & set(TOOLS[p])


def test_valid_fluid_passes_and_review_flow(cfg, qc_file):
    fl = DataService("fluid", cfg)
    vid = build_fluid(fl)
    v = call(fl, "run_validator", kind="fluid", version_id=vid)
    assert v["passed"], v["errors"]
    res, fid = submit(fl, vid, qc_file)
    assert res["ok"] and res["header"]["status"] == "qc_ready"
    assert "waiting for your review" in res["spoken_summary"]
    # frozen: writes refused
    r = call(fl, "write_tables", kind="fluid", version_id=vid, table="pvtw", rows=[])
    assert not r["ok"] and r["code"] == "FORBIDDEN"

    rv = DataService("reviewer", cfg)
    pend = call(rv, "list_pending_reviews")
    assert len(pend["pending"]) == 1 and "FieldA_Oil version 1" in pend["spoken_summary"]
    bad = call(rv, "reject", kind="fluid", version_id=vid, qc_report_file_id=fid, comment="  ")
    assert not bad["ok"]
    ok = call(rv, "approve", kind="fluid", version_id=vid, qc_report_file_id=fid)
    assert ok["ok"] and ok["header"]["status"] == "approved"
    # deck profile can read approved only
    dk = DataService("deck", cfg)
    assert call(dk, "get_model", kind="fluid", version_id=vid)["ok"]


def test_invalid_fluid_is_refused_with_rule_list(cfg, qc_file):
    rows = fluid_rows()
    rows["pvto"] = [r for r in rows["pvto"] if not (r["rs"] == 100 and not r["is_saturated"])]  # drop top undersat branch
    rows["pvdg"][1]["bg"] = 0.03  # Bg rising with pressure
    fl = DataService("fluid", cfg)
    vid = build_fluid(fl, rows=rows)
    res, _ = submit(fl, vid, qc_file)
    assert not res["ok"] and res["code"] == "VALIDATION_FAILED"
    rules = {e["rule"] for e in res["details"]["errors"]}
    assert {"pvto_undersat_top", "fvf_decreasing"} <= rules


def test_modeler_cannot_write_other_kind_or_import(cfg):
    fl = DataService("fluid", cfg)
    r = call(fl, "create_version", kind="geo", model_name="G", header={"unit_system": "METRIC", "grid_type": "cartesian",
                                                                       "nx": 1, "ny": 1, "nz": 1})
    assert r["code"] == "FORBIDDEN"
    r = call(fl, "create_version", kind="fluid", model_name="X", header=FLUID_HDR, origin="imported")
    assert r["code"] == "FORBIDDEN"


def test_deck_profile_cannot_read_unapproved(cfg):
    fl = DataService("fluid", cfg)
    vid = build_fluid(fl)
    r = call(DataService("deck", cfg), "get_model", kind="fluid", version_id=vid)
    assert r["code"] == "FORBIDDEN"


def test_resolve_spoken_names(cfg, qc_file):
    fl = DataService("fluid", cfg)
    v1 = build_fluid(fl, "FieldA_Oil")
    v2 = build_fluid(fl, "FieldA_Oil")
    build_fluid(fl, "FieldB_Oil")
    r = call(fl, "resolve_name", text="field a oil version two")
    assert r["match"]["version_id"] == v2
    r = call(fl, "resolve_name", text="field A oil, version 1")
    assert r["match"]["version_id"] == v1
    r = call(fl, "resolve_name", text="field b oil")
    assert r["match"]["model_name"] == "FieldB_Oil"
    assert call(fl, "resolve_name", text="unknown gizmo")["code"] == "NOT_FOUND"


def test_writes_are_audited(cfg):
    fl = DataService("fluid", cfg)
    build_fluid(fl)
    n = fl.con.execute("SELECT COUNT(*) FROM audit_log WHERE profile='fluid'").fetchone()[0]
    assert n == 5  # create_version + 4 write_tables


def test_inbox_lookup(cfg):
    (cfg.inbox / "WELL-A1_PVT_report.xlsx").write_text("x")
    r = call(DataService("fluid", cfg), "find_inbox_file", query="well a1 pvt")
    assert len(r["files"]) == 1
