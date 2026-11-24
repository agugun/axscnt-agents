"""History matching, phase 3: the optimiser, match proposals, the engineer's review, requests to the modelers, and
the confirmation that the deck rebuilt from the modelers' new versions equals the proposal (the phase 3 gate)."""
import json

import pytest

from opmagent.core import db, studies
from opmagent.core.errors import ToolError
from opmagent.hm import jobs, proposals
from opmagent.hm import params as pm

TRUTH = {"kx_L1": 0.5, "nw": 3.0}
PARAMS = [{"name": "kx_L1", "kind": "permx_mult", "target": {"layers": "1"}, "low": 0.25, "high": 4},
          {"name": "nw", "kind": "corey_nw", "target": {"satnum": 1}, "low": 1.5, "high": 4}]


def _truth_run(cfg, con, deck_id, tmp_path) -> int:
    from opmagent.sim import runner
    h = {"id": 0, "name": "truth", "revision": 1, "deck_id": deck_id,
         "parameters": pm.check(PARAMS, pm.deck_models(con, deck_id))}
    d = jobs.build_variant(con, h, TRUTH, tmp_path / "truth")
    r = runner.run_deck(d, tmp_path / "truth" / "run", cfg.flow_bin)
    assert r["ok"], r["log_tail"]
    return con.execute("INSERT INTO runs(deck_id, status, out_dir, smspec_path, note) VALUES (?,?,?,?,?)",
                       (deck_id, "done", str(tmp_path / "truth" / "run"), r["smspec"], "synthetic truth")).lastrowid


def _study_at_match(cfg, con, name="HMS"):
    """A study whose tasks are done up to the run, with the match task in progress (the matcher's)."""
    studies.init_study(con, cfg.studies_dir, name, "history match the hello box")
    sid = con.execute("SELECT id FROM studies WHERE name = ?", (name,)).fetchone()[0]
    con.execute("UPDATE study_tasks SET status = 'done' WHERE study_id = ? AND task NOT IN ('view', 'match')", (sid,))
    con.execute("UPDATE study_tasks SET status = 'in_progress' WHERE study_id = ? AND task = 'match'", (sid,))
    return name


def _approve(cfg, kind, vid):
    from opmagent.servers.data_server import DataService
    rv = DataService("reviewer", cfg)
    rid = rv.get_qc_report(kind=kind, version_id=vid)["qc_report"]["id"]
    r = rv.approve(kind=kind, version_id=vid, qc_report_file_id=rid)
    assert r.get("ok", True), r


def _modelers_build(cfg, con, base_deck, values):
    """What the geo, res and well modelers do with the requests: new versions with exactly these values."""
    from opmagent.hello import models as hm
    from opmagent.res.pipeline import build_res_model
    from opmagent.servers.data_server import DataService
    from opmagent.servers.data_server import wrap as dwrap
    base = pm.deck_models(con, base_deck)
    svc = DataService("geo", cfg)
    D = lambda n, **k: dwrap(svc, n)(**k)  # noqa: E731
    gh = base["geo"]["header"]
    v = D("create_version", kind="geo", model_name="HELLO_GEO_HM", origin="demo",
          header={k: gh[k] for k in ("unit_system", "grid_type", "nx", "ny", "nz", "dx", "dy", "dz", "tops")}
          | {"user_description": "hello box, history matched", "assumptions_json": ["history match proposal"]})
    assert v["ok"], v
    vid = v["version_id"]
    folder = cfg.file_store / "models" / "geo" / "HELLO_GEO_HM"
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in base["geo"]["tables"]["geo_properties"]:
        kw = p["keyword"]
        if kw in ("PERMX", "PERMY"):
            a = pm.np.full(300, float(p["const_value"]))
            a.reshape(3, 100)[0] *= values["kx_L1"]
            f = pm.write_array(folder / f"{kw}.INC", kw, a)
            fid = D("register_file", path=str(f), kind="property")["file_id"]
            rows.append({"keyword": kw, "file_id": fid})
        else:
            rows.append({"keyword": kw, "const_value": p["const_value"]})
    assert D("write_tables", kind="geo", version_id=vid, table="geo_properties", rows=rows)["ok"]
    D("run_validator", kind="geo", version_id=vid)
    rep = cfg.file_store / "qc" / "geo_hm.html"
    rep.write_text("<html>geo hm</html>")
    rid = D("register_file", path=str(rep), kind="qc_report")["file_id"]
    assert D("submit_for_review", kind="geo", version_id=vid, qc_report_file_id=rid)["ok"]
    _approve(cfg, "geo", vid)
    spec = json.loads(json.dumps(base["res"]["header"]["spec_json"]["res_spec"]))
    spec["scal"]["nw"] = values["nw"]
    r = build_res_model(cfg, spec=spec, name="HELLO_RES_HM", geo_version_id=vid, origin="demo")
    _approve(cfg, "res", r["version_id"])
    w = hm.build_wells(cfg, vid)
    _approve(cfg, "well", w["version_id"])
    return {"fluid": base["deck"]["fluid_version_id"], "geo": vid, "res": r["version_id"], "well": w["version_id"]}


