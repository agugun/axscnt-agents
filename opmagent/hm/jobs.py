"""History-match setups, variant cases and the jobs that run them (base case, sensitivities).

A variant is the base deck with parameter values applied in memory (params.apply) and rendered with the deck
builder's render() into <file_store>/hm/<name>_r<revision>/case_<n>/. It writes no model version, no decks row and
no runs row: the case lives in hm_cases. Variants run in Flow several at once, within the [hm] capacity, each
after a memory check.

Jobs run in a background thread of the HM server (or in the foreground for `opmagent hm`), with their own database
connection. Every case's values, status and misfit are stored as it finishes, so a stopped job, or one whose
server stopped, continues where it was: finished cases are never run again (hm_cases is unique on the values).
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from ..core import db
from ..core.errors import INVALID_INPUT, NOT_FOUND, ToolError
from . import misfit as mf
from . import params as pm
from . import series, service

VARIANT_HEADER = ("-- History-match variant, not for decisions. Made by opmagent hm from deck {deck} with:\n"
                  "--   {values}\n")
BUSY = "BUSY"
SETUP_FROZEN = "SETUP_FROZEN"


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- setups
def get_hm(con, ref) -> dict:
    """An HM study by id, or by name (its latest revision)."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        r = con.execute("SELECT * FROM hm_studies WHERE id = ?", (int(ref),)).fetchone()
    else:
        r = con.execute("SELECT * FROM hm_studies WHERE name = ? ORDER BY revision DESC LIMIT 1", (ref,)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"no history match {ref}")
    h = dict(r)
    h["objective"] = json.loads(h.pop("objective_json"))
    h["parameters"] = [dict(p, target=json.loads(p.pop("target_json")), active=bool(p["active"]))
                       for p in map(dict, con.execute("SELECT * FROM hm_parameters WHERE hm_id = ? ORDER BY position",
                                                      (h["id"],)))]
    return h


def read_back(h: dict) -> str:
    ps = [p for p in h["parameters"] if p["active"]]
    obj = h["objective"]
    s = obj["sigma"]
    src = {"data_production": f"study {h['study']}'s measured history", "deck_history": "the deck's history",
           "run": f"run {h['truth_run_id']} as the truth"}[h["source"]]
    period = obj.get("period")
    return (f"History match {h['name']} (revision {h['revision']}) on deck {h['deck_id']}: {len(ps)} parameter(s): "
            + "; ".join(pm.describe(p) for p in ps)
            + f". Observations: {src}" + (f", {period[0]} to {period[1]}" if period else "")
            + (f", leaving out {', '.join(obj['exclude_wells'])}" if obj.get("exclude_wells") else "")
            + f". σ: oil {s['oil']['rel']:.0%} (floor {s['oil']['abs']:g}), water {s['water']['rel']:.0%} "
              f"(floor {s['water']['abs']:g}), BHP {s['bhp']['abs']:g}.")


