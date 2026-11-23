"""Match proposals: the parameter values a history match found, offered to the engineer, turned into requests to
the modelers on acceptance, and confirmed once the modelers' new versions have run.

  hm_propose   the best case (or one named), its values rounded to what a modeler would type (3 significant
               figures by default), evaluated as its own case so the proposal is exactly what will be built.
               One open proposal per match.
  review       the engineer only (`opmagent review hm`): accept raises one study request per owner (from the
               study's match task, kind 'match'); reject sends the comment back to the matcher.
  hm_confirm   a run of the deck built from the modelers' new versions against the proposal's case: equal
               (rel_tol) -> confirmed, which lets the study's match task complete; not equal -> differs.
"""
from __future__ import annotations

import getpass
import json
import math
import os

import numpy as np

from ..core import db
from ..core.errors import FORBIDDEN, INVALID_INPUT, NOT_FOUND, ToolError
from . import jobs, series, service

OPEN = ("proposed", "accepted")
REL_TOL = 1e-4


def _round(x: float, digits: int) -> float:
    if x == 0 or not math.isfinite(x):
        return x
    return round(x, digits - 1 - int(math.floor(math.log10(abs(x)))))


def get(con, proposal_id: int) -> dict:
    r = con.execute("SELECT * FROM hm_proposals WHERE id = ?", (proposal_id,)).fetchone()
    if not r:
        raise ToolError(NOT_FOUND, f"match proposal {proposal_id} not found")
    p = dict(r)
    for k in ("values_json", "flags_json", "requests_json", "confirm_json"):
        p[k.removesuffix("_json")] = json.loads(p.pop(k)) if p[k] else None
    return p


def change_lines(h: dict, values: dict) -> dict[str, list[str]]:
    """What each owner is asked to change, in the modeler's words."""
    out: dict[str, list[str]] = {}
    for p in h["parameters"]:
        v = values[p["name"]]
        if p["type"] == "mult" and v == 1.0 or p["type"] == "value" and v == p["start"]:
            continue
        t = p["target"]
        where = (f"zone {t['zone']}" if t.get("zone") else f"layers {t['layers']}" if t.get("layers") else
                 f"fault {t['fault']}" if t.get("fault") else f"SATNUM {t['satnum']}" if "satnum" in t else
                 f"aquifer {t['aquifer']}" if "aquifer" in t else "all cells")
        k = p["kind"]
        if k == "permx_mult":
            line = f"PERMX{'' if t.get('permy') is False else ' and PERMY'} x {v:g} in {where}"
        elif k in ("permz_mult", "poro_mult"):
            line = f"{k.split('_')[0].upper()} x {v:g} in {where}"
        elif k == "multflt":
            line = f"MULTFLT {v:g} for {where}"
        elif k.startswith("aquifer_"):
            line = f"{'water volume' if 'volume' in k else 'productivity index'} x {v:g} for {where}"
        else:
            name = {"corey_nw": "Corey nw", "corey_now": "Corey now", "corey_ng": "Corey ng", "corey_nog": "Corey nog"}
            line = f"{name.get(k, k)} {v:g} in {where} (was {p['start']:g})"
        out.setdefault(p["owner"], []).append(line)
    return out


