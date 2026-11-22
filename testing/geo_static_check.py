"""Checks for the geo static view scenario (testing/geo-static-view.md).

    python testing/geo_static_check.py inputs  <dir>                     a small synthetic geo dataset
    python testing/geo_static_check.py model   <model name> [--nx N ...]  a built geo model's static view vs its arrays
    python testing/geo_static_check.py compare <study> <deck.DATA>        a study's geo static view vs the deck's own INIT
    python testing/geo_static_check.py geo-id  <study>                    the geo version of a study (for results open)

Reads the workspace from OPMAGENT_CONFIG (or --config). Prints one line per check and exits 1 on any failure.
Read only: it never changes the database; `compare` runs OPM Flow's dry run on a copy of the deck's folder.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

# arrays a static view must hold (OPM computes PORV, DEPTH, TRAN*)
REQUIRED = ["PORV", "DEPTH", "DX", "DY", "DZ", "PORO", "PERMX", "PERMY", "PERMZ", "NTG", "TRANX", "TRANY", "TRANZ"]
# arrays compared with the source deck's INIT
COMPARED = ["PORV", "DEPTH", "PORO", "PERMX", "PERMY", "PERMZ", "NTG", "TRANX", "TRANY", "TRANZ",
            "SATNUM", "EQLNUM", "FIPNUM", "PVTNUM"]
REL_TOL = 1e-6
failed = False


def say(ok: bool, what: str, detail: str = "") -> None:
    global failed
    failed |= not ok
    print(f"{'PASS' if ok else 'FAIL'} {what}" + (f": {detail}" if detail else ""))


def _cfg_con(config: str | None):
    from opmagent.core import config as cfgmod
    from opmagent.core import db
    cfg = cfgmod.load(config)
    return cfg, db.connect(cfg.database)


def _init(path) -> dict:
    from opm.io.ecl import EclFile
    f = EclFile(str(path))
    return {a[0]: np.asarray(f[a[0]], dtype=float) for a in f.arrays if a[0] not in ("INTEHEAD", "LOGIHEAD", "DOUBHEAD")}


# ---------------------------------------------------------------- inputs
def cmd_inputs(args) -> None:
    """Four vertical wells on a 2 km square, three surfaces dipping east, LAS with PHIE/PERM/VSH, a geo spec."""
    import json

    import lasio
    import pandas as pd
    d = Path(args.dir)
    d.mkdir(parents=True, exist_ok=True)
    wells = {"A": (300.0, 300.0), "B": (1700.0, 400.0), "C": (500.0, 1600.0), "D": (1600.0, 1700.0)}
    base = {"TopRes": 2000.0, "MidRes": 2030.0, "BaseRes": 2050.0}
    pd.DataFrame([{"well_name": w, "surface": s, "x": x, "y": y, "tvd": z + 0.01 * x} for w, (x, y) in wells.items()
                  for s, z in base.items()]).to_csv(d / "tops.csv", index=False)
    for w in wells:
        las = lasio.LASFile()
        las.well.WELL.value = w
        z = np.arange(1990.0, 2080.0, 0.5)
        phi = 0.18 + 0.04 * (w in "BD") + 0.01 * np.sin(z)
        las.append_curve("DEPT", z, unit="m")
        las.append_curve("PHIE", phi)
        las.append_curve("PERM", 10 ** (1 + 8 * phi))
        las.append_curve("VSH", np.where(np.sin(z / 3) > 0.8, 0.7, 0.2))
        las.write(str(d / f"{w}.las"), version=2)
    (d / "geo.json").write_text(json.dumps({
        "surfaces": ["TopRes", "MidRes", "BaseRes"], "layers": [3, 2],
        "areal": {"x0": 0.0, "y0": 0.0, "dx": 200.0, "dy": 200.0, "nx": 10, "ny": 10},
        "contacts": {"owc_depth": 2100.0}, "petro": {"vsh_cutoff": 0.5, "kv_kh": 0.1}}, indent=1))
    print(f"wrote {d}/tops.csv, {d}/A.las B.las C.las D.las, {d}/geo.json (grid 10 x 10 x 5)")


# ---------------------------------------------------------------- a built model
def cmd_model(args) -> None:
    from opmagent.core import models
    from opmagent.geo.static_view import static_case
    from opmagent.validators._util import read_array
    cfg, con = _cfg_con(args.config)
    rows = [m for m in models.list_models(con, "geo") if m["model_name"] == args.name]
    if not rows:
        say(False, "geo model built", f"no geo model named {args.name}")
        sys.exit(1)
    vid = max(rows, key=lambda m: m["version"])["version_id"]
    g = models.get_model(con, "geo", vid)
    h = g["header"]
    egrid = static_case(cfg, con, vid)
    say(egrid is not None, "static view made by the geo build", str(egrid) if egrid else "no EGRID in the QC folder")
    if egrid is None:
        sys.exit(1)
    init = egrid.with_suffix(".INIT")
    deck = next(egrid.parent.glob("*.DATA"))
    say(init.is_file(), "INIT written next to the EGRID", str(init))
    say("Not a simulation deck" in deck.read_text(), "static deck marked as not for simulation", deck.name)
    say(con.execute("SELECT COUNT(*) FROM decks").fetchone()[0] == 0, "static view not registered as a deck")
    a = _init(init)
    missing = [k for k in REQUIRED if k not in a]
    say(not missing, "INIT holds the static arrays", f"missing {missing}" if missing else ", ".join(REQUIRED))
    n = h["nx"] * h["ny"] * h["nz"]
    say((h["nx"], h["ny"], h["nz"]) == tuple(args.dims), "grid dimensions", f"{h['nx']} x {h['ny']} x {h['nz']}")
    say(a["PORO"].size == n, "active cells (all cells active)", f"{a['PORO'].size} of {n}")
    # the INIT holds what the model holds: its registered PORO and PERMX files, cell by cell
    for kw in ("PORO", "PERMX", "NTG"):
        p = next((p for p in g["tables"]["geo_properties"] if p["keyword"] == kw), None)
        if p is None:
            continue
        if p["const_value"] is not None:
            want = np.full(n, p["const_value"])
        else:
            path = con.execute("SELECT path FROM files WHERE id = ?", (p["file_id"],)).fetchone()[0]
            want = np.asarray(read_array(path), dtype=float)
        rel = float(np.max(np.abs(a[kw] - want) / np.maximum(np.abs(want), 1e-12)))
        say(rel <= 1e-5, f"INIT {kw} = the model's {kw}", f"max relative difference {rel:.1e}")
    print(f"geo version {vid}: opmagent results open --geo {vid}")
    sys.exit(1 if failed else 0)


# ---------------------------------------------------------------- vs the source deck
def _source_init(deck: Path, work: Path) -> Path:
    """Dry-run a copy of the deck's folder (INIT added after GRID if the deck lacks it) and return its INIT."""
    src = work / "source"
    shutil.copytree(deck.parent, src, symlinks=True)
    d = src / deck.name
    text = d.read_text(errors="replace")
    if not re.search(r"^\s*INIT\s*$", text, flags=re.M):
        text = re.sub(r"^\s*GRID\s*$", "GRID\nINIT", text, count=1, flags=re.M)
        d.write_text(text)
    out = work / "source_out"
    r = subprocess.run(["flow", str(d), "--enable-dry-run=true", f"--output-dir={out}"], cwd=src,
                       capture_output=True, text=True, timeout=1200)
    init = out / (deck.stem + ".INIT")
    if r.returncode != 0 or not init.is_file():
        raise RuntimeError("OPM Flow dry run of the source deck failed: " + (r.stdout + r.stderr)[-500:])
    return init


