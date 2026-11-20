"""Simulation MCP server (Run agent): background OPM Flow runs, status, stored results, comparison, debug.

    opmagent-sim [--config opmagent.toml]

Runs only validated decks. Results are stored in the `results` table (one row per vector and time).
"""
from __future__ import annotations

import argparse
import difflib
import functools
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from ..core import config as cfgmod
from ..core import db, models
from ..core.errors import INVALID_INPUT, NOT_FOUND, ToolError
from ..sim import runner

STOP_POINTS = {
    "parse": "Opm::Parser::parseFile",
    "state": "Opm::EclipseState::EclipseState",
    "schedule": "Opm::Schedule::Schedule",
}
_NUM = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                   "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}


def _now():
    return datetime.now().isoformat(timespec="seconds")


class SimService:
    def __init__(self, cfg: cfgmod.Config):
        self.cfg = cfg
        self.con = db.connect(cfg.database)
        self.lock = threading.Lock()
        self.procs: dict[int, subprocess.Popen] = {}

    # ------------------------------------------------------------ helpers
    def _deck(self, deck_id: int) -> dict:
        d = models.get_deck(self.con, deck_id)
        if d["status"] != "validated":
            raise ToolError(INVALID_INPUT, f"deck {d['name']} is {d['status']}; only validated decks can run")
        d["data_path"] = next((f["path"] for f in d["files"] if f["role"] == "DATA"), None)
        if not d["data_path"]:
            raise ToolError(NOT_FOUND, f"deck {deck_id} has no DATA file")
        return d

    def _run(self, run_id: int) -> dict:
        r = self.con.execute("SELECT r.*, d.name AS deck_name FROM runs r JOIN decks d ON d.id = r.deck_id WHERE r.id = ?",
                             (run_id,)).fetchone()
        if not r:
            raise ToolError(NOT_FOUND, f"run {run_id} not found")
        return dict(r)

    def _refresh(self, run_id: int) -> dict:
        r = self._run(run_id)
        if r["status"] != "running":
            return r
        p = self.procs.get(run_id)
        rc = p.poll() if p else None
        if p is None:  # server restarted: check the pid
            alive = False
            if r["pid"]:
                try:
                    os.kill(r["pid"], 0)
                    alive = True
                except OSError:
                    alive = False
            if alive:
                return r
            rc = -1 if not r["smspec_path"] or not Path(r["smspec_path"]).exists() else 0
        if rc is None:
            return r
        ok = rc == 0 and Path(r["smspec_path"]).exists()
        self.con.execute("UPDATE runs SET status=?, returncode=?, finished_at=? WHERE id=?",
                         ("done" if ok else "failed", rc, _now(), run_id))
        self.procs.pop(run_id, None)
        return self._run(run_id)

    # ------------------------------------------------------------ tools
    def resolve_deck(self, text: str) -> dict:
        """Find a deck by spoken name ('the field A waterflood deck'); returns the best matches."""
        words = re.sub(r"\b(the|deck|for|a|an)\b", " ", text.lower())
        joined = re.sub(r"[\s_\-]+", "", words)
        cands = []
        for d in models.list_decks(self.con):
            score = difflib.SequenceMatcher(None, joined, re.sub(r"[\s_\-]+", "", d["name"].lower())).ratio()
            if score >= 0.4:
                cands.append({**d, "score": round(score, 2)})
        cands.sort(key=lambda c: (-c["score"], -c["id"]))
        if not cands:
            raise ToolError(NOT_FOUND, f"no deck matches '{text}'")
        top = cands[0]
        return {"ok": True, "match": top, "candidates": cands[:5],
                "spoken_summary": f"Deck {top['name']}, number {top['id']}, {top['status']}."}

    def resolve_run(self, text: str) -> dict:
        """Find a run by spoken reference ('run twelve', 'the last run')."""
        t = text.lower()
        m = re.search(r"\brun\s*(\d+|" + "|".join(_NUM) + r")\b", t)
        if m:
            rid = int(m.group(1)) if m.group(1).isdigit() else _NUM[m.group(1)]
        elif "last" in t or "latest" in t:
            row = self.con.execute("SELECT MAX(id) AS id FROM runs").fetchone()
            rid = row["id"]
        else:
            raise ToolError(INVALID_INPUT, "say a run number, e.g. 'run twelve', or 'the last run'")
        r = self._refresh(rid)
        return {"ok": True, "run": r, "spoken_summary": f"Run {rid} of deck {r['deck_name']} is {r['status']}."}

    def run_simulation(self, deck_id: int, note: str | None = None) -> dict:
        """Start a validated deck in OPM Flow in the background. Returns a run id."""
        d = self._deck(deck_id)
        running = self.con.execute("SELECT COUNT(*) AS n FROM runs WHERE status='running'").fetchone()["n"]
        if running >= self.cfg.max_parallel:
            raise ToolError(INVALID_INPUT, f"{running} runs already active (max_parallel {self.cfg.max_parallel}); try later")
        cur = self.con.execute("INSERT INTO runs(deck_id, out_dir, note) VALUES (?, '', ?)", (deck_id, note))
        rid = cur.lastrowid
        out = self.cfg.file_store / "runs" / f"run{rid}_{d['name']}"
        try:
            proc, engine, log = runner.start(d["data_path"], out, self.cfg.flow_bin)
        except RuntimeError as e:
            self.con.execute("UPDATE runs SET status='failed', note=? WHERE id=?", (str(e), rid))
            raise ToolError(INVALID_INPUT, str(e)) from e
        smspec = out / (Path(d["data_path"]).stem + ".SMSPEC")
        self.con.execute("UPDATE runs SET status='running', engine=?, out_dir=?, pid=?, log_path=?, smspec_path=?, "
                         "started_at=? WHERE id=?", (engine, str(out), proc.pid, str(log), str(smspec), _now(), rid))
        self.procs[rid] = proc
        return {"ok": True, "run_id": rid, "engine": engine,
                "spoken_summary": f"Started run {rid} of deck {d['name']}."}

    def job_status(self, run_id: int, log_lines: int = 15) -> dict:
        """Status of a run (running, done, failed, stored), its progress from the Flow log (report step, simulated
        date, % of simulated time, elapsed, estimated time left, a stall warning) and the end of its log."""
        r = self._refresh(run_id)
        tail, prog = "", None
        if r["log_path"] and Path(r["log_path"]).exists():
            tail = "\n".join(Path(r["log_path"]).read_text(errors="replace").splitlines()[-log_lines:])
            elapsed = None
            if r["started_at"]:
                end = datetime.fromisoformat(r["finished_at"]) if r["finished_at"] else datetime.now()
                elapsed = (end - datetime.fromisoformat(r["started_at"])).total_seconds()
            prog = runner.progress(r["log_path"], elapsed)
        say = f"Run {run_id} is {r['status']}."
        if prog and r["status"] == "running" and "report_step" in prog:
            say = (f"Run {run_id} is running: report step {prog['report_step']} of {prog['report_steps']}, "
                   f"{prog['date']}, {prog['percent']:.0f} percent"
                   + (f", about {runner._dur(prog['eta_s'])} left." if "eta_s" in prog else "."))
            if prog.get("stalled"):
                say += f" No new log output for {runner._dur(prog['log_age_s'])}: it may be stuck."
        return {"ok": True, "run": r, "progress": prog, "log_tail": tail, "spoken_summary": say}

    def wait(self, run_id: int, timeout_s: int = 600) -> dict:
        """Wait for a run to finish (up to timeout_s), then store its results."""
        p = self.procs.get(run_id)
        if p:
            try:
                p.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                return {"ok": True, "run": self._refresh(run_id), "spoken_summary": f"Run {run_id} is still running."}
        r = self._refresh(run_id)
        if r["status"] == "done":
            return self.store_results(run_id)
        return {"ok": r["status"] != "failed", "run": r, "spoken_summary": f"Run {run_id} is {r['status']}."}

    def store_results(self, run_id: int) -> dict:
        """Read the run's summary file into the results table."""
        r = self._refresh(run_id)
        if r["status"] == "stored":
            n = self.con.execute("SELECT COUNT(DISTINCT vector) AS n FROM results WHERE run_id=?", (run_id,)).fetchone()["n"]
            return {"ok": True, "vectors": n, "spoken_summary": f"Run {run_id} results are already stored."}
        if r["status"] != "done":
            raise ToolError(INVALID_INPUT, f"run {run_id} is {r['status']}; results can be stored once it is done")
        t, vecs = runner.read_all(r["smspec_path"])
        rows = [(run_id, k, ti, v) for k, vals in vecs.items() for ti, v in zip(t, vals)]
        with db.transaction(self.con):
            self.con.executemany("INSERT OR REPLACE INTO results(run_id, vector, time_days, value) VALUES (?,?,?,?)", rows)
            self.con.execute("UPDATE runs SET status='stored' WHERE id=?", (run_id,))
        last = {k: vecs[k][-1] for k in ("FOPR", "FPR", "FWCT") if k in vecs}
        say = f"Stored {len(vecs)} vectors for run {run_id}."
        if last:
            say += " At the end: " + ", ".join(f"{k} {v:.3g}" for k, v in last.items()) + "."
        return {"ok": True, "vectors": len(vecs), "time_steps": len(t), "last": last, "spoken_summary": say}

    def query_results(self, run_id: int, vectors: list[str], max_points: int = 200) -> dict:
        """Series of stored vectors for a run (e.g. ['FOPR', 'WBHP:PROD1']), downsampled to max_points."""
        out = {}
        for v in vectors:
            rows = self.con.execute("SELECT time_days, value FROM results WHERE run_id=? AND vector=? ORDER BY time_days",
                                    (run_id, v)).fetchall()
            if not rows:
                continue
            step = max(1, len(rows) // max_points)
            pts = rows[::step] + ([rows[-1]] if (len(rows) - 1) % step else [])
            out[v] = {"time_days": [p["time_days"] for p in pts], "values": [p["value"] for p in pts],
                      "last": rows[-1]["value"]}
        missing = [v for v in vectors if v not in out]
        say = "; ".join(f"{k} ends at {d['last']:.3g}" for k, d in out.items()) or "No stored values for those vectors."
        return {"ok": True, "series": out, "missing": missing, "spoken_summary": say}

    def compare_runs(self, run_a: int, run_b: int, vectors: list[str] | None = None, rel_tol: float = 1e-4) -> dict:
        """Compare stored summary vectors of two runs: max relative difference per vector (at matching times)."""
        def load(r):
            data = {}
            for row in self.con.execute("SELECT vector, time_days, value FROM results WHERE run_id=?", (r,)):
                data.setdefault(row["vector"], {})[round(row["time_days"], 6)] = row["value"]
            if not data:
                raise ToolError(INVALID_INPUT, f"run {r} has no stored results; call store_results first")
            return data
        A, B = load(run_a), load(run_b)
        names = vectors or sorted(set(A) & set(B))
        diffs, fails = {}, []
        for v in names:
            if v not in A or v not in B:
                diffs[v] = {"missing_in": "a" if v not in A else "b"}
                fails.append(v)
                continue
            times = sorted(set(A[v]) & set(B[v]))
            scale = max([abs(x) for x in A[v].values()] + [1e-12])
            d = max((abs(A[v][t] - B[v][t]) / scale for t in times), default=0.0)
            diffs[v] = {"max_rel_diff": d, "times_compared": len(times)}
            if d > rel_tol:
                fails.append(v)
        worst = sorted(((k, x["max_rel_diff"]) for k, x in diffs.items() if "max_rel_diff" in x), key=lambda kv: -kv[1])[:5]
        say = (f"Runs {run_a} and {run_b} match within {rel_tol:g} on {len(names)} vectors." if not fails else
               f"{len(fails)} of {len(names)} vectors differ; largest: " +
               ", ".join(f"{k} {d:.2e}" for k, d in worst) + ".")
        return {"ok": True, "passed": not fails, "failed": fails, "diffs": diffs, "spoken_summary": say}

    def debug_simulation(self, deck_id: int, stop_at: str = "parse", custom: str | None = None, open_vscode: bool = True) -> dict:
        """Prepare a VS Code debug session of a Debug OPM build on this deck and open VS Code.
        stop_at: parse | state | schedule | custom (then give custom = function or file:line)."""
        cfg = self.cfg
        if not cfg.opm_source_root or not cfg.opm_debug_flow:
            raise ToolError(INVALID_INPUT, "set opm_source_root and opm_debug_flow in opmagent.toml ([debug] section)")
        d = self._deck(deck_id)
        if stop_at == "custom":
            if not custom:
                raise ToolError(INVALID_INPUT, "stop_at=custom needs custom='function' or 'file:line'")
            target = custom
        elif stop_at in STOP_POINTS:
            target = STOP_POINTS[stop_at]
        else:
            raise ToolError(INVALID_INPUT, f"stop_at must be one of {list(STOP_POINTS) + ['custom']}")
        dbg = cfg.file_store / "debug" / d["name"]
        deck = runner.prepare(d["data_path"], dbg)
        vs = Path(cfg.opm_source_root) / ".vscode"
        vs.mkdir(parents=True, exist_ok=True)
        launch_path = vs / "launch.json"
        launch = json.loads(launch_path.read_text()) if launch_path.exists() else {"version": "0.2.0", "configurations": []}
        name = f"opmagent: {d['name']} ({stop_at})"
        conf = {"name": name, "type": "cppdbg", "request": "launch", "program": str(cfg.opm_debug_flow),
                "args": [str(deck), f"--output-dir={dbg}", "--threads-per-process=1"], "cwd": str(dbg),
                "stopAtEntry": False, "MIMode": "gdb", "externalConsole": False,
                "setupCommands": [{"description": "pretty printing", "text": "-enable-pretty-printing", "ignoreFailures": True},
                                  {"description": f"break at {target}", "text": f"-break-insert -f {target}", "ignoreFailures": True}]}
        launch["configurations"] = [c for c in launch.get("configurations", []) if c.get("name") != name] + [conf]
        launch_path.write_text(json.dumps(launch, indent=2))
        opened = False
        if open_vscode and shutil.which(cfg.vscode_bin):
            subprocess.Popen([cfg.vscode_bin, str(cfg.opm_source_root)])
            opened = True
        return {"ok": True, "launch_json": str(launch_path), "configuration": name, "deck_copy": str(deck),
                "break_at": target, "vscode_opened": opened,
                "spoken_summary": (f"VS Code is opening on your OPM source. Start the configuration '{name}' with F5; "
                                   f"it stops at {stop_at}.") if opened else
                                  f"Debug configuration '{name}' is ready in {launch_path}; open your OPM folder in VS Code and press F5."}

    def list_runs(self, deck_id: int | None = None) -> dict:
        """List runs, newest first (optionally for one deck)."""
        sql = "SELECT r.id, r.deck_id, d.name AS deck_name, r.status, r.engine, r.started_at, r.finished_at FROM runs r JOIN decks d ON d.id=r.deck_id"
        rows = [dict(x) for x in self.con.execute(sql + (" WHERE r.deck_id=?" if deck_id else "") + " ORDER BY r.id DESC",
                                                  (deck_id,) if deck_id else ())]
        return {"ok": True, "runs": rows, "spoken_summary": f"{len(rows)} run(s)."}


TOOLS = ["resolve_deck", "resolve_run", "run_simulation", "job_status", "wait", "store_results", "query_results",
         "compare_runs", "debug_simulation", "list_runs"]


def wrap(svc: SimService, name: str):
    fn = getattr(svc, name)

    @functools.wraps(fn)
    def tool(**kwargs):
        with svc.lock:
            try:
                return fn(**kwargs)
            except ToolError as e:
                return e.to_dict()
    return tool


def build_server(cfg) -> tuple[MCPServer, SimService]:
    svc = SimService(cfg)
    server = MCPServer("opmagent-sim", instructions="OPM Flow runs: start validated decks, check status, store, query "
                                                    "and compare results, or prepare a VS Code debug session.")
    for name in TOOLS:
        server.add_tool(wrap(svc, name), name=name, description=getattr(SimService, name).__doc__)
    return server, svc


def main(argv=None):
    p = argparse.ArgumentParser(prog="opmagent-sim")
    p.add_argument("--config")
    a = p.parse_args(argv)
    server, _ = build_server(cfgmod.load(a.config))
    server.run("stdio")


if __name__ == "__main__":
    main()
