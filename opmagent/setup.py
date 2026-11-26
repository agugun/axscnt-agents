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
    "res-modeler": ("res", [], "Builds reservoir-engineering models on a geo grid (SCAL, rock compressibility, contacts and initial pressure, region arrays) and submits them for review."),
    "well-modeler": ("well", [], "Builds well models (wells, surveys, perforations, history, controls) and submits them for review."),
    "deck-importer": ("import", ["opm-sim"], "Imports existing OPM/Eclipse decks into models for review and verifies the round trip."),
    "deck-builder": ("deck", [], "Builds validated OPM decks from a simulation requirement and approved models."),
    "run-agent": ("run", ["opm-sim"], "Runs validated decks, stores and compares results, and prepares VS Code debug sessions."),
    "data-manager": ("data", [], "Loads a study's raw data from many sources (lab reports, LAS, tops, well tables, production history, Volve) into the database for the modelers, reverse-engineers existing decks into study data, and answers the modelers' data requests."),
    "results-viewer": ("view", [], "Opens finished runs in ResInsight: grid, properties over time and summary vectors."),
    "history-matcher": ("hm", ["opm-hm"], "Compares simulated with measured history (misfit per well and quantity), sets up history-match parameters, runs variant decks and sensitivities, and reports them. Never edits a model."),
}
SKILL_OF = {name: name for name in AGENTS}

CLAUDE_MD = """# OPM agents workspace

Agents build reservoir simulation inputs for OPM Flow. The SQLite database is the hub; agents never
talk to each other directly. Each agent uses only its own data-server profile.

## Studies
Work for one simulation question is a **study** (`opmagent study status <name>`): a folder with an inbox and
a task chain data -> fluid, geo -> res, well -> deck -> run -> view (and, optional, run -> match: the history
matcher) on a shared board. Each agent starts and completes
only its own task (`start_task`, `complete_task`), and asks another task for missing or poor data with
`raise_request` (to `data`, the data manager), which blocks it until the request is resolved. In your own
session, the `study` skill coordinates: it hands each agent its ready task. Outside a study, work as before.

In a study the database holds everything:
- **Raw data.** The data manager converts tabular data into the study data tables. It files the rest as it came (LAS, SEG-Y, surfaces, documents) in `<study>/data/` and catalogs those files in the database.
- **Models.** The modelers read both through the data tools (`study_data`, `study_data_rows`, `log_stats`, `log_samples`, `fluid_sample`, `study_files`, `check_study_data`), interpret the data, and build from it. Never open or edit files in a study's `data/` folder yourself.
- **Review feedback.** The reviewer's comments are in `review_feedback`. A modeler answers them (`get_review_feedback`, `answer_review_feedback`) with the version that addresses them. Geo, res and well comments from the review workbench (`opmagent geo|res|well review`) have threads (`get_review_thread`, `reply_review`) and geo and res have proposals the engineer accepts before a build (`preview_geo_change` / `preview_res_change` to see what a change does, `propose_geo_change` / `propose_res_change`, then `--from-proposal`; well changes are data requests); a review session is `await_review_activity` in a loop.

## Rules for every agent
- Numbers come from the opmagent tools, never from you. Never invent data; ask for what's missing.
- You can't approve or reject models. Only the user can, from their own client (reviewer profile)
  or with `opmagent review` in a terminal. With `[study] auto_approve = true` in opmagent.toml, a model
  that passes its validator is approved automatically and recorded as "auto (approval disabled)".
- Read back before anything that changes data or starts a job (import, run, debug, clean-up) and wait for "yes".
- Answers are short enough to read aloud: two or three sentences, numbers rounded. Every tool result has a
  `spoken_summary` you can use.
- New input files go in `data/files/inbox/`; find them by spoken name with `find_inbox_file`.
- Units: METRIC unless an imported deck says FIELD.

## Agents
{agents}

Quick check of the whole install: `opmagent hello` (see the hello skill).
"""


def _resinsight_line() -> str:
    """[viewer] resinsight_bin: left out (commented) when ResInsight is found without it (PATH, /opt/ResInsight)."""
    from .viewer import find_resinsight
    found = find_resinsight()
    if found:
        return f'# resinsight_bin = "{found}"   # found on PATH or in /opt; set it only to use another one'
    return '# resinsight_bin = "/opt/ResInsight/bin/resinsight"   # not found: install ResInsight there, or set this'


def _permissions(ws: Path, servers: dict) -> Path:
    """.claude/settings.json: allow the agents' own commands without a prompt each time: `opmagent ...` in Bash and the
    workspace's MCP servers. Kept: what the user added there. Never the reviewer server (it isn't in .mcp.json), and
    approving or rejecting still needs a person typing at an interactive terminal."""
    p = ws / ".claude" / "settings.json"
    s = json.loads(p.read_text()) if p.is_file() else {}
    perms = s.setdefault("permissions", {})
    want = ["Bash(opmagent:*)"] + [f"mcp__{name}" for name in sorted(servers) if "reviewer" not in name]
    perms["allow"] = list(dict.fromkeys(list(perms.get("allow", [])) + want))
    # removing files asks you first, even though opmagent is allowed (in Claude Code, ask wins over allow); so does the
    # geo review workbench, which writes comments as the reviewer
    perms["ask"] = list(dict.fromkeys(list(perms.get("ask", [])) + ["Bash(opmagent clean:*)", "Bash(opmagent geo review:*)", "Bash(opmagent res review:*)",
                                                         "Bash(opmagent well review:*)"]))
    p.write_text(json.dumps(s, indent=2) + "\n")
    return p


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

[study]
# true: a model that passes its validator is approved automatically (recorded as "auto (approval disabled)")
auto_approve = false

[viewer]
{_resinsight_line()}
""")
        created.append(str(cfg_path))
    c = str(cfg_path)
    servers = {f"opm-data-{prof}": {"command": _bin("opmagent-data"), "args": ["--profile", prof, "--config", c]}
               for prof in sorted({p for p, _, _ in AGENTS.values()})}
    servers["opm-pvt"] = {"command": _bin("opmagent-pvt"), "args": ["--config", c]}
    servers["opm-sim"] = {"command": _bin("opmagent-sim"), "args": ["--config", c]}
    servers["opm-hm"] = {"command": _bin("opmagent-hm"), "args": ["--config", c]}
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
        if profile in ("fluid", "geo", "well"):
            tools[3:3] = ["NotebookEdit"]   # writes the analysis cells of its investigation notebooks
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
    created.append(str(_permissions(ws, servers)))
    created += [str(agents_dir), str(skills_dir), str(ws / "CLAUDE.md")]
    return {"workspace": str(ws), "config": c, "created": created, "servers": sorted(servers), "reviewer": str(ws / "reviewer-mcp.json")}
