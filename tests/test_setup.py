"""opmagent setup: workspace files, profile separation, reviewer kept out of the agents' config."""
import json

from opmagent.cli import main


def test_setup_workspace(tmp_path):
    ws = tmp_path / "ws"
    main(["setup", str(ws)])
    mcp = json.loads((ws / ".mcp.json").read_text())["mcpServers"]
    assert {"opm-data-fluid", "opm-data-geo", "opm-data-well", "opm-data-import", "opm-data-deck", "opm-data-run",
            "opm-pvt", "opm-sim"} <= set(mcp)
    assert not any("reviewer" in json.dumps(v) for v in mcp.values())
    assert "reviewer" in (ws / "reviewer-mcp.json").read_text()
    fluid = (ws / ".claude" / "agents" / "fluid-modeler.md").read_text()
    assert "mcp__opm-data-fluid" in fluid and "mcp__opm-data-geo" not in fluid
    assert (ws / ".claude" / "skills" / "fluid-modeler" / "SKILL.md").exists()
    assert (ws / "data" / "opm.db").exists() and (ws / "data" / "files" / "inbox").is_dir()
    main(["setup", str(ws)])   # idempotent


def test_setup_allows_opmagent_and_the_workspace_servers_never_the_reviewer(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}, "x": 1}))
    main(["setup", str(ws)])
    main(["setup", str(ws)])   # again: no duplicates
    s = json.loads((ws / ".claude" / "settings.json").read_text())
    allow = s["permissions"]["allow"]
    assert s["x"] == 1 and allow[0] == "Bash(ls:*)"            # what was there is kept
    assert "Bash(opmagent:*)" in allow and "mcp__opm-data-fluid" in allow and "mcp__opm-sim" in allow
    assert len(allow) == len(set(allow))
    assert not any("reviewer" in a for a in allow)
    # removing files asks first; so does the geo review workbench (it writes comments as the reviewer)
    assert s["permissions"]["ask"] == ["Bash(opmagent clean:*)", "Bash(opmagent geo review:*)",
                                       "Bash(opmagent res review:*)", "Bash(opmagent well review:*)"]
