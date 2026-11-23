"""History matching, phase 1: monthly series, observations, misfit, the HM server and report."""
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from opmagent.hm import misfit as mf
from opmagent.hm import series


# ---------------------------------------------------------------- monthly series (no Flow)
def test_monthly_mean_of_step_rates():
    start = datetime(2026, 1, 1)
    # rate 10 over January (31 d), then 20 over February (28 d); report steps end-of-month
    t, v = [31.0, 59.0], [10.0, 20.0]
    assert series.monthly_mean(t, v, start) == [(datetime(2026, 1, 1).date(), 10.0), (datetime(2026, 2, 1).date(), 20.0)]
    # a step change mid-January: 10 for 10 days, 40 for 21 days -> (100 + 840) / 31
    (m, val), = series.monthly_mean([10.0, 31.0], [10.0, 40.0], start)
    assert val == pytest.approx(940 / 31)
    # a month only partly simulated is left out
    assert len(series.monthly_mean([31.0, 45.0], [1.0, 1.0], start)) == 1


def test_from_production_matches_the_well_modeler():
    from opmagent.well.ingest import monthly_history
    daily = pd.DataFrame({"well_name": ["P1"] * 4, "date": ["2020-01-01", "2020-01-02", "2020-02-01", "2020-02-02"],
                          "oil": [100, 100, 50, 0], "water": [0, 10, 5, 0], "gas": [1e4, 1e4, 5e3, 0],
                          "hours": [24, 12, 24, 0], "bhp": [200, 0, 190, np.nan]})
    obs = series.from_production(daily)
    ev = {e["event_date"]: e for e in monthly_history(daily, {"P1": {"well_type": "PRODUCER"}})}
    for mo in ("2020-01-01", "2020-02-01"):
        o = obs[obs["month"] == mo].set_index("quantity")["value"]
        assert o["oil"] == pytest.approx(ev[mo]["oil_rate"]) and o["water"] == pytest.approx(ev[mo]["water_rate"])
    jan = obs[obs["month"] == "2020-01-01"].set_index("quantity")
    assert jan.loc["oil", "days"] == pytest.approx(1.5) and jan.loc["bhp", "value"] == 200   # zero BHP is no reading


def _frame(rows):
    return pd.DataFrame(rows, columns=series.COLUMNS)


def test_misfit_sigma_floor_honoured_and_zero_terms():
    months = ["2020-01-01", "2020-02-01"]
    obs = _frame([("P1", "oil", m, 100.0, 31) for m in months] + [("P1", "water", m, 0.0, 31) for m in months]
                 + [("P1", "bhp", m, 200.0, 31) for m in months] + [("P1", "gas", m, 0.0, 31) for m in months])
    sim = _frame([("P1", "oil", m, 100.0, 31) for m in months] + [("P1", "water", m, 0.5, 31) for m in months]
                 + [("P1", "bhp", m, 210.0, 31) for m in months] + [("P1", "gas", m, 0.0, 31) for m in months])
    r = mf.compute(obs, sim, mf.objective(), {"P1": {"controls": ["ORAT"], "honoured": ["oil"]}})
    t = {x["quantity"]: x for x in r["terms"]}
    assert "gas" not in t                                       # zero in both: no information
    assert t["water"]["misfit"] == pytest.approx(0.25)          # σ = floor 1 sm3/d: (0.5 / 1)²
    assert t["bhp"]["misfit"] == pytest.approx(4.0)             # σ = 5 bar: (10 / 5)²
    assert t["oil"]["honoured"] and not t["oil"]["in_total"]
    assert r["total"] == pytest.approx((2 * 0.25 + 2 * 4.0) / 4)
    assert t["bhp"]["last_error"] == pytest.approx(10.0)
    only = mf.compute(obs, sim, mf.objective({"period": ["2020-02-01", None], "weights": {"bhp": 0}}))
    assert only["n"] == 3 and only["total"] == pytest.approx(0.25 / 3)   # Feb only, oil now in, BHP weighted 0
    with pytest.raises(ValueError):
        mf.objective({"nope": 1})


def test_breakthrough_in_words():
    months = [f"2020-{m:02d}-01" for m in range(1, 7)]
    obs = _frame([("P1", "oil", m, 100.0, 30) for m in months]
                 + [("P1", "water", m, 20.0 if i >= 4 else 0.0, 30) for i, m in enumerate(months)])
    sim = _frame([("P1", "oil", m, 100.0, 30) for m in months]
                 + [("P1", "water", m, 20.0 if i >= 1 else 0.0, 30) for i, m in enumerate(months)])
    r = mf.compute(obs, sim)
    w = r["wells"][0]
    assert (w["breakthrough_obs"], w["breakthrough_sim"]) == ("2020-05-01", "2020-02-01")
    assert any("arrives 3 month(s) early" in s for s in mf.describe(r))


