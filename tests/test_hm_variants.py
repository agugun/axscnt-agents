"""History matching, phase 2: parameters, variant decks, the job runner and sensitivities (the synthetic truth)."""
import json

import numpy as np
import pytest

from opmagent.core.errors import ToolError
from opmagent.hm import jobs
from opmagent.hm import params as pm

TRUTH = {"kx_L1": 0.5, "nw": 3.0}       # the synthetic truth: PERMX x 0.5 in layer 1, Corey nw 3


def _p(name, kind, low, high, **target):
    return {"name": name, "kind": kind, "target": target, "low": low, "high": high}


GATE = [_p("kx_L1", "permx_mult", 0.4, 2.5, layers="1"), _p("kz", "permz_mult", 0.1, 10),
        _p("nw", "corey_nw", 1.5, 4, satnum=1), _p("now", "corey_now", 1.5, 4, satnum=1),
        _p("krw", "krw_max", 0.2, 0.8, satnum=1)]
LAYERS = [_p(f"kx_L{k}", "permx_mult", 0.4, 2.5, layers=str(k)) for k in (1, 2, 3)] + \
         [_p("nw", "corey_nw", 1.5, 4, satnum=1)]


# ---------------------------------------------------------------- no Flow
def test_arrays_round_trip_and_values_key(tmp_path):
    a = np.array([1.5, 2.0, 2.0, 2.0, 1e-3])
    pm.write_array(tmp_path / "X.INC", "PERMX", a)
    assert np.allclose(pm.read_array(tmp_path / "X.INC", 5), a)
    (tmp_path / "R.INC").write_text("PERMX\n-- comment\n 2*1.5 3*7 /\n")
    assert list(pm.read_array(tmp_path / "R.INC", 5)) == [1.5, 1.5, 7, 7, 7]
    with pytest.raises(ToolError):
        pm.read_array(tmp_path / "R.INC", 6)
    assert pm.values_key({"b": 1.0, "a": 0.30000000001}) == pm.values_key({"a": 0.3, "b": 1})


# ---------------------------------------------------------------- on the hello deck
def test_setup_checks(cfg, hello_deck):
    from opmagent.core import db
    con = db.connect(cfg.database)
    m = pm.deck_models(con, hello_deck)
    bad = [([_p("k", "permx_mult", 0.5, 2, zone="Upper")], "unknown zone"),
           ([_p("k", "permx_mult", 2, 0.5)], "below high"),
           ([dict(_p("k", "permx_mult", 0, 2), scale="log")], "log scale"),
           ([_p("k", "corey_nw", 1.5, 4, satnum=2)], "no Corey"),
           ([_p("k", "multflt", 0, 1, fault="F1")], "unknown fault"),
           ([_p("k", "contact_depth", 2090, 2110)], "later phase"),
           ([_p("k", "corey_nw", 0.1, 4, satnum=1)], "within"),
           ([_p("k", "aquifer_pi_mult", 0.1, 10, aquifer=1)], "unknown aquifer"),
           ([_p("k", "sorw", 0.1, 0.8, satnum=1)], "within")]
    for ps, msg in bad:
        with pytest.raises(ToolError) as e:
            pm.check(ps, m)
        assert msg in e.value.message, (ps, e.value.message)
    ok = pm.check(GATE, m)
    assert [p["start"] for p in ok] == [1.0, 1.0, 2.0, 2.0, 0.4]          # model values for 'value' kinds
    assert [p["scale"] for p in ok] == ["log", "log", "lin", "lin", "lin"]
    t = pm.targets(con, hello_deck)
    assert t["grid"] == [10, 10, 3] and t["layers"][0]["mean_permx"] == 100.0
    assert t["satnums"][0]["corey"]["nw"] == 2.0 and not t["satnums"][0]["tabulated"]


