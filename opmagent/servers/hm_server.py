"""History-match MCP server (history matcher): observations, misfit and the HM report on finished runs.

    opmagent-hm [--config opmagent.toml]

Phase 1 of docs/design/history-match.md: read only. It runs no Flow and writes no model, deck or run; only the
report folders under <file_store>/hm/. Variant runs, sensitivities and the optimiser come in later phases, here
and not in opmagent-sim, which runs only validated decks whose initial state a person accepted.
"""
from __future__ import annotations

import argparse
import functools
import threading

from mcp.server.mcpserver import MCPServer

from ..core import config as cfgmod
from ..core import db
from ..core.errors import ToolError
from ..hm import service


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


TOOLS = ["hm_observations", "hm_misfit", "hm_report"]


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
