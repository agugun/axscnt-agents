"""Review feedback in the database: the reviewer comments, the modeler answers with the version that fixes it."""
import pytest

from opmagent.hello.models import build_geo
from opmagent.servers.data_server import TOOLS, DataService, wrap


def test_comment_answer_and_rules(cfg):
    call = lambda prof, tool, **k: wrap(DataService(prof, cfg), tool)(**k)  # noqa: E731
    g1 = build_geo(cfg, origin="built", name="F_Geo")
    c = call("reviewer", "add_review_comment", kind="geo", version_id=g1["version_id"], item="PERMX layer 3",
             comment="permeability too high in layer 3 compared with the core data")
    fid = c["feedback"]["id"]
    assert c["ok"] and c["feedback"]["status"] == "open"
    assert call("geo", "get_review_feedback")["feedback"][0]["id"] == fid
    assert "add_review_comment" not in TOOLS["geo"] and "add_review_comment" in TOOLS["reviewer"]
    assert call("fluid", "get_review_feedback", kind="geo")["code"] == "FORBIDDEN"
    assert call("fluid", "answer_review_feedback", feedback_id=fid, response="x")["code"] == "FORBIDDEN"
    assert call("geo", "answer_review_feedback", feedback_id=fid, response="done")["code"] == "INVALID_INPUT"  # no version
    g2 = build_geo(cfg, origin="built", name="F_Geo")
    a = call("geo", "answer_review_feedback", feedback_id=fid, addressed_in_version_id=g2["version_id"],
             response="perm transform recalibrated to core; layer 3 mean now 180 mD")
    assert a["ok"] and a["feedback"]["status"] == "addressed"
    assert call("geo", "get_review_feedback")["feedback"] == []
    assert call("geo", "get_review_feedback", status=None)["feedback"][0]["response"].startswith("perm")
    con = DataService("reviewer", cfg).con
    with pytest.raises(Exception, match="cannot be changed"):
        con.execute("UPDATE review_feedback SET comment = 'edited' WHERE id = ?", (fid,))


def test_rejection_comment_is_feedback(cfg, qc_file):
    call = lambda prof, tool, **k: wrap(DataService(prof, cfg), tool)(**k)  # noqa: E731
    g = build_geo(cfg, origin="built", name="R_Geo")
    rid = call("reviewer", "get_qc_report", kind="geo", version_id=g["version_id"])["qc_report"]["id"]
    assert call("reviewer", "reject", kind="geo", version_id=g["version_id"], qc_report_file_id=rid,
                comment="OWC is 20 m too deep")["ok"]
    f = call("geo", "get_review_feedback")["feedback"]
    assert len(f) == 1 and f[0]["item"] == "rejected" and f[0]["comment"] == "OWC is 20 m too deep"
