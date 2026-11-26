"""Geo review workbench, phase 2 (docs/design/geo-review.md): threads, proposals, the build from a proposal, the
version diff and the live review session, as one loop between the engineer (workbench) and the geo agent (tools)."""
import json
import threading
import time

import pytest

from opmagent.core import db, proposals, review
from opmagent.geo.review_app import ReviewApp
from opmagent.servers.data_server import TOOLS, DataService, wrap

pytest.importorskip("lasio")
from tests.test_geo_qc_notebook import build  # noqa: E402


def post(app, path, body, token="T"):
    st, _, out = app.handle("POST", path, {"host": "", "x-review-token": token}, json.dumps(body).encode())
    return st, json.loads(out)


def get(app, path):
    st, _, out = app.handle("GET", path, {"host": ""})
    return st, json.loads(out)


@pytest.fixture
def loop(cfg, tmp_path):
    g = build(cfg, tmp_path)
    assert g["ok"] and g["status"] == "qc_ready", g
    return g["version_id"], ReviewApp(cfg, g["version_id"], token="T"), DataService("geo", cfg)


def tool(svc, name, **kw):
    return wrap(svc, name)(**kw)


def test_review_loop(cfg, loop):
    v1, app, geo = loop
    # 1. the engineer pins a question on B in layer 2
    st, r = post(app, "/api/comments", {"comment": "Is B's layer 2 porosity from the logs or the kriging?",
                                        "severity": "question", "anchor": {"cell": [9, 3, 2], "kw": "PORO", "well": "B"},
                                        "view": {"fig": "layer_map", "kw": "PORO", "layer": 2}})
    assert st == 200, r
    fid = r["feedback"]["id"]
    assert review.get(app.con, fid)["waiting_on"] == "modeler"

    # 2. the agent is listening: the comment wakes it at once
    w = tool(geo, "await_review_activity", version_id=v1, timeout_s=5)
    assert w["event"] == "waiting_on_you" and [t["id"] for t in w["threads"]] == [fid], w
    assert get(app, f"/api/session?version_id={v1}")[1]["listening"] is True

    # 3. it replies with a view the engineer can open; the thread now waits on the engineer
    bad = tool(geo, "reply_review", feedback_id=fid, body="see", views=[{"fig": "layer_map", "kw": "BOGUS"}])
    assert bad["code"] == "INVALID_INPUT"
    r = tool(geo, "reply_review", feedback_id=fid, body="From the logs: B's upscaled phi is 0.218 in layer 2; the "
             "shaly interval keeps it high with the 0.5 Vsh cut-off.",
             views=[{"fig": "well", "well": "B"}])
    assert r["ok"] and r["message"]["views"][0]["fig"] == "well", r
    assert review.get(app.con, fid)["waiting_on"] == "reviewer"
    # nothing waits on the agent now: the wait times out
    t0 = time.monotonic()
    assert tool(geo, "await_review_activity", version_id=v1, timeout_s=1)["event"] == "timeout"
    assert time.monotonic() - t0 < 10

    # 4. the engineer answers while the agent waits (on its own connection, outside the server lock)
    got = {}
    th = threading.Thread(target=lambda: got.update(tool(geo, "await_review_activity", version_id=v1, timeout_s=20)))
    th.start()
    time.sleep(0.5)
    assert tool(geo, "list_geo_proposals")["ok"]          # other tools still answer while it waits
    assert post(app, "/api/messages", {"feedback_id": fid, "body": "Use a 0.4 cut-off then."})[0] == 200
    th.join(30)
    assert got["event"] == "waiting_on_you" and got["threads"][0]["last"]["body"] == "Use a 0.4 cut-off then.", got

    # 5. the agent proposes; checks first
    assert tool(geo, "propose_geo_change", base_version_id=v1, feedback_ids=[fid], spec_patch={"surfaces": ["X"]},
                rationale="x")["code"] == "INVALID_INPUT"
    assert tool(geo, "propose_geo_change", base_version_id=v1, feedback_ids=[], spec_patch={"petro": {"vsh_cutoff": 0.4}},
                rationale="x")["code"] == "INVALID_INPUT"
    assert tool(geo, "propose_geo_change", base_version_id=v1, feedback_ids=[fid], spec_patch={"layers": [3]},
                rationale="x")["code"] == "INVALID_INPUT"
    p1 = tool(geo, "propose_geo_change", base_version_id=v1, feedback_ids=[fid], spec_patch={"petro": {"vsh_cutoff": 0.45}},
              rationale="first try")["proposal"]
    p = tool(geo, "propose_geo_change", base_version_id=v1, feedback_ids=[fid],
             spec_patch={"petro": {"vsh_cutoff": 0.4}, "layers": [4, 2]}, rationale="as asked, and one more layer up top",
             expected="B layer 2 PORO a little lower")
    assert p["ok"], p
    p = p["proposal"]
    assert proposals.get(app.con, p1["id"])["status"] == "superseded"
    assert {c["key"] for c in p["changes"]} == {"petro.vsh_cutoff", "layers"}
    # only the engineer decides; only an accepted proposal is built
    assert "decide_geo_proposal" not in TOOLS["geo"] and "propose_geo_change" not in TOOLS["reviewer"]
    from opmagent.geo.proposal_build import build_from_proposal
    with pytest.raises(Exception, match="only an accepted proposal"):
        build_from_proposal(cfg, p["id"])
    assert post(app, "/api/proposals/decide", {"proposal_id": p["id"], "action": "accept"}, token="nope")[0] == 403
    st, r = post(app, "/api/proposals/decide", {"proposal_id": p["id"], "action": "accept"})
    assert st == 200 and r["proposal"]["status"] == "accepted", r
    assert review.get(app.con, fid)["waiting_on"] == "modeler"

    # 6. the agent builds it: the base version's inputs, the merged spec, the proposal recorded and marked built
    b = build_from_proposal(cfg, p["id"])
    assert b["ok"] and b["status"] == "qc_ready" and b["grid"] == "10 x 10 x 6", b
    v2 = b["version_id"]
    assert any("geo proposal" in a for a in b["assumptions"])
    pb = proposals.get(app.con, p["id"])
    assert pb["status"] == "built" and pb["built_version_id"] == v2
    with pytest.raises(Exception, match="built"):
        build_from_proposal(cfg, p["id"])                 # once

    # 7. the diff: compared with the base by default; the layering changed, so maps compare by zone
    d = tool(geo, "review_diff", version_id=v2)
    assert d["ok"], d
    d = d["diff"]
    assert d["base_version_id"] == v1 and d["maps"] == "zone"
    assert {s["key"] for s in d["spec"]} == {"petro.vsh_cutoff", "layers"}
    nums = {n["key"]: n for n in d["numbers"]}
    assert nums["cells"]["old"] == 500 and nums["cells"]["new"] == 600 and nums["cells"]["change"] == 100
    assert [t["id"] for t in d["threads"]] == [fid] and d["proposals"][0]["id"] == p["id"]
    st, r = post(app, "/api/view", {"spec": {"fig": "diff_map", "version_id": v2, "base_version_id": v1, "kw": "PORO",
                                             "zone": "TopRes"}})
    assert st == 200 and "minus" in r["figure"]["layout"]["title"]["text"], r
    st, r = post(app, "/api/view", {"spec": {"fig": "diff_map", "version_id": v2, "base_version_id": v1, "kw": "PORO",
                                             "zone": "Nope"}})
    assert st == 400
    s = tool(geo, "render_review_view", spec={"fig": "diff_map", "version_id": v2, "base_version_id": v1, "kw": "PORO",
                                             "zone": "TopRes"})
    assert s["ok"] and set(s["at_wells"]) == {"A", "B", "C", "D"}, s
    st, r = get(app, f"/api/diff?version_id={v2}")
    assert st == 200 and r["diff"]["base_version_id"] == v1

    # 8. it answers the thread with v2; the engineer reopens it by writing; the history stays
    assert tool(geo, "answer_review_feedback", feedback_id=fid, response="v2 uses Vsh 0.4 and 4 layers in TopRes",
                status="addressed", addressed_in_version_id=v2)["ok"]
    assert review.get(app.con, fid)["waiting_on"] is None
    assert tool(geo, "reply_review", feedback_id=fid, body="more")["code"] == "INVALID_INPUT"   # closed
    assert post(app, "/api/messages", {"feedback_id": fid, "body": "Layer 3 still looks high; check again."})[0] == 200
    t = review.thread(app.con, fid)
    assert t["status"] == "open" and t["response"] is None and t["waiting_on"] == "modeler"
    assert [m["kind"] for m in t["messages"]] == ["message", "message", "proposal", "proposal", "accepted", "built",
                                                  "answer", "reopen"], [m["kind"] for m in t["messages"]]
    # messages can't be changed or deleted
    with pytest.raises(Exception, match="cannot be changed"):
        app.con.execute("UPDATE review_messages SET body = 'x' WHERE feedback_id = ?", (fid,))
    with pytest.raises(Exception, match="cannot be deleted"):
        app.con.execute("DELETE FROM review_messages WHERE feedback_id = ?", (fid,))

    # 9. the engineer ends the session: a waiting agent hears it
    assert tool(geo, "answer_review_feedback", feedback_id=fid, response="checked: layer 3 matches B's logs",
                status="wontfix")["ok"]
    got.clear()
    th = threading.Thread(target=lambda: got.update(tool(geo, "await_review_activity", version_id=v1, timeout_s=20)))
    th.start()
    time.sleep(0.5)
    assert post(app, "/api/session/end", {"version_id": v2})[1]["ended"] is True
    th.join(30)
    assert got["event"] == "session_ended", got
    assert get(app, f"/api/session?version_id={v2}")[1]["listening"] is False


