"""opmagent doctor: the condition of each agent in a workspace."""
import json
from pathlib import Path

import pytest

from opmagent.cli import main
from opmagent.core import config, db, studies
from opmagent.doctor import agents_report, format_report
from opmagent.setup import AGENTS

REPO_SKILLS = Path(__file__).resolve().parent.parent / "skills"


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    main(["setup", str(w)])
    (w / ".claude" / "settings.local.json").write_text(json.dumps({"enableAllProjectMcpServers": True}))
    return w


def report(w, **kw):
    return agents_report(w, config.load(str(w / "opmagent.toml")), REPO_SKILLS, **kw)


def agent(r, name):
    return next(a for a in r["agents"] if a["agent"] == name)


def check(r, name, what):
    return next(c for c in agent(r, name)["checks"] if c["check"] == what)


def test_fresh_workspace_is_ready(ws):
    sk = (ws / ".claude" / "skills" / "opm-doctor" / "SKILL.md").read_text()   # the skill that runs the doctor
    assert "name: opm-doctor" in sk and "opmagent doctor" in sk
    r = report(ws)
    assert r["status"] == "ok", format_report(r, verbose=True)
    assert {a["agent"] for a in r["agents"]} == set(AGENTS)
    assert check(r, "res-modeler", "server opm-data-res")["detail"] == "approved"
    assert "All agents are ready" in r["spoken_summary"]


def test_what_breaks_an_agent_is_named(ws):
    (ws / ".claude" / "agents" / "geo-modeler.md").unlink()
    skill = ws / ".claude" / "skills" / "well-modeler" / "SKILL.md"
    skill.write_text(skill.read_text() + "\nlocal edit\n")
    (ws / ".claude" / "settings.local.json").write_text(json.dumps(
        {"enabledMcpjsonServers": ["opm-data-fluid", "opm-pvt"], "disabledMcpjsonServers": ["opm-data-run"]}))
    r = report(ws)
    assert r["status"] == "fail"
    assert agent(r, "geo-modeler")["status"] == "fail" and "missing" in check(r, "geo-modeler", "installed")["detail"]
    assert check(r, "well-modeler", "skill")["status"] == "warn"
    assert check(r, "res-modeler", "server opm-data-res")["status"] == "warn"           # not approved yet
    assert check(r, "run-agent", "server opm-data-run")["status"] == "fail"             # disabled
    assert agent(r, "fluid-modeler")["status"] == "ok"
    assert "geo-modeler" in r["spoken_summary"]


def test_reviewer_in_the_agents_workspace_fails(ws):
    mcp = json.loads((ws / ".mcp.json").read_text())
    mcp["mcpServers"]["opm-reviewer"] = json.loads((ws / "reviewer-mcp.json").read_text())["mcpServers"]["opm-reviewer"]
    (ws / ".mcp.json").write_text(json.dumps(mcp))
    r = report(ws)
    assert r["status"] == "fail" and r["checks"][0]["check"] == "reviewer" and r["checks"][0]["status"] == "fail"


def test_activity_and_work(ws):
    cfg = config.load(str(ws / "opmagent.toml"))
    con = db.connect(cfg.database)
    studies.init_study(con, cfg.file_store / "studies", "FieldD", "doctor test")
    studies.start_task(con, "FieldD", "data")
    con.execute("INSERT INTO audit_log(tool, profile, args_sha256, outcome) VALUES ('write_tables', 'geo', 'x', 'DB_RULE')")
    con.execute("INSERT INTO audit_log(tool, profile, args_sha256, outcome) VALUES ('create_version', 'fluid', 'x', 'ok')")
    r = report(ws)
    assert "1 task" in check(r, "data-manager", "work")["detail"]
    assert check(r, "geo-modeler", "activity")["status"] == "warn" and "DB_RULE" in check(r, "geo-modeler", "activity")["detail"]
    assert "create_version" in check(r, "fluid-modeler", "activity")["detail"]
    v = format_report(r, verbose=True)
    assert "task FieldD data: in_progress" in v


def test_deep_starts_each_server(ws):
    r = report(ws, deep=True)
    c = check(r, "res-modeler", "server opm-data-res")
    assert c["status"] == "ok" and "starts" in c["detail"], c


def test_cli_exit_code(ws, capsys):
    with pytest.raises(SystemExit) as e:
        main(["--config", str(ws / "opmagent.toml"), "doctor", "--json"])
    out = capsys.readouterr().out
    assert '"agents"' in out
    (ws / ".claude" / "agents" / "deck-builder.md").unlink()
    with pytest.raises(SystemExit) as e2:
        main(["--config", str(ws / "opmagent.toml"), "doctor"])
    assert e2.value.code == 1 and "[FAIL] deck-builder" in capsys.readouterr().out
    assert e.value.code in (0, 1)   # 0 unless an environment prerequisite (java, flow) is missing here
