"""Run a deck with OPM Flow: the `flow` binary if available, else the opm.simulators Python bindings."""
from __future__ import annotations

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


def run_deck(deck_path: str | Path, out_dir: str | Path, flow_bin: str = "flow", timeout: int = 3600) -> dict:
    src, out_dir = Path(deck_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    deck_path = out_dir / src.name          # run a copy: each run keeps its own outputs
    shutil.copy2(src, deck_path)
    exe = shutil.which(flow_bin)
    if exe:
        cmd, engine = [exe, str(deck_path), f"--output-dir={out_dir}"], "flow"
    else:
        try:
            import opm.simulators  # noqa: F401
        except ImportError:
            return {"ok": False, "engine": None, "message": "neither the flow binary nor opm.simulators is available"}
        cmd, engine = [sys.executable, "-c", _PY_RUN, str(deck_path)], "opm.simulators (Python)"
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=out_dir)
    log = out_dir / "run.log"
    log.write_text(r.stdout + r.stderr)
    smspec = out_dir / (deck_path.stem + ".SMSPEC")
    return {"ok": r.returncode == 0 and smspec.exists(), "engine": engine, "seconds": round(time.time() - t0, 1),
            "returncode": r.returncode, "log": str(log), "smspec": str(smspec) if smspec.exists() else None,
            "log_tail": (r.stdout + r.stderr)[-2000:]}


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