def _deck_and_run(cfg, con, base_deck, ids, name):
    from opmagent.deck.build import build_deck
    from opmagent.deck.init_review import accept, state
    from opmagent.servers.sim_server import SimService
    from opmagent.servers.sim_server import wrap as swrap
    spec = pm.deck_models(con, base_deck)["deck"]["spec_json"]
    d = build_deck({**{k: spec[k] for k in ("start_date", "end_date", "report") if k in spec}, "name": name,
                    "requirement": "history-matched hello", **ids}, cfg)
    assert d["ok"], d
    if state(con, d["deck_id"]) == "waiting":
        accept(cfg, con, d["deck_id"], "tester")
    sim = SimService(cfg)
    a = swrap(sim, "run_simulation")(deck_id=d["deck_id"])
    assert a["ok"], a
    assert swrap(sim, "wait")(run_id=a["run_id"], timeout_s=300)["ok"]
    return a["run_id"]


def test_gate_recover_propose_accept_confirm(cfg, hello_deck, tmp_path, monkeypatch):
    """Phase 3 gate: both truth parameters recovered within 5 % and the misfit near 0; the proposal, accepted,
    becomes requests to geo and res; the deck built from the modelers' versions runs equal to the proposal."""
    from opmagent.servers.data_server import DataService, wrap as dwrap
    from opmagent.servers.hm_server import HMService, wrap
    monkeypatch.setenv("OPMAGENT_REVIEWER", "engineer")
    cfg.hm_parallel = 4
    svc = HMService(cfg)
    H = lambda n, **k: wrap(svc, n)(**k)  # noqa: E731
    con = svc.con
    truth = _truth_run(cfg, con, hello_deck, tmp_path)
    study = _study_at_match(cfg, con)
    s = H("hm_setup", name="SYN", deck_id=hello_deck, parameters=PARAMS, source="run", truth_run_id=truth,
          study=study)
    assert s["ok"], s

    o = H("hm_optimise", hm="SYN", start="start", wait=True)
    assert o["ok"] and o["job"]["status"] == "done", o
    r = o["job"]["result"]
    assert r["best_values"]["kx_L1"] == pytest.approx(0.5, rel=0.05)
    assert r["best_values"]["nw"] == pytest.approx(3.0, rel=0.05)
    assert r["best_misfit"] < 1e-3 < r["base_misfit"] and not r["at_bound"]
    assert r["cases_run"] <= 60, r
    # again: the search continues from the best case and reuses every case it needs
    n = con.execute("SELECT COUNT(*) FROM hm_cases").fetchone()[0]
    again = H("hm_optimise", hm="SYN", wait=True)["job"]["result"]
    assert again["best_misfit"] <= r["best_misfit"] and \
        con.execute("SELECT COUNT(*) FROM hm_cases").fetchone()[0] - n <= 3 * len(PARAMS)

    p = H("hm_propose", hm="SYN", note="synthetic truth")
    assert p["ok"], p
    assert p["values"] == {"kx_L1": 0.5, "nw": 3.0}
    assert p["changes"] == {"geo": ["PERMX and PERMY x 0.5 in layers 1"], "res": ["Corey nw 3 in SATNUM 1 (was 2)"]}
    assert p["misfit"] < 1e-6 and p["report"]
    busy = H("hm_propose", hm="SYN")
    assert busy["ok"] is False and busy["code"] == "BUSY"
    # an agent can't send the modelers a match request itself
    hm_data = DataService("hm", cfg)
    no = dwrap(hm_data, "raise_request")(name=study, to_task="geo", kind="match", message="PERMX x 0.5")
    assert no["ok"] is False and no["code"] == "FORBIDDEN"
    # match can't complete before a confirmed proposal
    early = dwrap(hm_data, "complete_task")(name=study, result={})
    assert early["ok"] is False and "no confirmed match proposal" in early["message"]

    # the engineer accepts: one request per owner, the match task waits for them, geo and res reopen
    d = proposals.decide(con, p["proposal_id"], "accept")
    assert d["status"] == "accepted" and d["reviewer"] == "engineer"
    assert sorted(q["to"] for q in d["raised"]) == ["geo", "res"]
    assert "PERMX and PERMY x 0.5 in layers 1" in d["raised"][0]["message"] + d["raised"][1]["message"]
    st = {t["task"]: t["status"] for t in studies.status(con, study)["tasks"]}
    assert st["match"] == "blocked" and st["geo"] == "ready" and st["res"] == "ready"

    # the modelers build, the engineer approves, the deck is rebuilt and run
    ids = _modelers_build(cfg, con, hello_deck, p["values"])
    for q in d["raised"]:
        studies.resolve_request(con, study, q["request_id"], f"new version with proposal {p['proposal_id']}")
    run = _deck_and_run(cfg, con, hello_deck, ids, "HELLO_HM")
    with pytest.raises(ToolError, match="base deck"):
        proposals.confirm(con, p["proposal_id"], truth)
    c = H("hm_confirm", proposal_id=p["proposal_id"], run_id=run)
    assert c["ok"] and c["status"] == "confirmed", c
    assert c["max_rel_diff"] < 1e-6 and c["compared"] > 100
    done = dwrap(hm_data, "complete_task")(name=study, result={})
    assert done["ok"], done
    assert {t["task"]: t["status"] for t in done["tasks"]}["match"] == "done"