def test_variant_applies_the_modelers_change(cfg, hello_deck, tmp_path):
    from opmagent.core import db
    from opmagent.sim import runner
    con = db.connect(cfg.database)
    h = {"id": 0, "name": "v", "revision": 1, "deck_id": hello_deck,
         "parameters": pm.check(GATE, pm.deck_models(con, hello_deck))}
    deck = jobs.build_variant(con, h, TRUTH, tmp_path / "case")
    text = deck.read_text()
    assert text.startswith("-- History-match variant, not for decisions")
    kx = pm.read_array(tmp_path / "case" / "include" / "PERMX.INC", 300).reshape(3, 100)
    ky = pm.read_array(tmp_path / "case" / "include" / "PERMY.INC", 300).reshape(3, 100)
    assert (kx[0] == 50).all() and (kx[1:] == 100).all() and (ky == kx).all()
    assert "PERMZ\n 300*10 /" in text                                    # untouched: still the constant
    # SWOF rebuilt by the res modeler's own Corey builder with nw 3
    from opmagent.res.pipeline import scal_tables
    sw, _, _ = scal_tables({"swc": 0.2, "sorw": 0.2, "sorg": 0.1, "sgc": 0.05, "nw": 3.0, "now": 2.0, "ng": 2.0,
                            "nog": 2.0, "krw_max": 0.4, "krg_max": 0.8})
    mid = next(r for r in sw if 0.3 < r["sw"] < 0.5)
    assert f"{mid['krw']:.10g}" in text
    # the stored models are untouched
    geo = pm.deck_models(con, hello_deck)["geo"]
    assert {p["keyword"]: p["const_value"] for p in geo["tables"]["geo_properties"]}["PERMX"] == 100.0
    r = runner.run_deck(deck, tmp_path / "case" / "run", cfg.flow_bin)
    assert r["ok"], r["log_tail"]


def _truth_run(cfg, con, deck_id, tmp_path) -> int:
    """The synthetic truth: a variant with TRUTH applied, run in Flow and registered as a run of the deck."""
    from opmagent.sim import runner
    h = {"id": 0, "name": "truth", "revision": 1, "deck_id": deck_id,
         "parameters": pm.check(GATE, pm.deck_models(con, deck_id))}
    d = jobs.build_variant(con, h, TRUTH, tmp_path / "truth")
    r = runner.run_deck(d, tmp_path / "truth" / "run", cfg.flow_bin)
    assert r["ok"], r["log_tail"]
    return con.execute("INSERT INTO runs(deck_id, status, out_dir, smspec_path, note) VALUES (?,?,?,?,?)",
                       (deck_id, "done", str(tmp_path / "truth" / "run"), r["smspec"], "synthetic truth")).lastrowid


def test_gate_sensitivity_ranks_the_truth_first(cfg, hello_deck, tmp_path):
    """Phase 2 gate: truth PERMX x 0.5 in layer 1 and Corey nw 3; the sensitivity ranks those two first."""
    from opmagent.servers.hm_server import HMService, wrap
    cfg.hm_parallel = 4
    svc = HMService(cfg)
    H = lambda n, **k: wrap(svc, n)(**k)  # noqa: E731
    truth = _truth_run(cfg, svc.con, hello_deck, tmp_path)
    s = H("hm_setup", name="SYN", deck_id=hello_deck, parameters=GATE, source="run", truth_run_id=truth)
    assert s["ok"] and "kx_L1: permx x 0.4-2.5 log in layers 1" in s["read_back"], s
    b = H("hm_base", hm="SYN")
    assert b["ok"] and b["base"]["misfit"] > 0.5, b          # the base deck is off the truth
    j = H("hm_sensitivity", hm="SYN", wait=True)
    assert j["ok"] and j["job"]["status"] == "done", j
    tor = j["job"]["result"]["tornado"]
    assert [d["name"] for d in tor[:2]] == ["kx_L1", "nw"], [(d["name"], d["best_reduction"]) for d in tor]
    assert {d["name"]: d["better_end"] for d in tor[:2]} == {"kx_L1": "low", "nw": "high"}
    assert j["job"]["progress"]["failed"] == 0 and j["job"]["progress"]["done"] == 1 + 2 * len(GATE)
    st = H("hm_status", job_id=j["job_id"])
    assert "Most improving: kx_L1 at its low end" in st["spoken_summary"]

    # again: every case is reused, nothing runs twice
    n_cases = svc.con.execute("SELECT COUNT(*) FROM hm_cases").fetchone()[0]
    again = H("hm_sensitivity", hm="SYN", wait=True)
    assert again["job"]["status"] == "done"
    assert svc.con.execute("SELECT COUNT(*) FROM hm_cases").fetchone()[0] == n_cases
    # the setup is frozen: a new hm_setup is a new revision; editing a frozen parameter is refused
    with pytest.raises(Exception, match="frozen"):
        svc.con.execute("UPDATE hm_parameters SET high = 9 WHERE hm_id = ?", (s["hm_id"],))
    s2 = H("hm_setup", name="SYN", deck_id=hello_deck, parameters=GATE[:2], source="run", truth_run_id=truth)
    assert s2["revision"] == 2

    cs = H("hm_cases", hm=str(s["hm_id"]), limit=3)
    assert cs["cases"][0]["misfit"] <= cs["cases"][1]["misfit"]
    one = H("hm_case", hm=str(s["hm_id"]), case=cs["cases"][0]["case_no"])
    assert one["case"]["terms"] and one["case"]["status"] == "done"
    rep = H("hm_match_report", hm=str(s["hm_id"]))
    assert rep["ok"] and rep["report_ok"], rep.get("report_error")
    assert "Sensitivity" in open(rep["report"]).read()


