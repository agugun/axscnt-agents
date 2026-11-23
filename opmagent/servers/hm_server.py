"""History-match MCP server (history matcher): observations, misfit and the HM report on finished runs.

    opmagent-hm [--config opmagent.toml]

Phases 1-2 of docs/design/history-match.md. It writes no model, deck or run: only its own tables (hm_studies,
hm_parameters, hm_jobs, hm_cases, hm_misfits) and the folders under <file_store>/hm/. It runs Flow on variants
(the base deck with parameter values applied in memory), so it is separate from opmagent-sim, which runs only
validated decks whose initial state a person accepted. The optimiser and match proposals come in phase 3.
"""
from __future__ import annotations

import argparse
import functools
import re
import threading

from mcp.server.mcpserver import MCPServer

from ..core import config as cfgmod
from ..core import db
from ..core.errors import ToolError
from ..hm import jobs, params, proposals, service


class HMService:
    def __init__(self, cfg: cfgmod.Config):
        self.cfg = cfg
        self.con = db.connect(cfg.database)
        self.lock = threading.Lock()

    def hm_observations(self, study: str | None = None, run_id: int | None = None, source: str = "auto",
                        truth_run_id: int | None = None, wells: list[str] | None = None) -> dict:
        """What can be matched, before any search: the observation source (data_production: the study's measured
        history; deck_history: what the run's deck gave Flow under WCONHIST / WCONINJH; run: another run as a
        synthetic truth, with truth_run_id), the period, per well the quantities with data, and the history
        control, so which quantities are honoured (given to Flow) rather than matched. Give run_id to see the
        controls. Fails with NO_HISTORY if there is nothing to match."""
        inv = service.observation_inventory(self.con, study=study, run_id=run_id, source=source,
                                            truth_run_id=truth_run_id, wells=wells)
        hon = sorted({f"{w['well']} {', '.join(w['honoured'])}" for w in inv["wells"] if w["honoured"]})
        spoken = (f"{len(inv['wells'])} well(s) with {inv['source'].replace('_', ' ')}, "
                  f"{inv['period'][0]} to {inv['period'][1]}."
                  + (f" Honoured by the history control, not matched: {'; '.join(hon[:4])}." if hon else ""))
        return {"ok": True, **inv, "spoken_summary": spoken}

    def hm_misfit(self, run_id: int, study: str | None = None, source: str = "auto",
                  truth_run_id: int | None = None, objective: dict | None = None, max_terms: int = 20) -> dict:
        """Misfit of a finished run against the observations, per well and quantity: normalised sum of squares
        with σ = max(rel · |obs|, abs) (defaults: rates 10 % with a floor of 1 sm3/d, 100 for gas; BHP 5 bar),
        quantities the history control honours left out of the total. Also cumulative error per well and phase,
        water breakthrough (simulated against observed), BHP error at the last measurement, and the worst terms in
        words. objective overrides: weights, sigma (per quantity {rel, abs}), period [first, last month],
        exclude_wells, include_honoured."""
        return service.public(service.run_misfit(self.con, run_id, study=study, source=source,
                                                 truth_run_id=truth_run_id, objective=objective), max_terms)

    def hm_report(self, run_id: int, study: str | None = None, source: str = "auto",
                  truth_run_id: int | None = None, objective: dict | None = None) -> dict:
        """The HM report of a run (an executed notebook, exported to HTML): per well observed against simulated
        rates and BHP with the measurement band and water breakthrough, field cumulatives, misfit per well as bars
        and on a map at the well positions. Returns the report file and the main findings."""
        return service.write_report(self.cfg, self.con, run_id, study=study, source=source,
                                    truth_run_id=truth_run_id, objective=objective)


    # ------------------------------------------------------------ phase 2: setups, variants, sensitivities
    def hm_targets(self, deck_id: int) -> dict:
        """What parameters can act on in a deck's models, with current values: zones and layers (mean PERMX,
        PERMZ, PORO), faults (MULTFLT), SATNUMs (Corey parameters, or tabulated: no Corey parameters to change),
        aquifers (volume, PI), and the parameter kinds (owner, mult or value). Propose parameters from this; a
        target that isn't here is refused."""
        t = params.targets(self.con, deck_id)
        return {"ok": True, **t, "spoken_summary": (
            f"Deck {deck_id}: {len(t['zones'])} zone(s), {len(t['layers'])} layer(s), {len(t['faults'])} fault(s), "
            f"{len(t['satnums'])} SATNUM(s) ({sum(1 for s in t['satnums'] if not s['tabulated'])} Corey), "
            f"{len(t['aquifers'])} aquifer(s).")}

    def hm_setup(self, name: str, deck_id: int, parameters: list[dict], study: str | None = None,
                 source: str = "auto", truth_run_id: int | None = None, objective: dict | None = None) -> dict:
        """Check and store a history-match setup on a validated deck. parameters: [{name, kind, target, low, high,
        start?, scale?}] with kinds from hm_targets (permx_mult {zone|layers}, permz_mult, poro_mult, multflt {fault},
        corey_nw / corey_now / krw_max / sorw ... {satnum}, aquifer_volume_mult / aquifer_pi_mult {aquifer}). A
        multiplier starts at 1, a value at the model's current value; multipliers default to a log scale.
        Returns a read_back: say it to the engineer and start nothing until they say yes. Ranges are theirs: never
        widen one yourself. A frozen setup (a search has run) gets a new revision."""
        r = jobs.setup(self.con, name, deck_id, parameters, study=study, source=source, truth_run_id=truth_run_id,
                       objective=objective)
        return {"ok": True, **r, "spoken_summary": r["read_back"] + " Say yes to start."}

    def hm_resolve(self, text: str) -> dict:
        """Find a history match by spoken name ('the Norne match')."""
        import difflib
        words = re.sub(r"\b(the|match|history|hm)\b", " ", text.lower())
        joined = re.sub(r"[\s_\-]+", "", words)
        rows = [dict(r) for r in self.con.execute("SELECT id, name, revision, status, deck_id FROM hm_studies "
                                                   "ORDER BY id DESC")]
        for r in rows:
            r["score"] = round(difflib.SequenceMatcher(None, joined, re.sub(r"[\s_\-]+", "", r["name"].lower()))
                               .ratio(), 2)
        rows = sorted([r for r in rows if r["score"] >= 0.4], key=lambda r: (-r["score"], -r["revision"]))
        if not rows:
            raise ToolError("NOT_FOUND", f"no history match matches '{text}'")
        top = rows[0]
        return {"ok": True, "match": top, "candidates": rows[:5],
                "spoken_summary": f"History match {top['name']} revision {top['revision']} ({top['status']})."}

    def _start(self, hm, kind: str, wait: bool, options: dict | None = None) -> dict:
        h = jobs.get_hm(self.con, hm)
        job = jobs.create_job(self.con, h, kind, options)
        n = len(jobs.plan(h, kind)) if kind != "optimise" else (options or {}).get("budget_runs", 200)
        if wait:
            j = jobs.Runner(self.cfg, job).run()
        else:
            jobs.start_job(self.cfg, job)
            j = jobs.get_job(self.con, job)
        return {"ok": True, "job_id": job, "hm_id": h["id"], "cases": n, "slots": jobs.slots(self.cfg), "job": j,
                "spoken_summary": jobs.spoken_job(j) if wait else
                (f"Started job {job}: {n} case(s), {jobs.slots(self.cfg)} at a time. Check it with hm_status.")}

    def hm_base(self, hm: str, wait: bool = True) -> dict:
        """Case 0 of a setup: the base deck's misfit. It uses the deck's latest finished run (from the run agent);
        without one it runs the base deck as a variant. Freezes the setup."""
        r = self._start(hm, "base", wait)
        if wait and r["job"]["status"] == "done":
            h = jobs.get_hm(self.con, hm)
            c = jobs.cases(self.con, h["id"], order="case", limit=1)
            r["base"] = c[0] if c else None
            if c and c[0]["misfit"] is not None:
                r["spoken_summary"] = f"Base case of {h['name']}: misfit {c[0]['misfit']:.3g} over {c[0]['points']} points."
        return r

    def hm_sensitivity(self, hm: str, wait: bool = False) -> dict:
        """Start the sensitivity: each active parameter at its low and high end, the others at their start (2N
        cases plus the base), in parallel within the [hm] capacity, as a background job. Returns the job id; follow
        it with hm_status. The result is the tornado: misfit change per parameter end, ranked by how much each
        lowers the misfit, and the parameters with no effect (drop those with the engineer's yes)."""
        return self._start(hm, "sensitivity", wait)

    def hm_status(self, job_id: int) -> dict:
        """A job's progress: cases done, running, failed and left, the best misfit so far, time left; when done, its
        result (the base misfit, the tornado)."""
        j = jobs.get_job(self.con, job_id)
        return {"ok": True, "job": j, "spoken_summary": jobs.spoken_job(j)}

    def hm_stop(self, job_id: int, now: bool = False) -> dict:
        """Stop a job after its running cases finish (now=True: kill them). Starting the same job kind again
        continues: finished cases are reused."""
        j = jobs.stop_job(self.con, job_id, now)
        return {"ok": True, "job": j, "spoken_summary": f"Job {job_id} will stop" + (" now." if now else
                                                                                       " after its running cases.")}

    def hm_cases(self, hm: str, order: str = "misfit", limit: int = 10) -> dict:
        """The evaluated cases of a history match, best first (order='case' for case order): values, misfit,
        status."""
        h = jobs.get_hm(self.con, hm)
        cs = jobs.cases(self.con, h["id"], order, limit)
        best = next((c for c in cs if c["misfit"] is not None), None)
        return {"ok": True, "hm_id": h["id"], "cases": cs, "spoken_summary": (
            f"{len(cs)} case(s)" + (f"; best case {best['case_no']} ({best['label'] or 'search'}) misfit "
                                    f"{best['misfit']:.3g}" if best else "") + ".")}

    def hm_case(self, hm: str, case: int) -> dict:
        """One case in detail: its values, status, misfit terms per well and quantity, run folder, and for a failed
        case the end of its Flow log."""
        h = jobs.get_hm(self.con, hm)
        c = jobs.case_detail(self.con, h["id"], case)
        return {"ok": True, "case": c, "spoken_summary": (
            f"Case {case} is {c['status']}" + (f", misfit {c['misfit']:.3g}" if c["misfit"] is not None else "")
            + (f": {c['error'].splitlines()[-1]}" if c.get("error") else "") + ".")}

    def hm_match_report(self, hm: str) -> dict:
        """The HM report of a history match: the best case against the observations with the base case beside
        it, misfit per well, the sensitivity tornado and the parameter table (best values, flags at bounds)."""
        return jobs.write_match_report(self.cfg, self.con, hm)


    # ------------------------------------------------------------ phase 3: optimiser, proposals, confirmation
    def hm_optimise(self, hm: str, method: str = "lsq", start: str = "best", budget_runs: int = 200,
                    budget_hours: float | None = None, target_misfit: float | None = None,
                    wait: bool = False) -> dict:
        """Start the search as a background job: bounded least squares (trust region) on the weighted residuals,
        log parameters searched in log space, the Jacobian from forward differences (N cases per iteration, in
        parallel). start='best' begins from the best case so far, 'start' from the setup's start values. Stops at
        convergence, target_misfit, budget_runs (cases run by this search) or budget_hours. Every evaluation is a
        case, so a stopped search continues with the same call. method 'esmda' comes later (phase 5)."""
        if method != "lsq":
            raise ToolError("INVALID_INPUT", "method: 'lsq' (the ensemble smoother, 'esmda', comes in phase 5)")
        if start not in ("best", "start"):
            raise ToolError("INVALID_INPUT", "start: 'best' or 'start'")
        r = self._start(hm, "optimise", wait, {"method": "lsq", "start": start, "budget_runs": int(budget_runs),
                                                "budget_hours": budget_hours, "target_misfit": target_misfit})
        if not wait:
            r["spoken_summary"] = (f"Started the search, job {r['job_id']}: up to {budget_runs} case(s), "
                                   f"{r['slots']} at a time. Check it with hm_status.")
        return r

    def hm_propose(self, hm: str, case: int | None = None, note: str | None = None, digits: int = 3) -> dict:
        """Make a match proposal from the best case (or case): its values rounded to what a modeler would type
        (digits significant figures, default 3), run as a case of its own, written as the changes per owner
        ("geo: PERMX and PERMY x 0.5 in layers 1"), with flags for parameters at a bound and wells that got worse,
        and its report. One open proposal per match. It raises no request: the engineer reviews it
        (opmagent review hm show <id>) and accepting it sends the requests to the modelers."""
        return proposals.propose(self.cfg, self.con, hm, case=case, note=note, digits=digits)

    def hm_proposal(self, proposal_id: int) -> dict:
        """A match proposal: its values, changes per owner, flags, status (proposed, accepted, rejected with the
        engineer's comment, confirmed, differs) and the requests raised."""
        p = proposals.show(self.con, proposal_id)
        say = f"Proposal {proposal_id} is {p['status']}" + (f": {p['comment']}" if p.get("comment") else "") + "."
        return {"ok": True, "proposal": p, "spoken_summary": say}

    def hm_confirm(self, proposal_id: int, run_id: int) -> dict:
        """After the modelers' new versions are approved and the deck built from them has run: compare that run
        with the proposal's case on every matched quantity, monthly (rel_tol 1e-4). Equal: confirmed, and the
        study's match task can complete. Not equal: differs, with the first quantities and months that part, a
        finding for the engineer and the modelers, not something you fix."""
        return proposals.confirm(self.con, proposal_id, run_id)


TOOLS = ["hm_optimise", "hm_propose", "hm_proposal", "hm_confirm", "hm_observations", "hm_misfit", "hm_report", "hm_targets", "hm_setup", "hm_resolve", "hm_base",
         "hm_sensitivity", "hm_status", "hm_stop", "hm_cases", "hm_case", "hm_match_report"]


def wrap(svc: HMService, name: str):
    fn = getattr(svc, name)

    @functools.wraps(fn)
    def tool(**kwargs):
        with svc.lock:
            try:
                return fn(**kwargs)
            except ToolError as e:
                return e.to_dict()
    return tool


def build_server(cfg) -> tuple[MCPServer, HMService]:
    svc = HMService(cfg)
    server = MCPServer("opmagent-hm", instructions="History matching: observations, misfit per well and quantity, "
                                                   "and the HM report on finished runs. Read only.")
    for name in TOOLS:
        server.add_tool(wrap(svc, name), name=name, description=getattr(HMService, name).__doc__)
    return server, svc


def main(argv=None):
    p = argparse.ArgumentParser(prog="opmagent-hm")
    p.add_argument("--config")
    a = p.parse_args(argv)
    server, _ = build_server(cfgmod.load(a.config))
    server.run("stdio")


if __name__ == "__main__":
    main()
