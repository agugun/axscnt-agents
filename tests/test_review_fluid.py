"""The fluid review workbench: its QC report, comments and the decision, without views or threads; and
`opmagent review open <kind>`, the one command that opens any model in the workbench."""
import json

from opmagent import cli
from opmagent.geo.review_app import ReviewApp
from opmagent.servers.data_server import DataService, wrap
from tests.test_data_server import build_fluid, submit


def post(app, path, body, token="T"):
    st, _, out = app.handle("POST", path, {"host": "", "x-review-token": token}, json.dumps(body).encode())
    return st, json.loads(out)


def get(app, path):
    st, _, out = app.handle("GET", path, {"host": ""})
    return st, json.loads(out)


def test_fluid_in_the_workbench(cfg, qc_file):
    fluid = DataService("fluid", cfg)
    vid = build_fluid(fluid)
    r, _ = submit(fluid, vid, qc_file)
    assert r["ok"], r
    app = ReviewApp(cfg, vid, token="T", kind="fluid", can_decide=True)

    st, m = get(app, f"/api/meta?version_id={vid}")
    assert st == 200 and m["kind"] == "fluid" and m["views"] == [] and m["threads"] is False
    assert m["qc_report"] and not m["can_propose"] and not m["can_preview"]
    assert [v["version"] for v in m["versions"]] == [1] and m["decision"]["confirm_text"] == "FieldA_Oil v1"
    st, _, rep = app.handle("GET", f"/qc?version_id={vid}", {"host": ""})      # the report, in the QC report tab
    assert st == 200 and rep == b"<html>qc</html>"
    st, r = post(app, "/api/view", {"spec": {"fig": "phase_envelope"}})
    assert st == 400 and "QC report" in r["message"]

    # a comment (general, or on a finding); the fluid modeler answers it in its next version: no thread replies
    c = post(app, "/api/comments", {"comment": "Psat 2% high against the lab", "anchor": {"text": "Psat"}})[1]["feedback"]["id"]
    st, r = post(app, "/api/comments", {"comment": "x", "anchor": {"well": "B"}})
    assert st == 400 and "unknown key" in r["message"]
    st, r = post(app, "/api/messages", {"feedback_id": c, "body": "and the Bo?"})
    assert st >= 400 and ("no review threads" in r["message"] or "threads are for" in r["message"]), r
    st, cm = get(app, f"/api/comments?version_id={vid}")
    assert [x["id"] for x in cm["comments"]] == [c] and cm["decision"]["blockers"] == [c]

    # the decision, as for geo: blocked while the change comment is open
    st, r = post(app, "/api/decide", {"action": "approve", "confirm": "FieldA_Oil v1"})
    assert st == 400 and f"#{c}" in r["message"]
    assert wrap(fluid, "answer_review_feedback")(feedback_id=c, response="within the 2% tolerance", status="wontfix")["ok"]
    st, r = post(app, "/api/decide", {"action": "approve", "confirm": "FieldA_Oil v1"})
    assert st == 200 and r["header"]["status"] == "approved", r


def test_review_open_command(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_geo_review", lambda a: seen.append((getattr(a, "review_kind", None) or a.kind,
                                                                      a.version_id)))
    for argv in (["review", "open", "fluid", "3", "--no-open"], ["review", "open", "res", "2"], ["fluid", "review", "3"],
                 ["geo", "review", "4"]):
        cli.main(argv)
    assert seen == [("fluid", 3), ("res", 2), ("fluid", 3), ("geo", 4)]
