"""Results viewer: open a finished run, or a geo version's static view, in ResInsight.

ResInsight reads Flow's output directly: the grid (.EGRID), static properties (.INIT), properties over time
(.UNRST) and summary vectors (.SMSPEC/.UNSMRY) next to it. Set the launcher in opmagent.toml:

    [viewer]
    resinsight_bin = "~/opm-agents/ResInsight/bin/resinsight"

The prebuilt Linux ResInsight bundles Qt with the X11 platform plugin only, so on a Wayland desktop it runs
through XWayland: QT_QPA_PLATFORM=xcb is set unless you set it yourself.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .core import db, studies


def _egrid(path: Path) -> Path | None:
    path = Path(path).expanduser()
    if path.is_dir():
        found = sorted(path.glob("*.EGRID"))
        return found[0] if found else None
    if path.suffix.upper() == ".EGRID":
        return path if path.exists() else None
    cand = path.with_suffix(".EGRID")
    return cand if cand.exists() else None


def find_case(cfg, run_id: int | None = None, study: str | None = None, case: str | None = None,
              geo: int | None = None, deck: int | None = None) -> tuple[Path | None, str]:
    """The EGRID to open, and a short description of where it came from. geo: a geo version's static view (an
    OPM Flow dry run of its static model), made now if it isn't there yet."""
    if case:
        return _egrid(Path(case)), f"case {case}"
    con = db.connect(cfg.database)
    if geo is not None:
        from .core.errors import ToolError
        from .geo.static_view import current_case
        try:
            egrid = current_case(cfg, con, geo)
        except ToolError as e:
            return None, f"geo version {geo}: {e} (database {cfg.database}; is OPMAGENT_CONFIG or --config the workspace you mean?)"
        return egrid, f"static view of geo version {geo}"
    if deck is not None:
        from .core.errors import ToolError
        from .deck.init_view import build_init_view, init_case
        egrid = init_case(cfg, con, deck)
        if egrid is None:
            try:
                egrid = Path(build_init_view(cfg, con, deck)["egrid"])
            except ToolError as e:
                return None, f"deck {deck}: {e} (database {cfg.database})"
        return egrid, f"initialisation view of deck {deck} (time zero)"
    if study and run_id is None:
        res = {t["task"]: t["result"] for t in studies.status(con, study)["tasks"]}
        run_id = (res.get("run") or {}).get("run_id")
        if run_id is None:
            return None, f"study {study} has no finished run yet"
    if run_id is None:
        r = con.execute("SELECT id FROM runs WHERE status IN ('done','stored') ORDER BY id DESC LIMIT 1").fetchone()
        if not r:
            return None, "no finished runs yet"
        run_id = r["id"]
    r = con.execute("SELECT r.out_dir, r.status, d.name FROM runs r JOIN decks d ON d.id = r.deck_id WHERE r.id = ?",
                    (run_id,)).fetchone()
    if not r:
        return None, f"no run {run_id}"
    if r["status"] not in ("done", "stored"):
        return None, f"run {run_id} is {r['status']}, not finished"
    return _egrid(Path(r["out_dir"])), f"run {run_id} of deck {r['name']}"


def resinsight_bin(cfg) -> str | None:
    cand = cfg.resinsight_bin or shutil.which("ResInsight") or shutil.which("resinsight")
    return cand if cand and Path(cand).exists() else None


def open_in_resinsight(cfg, *, run_id: int | None = None, study: str | None = None, case: str | None = None,
                       geo: int | None = None, deck: int | None = None,
                       dry_run: bool = False) -> dict:
    egrid, where = find_case(cfg, run_id, study, case, geo, deck)
    if egrid is None:
        return {"ok": False, "spoken_summary": f"Nothing to open: {where}."}
    exe = resinsight_bin(cfg)
    if not exe:
        return {"ok": False, "spoken_summary": "ResInsight not found: set [viewer] resinsight_bin in opmagent.toml."}
    cmd = [exe, "--case", str(egrid)]
    env = dict(os.environ)
    if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        return {"ok": False, "command": cmd, "spoken_summary": "No display: ResInsight needs a desktop session."}
    env.setdefault("QT_QPA_PLATFORM", "xcb")
    has_summary = egrid.with_suffix(".SMSPEC").exists()
    say = (f"Opening {where} in ResInsight: {egrid.name}"
           + (", with its summary vectors." if has_summary else " (no summary file next to it)."))
    if dry_run:
        return {"ok": True, "command": cmd, "case": str(egrid), "summary": has_summary, "spoken_summary": say}
    log = egrid.parent / "resinsight.log"
    with open(log, "w") as fh:   # detached: ResInsight stays open after this returns
        p = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, env=env, start_new_session=True,
                             cwd=str(Path(exe).parent))
    return {"ok": True, "pid": p.pid, "command": cmd, "case": str(egrid), "summary": has_summary, "log": str(log),
            "spoken_summary": say}
