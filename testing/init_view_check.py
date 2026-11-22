"""Checks for the initialisation view scenario (testing/init-view.md).

    python testing/init_view_check.py deck    <deck id>              a deck's initialisation view: files, in place, checks
    python testing/init_view_check.py compare <study> <deck.DATA>    a study's deck at time zero vs the source deck's own
    python testing/init_view_check.py deck-id <study>                the deck of a study (for results open --deck)

Reads the workspace from OPMAGENT_CONFIG (or --config). Prints one line per check and exits 1 on any failure.
Read only for the database; `compare` initialises the source deck the same way in a temporary folder.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

REL_TOL = 1e-6
failed = False


def say(ok: bool, what: str, detail: str = "") -> None:
    global failed
    failed |= not ok
    print(f"{'PASS' if ok else 'FAIL'} {what}" + (f": {detail}" if detail else ""))


def _cfg_con(config):
    from opmagent.core import config as cfgmod
    from opmagent.core import db
    cfg = cfgmod.load(config)
    return cfg, db.connect(cfg.database)


def _study_deck(con, study: str) -> int | None:
    from opmagent.core import studies
    res = {t["task"]: t["result"] for t in studies.status(con, study)["tasks"]}
    return (res.get("deck") or {}).get("deck_id")


def cmd_deck(args) -> None:
    from opmagent.deck.init_view import folder_of, init_case
    cfg, con = _cfg_con(args.config)
    egrid = init_case(cfg, con, args.deck_id)
    say(egrid is not None, "initialisation view made by the deck build", str(egrid) if egrid else "none")
    if egrid is None:
        sys.exit(1)
    f = folder_of(cfg, con, args.deck_id)
    for ext in ("INIT", "UNRST", "SMSPEC"):
        say(egrid.with_suffix("." + ext).is_file(), f"{ext} written", egrid.with_suffix("." + ext).name)
    deck = next(f.glob("INIT_*.DATA"))
    text = deck.read_text()
    say("Not for simulation" in text and "WELSPECS" not in text, "init deck has no wells, marked not for simulation")
    say(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0, "no run recorded (the view is not a run)")
    s = json.loads((f / "init_view.json").read_text())
    ip, u = s["in_place"], s["units"]
    say(ip.get("FOIP", 0) > 0, "oil in place", f"{ip.get('FOIP', 0):,.0f} {u.get('FOIP', '')}")
    print(f"     gas {ip.get('FGIP', 0):,.0f} {u.get('FGIP', '')}, water {ip.get('FWIP', 0):,.0f} {u.get('FWIP', '')}, "
          f"average pressure {ip.get('FPR', 0):,.2f} {u.get('FPR', '')}, hydrocarbon pore-volume fraction "
          f"{s.get('hydrocarbon_pv_fraction') or 0:.3f}")
    if args.hc is not None:
        hc = s.get("hydrocarbon_pv_fraction") or 0
        say(abs(hc - args.hc) <= 1e-3, "hydrocarbon pore-volume fraction", f"{hc:.4f} (expected {args.hc:g})")
    if args.fpr is not None:
        say(abs(ip.get("FPR", 0) - args.fpr) <= args.fpr_tol, "average pressure",
            f"{ip.get('FPR', 0):.2f} (expected {args.fpr:g} ± {args.fpr_tol:g})")
    for c in s["checks"]:
        say(c["status"] in ("pass", "n/a"), c["check"] + (" (n/a)" if c["status"] == "n/a" else ""), c["detail"])
    print(f"deck {args.deck_id}: opmagent results open --deck {args.deck_id} --init")
    sys.exit(1 if failed else 0)


def _rel(x, y) -> float:
    x, y = np.asarray(x, float), np.asarray(y, float)
    return float(np.max(np.abs(x - y) / np.maximum(np.abs(x), 1e-12))) if x.size else 0.0


def cmd_compare(args) -> None:
    from opmagent.deck.init_view import _run, init_case, initial_state
    cfg, con = _cfg_con(args.config)
    did = _study_deck(con, args.study)
    say(did is not None, f"study {args.study} has a validated deck", f"deck {did}")
    if did is None:
        sys.exit(1)
    egrid = init_case(cfg, con, did)
    say(egrid is not None, "initialisation view made by the deck build", str(egrid) if egrid else "none")
    if egrid is None:
        sys.exit(1)
    ours = initial_state(egrid.parent)
    with tempfile.TemporaryDirectory() as tmp:
        _run(Path(args.deck).expanduser().resolve(), Path(tmp) / "source", cfg.flow_bin, 1800)
        src = initial_state(Path(tmp) / "source")
    a, b = src["state"], ours["state"]
    say(a["PRESSURE"].size == b["PRESSURE"].size, "active cells", f"{b['PRESSURE'].size} (deck: {a['PRESSURE'].size})")
    for kw in ("PRESSURE", "SWAT", "SGAS", "RS", "RV"):
        if kw not in a:
            continue
        if kw not in b or a[kw].shape != b[kw].shape:
            say(False, f"initial {kw}", "missing or a different size")
            continue
        say(_rel(a[kw], b[kw]) <= REL_TOL, f"initial {kw} as the source deck's", f"max relative difference {_rel(a[kw], b[kw]):.1e}")
    for k, v in src["in_place"].items():
        w = ours["in_place"].get(k)
        say(w is not None and _rel([v], [w]) <= REL_TOL, f"{k} as the source deck's",
            f"{w:,.6g} vs {v:,.6g} {src['units'].get(k, '')}" if w is not None else "missing")
    bad = [r for r in src["regions"] if any(_rel([x], [ours["regions"].get(r, {}).get(k, np.nan)]) > REL_TOL
                                           for k, x in src["regions"][r].items())]
    say(not bad and len(src["regions"]) == len(ours["regions"]), "fluids in place per FIPNUM region",
        f"{len(ours['regions'])} regions" + (f"; differ: {bad[:5]}" if bad else ", all equal"))
    print(f"deck {did}: opmagent results open --deck {did} --init")
    sys.exit(1 if failed else 0)


def cmd_deck_id(args) -> None:
    _, con = _cfg_con(args.config)
    did = _study_deck(con, args.study)
    if did is None:
        sys.exit(1)
    print(did)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="opmagent.toml (default: OPMAGENT_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("deck")
    x.add_argument("deck_id", type=int)
    x.add_argument("--hc", type=float, help="expected hydrocarbon pore-volume fraction")
    x.add_argument("--fpr", type=float, help="expected average pressure")
    x.add_argument("--fpr-tol", type=float, default=5.0)
    x.set_defaults(fn=cmd_deck)
    x = sub.add_parser("compare")
    x.add_argument("study")
    x.add_argument("deck")
    x.set_defaults(fn=cmd_compare)
    x = sub.add_parser("deck-id")
    x.add_argument("study")
    x.set_defaults(fn=cmd_deck_id)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
