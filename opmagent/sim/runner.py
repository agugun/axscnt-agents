"""Run a deck with OPM Flow: the `flow` binary if available, else the opm.simulators Python bindings.

Each run works on a copy of the deck in its own folder, so outputs never mix.
"""
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
        if sib == src or link.exists() or link.is_symlink() or sib.suffix.upper() in (
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


def run_deck(deck_path: str | Path, out_dir: str | Path, flow_bin: str = "flow", timeout: int = 3600) -> dict:
    """Run to completion (blocking)."""
    out_dir = Path(out_dir)
    t0 = time.time()
    try:
        proc, engine, log = start(deck_path, out_dir, flow_bin)
    except RuntimeError as e:
        return {"ok": False, "engine": None, "message": str(e)}
    rc = proc.wait(timeout=timeout)
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