def test_layers_that_act_alike_are_reported_alike(cfg, hello_deck, tmp_path):
    """Hello's three layers are the same rock completed in both wells: a PERMX multiplier in any of them lowers
    the misfit by about as much. The sensitivity shows that (the data can't tell them apart), and nw still
    ranks below the PERMX multipliers."""
    from opmagent.core import db
    cfg.hm_parallel = 4
    con = db.connect(cfg.database)
    truth = _truth_run(cfg, con, hello_deck, tmp_path)
    s = jobs.setup(con, "LAYERS", hello_deck, LAYERS, source="run", truth_run_id=truth)
    j = jobs.Runner(cfg, jobs.create_job(con, jobs.get_hm(con, s["hm_id"]), "sensitivity")).run()
    red = {d["name"]: d["best_reduction"] for d in j["result"]["tornado"]}
    kx = [red[f"kx_L{k}"] for k in (1, 2, 3)]
    assert max(kx) - min(kx) < 0.05 * max(kx), red
    assert red["nw"] > 0


def test_stop_before_start_and_continue(cfg, hello_deck, tmp_path):
    from opmagent.core import db
    con = db.connect(cfg.database)
    truth = _truth_run(cfg, con, hello_deck, tmp_path)
    s = jobs.setup(con, "STOP", hello_deck, GATE[:2], source="run", truth_run_id=truth)
    h = jobs.get_hm(con, s["hm_id"])
    job = jobs.create_job(con, h, "sensitivity")
    with pytest.raises(ToolError) as e:
        jobs.create_job(con, h, "sensitivity")
    assert e.value.code == "BUSY"
    jobs.stop_job(con, job)
    j = jobs.Runner(cfg, job).run()
    # stopped before any case started (the deck has no run of its own: the truth is never the base)
    assert j["status"] == "stopped" and j["progress"]["done"] == 0 and j["progress"]["left"] == 5
    j2 = jobs.Runner(cfg, jobs.create_job(con, h, "sensitivity")).run()
    assert j2["status"] == "done" and j2["progress"]["done"] == 5


def test_capacity(cfg):
    cfg.hm_parallel, cfg.hm_threads = None, 2
    assert jobs.slots(cfg) >= 1
    cfg.hm_parallel = 3
    assert jobs.slots(cfg) == 3
    cfg.hm_min_free_mb = 10 ** 9
    assert jobs.memory_ok(cfg, 100) is (jobs.free_mb() is None)
    assert json.loads(pm.values_key({"x": 1})) == {"x": 1.0}
