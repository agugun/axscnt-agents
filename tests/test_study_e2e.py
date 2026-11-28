"""The whole business process of a study, end to end, as in the user guide's "Working in a study": each step through
the profile of the agent that does it, and every decision made by the engineer (no auto-approve).

  data      the inbox loaded into the database; the modelers unlocked
  models    geo, res, fluid and well built from the study's data; each waits for the engineer, who reads its QC
            report and approves it. Geo v1 is rejected with a comment, rebuilt as v2, the comment answered, v2
            approved.
  deck      built from the four approved models; it can't run, nor its task complete, until the engineer accepts
            its initial state
  run       the deck in Flow: the history year under the measured rates, then the prediction controls to the
            study's end date
  match     the history matcher against the study's own production history (loaded by the data manager): the
            misfit and HM report, the base case, the sensitivity, the search and the match report; it recovers the
            water end point (Corey krw max) and proposes it; the engineer accepts; the request reaches the res modeler, who builds a new res
            version, approved; the deck is rebuilt, accepted, run, and confirmed equal to the proposal
  forecast  the matched deck's run reaches the study's end date, the producer's rate switching from the history's
            300 to the plan's 250 sm3/d on 2027-01-01 (and the injection from 350 to 300)

  view      the results viewer opens the run; the study is complete

The production history is made first, by a "truth" study on the same data with krw max 0.2 (its own workspace,
auto-approved): its first year's monthly volumes and bottom-hole pressures per well go into the study's inbox as a
production workbook, the way a field's history arrives. The study starts from the analogue's 0.4. The box is too
large for water to reach the producer in two years, so the water end point shows in the injector's pressure (water
mobility at the well), which the matcher matches with the rates.

Opt-in (marker e2e, about a minute with Flow): `pytest -m e2e`. A plain `pytest` leaves it out.
"""
import json
import shutil
from dataclasses import replace

import pandas as pd
import pytest

pytest.importorskip("opm.simulators")
pytest.importorskip("lasio")
from opmagent.cli import _study_build  # noqa: E402
from opmagent.core import db, models, studies  # noqa: E402
from opmagent.core.errors import ToolError  # noqa: E402
from opmagent.hm import proposals  # noqa: E402
from opmagent.res.pipeline import build_res_model  # noqa: E402
from opmagent.data import store  # noqa: E402
from opmagent.servers.data_server import DataService, wrap  # noqa: E402
from opmagent.servers.hm_server import HMService  # noqa: E402
from opmagent.servers.hm_server import wrap as hwrap  # noqa: E402
from opmagent.servers.sim_server import SimService  # noqa: E402
from opmagent.servers.sim_server import wrap as swrap  # noqa: E402
from opmagent.viewer import open_in_resinsight  # noqa: E402
from tests.study_data import write_raw  # noqa: E402

pytestmark = pytest.mark.e2e

NAME = "FieldE2E"
SCAL = {"swc": 0.2, "sorw": 0.2, "sorg": 0.1, "sgc": 0.05, "nw": 2.0, "now": 2.0, "ng": 2.0, "nog": 2.0,
        "krw_max": 0.4, "krg_max": 0.8, "source": "analogue core report"}
PARAMS = [{"name": "krw", "kind": "krw_max", "target": {"satnum": 1}, "low": 0.1, "high": 0.8}]
TRUTH_KRW = 0.2                     # the field's water end point; the analogue (SCAL) says 0.4
HISTORY_END = "2027-01-01"          # the history year (2026); the prediction controls start then
HISTORY_RATES = (300, 350)          # the field in 2026: oil 300 sm3/d produced, water 350 sm3/d injected
FORECAST_RATES = (250, 300)         # the plan from 2027 (prediction controls)


def _raw(folder, krw_max: float, controls_date: str, rates=HISTORY_RATES) -> None:
    write_raw(folder, controls_date=controls_date, rates=rates)
    g = json.loads((folder / "grid.json").read_text())
    (folder / "grid.json").write_text(json.dumps({**g, "scal": {**SCAL, "krw_max": krw_max}}))   # Corey SCAL


