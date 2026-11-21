"""`opmagent doctor --agents`: the condition of each agent in a workspace.

For every agent `opmagent setup` creates, it checks what the agent needs to work in Claude Code, and what it is
doing in the database:

  installed   .claude/agents/<agent>.md exists and names its MCP servers in `tools:`
  skill       .claude/skills/<skill>/SKILL.md exists and is the current version from the repo
  servers     each server the agent uses is in .mcp.json, its command exists, and Claude Code has approved it
              (.claude/settings.local.json); with deep=True each server is started over stdio and lists its tools
  profile     its data-server profile loads and exposes the tools it should
  activity    its last write through the data server (audit_log), and failed writes in the last 24 hours
  work        study tasks waiting for it (ready, in progress, blocked), open requests to it, models of its kind
              waiting for review, runs in progress

Each agent gets a status: ok, warn (works, but something needs a look) or fail (it can't work as set up).
Workspace-wide checks: the reviewer server is not in the agents' workspace, and the database is current.
Read only: nothing is written or approved.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from .setup import AGENTS, SKILL_OF

ORDER = {"ok": 0, "warn": 1, "fail": 2}
MODEL_KIND = {"fluid-modeler": "fluid", "geo-modeler": "geo", "res-modeler": "res", "well-modeler": "well"}


def _sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def _worst(items) -> str:
    return max((i["status"] for i in items), key=ORDER.get, default="ok")


def _check(name: str, status: str, detail: str) -> dict:
    return {"check": name, "status": status, "detail": detail}


def servers_of(name: str) -> list[str]:
    profile, extra, _ = AGENTS[name]
    return [f"opm-data-{profile}"] + list(extra)


def _approval(settings: dict, server: str) -> tuple[str, str]:
    if server in (settings.get("disabledMcpjsonServers") or []):
        return "fail", "disabled in .claude/settings.local.json"
    if settings.get("enableAllProjectMcpServers") or server in (settings.get("enabledMcpjsonServers") or []):
        return "ok", "approved"
    return "warn", ("not approved in .claude/settings.local.json: Claude Code asks at the next start "
                    "(or add it to enabledMcpjsonServers)")


def _list_tools(spec: dict, timeout: float = 60.0) -> set[str]:
    """Start one MCP server over stdio as Claude Code would, and list its tools."""
    import asyncio

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def go():
        params = StdioServerParameters(command=spec["command"], args=spec.get("args", []), env=spec.get("env"))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name for t in (await s.list_tools()).tools}
    return asyncio.run(asyncio.wait_for(go(), timeout))


def _expected_tools(server: str) -> set[str] | None:
    if server.startswith("opm-data-"):
        from .servers.data_server import TOOLS
        return set(TOOLS.get(server[len("opm-data-"):], []))
    return None


def _activity(con, profile: str) -> tuple[dict, list[dict]]:
    last = con.execute("SELECT tool, outcome, at FROM audit_log WHERE profile = ? ORDER BY id DESC LIMIT 1",
                       (profile,)).fetchone()
    failed = con.execute("SELECT tool, outcome, at FROM audit_log WHERE profile = ? AND outcome <> 'ok' "
                         "AND at >= datetime('now', '-1 day') ORDER BY id DESC", (profile,)).fetchall()
    act = {"last": dict(last) if last else None, "failed_24h": len(failed)}
    checks = [_check("activity", "warn" if failed else "ok",
                     (f"{len(failed)} failed write(s) in the last 24 h, last: {failed[0]['tool']} -> "
                      f"{failed[0]['outcome']} ({failed[0]['at']} UTC)" if failed else
                      f"last write {last['tool']} ({last['outcome']}) at {last['at']} UTC" if last else
                      "no writes yet"))]
    return act, checks


def _work(con, name: str) -> tuple[dict, list[dict]]:
    from .core.studies import AGENT
    tasks = [t for t, a in AGENT.items() if a == name]
    work: dict = {"tasks": [], "requests": [], "waiting_review": [], "runs": []}
    if tasks:
        q = ",".join("?" * len(tasks))
        work["tasks"] = [dict(r) for r in con.execute(
            f"SELECT s.name AS study, t.task, t.status, t.updated_at FROM study_tasks t JOIN studies s ON s.id = "
            f"t.study_id WHERE s.status = 'active' AND t.task IN ({q}) AND t.status IN "
            f"('ready','in_progress','blocked','failed') ORDER BY t.updated_at", tasks)]
        work["requests"] = [dict(r) for r in con.execute(
            f"SELECT s.name AS study, r.id, r.from_task, r.kind, r.message FROM study_requests r JOIN studies s "
            f"ON s.id = r.study_id WHERE r.status = 'open' AND r.to_task IN ({q})", tasks)]
    kind = MODEL_KIND.get(name)
    if kind:
        work["waiting_review"] = [dict(r) for r in con.execute(
            f"SELECT model_name, id AS version_id, version, qc_ready_at FROM pending_review_{kind} ORDER BY qc_ready_at")]
    if name == "run-agent":
        for r in con.execute("SELECT r.id, d.name AS deck, r.pid, r.started_at FROM runs r JOIN decks d "
                             "ON d.id = r.deck_id WHERE r.status = 'running'"):
            alive = bool(r["pid"]) and Path(f"/proc/{r['pid']}").exists()
            work["runs"].append({**dict(r), "process": "alive" if alive else "gone"})
    checks = []
    failed = [t for t in work["tasks"] if t["status"] == "failed"]
    blocked = [t for t in work["tasks"] if t["status"] == "blocked"]
    gone = [r for r in work["runs"] if r["process"] == "gone"]
    if failed:
        checks.append(_check("work", "warn", "failed task(s): " + ", ".join(f"{t['study']} {t['task']}" for t in failed)))
    if gone:
        checks.append(_check("work", "warn", "run(s) marked running but the Flow process is gone: "
                             + ", ".join(f"run {r['id']}" for r in gone) + " (job_status refreshes them)"))
    parts = []
    for key, label in (("tasks", "task"), ("requests", "open request"), ("waiting_review", "model waiting for review"),
                       ("runs", "run in progress")):
        n = len(work[key])
        if n:
            parts.append(f"{n} {label}{'s' if n > 1 else ''}")
    if not checks:
        detail = ", ".join(parts) or "nothing waiting"
        if blocked:
            detail += "; blocked in " + ", ".join(f"{t['study']} ({t['task']})" for t in blocked)
        checks.append(_check("work", "ok", detail))
    return work, checks


def agents_report(ws: Path, cfg, repo_skills: Path, *, deep: bool = False) -> dict:
    """The condition of every agent `opmagent setup` defines, in workspace `ws`."""
    ws = Path(ws).expanduser().resolve()
    mcp_path, settings_path = ws / ".mcp.json", ws / ".claude" / "settings.local.json"
    mcp = json.loads(mcp_path.read_text()).get("mcpServers", {}) if mcp_path.is_file() else {}
    settings = json.loads(settings_path.read_text()) if settings_path.is_file() else {}
    con = None
    if cfg is not None and cfg.database.exists():
        from .core import db
        con = db.connect(cfg.database)
    tool_cache: dict[str, set[str] | Exception] = {}
    agents = []
    for name, (profile, _extra, desc) in AGENTS.items():
        checks = []
        f = ws / ".claude" / "agents" / f"{name}.md"
        if not f.is_file():
            checks.append(_check("installed", "fail", f"{f.relative_to(ws)} missing: run opmagent setup {ws}"))
        else:
            head = f.read_text().split("---")[1] if f.read_text().startswith("---") else ""
            tools_line = next((ln for ln in head.splitlines() if ln.startswith("tools:")), "")
            missing = [s for s in servers_of(name) if f"mcp__{s}" not in tools_line]
            checks.append(_check("installed", "fail" if missing else "ok",
                                 f"tools: lacks {missing}" if missing else f"{f.relative_to(ws)}"))
        sk = SKILL_OF[name]
        have, want = ws / ".claude" / "skills" / sk / "SKILL.md", Path(repo_skills) / sk / "SKILL.md"
        if not have.is_file():
            checks.append(_check("skill", "fail", f"skill {sk} missing: run opmagent setup {ws}"))
        elif want.is_file() and _sha(have) != _sha(want):
            checks.append(_check("skill", "warn", f"skill {sk} differs from the repo's: run opmagent setup {ws} to update"))
        else:
            checks.append(_check("skill", "ok", f"{sk} (current)"))
        for s in servers_of(name):
            spec = mcp.get(s)
            if not spec:
                checks.append(_check(f"server {s}", "fail", f"not in .mcp.json: run opmagent setup {ws}"))
                continue
            cmd = spec.get("command", "")
            if not (shutil.which(cmd) or (os.path.isfile(cmd) and os.access(cmd, os.X_OK))):
                checks.append(_check(f"server {s}", "fail", f"command not found: {cmd}"))
                continue
            st, why = _approval(settings, s)
            detail = why
            if deep and st != "fail":
                if s not in tool_cache:
                    try:
                        tool_cache[s] = _list_tools(spec)
                    except Exception as e:  # noqa: BLE001
                        tool_cache[s] = e
                got = tool_cache[s]
                if isinstance(got, Exception):
                    st, detail = "fail", f"{why}; does not start: {type(got).__name__}: {got}"[:300]
                else:
                    exp = _expected_tools(s)
                    lack = sorted(exp - got) if exp else []
                    if lack:
                        st, detail = "fail", f"{why}; started but lacks tools {lack[:5]}"
                    else:
                        detail = f"{why}; starts, {len(got)} tools"
            checks.append(_check(f"server {s}", st, detail))
        try:
            from .servers.data_server import TOOLS
            n = len(TOOLS[profile])
            checks.append(_check("profile", "ok", f"{profile}: {n} tools"))
        except KeyError:
            checks.append(_check("profile", "fail", f"unknown data-server profile {profile}"))
        activity, work = {}, {}
        if con is not None:
            activity, c1 = _activity(con, profile)
            work, c2 = _work(con, name)
            checks += c1 + c2
        agents.append({"agent": name, "profile": profile, "description": desc, "status": _worst(checks),
                       "checks": checks, "activity": activity, "work": work})

    workspace = []
    if "opm-reviewer" in mcp or any(spec.get("args", [])[1:2] == ["reviewer"] for spec in mcp.values()):
        workspace.append(_check("reviewer", "fail", "the reviewer server (approve / reject) is in the agents' "
                                                     ".mcp.json: remove it; it belongs in your own client only"))
    else:
        workspace.append(_check("reviewer", "ok", "not in the agents' workspace"))
    unknown = sorted(set(mcp) - {s for n in AGENTS for s in servers_of(n)} - {"opm-reviewer"})
    if unknown:
        workspace.append(_check("servers", "warn", f".mcp.json has servers no agent uses: {unknown}"))
    if con is None:
        workspace.append(_check("database", "warn", "no database yet: activity and work not shown"))
    status = _worst([*workspace, *({"status": a["status"]} for a in agents)])
    bad = [a["agent"] for a in agents if a["status"] != "ok"]
    say = ("All agents are ready." if status == "ok" else
           f"{len(bad)} agent(s) need attention: {', '.join(bad)}.")
    return {"workspace": str(ws), "status": status, "agents": agents, "checks": workspace, "spoken_summary": say}


def format_report(r: dict, verbose: bool = False) -> str:
    mark = {"ok": "[ok]  ", "warn": "[warn]", "fail": "[FAIL]"}
    out = [f"\nAgents in {r['workspace']}"]
    w = max(len(a["agent"]) for a in r["agents"])
    for a in r["agents"]:
        act = (a.get("activity") or {}).get("last")
        last = f"last write {act['at'][5:16]}" if act else "no writes yet"
        work = next((c["detail"] for c in a["checks"] if c["check"] == "work"), "")
        out.append(f"{mark[a['status']]} {a['agent']:<{w}}  {last:<22} {work}")
        for c in a["checks"]:
            if verbose or c["status"] != "ok":
                if c["check"] in ("work",) and c["status"] == "ok":
                    continue
                out.append(f"         {mark[c['status']]} {c['check']}: {c['detail']}")
        if verbose:
            for t in (a.get("work") or {}).get("tasks", []):
                out.append(f"           task {t['study']} {t['task']}: {t['status']} (since {t['updated_at']})")
            for q in (a.get("work") or {}).get("requests", []):
                out.append(f"           request {q['id']} from {q['from_task']} ({q['study']}): {q['message'][:80]}")
            for m in (a.get("work") or {}).get("waiting_review", []):
                out.append(f"           review {m['model_name']} v{m['version']} (id {m['version_id']})")
            for x in (a.get("work") or {}).get("runs", []):
                out.append(f"           run {x['id']} {x['deck']} since {x['started_at']} (process {x['process']})")
    for c in r["checks"]:
        out.append(f"{mark[c['status']]} workspace {c['check']}: {c['detail']}")
    out.append(r["spoken_summary"])
    return "\n".join(out)