def propose(cfg, con, ref, case: int | None = None, note: str | None = None, digits: int = 3) -> dict:
    h = jobs.get_hm(con, ref)
    open_ = con.execute(f"SELECT id, status FROM hm_proposals WHERE hm_id = ? AND status IN {OPEN}",
                        (h["id"],)).fetchone()
    if open_:
        raise ToolError(jobs.BUSY, f"proposal {open_['id']} is {open_['status']}; one open proposal per match "
                                   "(a rejection lets you propose again)")
    q = ("SELECT * FROM hm_cases WHERE hm_id = ? AND case_no = ?" if case is not None else
         "SELECT * FROM hm_cases WHERE hm_id = ? AND status = 'done' AND misfit IS NOT NULL ORDER BY misfit LIMIT 1")
    src = con.execute(q, (h["id"], case) if case is not None else (h["id"],)).fetchone()
    if not src:
        raise ToolError(NOT_FOUND, f"history match {h['name']} has no {'case ' + str(case) if case is not None else 'finished case'}")
    if src["status"] != "done":
        raise ToolError(INVALID_INPUT, f"case {src['case_no']} is {src['status']}; never propose from a failed case")
    vals = {k: _round(float(v), digits) for k, v in json.loads(src["values_json"]).items()}
    pmap = {p["name"]: p for p in h["parameters"]}
    for k, v in vals.items():          # rounding must not leave the range
        vals[k] = min(max(v, pmap[k]["low"]), pmap[k]["high"])
    # the proposal is a case of its own: the rounded values, evaluated
    job = jobs.create_job(con, h, "base", {"values": vals, "method": "proposal"})
    j = jobs.Runner(cfg, job).run()
    pc = con.execute("SELECT * FROM hm_cases WHERE hm_id = ? AND values_key = ?",
                     (h["id"], jobs.pm.values_key({**jobs.start_values(h), **vals}))).fetchone()
    if j["status"] != "done" or not pc or pc["status"] != "done":
        raise ToolError(INVALID_INPUT, f"the proposal case (rounded values) did not run: "
                                       f"{(pc and pc['error']) or j.get('error') or j['status']}")
    base = con.execute("SELECT id, misfit FROM hm_cases WHERE hm_id = ? AND label = 'base' AND status = 'done'",
                       (h["id"],)).fetchone()
    at_bound = [p["name"] for p in h["parameters"] if p["active"] and (vals[p["name"]] in (p["low"], p["high"]))]
    worse = []
    if base:
        wm = lambda cid: {r["well"]: r["m"] for r in con.execute(  # noqa: E731
            "SELECT well, SUM(sum_sq) / SUM(n) AS m FROM hm_misfits WHERE case_id = ? AND in_total = 1 GROUP BY well",
            (cid,))}
        b, n = wm(base["id"]), wm(pc["id"])
        worse = sorted(w for w in n if w in b and n[w] > b[w] * 1.05 and n[w] - b[w] > 0.01)
    changes = change_lines(h, vals)
    flags = {"at_bound": at_bound, "wells_worse": worse, "from_case": src["case_no"],
             "from_case_misfit": src["misfit"], "digits": digits}
    cur = con.execute("INSERT INTO hm_proposals(hm_id, case_id, values_json, note, flags_json, requests_json) "
                      "VALUES (?,?,?,?,?,?)", (h["id"], pc["id"], json.dumps(vals), note, json.dumps(flags),
                                               json.dumps({"changes": changes})))
    con.execute("UPDATE hm_studies SET status = 'proposed' WHERE id = ?", (h["id"],))
    pid = cur.lastrowid
    rep = None
    try:
        rep = jobs.write_match_report(cfg, con, h["id"], case_no=pc["case_no"])
    except ToolError:
        pass
    if rep:
        con.execute("UPDATE hm_proposals SET report_path = ? WHERE id = ?", (rep["report"], pid))
    say = (f"Proposal {pid} for {h['name']}: misfit {pc['misfit']:.3g}"
           + (f" (base {base['misfit']:.3g})" if base else "") + ". "
           + " ".join(f"{o}: {'; '.join(ls)}." for o, ls in changes.items())
           + (f" At a bound: {', '.join(at_bound)}." if at_bound else "")
           + (f" Worse than the base: {', '.join(worse)}." if worse else "")
           + f" The engineer reviews it with: opmagent review hm show {pid}.")
    return {"ok": True, "proposal_id": pid, "case_no": pc["case_no"], "misfit": pc["misfit"],
            "base_misfit": base["misfit"] if base else None, "values": vals, "changes": changes, "flags": flags,
            "report": rep and rep["report"], "spoken_summary": say}


def show(con, proposal_id: int) -> dict:
    p = get(con, proposal_id)
    h = jobs.get_hm(con, p["hm_id"])
    c = con.execute("SELECT case_no, misfit FROM hm_cases WHERE id = ?", (p["case_id"],)).fetchone()
    base = con.execute("SELECT misfit FROM hm_cases WHERE hm_id = ? AND label = 'base'", (h["id"],)).fetchone()
    return {**p, "hm": {"id": h["id"], "name": h["name"], "revision": h["revision"], "study": h["study"],
                        "deck_id": h["deck_id"]},
            "case_no": c["case_no"], "misfit": c["misfit"], "base_misfit": base["misfit"] if base else None}


def _reviewer() -> str:
    return os.environ.get("OPMAGENT_REVIEWER") or getpass.getuser()


def decide(con, proposal_id: int, decision: str, comment: str | None = None, reviewer: str | None = None) -> dict:
    """The engineer's decision (opmagent review hm, never an agent). accept: one request per owner to the
    study's modelers; reject: the comment back to the matcher."""
    from ..core import studies
    p = get(con, proposal_id)
    if p["status"] != "proposed":
        raise ToolError(INVALID_INPUT, f"proposal {proposal_id} is {p['status']}; only a proposed one is decided")
    if decision == "reject" and not (comment or "").strip():
        raise ToolError(INVALID_INPUT, "a rejection needs a comment: what is wrong with it")
    h = jobs.get_hm(con, p["hm_id"])
    who = reviewer or _reviewer()
    raised = []
    with db.transaction(con):
        if decision == "accept":
            changes = (p["requests"] or {}).get("changes") or {}
            if h["study"] and changes:
                t = con.execute("SELECT t.status FROM study_tasks t JOIN studies s ON s.id = t.study_id "
                                "WHERE s.name = ? AND t.task = 'match'", (h["study"],)).fetchone()
                if not t:
                    raise ToolError(INVALID_INPUT, f"study {h['study']} has no match task (made before history "
                                                   "matching); ask the modelers yourself")
                for owner, lines in changes.items():
                    msg = (f"History match {h['name']} proposal {proposal_id} (accepted by {who}): "
                           + "; ".join(lines) + ". Build a new version with exactly these values and say in its "
                           f"assumptions that they come from history match proposal {proposal_id}.")
                    st = studies.raise_request(con, h["study"], "match", owner, "match", msg, actor=who)
                    raised.append({"request_id": st["request_id"], "to": owner, "message": msg})
            con.execute("UPDATE hm_proposals SET status = 'accepted', reviewer = ?, comment = ?, decided_at = "
                        "datetime('now'), requests_json = ? WHERE id = ?",
                        (who, comment, json.dumps({"changes": changes, "raised": raised}), proposal_id))
        else:
            con.execute("UPDATE hm_proposals SET status = 'rejected', reviewer = ?, comment = ?, decided_at = "
                        "datetime('now') WHERE id = ?", (who, comment, proposal_id))
            con.execute("UPDATE hm_studies SET status = 'frozen' WHERE id = ?", (h["id"],))
    out = show(con, proposal_id)
    out["raised"] = raised
    return out