def _history(cfg, tmp_path) -> pd.DataFrame:
    """The field's history: a study on the same data with krw max 0.2, in its own workspace, run for the history
    year. Monthly volumes (rate x days) and the mean bottom-hole pressure per well, as a production workbook has."""
    from opmagent.hm import series
    root = tmp_path / "truth"
    t = replace(cfg, database=root / "opm.db", file_store=root / "files", auto_approve=True)
    db.init_db(t.database)
    for sub in ("inbox", "qc"):
        (t.file_store / sub).mkdir(parents=True, exist_ok=True)
    call = lambda prof, tool, **k: wrap(DataService(prof, t), tool)(**k)  # noqa: E731
    con = db.connect(t.database)
    st = call("deck", "init_study", name="TRUTH", requirement="the field as it is", years=1, report="monthly",
              start_date="2026-01-01")
    _raw(root / "raw", TRUTH_KRW, "2026-01-01")
    shutil.copytree(root / "raw", f"{st['study']['folder']}/inbox", dirs_exist_ok=True)
    call("data", "start_task", name="TRUTH")
    assert call("data", "load_study_data", name="TRUTH")["all_ready"]
    call("data", "complete_task", name="TRUTH", result={})
    for kind in ("geo", "res", "fluid", "well"):
        call(kind, "start_task", name="TRUTH")
        r = _study_build(t, con, kind, "TRUTH")
        assert r["ok"] and r["status"] == "approved", r
        call(kind, "complete_task", name="TRUTH", result={"version_id": r["version_id"]})
    d = _study_build(t, con, "deck", "TRUTH")
    assert d["ok"], d.get("validation_log")
    sim = SimService(t)
    a = swrap(sim, "run_simulation")(deck_id=d["deck_id"])
    assert a["ok"], a
    assert swrap(sim, "wait")(run_id=a["run_id"], timeout_s=300)["ok"]
    s = series.from_summary(con.execute("SELECT smspec_path FROM runs WHERE id = ?", (a["run_id"],)).fetchone()[0])
    s = s[s["month"] < HISTORY_END]
    rows = []
    for (w, mo), g in s.groupby(["well", "month"]):
        days = pd.Period(mo, "M").days_in_month
        v = dict(zip(g["quantity"], g["value"]))
        rows.append({"Well": w, "Date": mo, "Oil": v.get("oil", 0) * days, "Water": v.get("water", 0) * days,
                     "Gas": v.get("gas", 0) * days, "Water_inj": v.get("water_inj", 0) * days, "BHP": v.get("bhp")})
    h = pd.DataFrame(rows)
    assert len(h) == 24 and h["BHP"].notna().all(), h      # 12 months, 2 wells
    return h


