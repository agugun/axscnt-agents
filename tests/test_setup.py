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