def test_rejection_and_status_rules(cfg, loop):
    v1, app, geo = loop
    fid = post(app, "/api/comments", {"comment": "kv/kh looks too high", "anchor": {"layer": 4}})[1]["feedback"]["id"]
    p = tool(geo, "propose_geo_change", base_version_id=v1, feedback_ids=[fid], spec_patch={"petro": {"kv_kh": 0.05}},
             rationale="halve it")["proposal"]
    assert post(app, "/api/proposals/decide", {"proposal_id": p["id"], "action": "reject"})[0] == 400   # comment needed
    st, r = post(app, "/api/proposals/decide", {"proposal_id": p["id"], "action": "reject", "comment": "0.08, not 0.05"})
    assert st == 200 and r["proposal"]["status"] == "rejected"
    t = review.thread(app.con, fid)
    assert t["messages"][-1]["kind"] == "rejected" and "0.08" in t["messages"][-1]["body"] and t["waiting_on"] == "modeler"
    # the database holds the status rules too
    with pytest.raises(Exception, match="proposal status"):
        app.con.execute("UPDATE model_proposals SET status = 'accepted' WHERE id = ?", (p["id"],))
    with pytest.raises(Exception, match="cannot be changed"):
        app.con.execute("UPDATE model_proposals SET spec_patch_json = '{}' WHERE id = ?", (p["id"],))
    # one accepted proposal per version blocks new ones until it is built
    p2 = tool(geo, "propose_geo_change", base_version_id=v1, feedback_ids=[fid], spec_patch={"petro": {"kv_kh": 0.08}},
              rationale="as asked")["proposal"]
    rv = DataService("reviewer", cfg)
    assert tool(rv, "decide_geo_proposal", proposal_id=p2["id"], action="accept")["ok"]
    r = tool(geo, "propose_geo_change", base_version_id=v1, feedback_ids=[fid], spec_patch={"petro": {"kv_kh": 0.07}},
             rationale="changed my mind")
    assert r["code"] == "INVALID_INPUT" and "build it first" in r["message"]
    # the engineer sees the thread in the CLI's terms too
    th = tool(rv, "get_review_thread", feedback_id=fid)
    assert th["thread"]["waiting_on"] == "modeler" and "accepted" in th["thread"]["messages"][-1]["body"]


def test_migration_adds_the_tables(cfg):
    con = db.connect(cfg.database)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert {"review_messages", "model_proposals", "review_sessions", "model_proposals_one_open"} <= names