def confirm(con, proposal_id: int, run_id: int, rel_tol: float = REL_TOL) -> dict:
    """Compare a run of the rebuilt deck with the proposal's case on every matched vector, monthly."""
    p = get(con, proposal_id)
    if p["status"] not in ("accepted", "differs", "confirmed"):
        raise ToolError(INVALID_INPUT, f"proposal {proposal_id} is {p['status']}: confirm an accepted proposal")
    h = jobs.get_hm(con, p["hm_id"])
    r = service.run_row(con, run_id)
    if r["deck_id"] == h["deck_id"]:
        raise ToolError(INVALID_INPUT, f"run {run_id} is of the base deck {h['deck_id']}; confirm with a run of the "
                                       "deck built from the modelers' new versions")
    case = con.execute("SELECT smspec_path, case_no FROM hm_cases WHERE id = ?", (p["case_id"],)).fetchone()
    a = series.from_summary(case["smspec_path"])
    b = series.from_summary(r["smspec_path"])
    m = a.merge(b[["well", "quantity", "month", "value"]].rename(columns={"value": "run"}),
                on=["well", "quantity", "month"], how="outer")
    missing = m[m["value"].isna() | m["run"].isna()]
    m = m.dropna(subset=["value", "run"])
    scale = m.groupby(["well", "quantity"])["value"].transform(lambda s: max(float(np.abs(s).max()), 1e-12))
    diff = (m["run"] - m["value"]).abs() / scale
    bad = m[diff > rel_tol].assign(rel=diff[diff > rel_tol]).sort_values("month")
    ok = bad.empty and missing.empty
    detail = {"run_id": run_id, "deck_id": r["deck_id"], "case_no": case["case_no"], "rel_tol": rel_tol,
              "compared": int(len(m)), "max_rel_diff": float(diff.max()) if len(diff) else 0.0,
              "missing": missing[["well", "quantity", "month"]].head(10).to_dict("records"),
              "differs": [{"well": x["well"], "quantity": x["quantity"], "first_month": x["month"],
                           "rel_diff": float(x["rel"])}
                          for x in bad.drop_duplicates(["well", "quantity"]).head(10).to_dict("records")]}
    status = "confirmed" if ok else "differs"
    con.execute("UPDATE hm_proposals SET status = ?, confirm_run_id = ?, confirm_json = ? WHERE id = ?",
                (status, run_id, json.dumps(detail), proposal_id))
    if ok:
        con.execute("UPDATE hm_studies SET status = 'confirmed' WHERE id = ?", (h["id"],))
    say = (f"Proposal {proposal_id} confirmed: run {run_id} equals case {case['case_no']} on {len(m)} monthly values "
           f"(largest difference {detail['max_rel_diff']:.1e})." if ok else
           f"Proposal {proposal_id} differs: run {run_id} is not case {case['case_no']}" +
           (f"; first {detail['differs'][0]['well']} {detail['differs'][0]['quantity']} from "
            f"{detail['differs'][0]['first_month']} ({detail['differs'][0]['rel_diff']:.1e})" if detail["differs"]
            else f"; {len(missing)} value(s) only in one of them") +
           ". A finding for the engineer and the modelers, not something the matcher fixes.")
    return {"ok": True, "proposal_id": proposal_id, "status": status, **detail, "spoken_summary": say}


def require_confirmed(con, study: str) -> dict:
    """The study's match task completes only with a confirmed proposal (or skipped: no history)."""
    r = con.execute("SELECT p.id, p.hm_id FROM hm_proposals p JOIN hm_studies h ON h.id = p.hm_id "
                    "WHERE h.study = ? AND p.status = 'confirmed' ORDER BY p.id DESC LIMIT 1", (study,)).fetchone()
    if not r:
        raise ToolError(FORBIDDEN, f"no confirmed match proposal for study {study}: hm_confirm a run of the rebuilt "
                                   "deck first (or complete with skipped if the study has no history)")
    return {"proposal_id": r["id"], "hm_id": r["hm_id"]}