def setup(con, name: str, deck_id: int, parameters: list[dict], *, study: str | None = None, source: str = "auto",
          truth_run_id: int | None = None, objective: dict | None = None) -> dict:
    """Check and store a setup. A name whose latest revision is frozen gets a new revision; one still in 'setup'
    is replaced."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name or ""):
        raise ToolError(INVALID_INPUT, "name: letters, digits, _ or -")
    deck = con.execute("SELECT status FROM decks WHERE id = ?", (deck_id,)).fetchone()
    if not deck:
        raise ToolError(NOT_FOUND, f"deck {deck_id} not found")
    if deck["status"] != "validated":
        raise ToolError(INVALID_INPUT, f"deck {deck_id} is {deck['status']}; a history match starts from a validated deck")
    m = pm.deck_models(con, deck_id)
    ps = pm.check(parameters, m)
    try:
        obj = mf.objective(objective)
    except ValueError as e:
        raise ToolError(INVALID_INPUT, str(e)) from None
    base_run = latest_run(con, deck_id, exclude=truth_run_id)   # a synthetic truth is never the base
    # the observations must exist now (NO_HISTORY otherwise); deck_history reads the deck's latest finished run
    src, _ = service.observations(con, study=study, run_id=base_run, source=source, truth_run_id=truth_run_id)
    prev = con.execute("SELECT id, revision, status FROM hm_studies WHERE name = ? ORDER BY revision DESC LIMIT 1",
                       (name,)).fetchone()
    with db.transaction(con):
        if prev and prev["status"] == "setup":
            con.execute("DELETE FROM hm_parameters WHERE hm_id = ?", (prev["id"],))
            con.execute("DELETE FROM hm_cases WHERE hm_id = ?", (prev["id"],))
            con.execute("DELETE FROM hm_jobs WHERE hm_id = ?", (prev["id"],))
            con.execute("DELETE FROM hm_studies WHERE id = ?", (prev["id"],))
            rev = prev["revision"]
        else:
            rev = (prev["revision"] + 1) if prev else 1
            if prev:
                con.execute("UPDATE hm_studies SET status = 'superseded' WHERE id = ? AND status = 'frozen'",
                            (prev["id"],))
        cur = con.execute("INSERT INTO hm_studies(name, revision, study, deck_id, base_run_id, source, truth_run_id, "
                          "objective_json) VALUES (?,?,?,?,?,?,?,?)",
                          (name, rev, study, deck_id, base_run, src, truth_run_id, json.dumps(obj)))
        hid = cur.lastrowid
        for i, p in enumerate(ps):
            con.execute("INSERT INTO hm_parameters(hm_id, position, name, kind, owner, type, target_json, low, high, "
                        "start, scale, active) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (hid, i, p["name"], p["kind"], p["owner"], p["type"], json.dumps(p["target"]), p["low"],
                         p["high"], p["start"], p["scale"], int(p["active"])))
    h = get_hm(con, hid)
    return {"hm_id": hid, "name": name, "revision": rev, "source": src, "base_run_id": base_run,
            "parameters": h["parameters"], "read_back": read_back(h)}


def latest_run(con, deck_id: int, exclude: int | None = None) -> int | None:
    r = con.execute("SELECT id FROM runs WHERE deck_id = ? AND status IN ('done','stored') AND smspec_path IS NOT NULL "
                    "AND id IS NOT ? ORDER BY id DESC LIMIT 1", (deck_id, exclude)).fetchone()
    return r["id"] if r else None


def freeze(con, hm_id: int) -> None:
    con.execute("UPDATE hm_studies SET status = 'frozen' WHERE id = ? AND status = 'setup'", (hm_id,))


# ---------------------------------------------------------------- variants
def hm_dir(file_store: Path, h: dict) -> Path:
    return Path(file_store) / "hm" / f"{h['name']}_r{h['revision']}"


def build_variant(con, h: dict, values: dict[str, float], case_dir: Path) -> Path:
    """Render the base deck with the values applied into case_dir. Returns the DATA file."""
    from ..deck.render import combine, render
    m = pm.deck_models(con, h["deck_id"])
    geo, res, paths = pm.apply(m, h["parameters"], values, case_dir / "include", con)
    spec = dict(m["deck"]["spec_json"])
    text = render(spec, m["fluid"], combine(geo, res), m["well"], paths)
    vals = ", ".join(f"{k} = {v:.6g}" for k, v in sorted(values.items()))
    path = case_dir / f"{re.sub(r'[^A-Za-z0-9_]+', '_', h['name']).upper()}.DATA"
    path.write_text(VARIANT_HEADER.format(deck=h["deck_id"], values=vals) + text)
    return path


# ---------------------------------------------------------------- cases
def start_values(h: dict) -> dict[str, float]:
    return {p["name"]: p["start"] for p in h["parameters"]}


def ensure_case(con, h: dict, values: dict[str, float], job_id: int | None, label: str | None = None) -> dict:
    """The case for these values: an existing one (any status), or a new queued one."""
    full = {**start_values(h), **values}
    key = pm.values_key(full)
    r = con.execute("SELECT * FROM hm_cases WHERE hm_id = ? AND values_key = ?", (h["id"], key)).fetchone()
    if r:
        return dict(r)
    n = con.execute("SELECT COALESCE(MAX(case_no), -1) + 1 AS n FROM hm_cases WHERE hm_id = ?",
                    (h["id"],)).fetchone()["n"]
    cur = con.execute("INSERT INTO hm_cases(hm_id, job_id, case_no, values_json, values_key, label) "
                      "VALUES (?,?,?,?,?,?)", (h["id"], job_id, n, json.dumps(full), key, label))
    return dict(con.execute("SELECT * FROM hm_cases WHERE id = ?", (cur.lastrowid,)).fetchone())


def _observations(con, h: dict):
    src, obs = service.observations(con, study=h["study"], run_id=h["base_run_id"], source=h["source"],
                                    truth_run_id=h["truth_run_id"])
    return obs


def score(con, h: dict, case_id: int, smspec: str, obs=None) -> dict:
    """Misfit of a finished case; stored in hm_cases and hm_misfits."""
    obs = _observations(con, h) if obs is None else obs
    well_version = con.execute("SELECT well_version_id FROM decks WHERE id = ?", (h["deck_id"],)).fetchone()[0]
    res = mf.compute(obs, series.from_summary(smspec), h["objective"], mf.honoured(con, well_version))
    with db.transaction(con):
        con.execute("DELETE FROM hm_misfits WHERE case_id = ?", (case_id,))
        for t in res["terms"]:
            detail = {k: t[k] for k in ("cum_obs", "cum_sim", "cum_error_pct", "last_month", "last_obs", "last_sim",
                                        "last_error", "weight", "honoured") if k in t}
            con.execute("INSERT INTO hm_misfits(case_id, well, quantity, n, misfit, sum_sq, in_total, detail_json) "
                        "VALUES (?,?,?,?,?,?,?,?)", (case_id, t["well"], t["quantity"], t["n"], t["misfit"],
                                                     t["sum_sq"], int(t["in_total"]), json.dumps(detail)))
        con.execute("UPDATE hm_cases SET misfit = ?, points = ? WHERE id = ?", (res["total"], res["n"], case_id))
    return res


# ---------------------------------------------------------------- capacity
def slots(cfg) -> int:
    if cfg.hm_parallel:
        return max(1, cfg.hm_parallel)
    return max(1, (os.cpu_count() or 2) // (2 * max(1, cfg.hm_threads)))


def free_mb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except OSError:
        return None
    return None


def case_memory_mb(cfg, con, h: dict) -> float:
    if cfg.hm_case_memory_mb:
        return float(cfg.hm_case_memory_mb)
    g = con.execute("SELECT nx * ny * nz AS n FROM geo_model_versions WHERE id = (SELECT geo_version_id FROM decks "
                    "WHERE id = ?)", (h["deck_id"],)).fetchone()
    return 300.0 + 0.02 * (g["n"] or 0)       # Flow's base footprint + ~20 kB per cell


def memory_ok(cfg, need_mb: float) -> bool:
    f = free_mb()
    return f is None or f >= need_mb + cfg.hm_min_free_mb


def ordinary_runs(con) -> int:
    """Ordinary (run agent) runs in progress: they come first, so each takes a variant slot."""
    return con.execute("SELECT COUNT(*) FROM runs WHERE status = 'running'").fetchone()[0]


# ---------------------------------------------------------------- jobs
def create_job(con, h: dict, kind: str, options: dict | None = None) -> int:
    busy = con.execute("SELECT id FROM hm_jobs WHERE hm_id = ? AND status IN ('queued','running')",
                       (h["id"],)).fetchone()
    if busy:
        raise ToolError(BUSY, f"job {busy['id']} is already running for history match {h['name']}; "
                              "wait for it or stop it (hm_stop)")
    cur = con.execute("INSERT INTO hm_jobs(hm_id, kind, method, options_json) VALUES (?,?,?,?)",
                      (h["id"], kind, (options or {}).get("method"), json.dumps(options or {})))
    freeze(con, h["id"])
    return cur.lastrowid


def get_job(con, job_id: int) -> dict:
    r = con.execute("SELECT * FROM hm_jobs WHERE id = ?", (job_id,)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"history-match job {job_id} not found")
    j = dict(r)
    for k in ("options_json", "progress_json", "result_json"):
        j[k.removesuffix("_json")] = json.loads(j.pop(k)) if j[k] else None
    return j


def plan(h: dict, kind: str) -> list[tuple[dict, str]]:
    """The cases a job needs: (values, label)."""
    base = start_values(h)
    if kind == "base":
        return [(base, "base")]
    out = [(base, "base")]
    for p in h["parameters"]:
        if not p["active"]:
            continue
        for end in ("low", "high"):
            out.append(({**base, p["name"]: p[end]}, f"{p['name']} {end}"))
    return out


class Runner:
    """Runs a job's cases: builds variants, starts Flow within capacity, scores finished cases."""

    def __init__(self, cfg, job_id: int, log=None):
        self.cfg, self.job_id = cfg, job_id
        self.con = db.connect(cfg.database)
        self.log = log or (lambda *_: None)

    def _progress(self, **p):
        self.con.execute("UPDATE hm_jobs SET progress_json = ? WHERE id = ?", (json.dumps(p), self.job_id))

    def _stop_requested(self) -> int:
        return self.con.execute("SELECT stop_requested FROM hm_jobs WHERE id = ?", (self.job_id,)).fetchone()[0]

    def run(self) -> dict:
        from ..sim import runner
        con, cfg = self.con, self.cfg
        job = get_job(con, self.job_id)
        h = get_hm(con, job["hm_id"])
        con.execute("UPDATE hm_jobs SET status = 'running', started_at = COALESCE(started_at, ?) WHERE id = ?",
                    (_now(), self.job_id))
        try:
            obs = _observations(con, h)
            cases = [ensure_case(con, h, v, self.job_id, lab) for v, lab in plan(h, job["kind"])]
            # a case left 'running' by a server that stopped: run it again
            for c in cases:
                if c["status"] == "running" and not _alive(c["pid"]):
                    con.execute("UPDATE hm_cases SET status = 'queued', pid = NULL WHERE id = ?", (c["id"],))
            # the base case is the approved deck's own run when there is one
            base = cases[0]
            if base["status"] != "done" and h["base_run_id"]:
                r = service.run_row(con, h["base_run_id"])
                con.execute("UPDATE hm_cases SET status = 'done', run_id = ?, run_dir = ?, smspec_path = ?, "
                            "finished_at = ? WHERE id = ?", (r["id"], r["out_dir"], r["smspec_path"], _now(), base["id"]))
                score(con, h, base["id"], r["smspec_path"], obs)
            folder = hm_dir(cfg.file_store, h)
            need = case_memory_mb(cfg, con, h)
            running: dict[int, tuple] = {}
            t0 = time.time()
            while True:
                rows = [dict(con.execute("SELECT * FROM hm_cases WHERE id = ?", (c["id"],)).fetchone()) for c in cases]
                queued = [c for c in rows if c["status"] == "queued" and c["id"] not in running]
                stop = self._stop_requested()
                if stop == 2:      # now: kill what runs
                    for cid, (p, *_rest) in running.items():
                        p.kill()
                        con.execute("UPDATE hm_cases SET status = 'queued', pid = NULL WHERE id = ?", (cid,))
                    running.clear()
                # finished?
                for cid, (p, cdir, deck, started) in list(running.items()):
                    rc = p.poll()
                    wall = time.time() - started
                    if rc is None and wall > cfg.hm_case_timeout_s:
                        p.kill()
                        rc = -9
                    if rc is None:
                        continue
                    del running[cid]
                    smspec = cdir / (deck.stem + ".SMSPEC")
                    ok = rc == 0 and smspec.exists()
                    con.execute("UPDATE hm_cases SET status = ?, returncode = ?, smspec_path = ?, wall_s = ?, "
                                "finished_at = ?, pid = NULL, error = ? WHERE id = ?",
                                ("done" if ok else "failed", rc, str(smspec) if ok else None, round(wall, 1), _now(),
                                 None if ok else _tail(cdir / "run.log"), cid))
                    if ok:
                        score(con, h, cid, str(smspec), obs)
                    self.log(f"case {cid}: {'done' if ok else 'failed'} in {wall:.0f} s")
                capacity = max(0, slots(cfg) - ordinary_runs(con))
                while queued and not stop and len(running) < capacity and memory_ok(cfg, need * (len(running) + 1)):
                    c = queued.pop(0)
                    cdir = folder / f"case_{c['case_no']}"
                    try:
                        deck = build_variant(con, h, json.loads(c["values_json"]), cdir)
                        p, _engine, _log = runner.start(deck, cdir / "run", cfg.flow_bin, threads=cfg.hm_threads)
                    except Exception as e:  # noqa: BLE001  (a variant that can't be built fails, the job goes on)
                        con.execute("UPDATE hm_cases SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                                    (f"{type(e).__name__}: {e}", _now(), c["id"]))
                        continue
                    running[c["id"]] = (p, cdir / "run", deck, time.time())
                    con.execute("UPDATE hm_cases SET status = 'running', run_dir = ?, pid = ?, started_at = ? "
                                "WHERE id = ?", (str(cdir), p.pid, _now(), c["id"]))
                rows = [dict(con.execute("SELECT status, misfit, case_no, wall_s FROM hm_cases WHERE id = ?",
                                         (c["id"],)).fetchone()) for c in cases]
                done = [r for r in rows if r["status"] == "done"]
                failed = [r for r in rows if r["status"] == "failed"]
                left = len(rows) - len(done) - len(failed)
                best = min((r for r in done if r["misfit"] is not None), key=lambda r: r["misfit"], default=None)
                walls = [r["wall_s"] for r in done if r["wall_s"]]
                eta = (left * (sum(walls) / len(walls)) / max(1, slots(cfg))) if walls and left else None
                self._progress(cases=len(rows), done=len(done), failed=len(failed), running=len(running),
                               left=left, best_case=best["case_no"] if best else None,
                               best_misfit=best["misfit"] if best else None, elapsed_s=round(time.time() - t0),
                               eta_s=round(eta) if eta else None, memory_mb_per_case=round(need))
                if not running and (left == 0 or stop):
                    break
                time.sleep(0.5)
            if self._stop_requested() and left:
                status, result = "stopped", None
            else:
                status, result = "done", summarise(con, h, job["kind"])
            con.execute("UPDATE hm_jobs SET status = ?, result_json = ?, finished_at = ? WHERE id = ?",
                        (status, json.dumps(result) if result else None, _now(), self.job_id))
            return get_job(con, self.job_id)
        except Exception as e:  # noqa: BLE001
            con.execute("UPDATE hm_jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                        (f"{type(e).__name__}: {e}", _now(), self.job_id))
            raise


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _tail(path: Path, n: int = 15) -> str | None:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return None


def start_job(cfg, job_id: int) -> threading.Thread:
    t = threading.Thread(target=_quiet_run, args=(cfg, job_id), daemon=True, name=f"hm-job-{job_id}")
    t.start()
    return t


def _quiet_run(cfg, job_id):
    try:
        Runner(cfg, job_id).run()
    except Exception:  # noqa: BLE001  (recorded on the job)
        pass


def stop_job(con, job_id: int, now: bool = False) -> dict:
    j = get_job(con, job_id)
    if j["status"] not in ("queued", "running"):
        return j
    con.execute("UPDATE hm_jobs SET stop_requested = ? WHERE id = ?", (2 if now else 1, job_id))
    return get_job(con, job_id)


# ---------------------------------------------------------------- results
def cases(con, hm_id: int, order: str = "misfit", limit: int = 10) -> list[dict]:
    o = {"misfit": "misfit IS NULL, misfit", "case": "case_no"}.get(order)
    if not o:
        raise ToolError(INVALID_INPUT, "order is misfit or case")
    rows = [dict(r) for r in con.execute(f"SELECT id, case_no, label, status, misfit, points, wall_s, values_json, "
                                         f"run_id, error FROM hm_cases WHERE hm_id = ? ORDER BY {o} LIMIT ?",
                                         (hm_id, limit))]
    for r in rows:
        r["values"] = json.loads(r.pop("values_json"))
    return rows


def case_detail(con, hm_id: int, case_no: int) -> dict:
    r = con.execute("SELECT * FROM hm_cases WHERE hm_id = ? AND case_no = ?", (hm_id, case_no)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"history match {hm_id} has no case {case_no}")
    c = dict(r)
    c["values"] = json.loads(c.pop("values_json"))
    c["terms"] = [dict(t, in_total=bool(t["in_total"]), **json.loads(t.pop("detail_json") or "{}"))
                  for t in map(dict, con.execute("SELECT well, quantity, n, misfit, sum_sq, in_total, detail_json "
                                                 "FROM hm_misfits WHERE case_id = ? ORDER BY misfit DESC", (c["id"],)))]
    return c


def summarise(con, h: dict, kind: str) -> dict:
    """A job's result: the base misfit, and for a sensitivity the tornado (misfit change per parameter end)."""
    rows = {r["label"]: dict(r) for r in con.execute("SELECT label, case_no, status, misfit FROM hm_cases "
                                                      "WHERE hm_id = ?", (h["id"],)) if r["label"]}
    base = rows.get("base", {}).get("misfit")
    out = {"base_misfit": base}
    if kind == "sensitivity":
        tor = []
        for p in h["parameters"]:
            if not p["active"]:
                continue
            lo, hi = rows.get(f"{p['name']} low", {}), rows.get(f"{p['name']} high", {})
            ml, mh = lo.get("misfit"), hi.get("misfit")
            d = {"name": p["name"], "kind": p["kind"], "low": p["low"], "high": p["high"],
                 "misfit_low": ml, "misfit_high": mh,
                 "delta_low": (ml - base) if ml is not None and base is not None else None,
                 "delta_high": (mh - base) if mh is not None and base is not None else None,
                 "failed": [e for e, r in (("low", lo), ("high", hi)) if r.get("status") == "failed"]}
            ds = [x for x in (d["delta_low"], d["delta_high"]) if x is not None]
            d["effect"] = max((abs(x) for x in ds), default=None)
            d["best_reduction"] = -min(ds) if ds and min(ds) < 0 else 0.0
            d["better_end"] = (None if not ds or min(ds) >= 0 else
                               "low" if d["delta_low"] is not None and d["delta_low"] == min(ds) else "high")
            tor.append(d)
        tor.sort(key=lambda d: (-(d["best_reduction"] or 0), -(d["effect"] or 0)))
        for i, d in enumerate(tor, start=1):
            d["rank"] = i
        tol = 0.01 * base if base else 1e-9
        out["tornado"] = tor
        out["no_effect"] = [d["name"] for d in tor if d["effect"] is not None and d["effect"] < tol]
    return out


def spoken_job(j: dict) -> str:
    p = j.get("progress") or {}
    s = f"Job {j['id']} ({j['kind']}) is {j['status']}"
    if p:
        s += (f": {p.get('done', 0)} of {p.get('cases', 0)} case(s) done, {p.get('running', 0)} running, "
              f"{p.get('failed', 0)} failed")
        if p.get("best_misfit") is not None:
            s += f"; best misfit {p['best_misfit']:.3g} (case {p['best_case']})"
        if p.get("eta_s"):
            s += f"; about {math.ceil(p['eta_s'] / 60)} min left"
    r = j.get("result") or {}
    if r.get("tornado"):
        top = [d for d in r["tornado"] if d["best_reduction"]][:3]
        if top:
            s += ". Most improving: " + ", ".join(f"{d['name']} at its {d['better_end']} end "
                                                  f"(-{d['best_reduction']:.3g})" for d in top)
        if r.get("no_effect"):
            s += f". No effect: {', '.join(r['no_effect'])}"
    if j.get("error"):
        s += f". Error: {j['error']}"
    return s + "."


# ---------------------------------------------------------------- the HM report of a match
def write_match_report(cfg, con, ref, *, timeout: int = 600) -> dict:
    """The HM report of a history match: the best case against the observations, with the base case beside it,
    the sensitivity tornado and the cases evaluated."""
    from ..notebooks import runner
    h = get_hm(con, ref)
    done = [c for c in cases(con, h["id"], limit=10_000) if c["status"] == "done" and c["misfit"] is not None]
    if not done:
        raise ToolError(INVALID_INPUT, f"history match {h['name']} has no finished case yet; run hm_base first")
    best = done[0]
    base = next((c for c in done if c["label"] == "base"), None)
    smspec = lambda c: con.execute("SELECT smspec_path FROM hm_cases WHERE id = ?", (c["id"],)).fetchone()[0]  # noqa: E731
    obs = _observations(con, h)
    well_version = con.execute("SELECT well_version_id FROM decks WHERE id = ?", (h["deck_id"],)).fetchone()[0]
    sim = series.from_summary(smspec(best))
    res = mf.compute(obs, sim, h["objective"], mf.honoured(con, well_version))
    folder = hm_dir(cfg.file_store, h) / f"report_{datetime.now():%Y%m%d_%H%M%S}"
    folder.mkdir(parents=True, exist_ok=True)
    obs.to_csv(folder / "observed.csv", index=False)
    sim.to_csv(folder / "simulated.csv", index=False)
    if base and base["id"] != best["id"]:
        series.from_summary(smspec(base)).to_csv(folder / "base.csv", index=False)
    sens = con.execute("SELECT result_json FROM hm_jobs WHERE hm_id = ? AND kind = 'sensitivity' AND status = 'done' "
                       "ORDER BY id DESC LIMIT 1", (h["id"],)).fetchone()
    deck_name = con.execute("SELECT name FROM decks WHERE id = ?", (h["deck_id"],)).fetchone()[0]
    payload = {"schema": 1, "created_at": f"{datetime.now():%Y-%m-%d %H:%M}", "study": h["study"],
               "hm": {"id": h["id"], "name": h["name"], "revision": h["revision"]},
               "run_id": h["base_run_id"], "deck_id": h["deck_id"], "deck_name": deck_name, "source": h["source"],
               "misfit": res["total"], "points": res["n"], "wells": res["wells"], "terms": res["terms"],
               "findings": mf.describe(res), "objective": h["objective"],
               "well_positions": service._well_positions(con, h["deck_id"]),
               "best_case": {k: best[k] for k in ("case_no", "label", "misfit", "values")},
               "base_case": {k: base[k] for k in ("case_no", "label", "misfit", "values")} if base else None,
               "parameters": h["parameters"], "cases": done[:50],
               "tornado": json.loads(sens[0]).get("tornado") if sens else None}
    (folder / "payload.json").write_text(json.dumps(payload, indent=2, default=str))
    out = runner.execute(service.REPORT_TEMPLATE, folder / "hm_report.ipynb", {"payload_path": "payload.json"},
                         cwd=folder, timeout=timeout)
    say = (f"History match {h['name']}: best case {best['case_no']} ({best['label'] or 'search'}) misfit "
           f"{best['misfit']:.3g}" + (f", base {base['misfit']:.3g}" if base else "") + ".")
    return {"ok": True, "hm_id": h["id"], "best_case": payload["best_case"], "base_case": payload["base_case"],
            "findings": payload["findings"], "report": out["html"], "notebook": out["notebook"],
            "report_ok": out["ok"], "report_error": out["error"], "folder": str(folder),
            "spoken_summary": say + (" Report written." if out["ok"] else f" The report failed: {out['error']}")}
