"""Study board: task chain, dependencies, requests between agents, result checks, event log."""
import json

import pytest

from opmagent.core import db, studies
from opmagent.core.errors import ToolError

pytest.importorskip("lasio")
from tests.study_data import load_raw, write_raw  # noqa: E402


@pytest.fixture
def con(cfg):
    return db.connect(cfg.database)


def by_task(st):
    return {t["task"]: t["status"] for t in st["tasks"]}


def test_init_creates_folder_tasks_and_spec(cfg, con):
    st = studies.init_study(con, cfg.studies_dir, "FieldA_WF", "Waterflood for 20 years, 2 injectors", years=20,
                            report="quarterly", actor="eng")
    folder = cfg.studies_dir / "FieldA_WF"
    assert all((folder / sub).is_dir() for sub in studies.SUBFOLDERS)
    spec = json.loads((folder / "study.json").read_text())
    assert spec["years"] == 20 and spec["report"] == "quarterly" and spec["models"]["geo"] == "FieldA_WF_Geo"
    assert by_task(st) == {"data": "ready", "fluid": "waiting", "geo": "waiting", "well": "waiting",
                           "deck": "waiting", "run": "waiting", "view": "waiting"}
    assert st["next"] == [{"task": "data", "agent": "data-manager"}]
    with pytest.raises(ToolError, match="already exists"):
        studies.init_study(con, cfg.studies_dir, "FieldA_WF", "again")
    with pytest.raises(ToolError, match="requirement"):
        studies.init_study(con, cfg.studies_dir, "Other", "  ")


def test_data_done_unlocks_modelers_and_requests_block_and_reopen(cfg, con):
    studies.init_study(con, cfg.studies_dir, "S1", "base case")
    with pytest.raises(ToolError, match="waits for data"):
        studies.start_task(con, "S1", "fluid")
    studies.start_task(con, "S1", "data")
    with pytest.raises(ToolError, match="data isn't ready"):     # nothing loaded yet
        studies.complete_task(con, "S1", "data", {})
    assert load_raw(con, "S1")["all_ready"]
    st = studies.complete_task(con, "S1", "data", {})
    assert st["tasks"][0]["result"]["loaded"]["tops"] == 12
    assert st["unlocked"] == ["fluid", "geo", "well"] and by_task(st)["deck"] == "waiting"
    # the geo modeler finds the LAS files lack porosity: geo is blocked, the finished data task is reopened
    studies.start_task(con, "S1", "geo")
    st = studies.raise_request(con, "S1", "geo", "data", "missing_data", "LAS for B and D have no PHIE curve")
    rid = st["request_id"]
    assert by_task(st)["geo"] == "blocked" and by_task(st)["data"] == "ready" and len(st["open_requests"]) == 1
    with pytest.raises(ToolError, match="open request"):
        studies.complete_task(con, "S1", "data", {})
    with pytest.raises(ToolError, match="is for data"):
        studies.resolve_request(con, "S1", rid, "fixed", by_task="geo")
    st = studies.resolve_request(con, "S1", rid, "PHIE computed from density logs", by_task="data")
    assert by_task(st)["geo"] == "ready" and not st["open_requests"]
    studies.complete_task(con, "S1", "data", {})
    ev = [e["event"] for e in studies.events(con, "S1")]
    assert ev[:4] == ["created", "started", "loaded", "completed"] and "request" in ev and "unblocked" in ev
    with pytest.raises(Exception, match="append-only"):
        con.execute("DELETE FROM study_events WHERE id = 1")


def test_model_task_needs_an_approved_model(cfg, con):
    from opmagent.hello.models import build_geo
    studies.init_study(con, cfg.studies_dir, "S2", "base case")
    load_raw(con, "S2")
    studies.complete_task(con, "S2", "data", {})
    g = build_geo(cfg, origin="built", name="S2_Geo")           # submitted: qc_ready, not approved
    with pytest.raises(ToolError, match="qc_ready: approve it first"):
        studies.complete_task(con, "S2", "geo", {"version_id": g["version_id"]})
    with pytest.raises(ToolError, match="version_id"):
        studies.complete_task(con, "S2", "geo", {})
    with pytest.raises(ToolError, match="deck is waiting; start it first"):   # its models aren't done
        studies.complete_task(con, "S2", "deck", {"deck_id": 999})


def test_failed_task_can_be_retried(cfg, con):
    studies.init_study(con, cfg.studies_dir, "S3", "base case")
    st = studies.fail_task(con, "S3", "data", "inbox is empty")
    assert by_task(st)["data"] == "failed"
    assert by_task(studies.retry_task(con, "S3", "data"))["data"] == "ready"
    assert [s["name"] for s in studies.list_studies(con)] == ["S3"]


def test_auto_approve_and_profiles_act_on_their_own_task(cfg):
    from dataclasses import replace
    from opmagent.hello.models import build_geo
    from opmagent.servers.data_server import AUTO_REVIEWER, DataService, wrap
    cfg = replace(cfg, auto_approve=True)
    call = lambda prof, tool, **k: wrap(DataService(prof, cfg), tool)(**k)  # noqa: E731
    assert call("fluid", "init_study", name="S4", requirement="x")["code"] == "FORBIDDEN"   # deck builder only
    assert call("deck", "init_study", name="S4", requirement="base case", years=5)["ok"]
    assert call("data", "start_task", name="S4")["task"]["task"] == "data"
    con = DataService("reviewer", cfg).con
    write_raw(cfg.studies_dir / "S4" / "inbox")
    assert call("geo", "load_study_data", name="S4")["code"] == "FORBIDDEN"   # the data manager only
    assert call("data", "load_study_data", name="S4")["all_ready"]
    r = call("data", "complete_task", name="S4", result={})
    assert r["unlocked"] == ["fluid", "geo", "well"] and "Now ready" in r["spoken_summary"]
    g = build_geo(cfg, origin="built", name="S4_Geo")
    assert con.execute("SELECT status FROM geo_model_versions WHERE id=?", (g["version_id"],)).fetchone()[0] == "approved"
    assert con.execute("SELECT reviewer FROM approvals WHERE version_id=? AND model_kind='geo'",
                       (g["version_id"],)).fetchone()[0] == AUTO_REVIEWER
    call("geo", "start_task", name="S4")
    r = call("geo", "raise_request", name="S4", to_task="data", kind="improve_data", message="tops for C look shifted")
    rid = r["request_id"]
    assert call("fluid", "resolve_request", name="S4", request_id=rid, resolution="x")["code"] == "FORBIDDEN"
    assert call("data", "resolve_request", name="S4", request_id=rid, resolution="re-picked C tops")["ok"]
    call("data", "complete_task", name="S4", result={})
    call("geo", "start_task", name="S4")
    r = call("geo", "complete_task", name="S4", result={"version_id": g["version_id"]})
    assert r["ok"] and {t["task"]: t["status"] for t in r["tasks"]}["geo"] == "done"
    assert "study_status" in DataService.__dict__ and call("reviewer", "study_status", name="S4")["ok"]
