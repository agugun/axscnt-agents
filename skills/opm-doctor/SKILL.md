---
name: opm-doctor
description: Check the condition of the OPM agents in this workspace - whether each agent is installed, its skill is current, its MCP servers are approved and start, what it last did, and what work is waiting for it (study tasks, requests, reviews, runs). Use when asked how the agents are, whether they are ready, why an agent doesn't respond or can't use its tools, or what is waiting in the workspace.
---

# OPM doctor

Reports the condition of every agent `opmagent setup` created in this workspace. Read only: it never changes files,
settings, studies or models.

## Run it
In the workspace folder:
```bash
opmagent doctor              # prerequisites, then one line per agent
opmagent doctor -v           # every check, and each agent's tasks, requests, reviews and runs
opmagent doctor --deep       # also start each MCP server and list its tools (about 10 s)
opmagent doctor --json       # the report as JSON, if you need to pick details out
```
Use `-v` by default when the user asks about one agent or something looks wrong; add `--deep` when an agent can't
call its tools. Outside the workspace pass `--config <workspace>/opmagent.toml` or `--workspace <folder>`.

## What it checks, per agent
- **installed**: `.claude/agents/<agent>.md` exists and names its MCP servers.
- **skill**: the workspace copy is the repo's current version.
- **servers**: each server is in `.mcp.json`, its command exists, and Claude Code has approved it
  (`.claude/settings.local.json`); with `--deep` it starts and lists the tools its profile should have.
- **activity**: its last write through the data server, and failed writes in the last 24 hours.
- **work**: study tasks waiting for it (ready, in progress, blocked, failed), open requests to it, models of its
  kind waiting for review, runs in progress or whose Flow process is gone.
- **workspace**: the reviewer server must not be in the agents' `.mcp.json`.

The prerequisites before it (config, Java, NeqSim, OPM parser, Flow, database) are the environment the servers need.

## Report
Lead with the answer: "All nine agents are ready" or "Two agents need attention: ...". Then, for each agent that
isn't ok, one line: what is wrong and the fix the doctor names. Then what is waiting, if anything (tasks, reviews,
runs). Don't list every ok check unless asked.

Fixes are the engineer's to make; say which one, don't do it yourself:
| Doctor says | Fix |
| --- | --- |
| agent file or skill missing, skill differs from the repo's | `opmagent setup <workspace>` again, then restart Claude Code |
| server not approved | approve it when Claude Code asks at start, or add it to `enabledMcpjsonServers` in `.claude/settings.local.json`, then restart |
| server disabled | remove it from `disabledMcpjsonServers` |
| command not found / does not start | the opmagent virtual environment moved or broke: reinstall (`pip install -e .`) and run setup again |
| reviewer server in the workspace | remove `opm-reviewer` from `.mcp.json`; it belongs only in the engineer's own client |
| failed writes | read the tool and error it names; usually a data or approval rule, explained by the agent's last answer |
| run marked running, process gone | ask the run agent for `job_status` of that run: it refreshes the status |
| a prerequisite FAIL (java, flow, database) | as printed: install it, or `opmagent init-db` for a missing database |

## Rules
- Never edit `.mcp.json`, `.claude/settings.local.json`, agent files or the database to "fix" what it reports.
- Never approve models or resolve requests because the doctor lists them; tell the user they are waiting.
- Keep the spoken answer to two or three sentences; offer the full `-v` output on screen.