def test_reject_needs_a_comment_and_reopens(cfg, hello_deck, tmp_path):
    cfg.hm_parallel = 4
    con = db.connect(cfg.database)
    truth = _truth_run(cfg, con, hello_deck, tmp_path)
    s = jobs.setup(con, "REJ", hello_deck, PARAMS, source="run", truth_run_id=truth)
    jobs.Runner(cfg, jobs.create_job(con, jobs.get_hm(con, s["hm_id"]), "base")).run()
    p = proposals.propose(cfg, con, "REJ", case=0)       # the base case: nothing changes
    assert p["changes"] == {}
    with pytest.raises(ToolError, match="comment"):
        proposals.decide(con, p["proposal_id"], "reject")
    r = proposals.decide(con, p["proposal_id"], "reject", comment="that is the base model", reviewer="eng")
    assert r["status"] == "rejected" and r["comment"] == "that is the base model"
    p2 = proposals.propose(cfg, con, "REJ", case=0, digits=2)
    assert p2["proposal_id"] != p["proposal_id"]
    with pytest.raises(ToolError, match="accepted proposal"):
        proposals.confirm(con, p2["proposal_id"], truth)


def test_confirm_reports_a_difference(cfg, hello_deck, tmp_path):
    """A rebuilt deck that isn't the proposal (here: the modelers used nw 2.5) is a finding, not a pass."""
    cfg.hm_parallel = 4
    con = db.connect(cfg.database)
    truth = _truth_run(cfg, con, hello_deck, tmp_path)
    s = jobs.setup(con, "DIFF", hello_deck, PARAMS, source="run", truth_run_id=truth)
    h = jobs.get_hm(con, s["hm_id"])
    jobs.Runner(cfg, jobs.create_job(con, h, "base", {"values": TRUTH, "method": "proposal"})).run()
    p = proposals.propose(cfg, con, "DIFF")
    proposals.decide(con, p["proposal_id"], "accept", reviewer="eng")
    ids = _modelers_build(cfg, con, hello_deck, {"kx_L1": 0.5, "nw": 2.5})
    run = _deck_and_run(cfg, con, hello_deck, ids, "HELLO_OFF")
    c = proposals.confirm(con, p["proposal_id"], run)
    assert c["status"] == "differs" and c["differs"], c
    assert "differs" in c["spoken_summary"]


