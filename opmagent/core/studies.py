"""Studies: the shared board the agents coordinate through.

A study is started by the deck builder (or `opmagent study init`) for one simulation question. It gets a folder

    <file_store>/studies/<name>/
      study.json        requirement, period, report frequency, model and deck names
      inbox/            raw data for this study (lab reports, LAS, tops, well tables, Volve exports ...)
      data/             the data manager's filed originals, as they came (logs/, seismic/, surfaces/, tables/ ...)
      deck/  runs/      deck builder and run agent output

The data manager converts the tabular data into the study data tables (data_*) and catalogs the files that stay
files (data_files, data_log_curves), see opmagent.data.load; the modelers read both through one layer
(opmagent.data.store) for their analysis and build their models from it. Models, review feedback and approvals
live in the same database.

and a fixed chain of tasks, one per agent:

    data ──► fluid ─┐
         ├─► geo ───┼─► deck ──► run ──► view
         └─► well ──┘

A task is `ready` when everything it depends on is `done`. An agent `start`s its ready task, and `complete`s it with
its result (a model version, a deck, a run); completing checks the result is real and unlocks the next tasks.
When an agent lacks data it raises a request to another task (usually the data manager): it is then `blocked`
until the request is resolved, and a finished data task is reopened to work on it. Every change is logged in
`study_events`. Nothing here calls an agent: a coordinator (the `study` skill) reads the board and hands out work.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .errors import FORBIDDEN, INVALID_INPUT, NOT_FOUND, ToolError

DEPENDS = {"data": [], "fluid": ["data"], "geo": ["data"], "well": ["data"],
           "deck": ["fluid", "geo", "well"], "run": ["deck"], "view": ["run"]}
TASKS = list(DEPENDS)
AGENT = {"data": "data-manager", "fluid": "fluid-modeler", "geo": "geo-modeler", "well": "well-modeler",
         "deck": "deck-builder", "run": "run-agent", "view": "results-viewer"}
MODEL_TASKS = {"fluid", "geo", "well"}
SUBFOLDERS = ["inbox", "data", "deck", "runs"]
REPORTS = ("monthly", "quarterly", "yearly")


def _row(r):
    return dict(r) if r is not None else None


def _event(con, study_id, task, event, detail=None, actor=None):
    con.execute("INSERT INTO study_events(study_id, task, event, detail, actor) VALUES (?,?,?,?,?)",
                (study_id, task, event, json.dumps(detail, default=str) if isinstance(detail, (dict, list)) else detail,
                 actor))


def _study(con, name: str) -> dict:
    s = _row(con.execute("SELECT * FROM studies WHERE name = ?", (name,)).fetchone())
    if not s:
        raise ToolError(NOT_FOUND, f"no study named {name}")
    s["spec"] = json.loads(s.pop("spec_json"))
    return s


def _task(con, study_id: int, task: str) -> dict:
    if task not in DEPENDS:
        raise ToolError(INVALID_INPUT, f"task must be one of {TASKS}")
    t = _row(con.execute("SELECT * FROM study_tasks WHERE study_id = ? AND task = ?", (study_id, task)).fetchone())
    t["result"] = json.loads(t["result_json"]) if t["result_json"] else None
    return t


def _set(con, study_id, task, status, note=None, result=None, actor=None, event=None):
    sets, args = ["status = ?", "updated_at = datetime('now')"], [status]
    if note is not None:
        sets.append("note = ?")
        args.append(note)
    if result is not None:
        sets.append("result_json = ?")
        args.append(json.dumps(result, default=str))
    con.execute(f"UPDATE study_tasks SET {', '.join(sets)} WHERE study_id = ? AND task = ?", (*args, study_id, task))
    con.execute("UPDATE studies SET updated_at = datetime('now') WHERE id = ?", (study_id,))
    _event(con, study_id, task, event or status, result or note, actor)


def default_spec(name: str, requirement: str, **kw) -> dict:
    spec = {"requirement": requirement, "start_date": kw.get("start_date") or date.today().replace(day=1).isoformat(),
            "report": kw.get("report") or "monthly", "unit_system": kw.get("unit_system") or "METRIC",
            "models": {"fluid": f"{name}_Oil", "geo": f"{name}_Geo", "well": f"{name}_Wells"}, "deck": name}
    if kw.get("end_date"):
        spec["end_date"] = kw["end_date"]
    else:
        spec["years"] = kw.get("years") or 10
    if kw.get("summary"):
        spec["summary"] = kw["summary"]
    if spec["report"] not in REPORTS:
        raise ToolError(INVALID_INPUT, f"report must be one of {REPORTS}")
    if spec["unit_system"] not in ("METRIC", "FIELD"):
        raise ToolError(INVALID_INPUT, "unit_system must be METRIC or FIELD")
    return spec


def init_study(con, studies_dir: Path, name: str, requirement: str, actor: str | None = None, **spec_kw) -> dict:
    """Create the study, its folder and its task chain; the data task is ready."""
    import re
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", name or ""):
        raise ToolError(INVALID_INPUT, "study name must be letters, digits, _ or -")
    if not (requirement or "").strip():
        raise ToolError(INVALID_INPUT, "give the study's requirement: what the simulation should answer")
    if con.execute("SELECT 1 FROM studies WHERE name = ?", (name,)).fetchone():
        raise ToolError(INVALID_INPUT, f"a study named {name} already exists")
    spec = default_spec(name, requirement, **spec_kw)
    folder = Path(studies_dir) / name
    for sub in SUBFOLDERS:
        (folder / sub).mkdir(parents=True, exist_ok=True)
    (folder / "study.json").write_text(json.dumps({"name": name, **spec}, indent=2))
    cur = con.execute("INSERT INTO studies(name, requirement, spec_json, folder, created_by) VALUES (?,?,?,?,?)",
                      (name, requirement, json.dumps(spec), str(folder), actor))
    sid = cur.lastrowid
    for t, deps in DEPENDS.items():
        con.execute("INSERT INTO study_tasks(study_id, task, status, depends_on) VALUES (?,?,?,?)",
                    (sid, t, "ready" if not deps else "waiting", ",".join(deps)))
    _event(con, sid, None, "created", {"requirement": requirement, **spec}, actor)
    return status(con, name)


def status(con, name: str) -> dict:
    s = _study(con, name)
    tasks = [_task(con, s["id"], t) for t in TASKS]
    reqs = [dict(r) for r in con.execute("SELECT * FROM study_requests WHERE study_id = ? ORDER BY id", (s["id"],))]
    open_reqs = [r for r in reqs if r["status"] == "open"]
    ready = [t["task"] for t in tasks if t["status"] == "ready"]
    by = {t["task"]: t["status"] for t in tasks}
    names = s["spec"].get("models", {})
    feedback = [dict(r) for r in con.execute(
        "SELECT id, model_kind, model_name, version_id, item, comment, severity FROM review_feedback "
        "WHERE status = 'open' AND ((model_kind = 'fluid' AND model_name = ?) OR (model_kind = 'geo' AND "
        "model_name = ?) OR (model_kind = 'well' AND model_name = ?)) ORDER BY id",
        (names.get("fluid"), names.get("geo"), names.get("well")))]
    if s["status"] == "complete":
        say = f"Study {name} is complete."
    else:
        bits = [f"{sum(v == 'done' for v in by.values())} of {len(TASKS)} tasks done"]
        if ready:
            bits.append("ready: " + ", ".join(f"{t} ({AGENT[t]})" for t in ready))
        if open_reqs:
            bits.append(f"{len(open_reqs)} open request(s)")
        blocked = [t for t, v in by.items() if v == "blocked"]
        if blocked:
            bits.append("blocked: " + ", ".join(blocked))
        if feedback:
            bits.append(f"{len(feedback)} open review comment(s) on "
                        + ", ".join(sorted({f['model_kind'] for f in feedback})))
        say = f"Study {name}: " + "; ".join(bits) + "."
    return {"study": s, "tasks": tasks, "requests": reqs, "open_requests": open_reqs, "open_feedback": feedback,
            "next": [{"task": t, "agent": AGENT[t]} for t in ready], "spoken_summary": say}


def list_studies(con) -> list[dict]:
    out = []
    for r in con.execute("SELECT name, status, created_at, updated_at FROM studies ORDER BY id DESC"):
        done = con.execute("SELECT COUNT(*) FROM study_tasks t JOIN studies s ON s.id = t.study_id "
                           "WHERE s.name = ? AND t.status = 'done'", (r["name"],)).fetchone()[0]
        out.append({**dict(r), "tasks_done": done, "tasks": len(TASKS)})
    return out


def start_task(con, name: str, task: str, actor: str | None = None) -> dict:
    s = _study(con, name)
    t = _task(con, s["id"], task)
    if t["status"] == "in_progress":
        return t
    if t["status"] != "ready":
        why = {"waiting": f"it waits for {t['depends_on']}", "blocked": "it has an open request",
               "done": "it is already done"}.get(t["status"], t["status"])
        raise ToolError(INVALID_INPUT, f"{task} can't start: {why}")
    _set(con, s["id"], task, "in_progress", actor=actor, event="started")
    return _task(con, s["id"], task)


def _check_result(con, study: str, task: str, result: dict, auto_approve: bool) -> dict:
    if task in MODEL_TASKS:
        vid = result.get("version_id")
        row = con.execute(f"SELECT v.status, v.version, m.name FROM {task}_model_versions v "
                          f"JOIN {task}_models m ON m.id = v.{task}_model_id WHERE v.id = ?", (vid,)).fetchone()
        if not row:
            raise ToolError(INVALID_INPUT, f"{task} result needs the version_id of the model built")
        if row["status"] != "approved":
            hint = ("it failed its validator or was not submitted" if auto_approve
                    else "approve it first (opmagent review approve), or set [study] auto_approve = true")
            raise ToolError(INVALID_INPUT, f"{task} {row['name']} v{row['version']} is {row['status']}: {hint}")
        return {"version_id": vid, "model": row["name"], "version": row["version"]}
    if task == "data":
        from ..data import store
        inv = store.inventory(con, study)
        if not inv["all_ready"]:
            missing = [m for m, ok in inv["ready"].items() if not ok]
            raise ToolError(INVALID_INPUT, f"the data isn't ready for {', '.join(missing)}: {inv['spoken_summary']} "
                                           f"(load it with opmagent data load, and resolve what it reports)")
        return {"loaded": {k: v["rows"] for k, v in inv["datasets"].items() if v["rows"]},
                **({"note": result["note"]} if result.get("note") else {})}
    if task == "deck":
        d = con.execute("SELECT id, name, status FROM decks WHERE id = ?", (result.get("deck_id"),)).fetchone()
        if not d or d["status"] != "validated":
            raise ToolError(INVALID_INPUT, "deck result needs the deck_id of a validated deck")
        return {"deck_id": d["id"], "deck": d["name"]}
    if task == "run":
        r = con.execute("SELECT id, status, out_dir FROM runs WHERE id = ?", (result.get("run_id"),)).fetchone()
        if not r or r["status"] not in ("done", "stored"):
            raise ToolError(INVALID_INPUT, "run result needs the run_id of a finished run")
        return {"run_id": r["id"], "out_dir": r["out_dir"]}
    return result


def complete_task(con, name: str, task: str, result: dict | None = None, actor: str | None = None,
                  auto_approve: bool = False) -> dict:
    """Mark a task done with its result; tasks whose dependencies are now all done become ready."""
    s = _study(con, name)
    t = _task(con, s["id"], task)
    if t["status"] not in ("in_progress", "ready"):
        raise ToolError(INVALID_INPUT, f"{task} is {t['status']}; start it first")
    opened = con.execute("SELECT COUNT(*) FROM study_requests WHERE study_id = ? AND to_task = ? AND status = 'open'",
                         (s["id"], task)).fetchone()[0]
    if opened:
        raise ToolError(INVALID_INPUT, f"{task} still has {opened} open request(s) addressed to it: resolve them first")
    res = _check_result(con, name, task, result or {}, auto_approve)
    _set(con, s["id"], task, "done", result=res, actor=actor, event="completed")
    unlocked = []
    for nxt, deps in DEPENDS.items():
        if task in deps and _task(con, s["id"], nxt)["status"] == "waiting" and \
                all(_task(con, s["id"], d)["status"] == "done" for d in deps):
            _set(con, s["id"], nxt, "ready", actor=actor, event="ready")
            unlocked.append(nxt)
    if all(_task(con, s["id"], x)["status"] == "done" for x in TASKS):
        con.execute("UPDATE studies SET status = 'complete', updated_at = datetime('now') WHERE id = ?", (s["id"],))
        _event(con, s["id"], None, "complete", None, actor)
    out = status(con, name)
    out["unlocked"] = unlocked
    return out


def fail_task(con, name: str, task: str, note: str, actor: str | None = None) -> dict:
    s = _study(con, name)
    _set(con, s["id"], task, "failed", note=note, actor=actor, event="failed")
    return status(con, name)


def retry_task(con, name: str, task: str, actor: str | None = None) -> dict:
    """A failed task goes back to ready (when its dependencies are done)."""
    s = _study(con, name)
    t = _task(con, s["id"], task)
    if t["status"] != "failed":
        raise ToolError(INVALID_INPUT, f"{task} is {t['status']}, not failed")
    deps_done = all(_task(con, s["id"], d)["status"] == "done" for d in DEPENDS[task])
    _set(con, s["id"], task, "ready" if deps_done else "waiting", actor=actor, event="retry")
    return status(con, name)


def raise_request(con, name: str, from_task: str, to_task: str, kind: str, message: str,
                  actor: str | None = None) -> dict:
    """from_task needs something from to_task (usually 'data'): from_task is blocked until it is resolved,
    and a finished to_task is reopened to work on it."""
    s = _study(con, name)
    if from_task == to_task:
        raise ToolError(INVALID_INPUT, "a task can't raise a request to itself")
    ft, tt = _task(con, s["id"], from_task), _task(con, s["id"], to_task)
    if tt["status"] == "waiting":
        raise ToolError(INVALID_INPUT, f"{to_task} hasn't started yet; ask an earlier task")
    cur = con.execute("INSERT INTO study_requests(study_id, from_task, to_task, kind, message) VALUES (?,?,?,?,?)",
                      (s["id"], from_task, to_task, kind, message))
    rid = cur.lastrowid
    _event(con, s["id"], from_task, "request", {"id": rid, "to": to_task, "kind": kind, "message": message}, actor)
    if ft["status"] in ("ready", "in_progress"):
        _set(con, s["id"], from_task, "blocked", note=f"waiting for request {rid} ({to_task})", actor=actor,
             event="blocked")
    if tt["status"] == "done":
        _set(con, s["id"], to_task, "ready", note=f"reopened for request {rid} from {from_task}", actor=actor,
             event="reopened")
    out = status(con, name)
    out["request_id"] = rid
    return out


def resolve_request(con, name: str, request_id: int, resolution: str, by_task: str | None = None,
                    actor: str | None = None) -> dict:
    s = _study(con, name)
    r = con.execute("SELECT * FROM study_requests WHERE id = ? AND study_id = ?", (request_id, s["id"])).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"no request {request_id} in study {name}")
    if r["status"] != "open":
        raise ToolError(INVALID_INPUT, f"request {request_id} is already resolved")
    if by_task and by_task != r["to_task"]:
        raise ToolError(FORBIDDEN, f"request {request_id} is for {r['to_task']}, not {by_task}")
    con.execute("UPDATE study_requests SET status = 'resolved', resolution = ?, resolved_at = datetime('now') "
                "WHERE id = ?", (resolution, request_id))
    _event(con, s["id"], r["to_task"], "resolved", {"id": request_id, "resolution": resolution}, actor)
    left = con.execute("SELECT COUNT(*) FROM study_requests WHERE study_id = ? AND from_task = ? AND status = 'open'",
                       (s["id"], r["from_task"])).fetchone()[0]
    if not left and _task(con, s["id"], r["from_task"])["status"] == "blocked":
        _set(con, s["id"], r["from_task"], "ready", note=f"request {request_id} resolved: {resolution}", actor=actor,
             event="unblocked")
    return status(con, name)


def events(con, name: str, limit: int = 50) -> list[dict]:
    s = _study(con, name)
    return [dict(r) for r in con.execute("SELECT * FROM study_events WHERE study_id = ? ORDER BY id DESC LIMIT ?",
                                         (s["id"], limit))][::-1]
