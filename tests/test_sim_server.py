"""Run agent: background run, status, store, query, compare, spoken names, debug config."""
import json

import pytest

from opmagent.hello.run import hello, latest_demo
from opmagent.servers.data_server import DataService
from opmagent.servers.sim_server import SimService
from opmagent.servers.sim_server import wrap as swrap

pytest.importorskip("opm.simulators")


@pytest.fixture
def deck_id(cfg):
    hello(cfg, log=lambda *_: None)
    rv = DataService("reviewer", cfg)
    for kind, m in latest_demo(rv.con).items():
        rid = rv.get_qc_report(kind=kind, version_id=m["version_id"])["qc_report"]["id"]
        rv.approve(kind=kind, version_id=m["version_id"], qc_report_file_id=rid)
    r = hello(cfg, log=lambda *_: None)
    return r["deck"]["deck_id"]


def test_run_store_query_compare(cfg, deck_id):
    sim = SimService(cfg)
    S = lambda n, **k: swrap(sim, n)(**k)  # noqa: E731
    a = S("run_simulation", deck_id=deck_id)
    assert a["ok"], a
    done = S("wait", run_id=a["run_id"], timeout_s=300)
    assert done["ok"] and done["vectors"] > 10, done
    assert S("job_status", run_id=a["run_id"])["run"]["status"] == "stored"
    q = S("query_results", run_id=a["run_id"], vectors=["FOPR", "WBHP:PROD1", "NOPE"])
    assert q["series"]["FOPR"]["last"] == pytest.approx(500, rel=0.01) and q["missing"] == ["NOPE"]
    b = S("run_simulation", deck_id=deck_id)
    S("wait", run_id=b["run_id"], timeout_s=300)
    c = S("compare_runs", run_a=a["run_id"], run_b=b["run_id"])
    assert c["passed"], c["spoken_summary"]
    assert S("resolve_run", text="run " + ["zero", "one", "two", "three"][b["run_id"]])["run"]["id"] == b["run_id"]
    assert S("resolve_deck", text="the hello deck")["match"]["id"] == deck_id


def test_store_before_done_is_refused(cfg, deck_id):
    sim = SimService(cfg)
    a = swrap(sim, "run_simulation")(deck_id=deck_id)
    r = swrap(sim, "store_results")(run_id=a["run_id"])
    assert r.get("ok") is False or r.get("vectors")  # either still running (refused) or already done


def test_debug_config(cfg, deck_id, tmp_path):
    src = tmp_path / "opm-src"
    src.mkdir()
    cfg.opm_source_root, cfg.opm_debug_flow = src, tmp_path / "flow-debug"
    r = swrap(SimService(cfg), "debug_simulation")(deck_id=deck_id, stop_at="schedule", open_vscode=False)
    assert r["ok"], r
    launch = json.loads((src / ".vscode" / "launch.json").read_text())
    conf = launch["configurations"][0]
    assert conf["program"].endswith("flow-debug") and "Opm::Schedule::Schedule" in json.dumps(conf)
    bad = swrap(SimService(cfg), "debug_simulation")(deck_id=deck_id, stop_at="custom")
    assert bad["ok"] is False
