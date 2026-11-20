"""`opmagent setup`: create a local agents workspace for Claude Code.

    <workspace>/
      opmagent.toml                 config (paths inside the workspace unless given)
      .mcp.json                     MCP servers: one data-server profile per agent, PVT and simulation servers
      .claude/agents/*.md           one agent definition per role, limited to its own servers
      .claude/skills/*/SKILL.md     the skills
      CLAUDE.md                     shared rules for every agent
      reviewer-mcp.json             the reviewer server, for YOUR client only (not in .mcp.json)
      data/                         database and file store (inbox/ for new files)
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

AGENTS = {
    "fluid-modeler": ("fluid", ["opm-pvt"], "Builds black-oil fluid models from Excel PVT lab reports (NeqSim) and submits them for review."),
    "geo-modeler": ("geo", [], "Builds corner-point geo models from tops, LAS logs and a spec, and submits them for review."),
    "well-modeler": ("well", [], "Builds well models (wells, surveys, perforations, history, controls) and submits them for review."),
    "deck-importer": ("import", ["opm-sim"], "Imports existing OPM/Eclipse decks into models for review and verifies the round trip."),
    "deck-builder": ("deck", [], "Builds validated OPM decks from a simulation requirement and approved models."),
    "run-agent": ("run", ["opm-sim"], "Runs validated decks, stores and compares results, and prepares VS Code debug sessions."),
}
SKILL_OF = {"fluid-modeler": "fluid-modeler", "geo-modeler": "geo-modeler", "well-modeler": "well-modeler",
            "deck-importer": "deck-importer", "deck-builder": "deck-builder", "run-agent": "run-agent"}

CLAUDE_MD = """# OPM agents workspace

Agents build reservoir simulation inputs for OPM Flow. The SQLite database is the hub; agents never
talk to each other. Each agent uses only its own data-server profile.

## Rules for every agent
- Numbers come from the opmagent tools, never from you. Never invent data; ask for what's missing.
- You can't approve or reject models. Only the user can, from their own client (reviewer profile)
  or with `opmagent review` in a terminal.
- Read back before anything that changes data or starts a job (import, run, debug, clean-up) and wait for "yes".
- Answers are short enough to read aloud: two or three sentences, numbers rounded. Every tool result has a
  `spoken_summary` you can use.
- New input files go in `data/files/inbox/`; find them by spoken name with `find_inbox_file`.
- Units: METRIC unless an imported deck says FIELD.

## Agents
{agents}

Quick check of the whole install: `opmagent hello` (see the hello skill).
"""


def _bin(name: str) -> str:
    cand = Path(sys.executable).parent / name
    return str(cand) if cand.exists() else name


def write_workspace(ws: Path, *, repo_skills: Path, config: Path | None = None, force: bool = False) -> dict:
    ws = ws.expanduser().resolve()
    ws.mkdir(parents=True, exist_ok=True)
    cfg_path = Path(config).expanduser().resolve() if config else ws / "opmagent.toml"
    created = []
    if not cfg_path.exists():
        cfg_path.write_text(f"""[paths]
database   = "{ws / 'data' / 'opm.db'}"
file_store = "{ws / 'data' / 'files'}"
flow_bin   = "flow"

[defaults]
unit_system       = "METRIC"
eos               = "PR"
volume_shift      = true
pseudo_components = 6

[tolerances]
psat = 0.02
bo   = 0.02
rs   = 0.03
mu_o = 0.10

[simulation]
max_parallel = 2

[debug]
# opm_source_root = "~/src/opm"
# opm_debug_flow  = "~/src/opm/build/opm-simulators/bin/flow"
vscode_bin = "code"
""")
        created.append(str(cfg_path))
    c = str(cfg_path)
    servers = {f"opm-data-{prof}": {"command": _bin("opmagent-data"), "args": ["--profile", prof, "--config", c]}
               for prof in sorted({p for p, _, _ in AGENTS.values()})}
    servers["opm-pvt"] = {"command": _bin("opmagent-pvt"), "args": ["--config", c]}
    servers["opm-sim"] = {"command": _bin("opmagent-sim"), "args": ["--config", c]}
    mcp = ws / ".mcp.json"
    if mcp.exists() and not force:
        existing = json.loads(mcp.read_text())
        existing.setdefault("mcpServers", {}).update(servers)
        mcp.write_text(json.dumps(existing, indent=2))
    else:
        mcp.write_text(json.dumps({"mcpServers": servers}, indent=2))
    created.append(str(mcp))
    reviewer = {"mcpServers": {"opm-reviewer": {"command": _bin("opmagent-data"), "args": ["--profile", "reviewer", "--config", c]}}}
    (ws / "reviewer-mcp.json").write_text(json.dumps(reviewer, indent=2))
    created.append(str(ws / "reviewer-mcp.json"))

    agents_dir, skills_dir = ws / ".claude" / "agents", ws / ".claude" / "skills"
    agents_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, (profile, extra, desc) in AGENTS.items():
        tools = ["Bash", "Read", "Write", "Glob", "Grep", f"mcp__opm-data-{profile}"] + [f"mcp__{e}" for e in extra]
        body = f"""---
name: {name}
description: {desc}
tools: {", ".join(tools)}
---

You are the {name.replace('-', ' ')}. Follow the `{SKILL_OF[name]}` skill (.claude/skills/{SKILL_OF[name]}/SKILL.md)
and the workspace rules in CLAUDE.md. Use only the data server `opm-data-{profile}`{(" and " + ", ".join(extra)) if extra else ""}.
Run `opmagent` commands with `--config {c}` if the working directory isn't the workspace.
"""
        (agents_dir / f"{name}.md").write_text(body)
        lines.append(f"- **{name}** ({profile} profile): {desc}")
    for sk in sorted(Path(repo_skills).iterdir()):
        if (sk / "SKILL.md").exists():
            dst = skills_dir / sk.name
            dst.mkdir(exist_ok=True)
            shutil.copy2(sk / "SKILL.md", dst / "SKILL.md")
    (ws / "CLAUDE.md").write_text(CLAUDE_MD.format(agents="\n".join(lines)))
    created += [str(agents_dir), str(skills_dir), str(ws / "CLAUDE.md")]
    return {"workspace": str(ws), "config": c, "created": created, "servers": sorted(servers), "reviewer": str(ws / "reviewer-mcp.json")}
