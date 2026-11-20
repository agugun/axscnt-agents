"""Deck builder and hello end to end (uses opm.io and opm.simulators if installed)."""
import pytest

from opmagent.core.errors import ToolError
from opmagent.deck.build import build_deck
from opmagent.hello.run import clean, hello, latest_demo
from opmagent.servers.data_server import DataService

opm = pytest.importorskip("opm.io")


def approve_hello(cfg):
    rv = DataService("reviewer", cfg)
    for kind, m in latest_demo(rv.con).items():
        if m["status"] == "qc_ready":
            rid = rv.get_qc_report(kind=kind, version_id=m["version_id"])["qc_report"]["id"]
            assert rv.approve(kind=kind, version_id=m["version_id"], qc_report_file_id=rid)["ok"]


def test_hello_end_to_end(cfg):
    r = hello(cfg, log=lambda *_: None)
    assert r["ok"] and r["stage"] == "waiting_for_approval"
    assert hello(cfg, log=lambda *_: None)["stage"] == "waiting_for_approval"  # idempotent: no rebuild
    approve_hello(cfg)
    r = hello(cfg, log=lambda *_: None)
    assert r["deck"]["ok"], r["deck"]["validation_log"]
    if "opm.simulators" in str(r.get("run", {}).get("engine")) or r.get("stage") == "done":
        assert r["ok"] and r["stage"] == "done"
        assert r["results"]["FOPR"] == pytest.approx(500, rel=0.01)
        assert 150 < r["results"]["FPR"] < 260


def test_demo_cannot_mix_with_real(cfg):
    hello(cfg, log=lambda *_: None)
    approve_hello(cfg)
    ids = {k: m["version_id"] for k, m in latest_demo(DataService("reviewer", cfg).con).items()}
    with pytest.raises(ToolError, match="demo and non-demo"):
        build_deck({"name": "MIX", **ids}, cfg, origin="built")


def test_cross_check_pressure_outside_pvt(cfg):
    hello(cfg, log=lambda *_: None)
    approve_hello(cfg)
    con = DataService("reviewer", cfg).con
    ids = {k: m["version_id"] for k, m in latest_demo(con).items()}
    # a well BHP far above the PVT table range must be refused before rendering
    from opmagent.deck import build as b
    fluid = DataService("deck", cfg).get_model(kind="fluid", version_id=ids["fluid"])
    geo = DataService("deck", cfg).get_model(kind="geo", version_id=ids["geo"])
    well = DataService("deck", cfg).get_model(kind="well", version_id=ids["well"])
    well["tables"]["well_events"][0]["bhp"] = 900.0
    assert any("outside the PVT range" in p for p in b.cross_checks(fluid, geo, well))


def test_clean_removes_demo_drafts(cfg):
    hello(cfg, log=lambda *_: None)
    r = clean(cfg)
    assert len(r["removed"]) == 3
    assert all(m is None for m in latest_demo(DataService("reviewer", cfg).con).values())