def test_match_task_skipped_without_history(cfg):
    con = db.connect(cfg.database)
    name = _study_at_match(cfg, con, "NOHIST")
    r = studies.complete_task(con, name, "match", {"skipped": "no production history"})
    assert {t["task"]: t["status"] for t in r["tasks"]}["match"] == "done"
    # and a study completes without ever starting match
    studies.init_study(con, cfg.studies_dir, "PLAIN", "no history match")
    sid = con.execute("SELECT id FROM studies WHERE name = 'PLAIN'").fetchone()[0]
    con.execute("UPDATE study_tasks SET status = 'done' WHERE study_id = ? AND task NOT IN ('view', 'match')", (sid,))
    con.execute("UPDATE study_tasks SET status = 'ready' WHERE study_id = ? AND task IN ('view', 'match')", (sid,))
    end = studies.complete_task(con, "PLAIN", "view", {"opened": "x"})
    assert end["study"]["status"] == "complete"


def test_old_database_gets_the_match_task(tmp_path):
    """A database from before history matching: study_tasks / study_requests are rebuilt with 'match', rows kept."""
    import sqlite3
    from opmagent.core.config import Config
    c = Config(database=tmp_path / "old.db", file_store=tmp_path / "f")
    db.init_db(c.database)
    con = sqlite3.connect(c.database)
    con.executescript("""
        DROP INDEX study_tasks_match;
        INSERT INTO studies(name, requirement, spec_json, folder) VALUES ('OLD', 'r', '{}', '.');
        INSERT INTO study_tasks(study_id, task, status) VALUES (1, 'data', 'done');
    """)
    con.commit()
    con.close()
    con = db.connect(c.database)          # runs the migration again (its index was dropped)
    assert con.execute("SELECT task, status FROM study_tasks").fetchall()[0][:] == ("data", "done")
    con.execute("INSERT INTO study_tasks(study_id, task) VALUES (1, 'match')")
    st = studies.status(con, "OLD")      # only the tasks it has
    assert [t["task"] for t in st["tasks"]] == ["data", "match"]


def test_proposal_runs_even_when_the_deck_has_a_run(cfg, hello_run, tmp_path):
    """The base case takes the deck's own run; a proposal's case (other values) must run as a variant, not take
    the base run's results (found while capturing the user guide: misfit 1.2 = the base, not ~0)."""
    cfg.hm_parallel = 4
    con = db.connect(cfg.database)
    deck = con.execute("SELECT deck_id FROM runs WHERE id = ?", (hello_run,)).fetchone()[0]
    truth = _truth_run(cfg, con, deck, tmp_path)
    s = jobs.setup(con, "WITHRUN", deck, PARAMS, source="run", truth_run_id=truth)
    assert s["base_run_id"] == hello_run
    h = jobs.get_hm(con, s["hm_id"])
    jobs.Runner(cfg, jobs.create_job(con, h, "base")).run()
    jobs.Runner(cfg, jobs.create_job(con, h, "base", {"values": TRUTH, "method": "proposal"})).run()
    p = proposals.propose(cfg, con, "WITHRUN")
    assert p["values"] == TRUTH and p["misfit"] < 1e-6 < p["base_misfit"], p
    rows = {r["label"]: r for r in con.execute("SELECT label, run_id, misfit FROM hm_cases WHERE hm_id = ?",
                                                (h["id"],))}
    assert rows["base"]["run_id"] == hello_run and rows["proposal"]["run_id"] is None