def cmd_compare(args) -> None:
    from opmagent.core import studies
    from opmagent.geo.static_view import static_case
    cfg, con = _cfg_con(args.config)
    res = {t["task"]: t["result"] for t in studies.status(con, args.study)["tasks"]}
    vid = (res.get("geo") or {}).get("version_id")
    say(vid is not None, f"study {args.study} has a finished geo model", f"version {vid}")
    if vid is None:
        sys.exit(1)
    egrid = static_case(cfg, con, vid)
    say(egrid is not None, "static view made by the geo build", str(egrid) if egrid else "none")
    if egrid is None:
        sys.exit(1)
    with tempfile.TemporaryDirectory() as tmp:
        src = _init(_source_init(Path(args.deck).expanduser().resolve(), Path(tmp)))
    view = _init(egrid.with_suffix(".INIT"))
    say(src["PORO"].size == view["PORO"].size, "active cells", f"{view['PORO'].size} (deck: {src['PORO'].size})")
    for kw in COMPARED:
        if kw not in src:
            continue
        if kw not in view:
            say(False, f"{kw}", "missing in the static view")
            continue
        x, y = src[kw], view[kw]
        if x.shape != y.shape:
            say(False, f"{kw}", f"{y.size} values, deck {x.size}")
            continue
        rel = float(np.max(np.abs(x - y) / np.maximum(np.abs(x), 1e-12))) if x.size else 0.0
        say(rel <= REL_TOL, f"{kw} as in the deck's own INIT", f"max relative difference {rel:.1e}")
    print(f"geo version {vid}: opmagent results open --geo {vid}")
    sys.exit(1 if failed else 0)


def cmd_geo_id(args) -> None:
    from opmagent.core import studies
    _, con = _cfg_con(args.config)
    res = {t["task"]: t["result"] for t in studies.status(con, args.study)["tasks"]}
    vid = (res.get("geo") or {}).get("version_id")
    if vid is None:
        sys.exit(1)
    print(vid)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="opmagent.toml (default: OPMAGENT_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("inputs")
    x.add_argument("dir")
    x.set_defaults(fn=cmd_inputs)
    x = sub.add_parser("model")
    x.add_argument("name")
    x.add_argument("--dims", type=int, nargs=3, default=[10, 10, 5], metavar=("NX", "NY", "NZ"))
    x.set_defaults(fn=cmd_model)
    x = sub.add_parser("compare")
    x.add_argument("study")
    x.add_argument("deck")
    x.set_defaults(fn=cmd_compare)
    x = sub.add_parser("geo-id")
    x.add_argument("study")
    x.set_defaults(fn=cmd_geo_id)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