def test_study_full_cycle(cfg, tmp_path, monkeypatch):
    fake = tmp_path / "resinsight"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    cfg = replace(cfg, auto_approve=False, resinsight_bin=str(fake), hm_parallel=4)
    monkeypatch.setenv("OPMAGENT_REVIEWER", "engineer")
    call = lambda prof, tool, **k: wrap(DataService(prof, cfg), tool)(**k)  # noqa: E731
    con = db.connect(cfg.database)
    tasks = lambda: {t["task"]: t["status"] for t in studies.status(con, NAME)["tasks"]}  # noqa: E731

    def review(kind, vid, decision="approve", comment=None):
        """The engineer: open the version's QC report, then decide on it."""
        qc = call("reviewer", "get_qc_report", kind=kind, version_id=vid)
        assert qc["ok"] and qc["qc_report"]["path"], qc
        kw = {"comment": comment} if comment else {}
        r = call("reviewer", decision, kind=kind, version_id=vid, qc_report_file_id=qc["qc_report"]["id"], **kw)
        assert r["ok"], r
        return r

    # ---------------------------------------------------------------- study and data
    st = call("deck", "init_study", name=NAME, requirement="Waterflood, 2 years", years=2, report="quarterly",
              start_date="2026-01-01")
    assert st["ok"], st
    _raw(tmp_path / "raw", SCAL["krw_max"], HISTORY_END, FORECAST_RATES)   # the analogue's krw max 0.4
    _history(cfg, tmp_path).to_excel(tmp_path / "raw" / "production history 2026.xlsx", sheet_name="Production",
                                     index=False)
    shutil.copytree(tmp_path / "raw", f"{st['study']['folder']}/inbox", dirs_exist_ok=True)
    call("data", "start_task", name=NAME)
    m = call("data", "load_study_data", name=NAME)
    assert m["all_ready"], m["issues"]
    assert m["datasets"]["history"]["rows"] == 24, m["datasets"].get("history")
    assert call("data", "complete_task", name=NAME, result={})["unlocked"] == ["fluid", "geo", "well"]

    # ---------------------------------------------------------------- models, each reviewed by the engineer
    def build(kind):
        r = _study_build(cfg, con, kind, NAME)
        assert r["ok"], r
        assert r["status"] == "qc_ready", r["spoken_summary"]          # waits for the engineer
        return r["version_id"]

    call("geo", "start_task", name=NAME)
    g1 = build("geo")
    early = call("geo", "complete_task", name=NAME, result={"version_id": g1})
    assert early["ok"] is False                                       # not approved yet
    review("geo", g1, "reject", "Layering: say why MidRes has 2 layers before I approve")
    fb = call("geo", "get_review_feedback")["feedback"]
    assert [f["item"] for f in fb] == ["rejected"] and fb[0]["version_id"] == g1
    g2 = build("geo")
    assert models.header(con, "geo", g2)["version"] == 2
    assert call("geo", "answer_review_feedback", feedback_id=fb[0]["id"], status="addressed",
                response="MidRes is 20 m: 2 layers of 10 m keep the cells near the logs' resolution",
                addressed_in_version_id=g2)["ok"]
    review("geo", g2)
    assert call("geo", "complete_task", name=NAME, result={"version_id": g2})["unlocked"] == ["res"]

    for kind in ("res", "fluid", "well"):
        call(kind, "start_task", name=NAME)
        vid = build(kind)
        review(kind, vid)
        done = call(kind, "complete_task", name=NAME, result={"version_id": vid})
        assert done["ok"], done
    assert done["unlocked"] == ["deck"]
    modes = {r[0]: r[1] for r in con.execute("SELECT mode, MIN(event_date) FROM well_events WHERE version_id = ? "
                                              "GROUP BY mode", (vid,))}
    assert modes == {"HISTORY": "2026-01-01", "PREDICTION": HISTORY_END}, modes   # history, then the forecast
    assert {r[0] for r in con.execute("SELECT reviewer FROM approvals WHERE decision = 'approved'")} == {"engineer"}
    res1 = studies.status(con, NAME)
    res_v1 = next(t["result"]["version_id"] for t in res1["tasks"] if t["task"] == "res")

    # ---------------------------------------------------------------- deck and its initial state
    call("deck", "start_task", name=NAME)
    d = _study_build(cfg, con, "deck", NAME)
    assert d["ok"], d.get("validation_log")
    deck = d["deck_id"]
    sim = SimService(cfg)
    with pytest.raises(ToolError, match="initial state"):
        sim.run_simulation(deck_id=deck)                              # the initialisation gate
    blocked = call("deck", "complete_task", name=NAME, result={"deck_id": deck})
    assert blocked["ok"] is False and "initial state" in blocked["message"], blocked
    assert call("reviewer", "accept_init_view", deck_id=deck)["ok"]
    assert call("deck", "complete_task", name=NAME, result={"deck_id": deck})["unlocked"] == ["run"]

    # ---------------------------------------------------------------- run to the study's end date
    def run(deck_id):
        a = swrap(sim, "run_simulation")(deck_id=deck_id)
        assert a["ok"], a
        w = swrap(sim, "wait")(run_id=a["run_id"], timeout_s=300)
        assert w["ok"], w
        return a["run_id"]

    def ends_at_study_end(run_id):
        """Flow reached the study's end date; the wells followed the history in 2026 and the prediction controls
        from 2027 (the producer's oil rate switches from the history's 300 to the plan's 250 on the date)."""
        from datetime import date
        sp = studies.status(con, NAME)["study"]["spec"]
        start = date.fromisoformat(sp["start_date"])
        end = date.fromisoformat(sp["end_date"]) if sp.get("end_date") else \
            start.replace(year=start.year + int(sp["years"]))
        r = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert r["status"] in ("done", "stored"), dict(r)
        fopr = [(t, v) for t, v in con.execute("SELECT time_days, value FROM results WHERE run_id = ? AND "
                                               "vector = 'FOPR' ORDER BY time_days", (run_id,))]
        assert fopr and fopr[-1][0] == pytest.approx((end - start).days, abs=1), (fopr[-1], end)
        switch = (date.fromisoformat(HISTORY_END) - start).days
        hist = [v for t, v in fopr if 0 < t <= switch]
        plan = [v for t, v in fopr if t > switch]
        assert hist and all(v == pytest.approx(HISTORY_RATES[0], rel=0.01) for v in hist), hist
        assert plan and all(v == pytest.approx(FORECAST_RATES[0], rel=0.01) for v in plan), plan
        wwir = [v for t, v in con.execute("SELECT time_days, value FROM results WHERE run_id = ? AND "
                                          "vector = 'FWIR' ORDER BY time_days", (run_id,)) if t > switch]
        assert wwir and all(v == pytest.approx(FORECAST_RATES[1], rel=0.01) for v in wwir), wwir
        return end

    call("run", "start_task", name=NAME)
    run1 = run(deck)
    ends_at_study_end(run1)
    assert call("run", "complete_task", name=NAME, result={"run_id": run1})["unlocked"] == ["view", "match"]

    # ---------------------------------------------------------------- history match, on the study's production
    H = lambda n, **k: hwrap(HMService(cfg), n)(**k)  # noqa: E731
    call("hm", "start_task", name=NAME)
    mf = H("hm_misfit", study=NAME, run_id=run1)
    assert mf["ok"] and mf["source"] == "data_production" and mf["misfit"] > 0, mf
    worst = mf["terms"][0]                    # 0.4 doesn't match the field: the injector's pressure is off
    assert (worst["well"], worst["quantity"]) == ("I1", "bhp") and abs(worst["last_error"]) > 1, worst
    honoured = {(t["well"], t["quantity"]) for t in mf["terms"] if t["honoured"]}
    assert honoured == {("P1", "oil"), ("I1", "water_inj")}  # the history controls: given to Flow, not matched
    s = H("hm_setup", name="E2E", deck_id=deck, parameters=PARAMS, source="data_production", study=NAME)
    assert s["ok"], s
    rep = H("hm_report", run_id=run1, study=NAME)              # the HM report of the run: observed against simulated
    assert rep["ok"], rep
    b = H("hm_base", hm="E2E")                                 # case 0: the run agent's run, as it is
    assert b["ok"] and b["base"]["misfit"] == pytest.approx(mf["misfit"], rel=1e-6), b
    sens = H("hm_sensitivity", hm="E2E", wait=True)            # krw at its low and high end
    assert sens["ok"] and sens["job"]["status"] == "done", sens
    assert len([c for c in H("hm_cases", hm="E2E", order="case", limit=50)["cases"] if c["label"]]) >= 2
    o = H("hm_optimise", hm="E2E", wait=True)                  # the search, from the best case so far
    assert o["ok"] and o["job"]["status"] == "done", o
    assert o["job"]["result"]["best_values"]["krw"] == pytest.approx(TRUTH_KRW, rel=0.05)
    mr = H("hm_match_report", hm="E2E")                        # best against base, the tornado, the parameters
    assert mr["ok"], mr
    p = H("hm_propose", hm="E2E", note="water end point from the 2026 history")
    assert p["ok"] and p["values"]["krw"] == pytest.approx(TRUTH_KRW, rel=0.05), p
    assert list(p["changes"]) == ["res"] and "SATNUM 1" in p["changes"]["res"][0], p["changes"]

    # the engineer accepts: the request goes to the res modeler, the match task waits for it
    dec = proposals.decide(con, p["proposal_id"], "accept")
    assert dec["status"] == "accepted" and [q["to"] for q in dec["raised"]] == ["res"]
    assert tasks()["match"] == "blocked" and tasks()["res"] == "ready"

    # the res modeler builds the new version from the study's spec with the proposal's value; the engineer approves
    call("res", "start_task", name=NAME)
    spec = store.res_spec(con, NAME)
    spec["scal"] = {**spec["scal"], "krw_max": p["values"]["krw"]}
    geo_v = models.header(con, "res", res_v1)["geo_version_id"]
    r2 = build_res_model(cfg, spec=spec, name=models.header(con, "res", res_v1)["model_name"], geo_version_id=geo_v,
                         description=f"study {NAME}, history match proposal {p['proposal_id']}",
                         data_source={"study": NAME})
    assert r2["ok"] and r2["status"] == "qc_ready", r2
    review("res", r2["version_id"])
    assert call("res", "resolve_request", name=NAME, request_id=dec["raised"][0]["request_id"],
                resolution=f"res v{r2['version']} with krw max {p['values']['krw']} (proposal {p['proposal_id']})")["ok"]
    assert call("res", "complete_task", name=NAME, result={"version_id": r2["version_id"]})["ok"]

    # the deck again from the approved models, its initial state accepted, run, and confirmed equal to the proposal
    d2 = _study_build(cfg, con, "deck", NAME)
    assert d2["ok"], d2.get("validation_log")
    assert call("reviewer", "accept_init_view", deck_id=d2["deck_id"])["ok"]
    run2 = run(d2["deck_id"])
    c = H("hm_confirm", proposal_id=p["proposal_id"], run_id=run2)
    assert c["ok"] and c["status"] == "confirmed" and c["max_rel_diff"] < 1e-6, c
    assert call("hm", "complete_task", name=NAME, result={})["ok"]

    # ---------------------------------------------------------------- forecast: the matched deck to the end date
    ends_at_study_end(run2)

    # ---------------------------------------------------------------- results viewer; the study is complete
    call("view", "start_task", name=NAME)
    monkeypatch.setenv("DISPLAY", ":0")
    v = open_in_resinsight(cfg, study=NAME, dry_run=True)
    assert v["ok"] and v["case"].endswith(".EGRID"), v
    end = call("view", "complete_task", name=NAME, result={"opened": v["case"]})
    assert end["study"]["status"] == "complete", end
    assert all(s_ in ("done", "skipped") for s_ in tasks().values()), tasks()