# ---------------------------------------------------------------- on the hello deck (Flow)
@pytest.fixture
def hello_run(cfg):
    """A finished run of the hello deck (models approved and initial state accepted, as a person would)."""
    pytest.importorskip("opm.simulators")
    from opmagent.deck.init_review import accept, state
    from opmagent.hello.run import hello, latest_demo
    from opmagent.servers.data_server import DataService
    from opmagent.servers.sim_server import SimService
    from opmagent.servers.sim_server import wrap as swrap
    hello(cfg, log=lambda *_: None)
    rv = DataService("reviewer", cfg)
    for kind, m in latest_demo(rv.con).items():
        rid = rv.get_qc_report(kind=kind, version_id=m["version_id"])["qc_report"]["id"]
        rv.approve(kind=kind, version_id=m["version_id"], qc_report_file_id=rid)
    deck = hello(cfg, log=lambda *_: None)["deck"]["deck_id"]
    if state(rv.con, deck) == "waiting":
        accept(cfg, rv.con, deck, "tester")
    sim = SimService(cfg)
    a = swrap(sim, "run_simulation")(deck_id=deck)
    assert a["ok"], a
    done = swrap(sim, "wait")(run_id=a["run_id"], timeout_s=300)
    assert done["ok"], done
    return a["run_id"]


def _study_with_history(cfg, con, run_id, scale=1.0):
    """A study whose measured history is the run's own simulated monthly volumes (× scale)."""
    from opmagent.hm.service import run_row
    con.execute("INSERT INTO studies(name, requirement, spec_json, folder) VALUES ('HMHELLO', 'hm test', '{}', ?)",
                (str(cfg.file_store),))
    sid = con.execute("SELECT id FROM studies WHERE name='HMHELLO'").fetchone()["id"]
    sim = series.from_summary(run_row(con, run_id)["smspec_path"])
    wide = sim.pivot_table(index=["well", "month", "days"], columns="quantity", values="value").reset_index()
    for r in wide.to_dict("records"):
        vol = {q: (r.get(q) or 0.0) * r["days"] * scale for q in series.RATES}
        con.execute("INSERT INTO data_production(study_id, well_name, date, oil, water, gas, water_inj, gas_inj, bhp, "
                    "hours) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sid, r["well"], r["month"], vol["oil"], vol["water"], vol["gas"], vol["water_inj"],
                     vol["gas_inj"], r.get("bhp"), None))
    return "HMHELLO"


def test_gate_hello_misfit_zero_against_itself(cfg, hello_run):
    """Phase 1 gate: the hello run against its own simulated history has misfit 0."""
    from opmagent.servers.hm_server import HMService, wrap
    svc = HMService(cfg)
    H = lambda n, **k: wrap(svc, n)(**k)  # noqa: E731
    r = H("hm_misfit", run_id=hello_run, source="run", truth_run_id=hello_run)
    assert r["ok"], r
    assert r["points"] > 0 and r["misfit"] == pytest.approx(0.0, abs=1e-12)
    assert {w["well"] for w in r["wells"]} == {"PROD1", "INJ1"}

    # the same through data_production: monthly volumes in, monthly rates out
    study = _study_with_history(cfg, svc.con, hello_run)
    d = H("hm_misfit", run_id=hello_run, study=study)
    assert d["ok"] and d["source"] == "data_production", d
    assert d["misfit"] == pytest.approx(0.0, abs=1e-9)
    inv = H("hm_observations", study=study, run_id=hello_run)
    assert inv["ok"] and {w["well"] for w in inv["wells"]} == {"PROD1", "INJ1"}
    assert "oil" in next(w for w in inv["wells"] if w["well"] == "PROD1")["quantities"]

    # the hello deck gives Flow no history
    nh = H("hm_misfit", run_id=hello_run)
    assert nh["ok"] is False and nh["code"] == "NO_HISTORY"


def test_misfit_grows_with_error_and_report(cfg, hello_run):
    from opmagent.servers.hm_server import HMService, wrap
    svc = HMService(cfg)
    study = _study_with_history(cfg, svc.con, hello_run, scale=0.8)   # history 20 % lower than the model
    r = wrap(svc, "hm_misfit")(run_id=hello_run, study=study)
    assert r["ok"] and r["misfit"] > 1, r
    oil = next(t for t in r["terms"] if t["well"] == "PROD1" and t["quantity"] == "oil")
    assert oil["cum_error_pct"] == pytest.approx(25.0, rel=1e-6)      # sim / obs = 1 / 0.8
    rep = wrap(svc, "hm_report")(run_id=hello_run, study=study)
    assert rep["ok"] and rep["report_ok"], rep.get("report_error")
    html = open(rep["report"]).read()
    assert "History match report" in html and "PROD1" in html
