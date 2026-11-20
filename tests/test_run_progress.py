"""Live run progress parsed from Flow's log: report step, simulated date, percent, ETA, stall, errors."""
import os
import time

import pytest

from opmagent.sim import runner

LOG = """Report step  0/4 at day 0/100, date = 01-Jan-2026
Starting time step 0, stepsize 1 days, at day 0/25, date = 01-Jan-2026
 Newton its= 3, linearizations= 4 (0.1sec), linear its=  6 (0.1sec)
Report step  1/4 at day 25/100, date = 26-Jan-2026
Starting time step 0, stepsize 10 days, at day 25/50, date = 26-Jan-2026
Starting time step 1, stepsize 15 days, at day 35/50, date = 05-Feb-2026
"""
END = """
================    End of simulation     ===============
Simulation time:               12.50 s
"""


def test_progress_while_running(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(LOG)
    p = runner.progress(log, elapsed_s=70)
    assert p["state"] == "running" and (p["report_step"], p["report_steps"]) == (1, 4)
    # the timestep line's '/50' is the end of the current report step: the total comes from the report line
    assert p["day"] == 35 and p["total_days"] == 100 and p["percent"] == 35.0 and p["date"] == "05-Feb-2026"
    assert p["timesteps"] == 3 and p["eta_s"] == 130          # 70 s for 35 % -> 130 s for the other 65 %
    assert "report step 1/4" in p["line"] and "about 2m10s left" in p["line"]
    assert not p.get("stalled")


def test_progress_stall_errors_and_finish(tmp_path):
    log = tmp_path / "run.log"
    log.write_text(LOG + "Error: Linear solver did not converge\n")
    old = time.time() - runner.STALL_S - 30
    os.utime(log, (old, old))
    p = runner.progress(log, elapsed_s=500)
    assert p["stalled"] and "NO NEW OUTPUT" in p["line"] and p["errors"] == ["Error: Linear solver did not converge"]
    log.write_text(LOG + END)
    p = runner.progress(log)
    assert p["state"] == "finished" and p["percent"] == 100.0 and p["simulation_seconds"] == 12.5
    assert "eta_s" not in p and "Flow simulation time 12s" in p["line"]
    assert runner.progress(tmp_path / "missing.log")["state"] == "starting"


def test_run_deck_reports_progress(cfg, tmp_path):
    pytest.importorskip("opm.simulators")
    from tests.test_importer import hello_deck
    seen = []
    r = runner.run_deck(hello_deck(cfg), tmp_path / "run", cfg.flow_bin, on_progress=seen.append, poll_s=0.2)
    assert r["ok"], r
    assert seen and seen[-1]["state"] == "finished" and seen[-1]["percent"] == 100.0
