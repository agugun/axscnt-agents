"""Run a deck with OPM Flow: the `flow` binary if available, else the opm.simulators Python bindings.

Each run works on a copy of the deck in its own folder, so outputs never mix.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_PY_RUN = r"""
import sys
from opm.simulators import BlackOilSimulator
sim = BlackOilSimulator(sys.argv[1])
sim.run()
"""


def command(deck_path: Path, out_dir: Path, flow_bin: str = "flow") -> tuple[list[str], str]:
    exe = shutil.which(flow_bin)
    if exe:
        return [exe, str(deck_path), f"--output-dir={out_dir}"], "flow"
    try:
        import opm.simulators  # noqa: F401
    except ImportError as e:
        raise RuntimeError("neither the flow binary nor opm.simulators is available") from e
    return [sys.executable, "-c", _PY_RUN, str(deck_path)], "opm.simulators (Python)"


def prepare(deck_path: str | Path, out_dir: str | Path) -> Path:
    """Copy the deck into out_dir and symlink its sibling files/folders, so relative INCLUDEs still resolve."""
    src, out_dir = Path(deck_path).resolve(), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / src.name
    shutil.copy2(src, dst)
    for sib in src.parent.iterdir():
        link = out_dir / sib.name
        if sib == src or sib.resolve() == out_dir.resolve() or link.exists() or link.is_symlink() or sib.suffix.upper() in (
                ".SMSPEC", ".UNSMRY", ".UNRST", ".INIT", ".EGRID", ".PRT", ".DBG", ".ESMRY", ".RFT"):
            continue
        try:
            link.symlink_to(sib)
        except OSError:
            pass
    return dst


def start(deck_path: str | Path, out_dir: str | Path, flow_bin: str = "flow") -> tuple[subprocess.Popen, str, Path]:
    """Start a run in the background. Returns (process, engine, log path)."""
    out_dir = Path(out_dir)
    deck = prepare(deck_path, out_dir)
    cmd, engine = command(deck, out_dir, flow_bin)
    log = out_dir / "run.log"
    fh = open(log, "w")
    proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=out_dir)
    return proc, engine, log


_REPORT = re.compile(r"Report step\s+(\d+)/(\d+) at day ([\d.]+)/([\d.]+), date = (\S+)")
_TSTEP = re.compile(r"Starting time step (\d+), stepsize ([\d.]+) days?, at day ([\d.]+)/([\d.]+), date = (\S+)")
_SIMTIME = re.compile(r"^Simulation time:\s+([\d.]+) s", re.M)
STALL_S = 120   # no new log output for this long while running: worth a look


def progress(log_path: str | Path, elapsed_s: float | None = None) -> dict:
    """Where a Flow run is, from its log: report step, simulated date and day, % of simulated time, errors.
    elapsed_s (wall time since start) adds an ETA. Works on a log that is still being written."""
    log = Path(log_path)
    if not log.exists():
        return {"state": "starting", "line": "waiting for the log"}
    text = log.read_text(errors="replace")
    p = {"state": "finished" if "End of simulation" in text else "running",
         "log_age_s": round(time.time() - log.stat().st_mtime, 1),
         "errors": [ln.strip() for ln in text.splitlines() if ln.lstrip().startswith(("Error", "error:"))][-5:]}
    reps, ts = _REPORT.findall(text), _TSTEP.findall(text)
    if reps:
        step, steps, _, days, date = reps[-1]
        p.update(report_step=int(step), report_steps=int(steps), date=date, total_days=float(days))
    if ts:   # timesteps are finer than report steps: use the latest for the simulated day.
        # Its "at day d/D" D is the end of the current report step, not of the run: the total comes from reports
        _, dt, day, _, date = ts[-1]
        p.update(day=float(day), date=date, stepsize_days=float(dt), timesteps=len(ts))
    elif reps:
        p["day"] = float(reps[-1][2])
    if p.get("total_days"):
        p["percent"] = round(100.0 * min(p.get("day", 0.0) / p["total_days"], 1.0), 1)
    m = _SIMTIME.search(text)
    if m:
        p["simulation_seconds"] = float(m.group(1))
    if p["state"] == "finished":
        p["percent"] = 100.0
    if elapsed_s is not None:
        p["elapsed_s"] = round(elapsed_s)
        frac = (p.get("percent") or 0) / 100.0
        if p["state"] == "running" and frac >= 0.02:
            p["eta_s"] = round(elapsed_s * (1 - frac) / frac)
    if p["state"] == "running" and p["log_age_s"] > STALL_S:
        p["stalled"] = True
    p["line"] = progress_line(p)
    return p


def _dur(s: float) -> str:
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else (f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s")


def progress_line(p: dict) -> str:
    """One line for a terminal or a spoken summary: 'step 188/247  03-Jul-2004  73.4%  4m00s elapsed  ~1m30s left'."""
    if p["state"] == "starting":
        return "starting: waiting for the log"
    parts = [p["state"]]
    if "report_step" in p:
        parts.append(f"report step {p['report_step']}/{p['report_steps']}")
    if "date" in p:
        parts.append(p["date"])
    if "percent" in p:
        parts.append(f"{p['percent']:.1f}% of simulated time")
    if "elapsed_s" in p:
        parts.append(f"{_dur(p['elapsed_s'])} elapsed")
    elif "simulation_seconds" in p:
        parts.append(f"Flow simulation time {_dur(p['simulation_seconds'])}")
    if "eta_s" in p:
        parts.append(f"about {_dur(p['eta_s'])} left")
    if p.get("stalled"):
        parts.append(f"NO NEW OUTPUT for {_dur(p['log_age_s'])}")
    if p.get("errors"):
        parts.append(f"{len(p['errors'])} error line(s) in the log")
    return ", ".join(parts)


def run_deck(deck_path: str | Path, out_dir: str | Path, flow_bin: str = "flow", timeout: int = 3600,
             on_progress=None, poll_s: float = 2.0) -> dict:
    """Run to completion (blocking). on_progress(progress_dict) is called whenever the log moves on."""
    out_dir = Path(out_dir)
    t0 = time.time()
    try:
        proc, engine, log = start(deck_path, out_dir, flow_bin)
    except RuntimeError as e:
        return {"ok": False, "engine": None, "message": str(e)}
    if on_progress is None:
        rc = proc.wait(timeout=timeout)
    else:
        last = None
        while (rc := proc.poll()) is None:
            if time.time() - t0 > timeout:
                proc.kill()
                rc = proc.wait()
                break
            p = progress(log, time.time() - t0)
            key = (p.get("timesteps"), p.get("report_step"), p.get("stalled"))
            if key != last:
                on_progress(p)
                last = key
            time.sleep(poll_s)
        on_progress(progress(log, time.time() - t0))
    smspec = out_dir / (Path(deck_path).stem + ".SMSPEC")
    text = log.read_text(errors="replace")
    return {"ok": rc == 0 and smspec.exists(), "engine": engine, "seconds": round(time.time() - t0, 1),
            "returncode": rc, "log": str(log), "smspec": str(smspec) if smspec.exists() else None,
            "log_tail": text[-2000:]}


def summary_values(smspec: str | Path, vectors: list[str]) -> dict:
    """Last value and full series of each available vector."""
    from opm.io.ecl import ESmry
    s = ESmry(str(smspec))
    out = {}
    keys = set(s.keys())
    for v in vectors:
        if v in keys:
            series = s[v]
            out[v] = {"last": float(series[-1]), "series": [float(x) for x in series]}
    out["TIME"] = [float(x) for x in s["TIME"]] if "TIME" in keys else None
    return out


def read_all(smspec: str | Path) -> tuple[list[float], dict[str, list[float]]]:
    """All summary vectors: (time in days, {vector: values})."""
    from opm.io.ecl import ESmry
    s = ESmry(str(smspec))
    t = [float(x) for x in s["TIME"]]
    out = {}
    for k in s.keys():
        if k in ("TIME", "YEARS", "DAY", "MONTH", "YEAR", "TIMESTEP"):
            continue
        try:
            out[k] = [float(x) for x in s[k]]
        except Exception:  # noqa: BLE001
            continue
    return t, out
