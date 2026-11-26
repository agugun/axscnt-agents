"""Approve and reject in the review workbench (design decision 1), with its safeguards: only a workbench started from
a terminal, the token, a typed confirmation, and no approval while a "change" comment on the version is open."""
import json

import pytest

from opmagent.core import db
from opmagent.geo.review_app import ReviewApp
from opmagent.servers.data_server import DataService, wrap

pytest.importorskip("lasio")
from tests.test_geo_qc_notebook import build  # noqa: E402


def post(app, path, body, token="T"):
    st, _, out = app.handle("POST", path, {"host": "", "x-review-token": token}, json.dumps(body).encode())
    return st, json.loads(out)


def get(app, path):
    st, _, out = app.handle("GET", path, {"host": ""})
    return st, json.loads(out)


def test_approve_and_reject_in_the_workbench(cfg, tmp_path):
    v1 = build(cfg, tmp_path)["version_id"]
    con = db.connect(cfg.database)

    # started without a terminal (an agent): no decisions here, the commands instead
    agent_app = ReviewApp(cfg, v1, token="T")
    st, m = get(agent_app, f"/api/meta?version_id={v1}")
    assert m["decision"]["can_decide"] is False and m["review_commands"]
    st, r = post(agent_app, "/api/decide", {"version_id": v1, "action": "approve", "confirm": "FieldG_Geo v1"})
    assert st == 400 and r["code"] == "FORBIDDEN" and "without a terminal" in r["message"]

    app = ReviewApp(cfg, v1, token="T", can_decide=True)
    st, m = get(app, f"/api/meta?version_id={v1}")
    assert m["decision"] == {"can_decide": True, "status": "qc_ready", "confirm_text": "FieldG_Geo v1",
                             "blockers": [], "open_other": []}
    # the token, the typed confirmation
    assert post(app, "/api/decide", {"action": "approve", "confirm": "FieldG_Geo v1"}, token="no")[0] == 403
    st, r = post(app, "/api/decide", {"action": "approve", "confirm": "FieldG_Geo v2"})
    assert st == 400 and "to confirm" in r["message"]
    # a change comment blocks approval; a question only shows
    c = post(app, "/api/comments", {"comment": "PORO at B too high", "anchor": {"well": "B"}})[1]["feedback"]["id"]
    q = post(app, "/api/comments", {"comment": "why 5 layers?", "severity": "question"})[1]["feedback"]["id"]
    st, comments = get(app, f"/api/comments?version_id={v1}")
    assert comments["decision"]["blockers"] == [c] and comments["decision"]["open_other"] == [q]
    st, r = post(app, "/api/decide", {"action": "approve", "confirm": "FieldG_Geo v1"})
    assert st == 400 and f"#{c}" in r["message"]
    # answered (won't fix): approval goes through the reviewer profile, against the QC report
    geo = DataService("geo", cfg)
    assert wrap(geo, "answer_review_feedback")(feedback_id=c, response="matches B's logs", status="wontfix")["ok"]
    st, r = post(app, "/api/decide", {"action": "approve", "confirm": "FieldG_Geo v1"})
    assert st == 200 and r["header"]["status"] == "approved", r
    a = dict(con.execute("SELECT * FROM approvals WHERE model_kind = 'geo' AND version_id = ?", (v1,)).fetchone())
    qc = DataService("reviewer", cfg).get_qc_report(kind="geo", version_id=v1)["qc_report"]
    assert a["decision"] == "approved" and a["qc_report_file_id"] == qc["id"] and a["reviewer"]
    assert con.execute("SELECT COUNT(*) FROM audit_log WHERE tool = 'approve' AND outcome = 'ok'").fetchone()[0] == 1
    st, r = post(app, "/api/decide", {"action": "approve", "confirm": "FieldG_Geo v1"})
    assert st == 400 and "not waiting for review" in r["message"]
    st, m = get(app, f"/api/meta?version_id={v1}")
    assert m["status"] == "approved" and "review_commands" not in m

    # reject: needs a comment, which goes to the modeler like a terminal rejection
    v2 = build(cfg, tmp_path, name="FieldG_Geo")["version_id"]
    app2 = ReviewApp(cfg, v2, token="T", can_decide=True)
    st, r = post(app2, "/api/decide", {"action": "reject", "confirm": "FieldG_Geo v2"})
    assert st == 400 and "needs a comment" in r["message"]
    st, r = post(app2, "/api/decide", {"action": "reject", "confirm": "FieldG_Geo v2", "comment": "same as v1: why?"})
    assert st == 200 and r["header"]["status"] == "rejected"
    f = wrap(geo, "get_review_feedback")()["feedback"]
    assert any(x["item"] == "rejected" and x["comment"] == "same as v1: why?" and x["version_id"] == v2 for x in f)


def test_res_decides_the_same_way(cfg, tmp_path):
    from opmagent.res.pipeline import build_res_model
    from tests.test_review_res_well import RES_SPEC
    g = build(cfg, tmp_path)
    r = build_res_model(cfg, spec=RES_SPEC, name="FieldG_Res", geo_version_id=g["version_id"])
    app = ReviewApp(cfg, r["version_id"], token="T", kind="res", can_decide=True)
    st, m = get(app, f"/api/meta?version_id={r['version_id']}")
    assert m["decision"]["confirm_text"] == "FieldG_Res v1"
    st, out = post(app, "/api/decide", {"action": "approve", "confirm": "FieldG_Res v1"})
    assert st == 200 and out["header"]["status"] == "approved", out
